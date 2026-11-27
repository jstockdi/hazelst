"""report-build and report-refresh with the narrative, on recorded data and a stub Claude."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from hazelst import config, db, jobs
from hazelst.agents.client import AgentRequest
from hazelst.agents.schemas import ReportDraft
from hazelst.agents.stub import Responder, StubBackend
from hazelst.sources import base
from tests.drafts import passing_draft, request_digest, stub_agents
from tests.support import FIXTURES, load_fixtures, replay_client

DAY = date(2026, 11, 26)
NOON = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


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
    return stub_agents(monkeypatch, responses)


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
    assert headline == "Forecast numbers for all eight"
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


def runs(c: config.Config) -> list[tuple[Any, ...]]:
    conn = db.connect(c.settings.db_path)
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT kind, status, error FROM run WHERE kind LIKE 'report-%' ORDER BY id"
        )
    ]


def at_local(monkeypatch: pytest.MonkeyPatch, hour: int, minute: int) -> None:
    when = datetime(2026, 11, 26, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(jobs, "clock", lambda: when.astimezone(UTC))


def test_failed_checks_fail_the_job_and_publish_nothing(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": failing_draft, "report-revise": failing_draft})
    with pytest.raises(jobs.JobError, match="report-check failed after 3 revisions"):
        jobs.run_job(cfg, "report-build", DAY)
    assert reports(cfg) == []
    assert [c[0] for c in calls(cfg)] == ["report-write"] + ["report-revise"] * 3
    ((kind, status, error),) = runs(cfg)
    assert (kind, status) == ("report-build", "failed")
    assert "unknown mountain 'stowe'" in error


def test_failed_refresh_keeps_the_published_edition(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": passing_draft, "report-revise": RuntimeError("down")})
    jobs.run_job(cfg, "report-build", DAY)
    conn = db.connect(cfg.settings.db_path)
    conn.execute(
        "INSERT INTO alert (id, mountain, event, headline, onset, expires)"
        " VALUES ('x1', 'loon', 'Winter Storm Warning', 'h', NULL, NULL)"
    )
    with pytest.raises(jobs.JobError, match="report-revise"):
        jobs.run_job(cfg, "report-refresh", DAY, options={"edition": "0815"})
    assert reports(cfg) == [(1, "published", "0700", 1)]


def test_agents_disabled_fail_the_job(cfg: config.Config, monkeypatch: pytest.MonkeyPatch) -> None:
    off = cfg.model_copy(
        update={"settings": cfg.settings.model_copy(update={"enable_agents": False})}
    )
    backend = use_stub(monkeypatch, {"report-write": passing_draft})
    with pytest.raises(jobs.JobError, match="disabled"):
        jobs.run_job(off, "report-build", DAY)
    assert backend.requests == [] and reports(cfg) == []
    again = jobs.run_job(off, "report-build", DAY, options={"rebuild": True})
    assert again["status"] == "skipped (agents disabled)"


def test_rebuild_retries_a_failed_edition(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": [RuntimeError("busy"), passing_draft]})
    with pytest.raises(jobs.JobError, match="busy"):
        jobs.run_job(cfg, "report-build", DAY, options={"edition": "0700"})
    rebuilt = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert (rebuilt["edition"], rebuilt["status"]) == ("0700", "published")
    done = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert done["status"] == "skipped (0700 is published)"
    assert reports(cfg) == [(1, "published", "0700", 1)]


def test_rebuild_leaves_an_edition_to_its_own_slot(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = use_stub(monkeypatch, {"report-write": passing_draft})
    at_local(monkeypatch, 6, 59)
    early = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert early["status"] == "skipped (no edition due yet)"
    at_local(monkeypatch, 7, 15)
    waiting = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert waiting["status"] == "skipped (0700 has not failed)"
    assert backend.requests == []


def test_rebuild_stops_at_the_daily_cap(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    capped = cfg.model_copy(
        update={"settings": cfg.settings.model_copy(update={"agent_daily_calls": 1})}
    )
    use_stub(monkeypatch, {"report-write": RuntimeError("busy")})
    with pytest.raises(jobs.JobError):
        jobs.run_job(capped, "report-build", DAY)
    again = jobs.run_job(capped, "report-build", DAY, options={"rebuild": True})
    assert again["status"] == "skipped (daily call cap reached)"


def test_refresh_without_a_checked_draft_writes_one(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(monkeypatch, {"report-write": [RuntimeError("busy"), passing_draft]})
    with pytest.raises(jobs.JobError):
        jobs.run_job(cfg, "report-build", DAY)
    refresh = jobs.run_job(cfg, "report-refresh", DAY, options={"edition": "0815"})
    assert refresh["narrative"]["from"] == "report-write"
    assert reports(cfg) == [(1, "published", "0815", 1)]


def test_rebuild_retries_a_failed_refresh_as_a_refresh(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub(
        monkeypatch,
        {"report-write": passing_draft, "report-revise": [RuntimeError("busy"), passing_draft]},
    )
    jobs.run_job(cfg, "report-build", DAY)
    conn = db.connect(cfg.settings.db_path)
    conn.execute(
        "INSERT INTO alert (id, mountain, event, headline, onset, expires)"
        " VALUES ('x1', 'loon', 'Winter Storm Warning', 'h', NULL, NULL)"
    )
    at_local(monkeypatch, 8, 20)
    with pytest.raises(jobs.JobError):
        jobs.run_job(cfg, "report-refresh", DAY, options={"edition": "0815"})
    rebuilt = jobs.run_job(cfg, "report-build", DAY, options={"rebuild": True})
    assert (rebuilt["edition"], rebuilt["status"]) == ("0815", "published")
    assert rebuilt["narrative"]["from"] == "morning draft"
    roles = [c[:2] for c in calls(cfg)]
    assert roles == [
        ("report-write", "report-build"),
        ("report-revise", "report-refresh"),
        ("report-revise", "report-build"),
    ]


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
    assert summary["narrative"]["revisions"] == 1
    roles = [c[:2] for c in calls(cfg)]
    assert roles == [("report-write", "report-build"), ("report-revise", "report-refresh")]
