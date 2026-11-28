"""The checked narrative: report-write, report-check, then up to 3 rounds of report-revise.

A narrative is published only when `report_check` passes. A draft that fails is
revised up to `MAX_REVISIONS` times, each round sent the exact errors of the
draft before it. When a Claude call fails or the last revision still fails the
check, `NarrativeError` is raised: the job fails and the previously published
edition stays (agent-runs.md, Failure behavior). There is no page without prose.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from hazelst.agents.client import AgentClient, AgentError
from hazelst.agents.report_revise import revise_report
from hazelst.agents.report_write import write_report
from hazelst.agents.schemas import ReportDraft
from hazelst.digest import ReportDigest
from hazelst.models import BestBet as PageBestBet
from hazelst.models import Narrative as PageNarrative
from hazelst.report_check import check

MAX_REVISIONS = 3


@dataclass(frozen=True)
class Narrative:
    """A draft that passed `report-check`, and the page narrative made from it."""

    draft: ReportDraft
    page: PageNarrative
    call_ids: list[int] = field(default_factory=list)
    revisions: int = 0


class NarrativeError(Exception):
    """No checked narrative: a Claude call failed or the checks still fail."""

    def __init__(self, reason: str, errors: Sequence[str] = (), call_ids: Sequence[int] = ()):
        super().__init__(reason if not errors else f"{reason}: {'; '.join(errors)}")
        self.reason = reason
        self.errors = list(errors)
        self.call_ids = list(call_ids)


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


def _published(
    digest: ReportDigest, draft: ReportDraft, call_ids: list[int], revisions: int
) -> Narrative:
    return Narrative(draft, page_narrative(draft, digest), call_ids, revisions)


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
    """Up to `MAX_REVISIONS` `report-revise` rounds, each with the previous draft's errors."""
    for revision in range(1, MAX_REVISIONS + 1):
        try:
            revised = revise_report(client, digest, draft, errors, knowledge, run_id=run_id)
        except AgentError as exc:
            raise NarrativeError(f"report-revise: {exc}", errors, call_ids) from exc
        call_ids = [*call_ids, revised.call_id]
        draft = revised.output
        errors = _checked(client, revised.call_id, draft, digest)
        if not errors:
            return _published(digest, draft, call_ids, revision)
    raise NarrativeError(f"report-check failed after {MAX_REVISIONS} revisions", errors, call_ids)


def build_narrative(
    client: AgentClient,
    digest: ReportDigest,
    knowledge: Sequence[str] = (),
    *,
    run_id: int | None = None,
) -> Narrative:
    """`report-build`: write, check, and revise until the check passes (at most 3 rounds)."""
    try:
        written = write_report(client, digest, knowledge, run_id=run_id)
    except AgentError as exc:
        raise NarrativeError(f"report-write: {exc}") from exc
    errors = _checked(client, written.call_id, written.output, digest)
    if not errors:
        return _published(digest, written.output, [written.call_id], 0)
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
    """`report-refresh`: re-check the morning draft on new numbers; revise it if it fails."""
    errors = [str(e) for e in check(draft, digest)]
    if not errors:
        return _published(digest, draft, [], 0)
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
    """The narrative as the page and the `report` row store it (no best bet: `None`)."""
    bet = draft.best_bet
    if bet is None:
        return PageNarrative(
            headline=draft.headline, best_bet=None, body_md=body_markdown(draft, digest)
        )
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
