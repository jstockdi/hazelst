"""report-build and report-refresh with the narrative, on recorded data and a stub Claude."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from hazelst import config, db, jobs
from hazelst.agents.client import AgentClient, AgentRequest
from hazelst.agents.report_revise import ReviseInput
from hazelst.agents.schemas import BestBet, ReportDraft
from hazelst.agents.stub import Responder, StubBackend
from hazelst.digest import ReportDigest
from hazelst.report_check import check
from hazelst.sources import base
from tests.support import FIXTURES, load_fixtures, replay_client

DAY = date(2026, 11, 26)
NOON = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


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
        headline="Forecast numbers for the next five days",
        best_bet=BestBet(mountain=first.key, day=digest.days[0], why="Closest to home."),
        storm="The table below has the model mean and range for each mountain.",
        mountains={m.key: "See the table for each day." for m in digest.mountains},
        cautions=cautions,
    )
    assert check(draft, digest) == []
    return draft


def failing_draft(request: AgentRequest) -> ReportDraft:
    draft = passing_draft(request)
    return draft.model_copy(update={"mountains": {**draft.mountains, "stowe": "Off roster."}})


def recorded() -> list[base.Fetched]:
    return [
        *load_fixtures(FIXTURES / "open_meteo" / "2026-11-26"),
        *load_fixtures(FIXTURES / "nws" / "2026-11-26"),
        *load_fixtures(FIXTURES / "discussions" / "2026-11-26"),
    ]


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    monkeypatch.setattr(jobs, "clock", lambda: NOON)
    c = config.load(
        {
            "HAZELST_DB": str(tmp_path / "h.db"),
            "HAZELST_ENABLE_AGENTS": "true",
            "CLAUDE_CODE_OAUTH_TOKEN": "test-token",
        }
    )
    jobs.run_job(c, "ingest-forecast", DAY, client=replay_client(recorded()))
    return c


def use_stub(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, Responder | list[Responder]]
) -> StubBackend:
    backend = StubBackend(responses)
    monkeypatch.setattr(
        jobs,
        "make_agents",
        lambda ctx: AgentClient(ctx.conn, ctx.cfg.settings, backend, clock=jobs.clock),
    )
    return backend


def reports(c: config.Config) -> list[tuple[Any, ...]]:
    conn = db.connect(c.settings.db_path)
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT version, status, json_extract(payload, '$.edition'), narrative_md IS NOT NULL"
            " FROM report ORDER BY version"
        )
    ]


def calls(c: config.Config) -> list[tuple[Any, ...]]:
    conn = db.connect(c.settings.db_path)
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT a.role, r.kind, a.status, a.check_errors FROM agent_call a"
            " JOIN run r ON r.id = a.run_id ORDER BY a.id"
        )
    ]


def test_build_publishes_a_checked_narrative(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = use_stub(monkeypatch, {"report-write": passing_draft})
    summary = jobs.run_job(cfg, "report-build", DAY)
    assert summary["status"] == "published"
    assert summary["narrative"]["status"] == "published"
    assert reports(cfg) == [(1, "published", "0700", 1)]
    assert calls(cfg) == [("report-write", "report-build", "ok", "[]")]
    conn = db.connect(cfg.settings.db_path)
    headline, best, body, payload = conn.execute(
        "SELECT headline, best_bet, narrative_md, payload FROM report"
    ).fetchone()
    assert headline == "Forecast numbers for the next five days"
    assert json.loads(best) == {
        "mountain": "Killington",
        "day": "Thu 26",
        "why": "Closest to home.",
    }
    assert body.startswith("## The storm")
    assert json.loads(payload)["status"] == "published"
    digest = request_digest(backend.requests[0])
    assert digest.date == DAY and len(digest.mountains) == len(cfg.mountains)
    assert {d.office for d in digest.discussions} == {"BTV", "ALY", "GYX"}
    stratton = digest.mountain("stratton")
    assert stratton is not None and "Wind Advisory" in [a.event for a in stratton.alerts]


def test_revise_fixes_a_failed_check(cfg: config.Config, monkeypatch: pytest.MonkeyPatch) -> None:
    use_stub(monkeypatch, {"report-write": failing_draft, "report-revise": passing_draft})
    summary = jobs.run_job(cfg, "report-build", DAY)
    assert summary["status"] == "published"
    roles = [(c[0], c[2]) for c in calls(cfg)]
    assert roles == [("report-write", "ok"), ("report-revise", "ok")]
    assert json.loads(calls(cfg)[0][3]) == ["mountains: unknown mountain 'stowe'"]


def test_two_failed_checks_publish_numbers_only(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": failing_draft, "report-revise": failing_draft})
    summary = jobs.run_job(cfg, "report-build", DAY)
    assert summary["status"] == "numbers_only"
    assert summary["narrative"]["reason"] == "report-check failed twice"
    assert reports(cfg) == [(1, "numbers_only", "0700", 0)]


def test_agents_disabled_publish_numbers_only(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = cfg.model_copy(
        update={"settings": cfg.settings.model_copy(update={"enable_agents": False})}
    )
    backend = use_stub(monkeypatch, {"report-write": passing_draft})
    summary = jobs.run_job(off, "report-build", DAY)
    assert summary["status"] == "numbers_only"
    assert "disabled" in summary["narrative"]["reason"]
    assert backend.requests == []
    again = jobs.run_job(off, "report-build", DAY, options={"rebuild": True})
    assert again["status"] == "skipped (agents disabled)"


def test_rebuild_rewrites_a_numbers_only_edition(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": [RuntimeError("busy"), passing_draft]})
    first = jobs.run_job(cfg, "report-build", DAY, options={"edition": "0700"})
    assert first["status"] == "numbers_only"
    rebuilt = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert (rebuilt["edition"], rebuilt["status"]) == ("0700", "published")
    done = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert done["status"] == "skipped (0700 already has a checked narrative)"
    assert reports(cfg) == [(1, "numbers_only", "0700", 0), (2, "published", "0700", 1)]


def test_rebuild_stops_at_the_daily_cap(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    capped = cfg.model_copy(
        update={"settings": cfg.settings.model_copy(update={"agent_daily_calls": 1})}
    )
    use_stub(monkeypatch, {"report-write": RuntimeError("busy")})
    jobs.run_job(capped, "report-build", DAY)
    again = jobs.run_job(capped, "report-build", DAY, options={"rebuild": True})
    assert again["status"] == "skipped (daily call cap reached)"


def test_rebuild_targets_the_newest_edition(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": [RuntimeError("busy"), passing_draft]})
    jobs.run_job(cfg, "report-build", DAY)
    refresh = jobs.run_job(cfg, "report-refresh", DAY, options={"edition": "0815"})
    assert refresh["narrative"]["reason"] == "no checked narrative today"
    rebuilt = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert (rebuilt["edition"], rebuilt["status"]) == ("0815", "published")


def test_refresh_rechecks_the_morning_draft_without_a_call(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = use_stub(monkeypatch, {"report-write": passing_draft})
    jobs.run_job(cfg, "report-build", DAY)
    summary = jobs.run_job(cfg, "report-refresh", DAY, options={"edition": "0815"})
    assert summary["status"] == "published"
    assert summary["narrative"]["agent_calls"] == []
    assert len(backend.requests) == 1
    assert reports(cfg) == [(1, "published", "0700", 1), (2, "published", "0815", 1)]


def test_refresh_revises_when_the_check_now_fails(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": passing_draft, "report-revise": passing_draft})
    jobs.run_job(cfg, "report-build", DAY)
    # A new alert after the morning build: the morning cautions no longer cover it.
    conn = db.connect(cfg.settings.db_path)
    conn.execute(
        "INSERT INTO alert (id, mountain, event, headline, onset, expires)"
        " VALUES ('x1', 'loon', 'Winter Storm Warning', 'h', NULL, NULL)"
    )
    summary = jobs.run_job(cfg, "report-refresh", DAY, options={"edition": "0815"})
    assert summary["status"] == "published"
    assert summary["narrative"]["check_errors"] == []
    roles = [c[:2] for c in calls(cfg)]
    assert roles == [("report-write", "report-build"), ("report-revise", "report-refresh")]
