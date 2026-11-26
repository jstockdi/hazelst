"""`report-revise`: draft + digest + `report-check` errors -> corrected `ReportDraft`."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from hazelst.agents.client import AgentClient, AgentResult
from hazelst.agents.prompts import system_prompt
from hazelst.agents.schemas import ReportDraft
from hazelst.digest import ReportDigest

ROLE = "report-revise"


class ReviseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    digest: ReportDigest
    draft: ReportDraft
    errors: list[str]


def revise_report(
    client: AgentClient,
    digest: ReportDigest,
    draft: ReportDraft,
    errors: Sequence[str],
    knowledge: Sequence[str] = (),
    *,
    run_id: int | None = None,
) -> AgentResult[ReportDraft]:
    return client.call(
        ROLE,
        system_prompt=system_prompt(ROLE, knowledge),
        data=ReviseInput(digest=digest, draft=draft, errors=list(errors)),
        output_model=ReportDraft,
        run_id=run_id,
    )
