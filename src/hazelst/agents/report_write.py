"""`report-write`: digest -> `ReportDraft`."""

from __future__ import annotations

from collections.abc import Sequence

from hazelst.agents.client import AgentClient, AgentResult
from hazelst.agents.prompts import system_prompt
from hazelst.agents.schemas import ReportDraft
from hazelst.digest import ReportDigest

ROLE = "report-write"


def write_report(
    client: AgentClient,
    digest: ReportDigest,
    knowledge: Sequence[str] = (),
    *,
    run_id: int | None = None,
) -> AgentResult[ReportDraft]:
    return client.call(
        ROLE,
        system_prompt=system_prompt(ROLE, knowledge),
        data=digest,
        output_model=ReportDraft,
        run_id=run_id,
    )
