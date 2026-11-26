"""The checked narrative: `report-write` -> `report-check` -> (`report-revise` -> `report-check`).

A narrative is published only when `report_check` passes. Any agent failure
(disabled, cap reached, call failed) or a second failed check yields
`numbers_only`, and the caller publishes the page without prose
(agent-runs.md, Failure behavior).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from hazelst.agents.client import AgentClient, AgentError
from hazelst.agents.report_revise import revise_report
from hazelst.agents.report_write import write_report
from hazelst.agents.schemas import ReportDraft
from hazelst.digest import ReportDigest
from hazelst.models import BestBet as PageBestBet
from hazelst.models import Narrative as PageNarrative
from hazelst.report_check import check

NarrativeStatus = Literal["published", "numbers_only"]


@dataclass(frozen=True)
class Narrative:
    status: NarrativeStatus
    draft: ReportDraft | None = None
    page: PageNarrative | None = None
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
    return Narrative("published", draft, page_narrative(draft, digest), call_ids=call_ids)


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


# --- page ------------------------------------------------------------------------


def body_markdown(draft: ReportDraft, digest: ReportDigest) -> str:
    """`report.narrative_md`: The storm, Mountain by mountain, Cautions.

    The headline and best bet have their own `report` columns; the page shows
    them in the band and the best-bet card (narrative.md, Output structure).
    The markdown uses only what `render.page.md_to_html` supports: `## `
    headings, paragraphs, and `**bold**`.
    """
    names = {m.key: m.name for m in digest.mountains}
    blocks = ["## The storm", draft.storm, "## Mountain by mountain"]
    order = [m.key for m in digest.mountains if m.key in draft.mountains]
    order += [k for k in draft.mountains if k not in names]
    blocks += [
        f"**{names.get(k, k)}.** {draft.mountains[k]}" for k in order if k in draft.mountains
    ]
    blocks += ["## Cautions", *(draft.cautions or ["None today."])]
    return "\n\n".join(blocks) + "\n"


def page_narrative(draft: ReportDraft, digest: ReportDigest) -> PageNarrative:
    """The narrative as the page and the `report` row store it."""
    bet = draft.best_bet
    mountain = digest.mountain(bet.mountain)
    return PageNarrative(
        headline=draft.headline,
        best_bet=PageBestBet(
            mountain=mountain.name if mountain else bet.mountain,
            day=f"{bet.day:%a} {bet.day.day:02d}",
            why=bet.why,
        ),
        body_md=body_markdown(draft, digest),
    )
