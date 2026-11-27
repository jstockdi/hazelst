"""Stub `report-write` / `report-revise` answers for job tests: every report needs prose."""

from __future__ import annotations

from pathlib import Path

import pytest

from hazelst import config, jobs
from hazelst.agents.client import AgentClient, AgentRequest
from hazelst.agents.report_revise import ReviseInput
from hazelst.agents.schemas import BestBet, ReportDraft
from hazelst.agents.stub import Responder, StubBackend
from hazelst.digest import ReportDigest
from hazelst.report_check import check


def request_digest(request: AgentRequest) -> ReportDigest:
    if request.role == "report-revise":
        return ReviseInput.model_validate_json(request.prompt).digest
    return ReportDigest.model_validate_json(request.prompt)


def passing_draft(request: AgentRequest) -> ReportDraft:
    """A minimal draft that satisfies report-check for whatever digest it is given."""
    digest = request_digest(request)
    cautions = sorted({a.event for m in digest.mountains for a in m.alerts})
    for m in digest.mountains:
        for d in m.days:
            if "wind_hold_likely" in d.tags:
                cautions.append(f"Wind holds likely at {m.name} {d.date:%A}.")
    first = digest.mountains[0]
    draft = ReportDraft(
        headline="Forecast numbers for all eight",
        best_bet=BestBet(mountain=first.key, day=digest.days[0], why="Closest to home."),
        storm="The table below has the model mean and range for each mountain.",
        mountains={m.key: "See the table for each day." for m in digest.mountains},
        cautions=cautions,
    )
    assert check(draft, digest) == []
    return draft


def stub_agents(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, Responder | list[Responder]] | None = None,
) -> StubBackend:
    """Route the jobs' Claude client to a stub (default: a passing draft for every write)."""
    backend = StubBackend(responses if responses is not None else {"report-write": passing_draft})
    monkeypatch.setattr(
        jobs,
        "make_agents",
        lambda ctx: AgentClient(ctx.conn, ctx.cfg.settings, backend, clock=jobs.clock),
    )
    return backend


def agents_env(db_path: Path) -> dict[str, str]:
    """Settings for a job test that publishes reports (with `stub_agents`)."""
    return {
        "HAZELST_DB": str(db_path),
        "HAZELST_ENABLE_AGENTS": "true",
        "CLAUDE_CODE_OAUTH_TOKEN": "test-token",
    }


def report_config(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> config.Config:
    """A config whose report jobs write prose through a stub Claude."""
    stub_agents(monkeypatch)
    return config.load(agents_env(db_path))
