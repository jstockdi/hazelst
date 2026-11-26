"""The checked narrative: `report-write` -> `report-check` -> (`report-revise` -> `report-check`).

A narrative is published only when `report_check` passes. Any agent failure
(disabled, cap reached, call failed) or a second failed check yields
`numbers_only`, and the caller publishes the page without prose
(agent-runs.md, Failure behavior).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from hazelst.agents.client import AgentClient, AgentError
from hazelst.agents.report_revise import revise_report
from hazelst.agents.report_write import write_report
from hazelst.agents.schemas import ReportDraft
from hazelst.digest import ReportDigest
from hazelst.report_check import check

NarrativeStatus = Literal["published", "numbers_only"]


@dataclass(frozen=True)
class Narrative:
    status: NarrativeStatus
    draft: ReportDraft | None = None
    markdown: str | None = None
    reason: str | None = None  # why the page is numbers_only
    errors: list[str] = field(default_factory=list)  # the last report-check errors
    call_ids: list[int] = field(default_factory=list)


def accepted_knowledge(conn: sqlite3.Connection) -> list[str]:
    """Accepted `knowledge` statements, oldest first, for the report system prompt."""
    rows = conn.execute(
        "SELECT statement FROM knowledge WHERE status = 'accepted' ORDER BY id"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _checked(
    client: AgentClient, call_id: int, draft: ReportDraft, digest: ReportDigest
) -> list[str]:
    errors = [str(e) for e in check(draft, digest)]
    client.record_check_errors(call_id, errors)
    return errors


def _published(digest: ReportDigest, draft: ReportDraft, call_ids: list[int]) -> Narrative:
    return Narrative("published", draft, to_markdown(draft, digest), call_ids=call_ids)


def revise_until_checked(
    client: AgentClient,
    digest: ReportDigest,
    draft: ReportDraft,
    errors: list[str],
    knowledge: Sequence[str],
    *,
    run_id: int | None,
    call_ids: list[int],
) -> Narrative:
    """One `report-revise` call and a second check; numbers_only if either fails."""
    try:
        revised = revise_report(client, digest, draft, errors, knowledge, run_id=run_id)
    except AgentError as exc:
        return Narrative(
            "numbers_only", reason=f"report-revise: {exc}", errors=errors, call_ids=call_ids
        )
    call_ids = [*call_ids, revised.call_id]
    errors = _checked(client, revised.call_id, revised.output, digest)
    if not errors:
        return _published(digest, revised.output, call_ids)
    return Narrative(
        "numbers_only", reason="report-check failed twice", errors=errors, call_ids=call_ids
    )


def build_narrative(
    client: AgentClient,
    digest: ReportDigest,
    knowledge: Sequence[str] = (),
    *,
    run_id: int | None = None,
) -> Narrative:
    """`report-build`: write, check, and revise once if needed."""
    try:
        written = write_report(client, digest, knowledge, run_id=run_id)
    except AgentError as exc:
        return Narrative("numbers_only", reason=f"report-write: {exc}")
    errors = _checked(client, written.call_id, written.output, digest)
    if not errors:
        return _published(digest, written.output, [written.call_id])
    return revise_until_checked(
        client,
        digest,
        written.output,
        errors,
        knowledge,
        run_id=run_id,
        call_ids=[written.call_id],
    )


def recheck_narrative(
    client: AgentClient,
    digest: ReportDigest,
    draft: ReportDraft,
    knowledge: Sequence[str] = (),
    *,
    run_id: int | None = None,
) -> Narrative:
    """`report-refresh`: re-check the morning draft on new numbers; revise only if it fails."""
    errors = [str(e) for e in check(draft, digest)]
    if not errors:
        return _published(digest, draft, [])
    return revise_until_checked(
        client, digest, draft, errors, knowledge, run_id=run_id, call_ids=[]
    )


# --- markdown --------------------------------------------------------------------


def to_markdown(draft: ReportDraft, digest: ReportDigest) -> str:
    """`report.narrative_md`: front matter, then Best bet, The storm, Mountain by mountain,
    Cautions (narrative.md, Output structure)."""
    names = {m.key: m.name for m in digest.mountains}
    bet = draft.best_bet
    bet_name = names.get(bet.mountain, bet.mountain)
    front = {
        "title": digest.date.strftime("%a %-d %b %Y"),
        "headline": draft.headline,
        "best_bet": {
            "mountain": bet_name,
            "day": bet.day.strftime("%a %-d"),
            "why": bet.why,
        },
    }
    lines = ["---"]
    lines += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()]
    lines += ["---", "", "## Best bet", "", f"**{bet_name}, {bet.day.strftime('%A')}.** {bet.why}"]
    lines += ["", "## The storm", "", draft.storm, "", "## Mountain by mountain", ""]
    order = [m.key for m in digest.mountains] + [k for k in draft.mountains if k not in names]
    for key in order:
        if key in draft.mountains:
            lines += [f"**{names.get(key, key)}.** {draft.mountains[key]}", ""]
    lines += ["## Cautions", ""]
    lines += [f"- {c}" for c in draft.cautions] or ["None today."]
    return "\n".join(lines).rstrip() + "\n"
