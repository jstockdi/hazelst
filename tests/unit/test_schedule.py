import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from hazelst import config, db, jobs, scheduler
from hazelst.sources import base
from tests.support import FIXTURES, load_fixtures, replay_client

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 26)


# --- cron slots across DST ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "utc_offset_h"),
    [
        (date(2026, 10, 31), 4),  # EDT, the day before DST ends
        (date(2026, 11, 1), 5),  # EST from 2 AM
        (date(2027, 3, 13), 5),  # EST, the day before DST starts
        (date(2027, 3, 14), 4),  # EDT from 2 AM
    ],
)
def test_each_slot_fires_at_local_time_across_dst(day: date, utc_offset_h: int) -> None:
    start = datetime.combine(day, dtime(0, 0), ET)
    for slot in scheduler.SLOTS:
        fire = scheduler.trigger(slot).get_next_fire_time(None, start)
        assert fire.astimezone(ET).date() == day
        assert fire.astimezone(ET).time() == slot.at
        expected_utc = datetime.combine(day, slot.at) + timedelta(hours=utc_offset_h)
        assert fire.astimezone(UTC).replace(tzinfo=None) == expected_utc


def test_slots_follow_agent_runs_schedule() -> None:
    table = [(s.at.strftime("%H:%M"), [st.kind for st in s.steps]) for s in scheduler.SLOTS]
    assert table == [
        ("06:45", ["ingest-forecast"]),
        ("06:50", ["ingest-obs"]),
        ("07:00", ["report-build"]),
        ("08:10", ["ingest-forecast", "ingest-obs", "report-refresh"]),
        ("14:30", ["ingest-forecast", "ingest-obs"]),
        ("14:40", ["report-refresh"]),
        ("21:00", ["forecast-verify"]),
        ("04:00", ["backup"]),
    ]
    morning = scheduler.SLOTS[3].steps
    assert morning[0].ecmwf_gate_until == dtime(8, 45)
    assert (morning[2].edition, morning[2].not_before) == ("0815", dtime(8, 15))


def test_scheduler_registers_every_slot() -> None:
    cfg = config.load({})
    sched = scheduler.build(cfg)
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {s.id for s in scheduler.SLOTS} | {"catch-up", "report-rebuild"}


@pytest.mark.parametrize("day", [date(2026, 11, 1), date(2027, 3, 14)])
def test_rebuild_fires_every_15_minutes_until_9(day: date) -> None:
    trig = scheduler.rebuild_trigger()
    fire = datetime.combine(day, dtime(0, 0), ET)
    times = []
    for _ in range(8):
        fire = trig.get_next_fire_time(None, fire + timedelta(seconds=1))
        times.append(fire.astimezone(ET).strftime("%H:%M"))
    assert times == ["07:15", "07:30", "07:45", "08:00", "08:15", "08:30", "08:45", "09:00"]
    after = trig.get_next_fire_time(None, fire + timedelta(seconds=1))
    assert after.astimezone(ET).date() == day + timedelta(days=1)


# --- run_slot ------------------------------------------------------------------------------


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """jobs.clock starts at 8:12 AM EST on DAY; jobs.sleep advances it."""
    now = [datetime(2026, 11, 26, 13, 12, tzinfo=UTC)]
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(jobs, "clock", lambda: now[0])
    monkeypatch.setattr(jobs, "sleep", fake_sleep)
    yield slept


def test_run_slot_order_options_and_failures(frozen: list[float]) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def runner(cfg: config.Config, kind: str, day: date, **kw: Any) -> dict[str, Any]:
        calls.append((kind, kw["options"]))
        assert kw["lock_timeout_s"] == scheduler.LOCK_WAIT_S
        if kind == "ingest-obs":
            raise RuntimeError("resort site down")
        return {"kind": kind}

    results = scheduler.run_slot(config.load({}), scheduler.SLOTS[3], day=DAY, runner=runner)
    assert [k for k, _ in calls] == ["ingest-forecast", "ingest-obs", "report-refresh"]
    assert calls[0][1]["ecmwf_gate_until"] == datetime(2026, 11, 26, 8, 45, tzinfo=ET)
    assert calls[2][1] == {"edition": "0815"}
    assert "error" in results[1]
    assert frozen == [180.0]  # 8:12 -> waits until 8:15 for the refresh


# --- ECMWF 06Z gate --------------------------------------------------------------------------


def meta_client(inits: list[datetime]) -> httpx.Client:
    """meta.json for ECMWF reporting each init in turn (the last one repeats)."""
    seq = list(inits)

    def handler(request: httpx.Request) -> httpx.Response:
        init = seq.pop(0) if len(seq) > 1 else seq[0]
        body = {
            "last_run_initialisation_time": int(init.timestamp()),
            "last_run_availability_time": int((init + timedelta(hours=2)).timestamp()),
            "data_end_time": int((init + timedelta(days=10)).timestamp()),
            "update_interval_seconds": 21600,
        }
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def gate_ctx(client: httpx.Client) -> jobs.JobContext:
    return jobs.JobContext(
        cfg=config.load({}),
        conn=sqlite3.connect(":memory:"),
        run_id=0,
        for_date=DAY,
        client=client,
    )


def test_ecmwf_gate_waits_for_06z(frozen: list[float]) -> None:
    z00, z06 = datetime(2026, 11, 26, 0, tzinfo=UTC), datetime(2026, 11, 26, 6, tzinfo=UTC)
    ctx = gate_ctx(meta_client([z00, z00, z06]))
    assert jobs.wait_for_ecmwf_06z(ctx, datetime(2026, 11, 26, 8, 45, tzinfo=ET))
    assert frozen == [300.0, 300.0]


def test_ecmwf_gate_gives_up_at_845(frozen: list[float]) -> None:
    z00 = datetime(2026, 11, 26, 0, tzinfo=UTC)
    ctx = gate_ctx(meta_client([z00]))
    assert not jobs.wait_for_ecmwf_06z(ctx, datetime(2026, 11, 26, 8, 45, tzinfo=ET))
    # 8:12 -> polls at 8:12, 8:17, ..., 8:37; the next poll would pass 8:45.
    assert frozen == [300.0] * 6


# --- report-build / report-refresh on recorded data --------------------------------------------


def recorded() -> list[base.Fetched]:
    return [
        *load_fixtures(FIXTURES / "open_meteo" / "2026-11-26"),
        *load_fixtures(FIXTURES / "nws" / "2026-11-26"),
        *load_fixtures(FIXTURES / "discussions" / "2026-11-26"),
    ]


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    monkeypatch.setattr(jobs, "clock", lambda: datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    c = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    jobs.run_job(c, "ingest-forecast", DAY, client=replay_client(recorded()))
    return c


def reports(c: config.Config) -> list[tuple[Any, ...]]:
    conn = db.connect(c.settings.db_path)
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT version, status, json_extract(payload, '$.edition') FROM report"
            " ORDER BY version"
        )
    ]


def test_three_numbers_only_editions_in_schedule_order(cfg: config.Config) -> None:
    for kind in ("report-build", "report-refresh", "report-refresh"):
        summary = jobs.run_job(cfg, kind, DAY, options={"force": True})
        assert summary["status"] == "numbers_only"
    assert reports(cfg) == [
        (1, "numbers_only", "0700"),
        (2, "numbers_only", "0815"),
        (3, "numbers_only", "1440"),
    ]
    conn = db.connect(cfg.settings.db_path)
    payload = json.loads(conn.execute("SELECT payload FROM report WHERE version = 1").fetchone()[0])
    assert payload["status"] == "numbers_only" and payload["edition"] == "0700"
    assert payload["notes"] == [] and payload["sources_unavailable"] == []
    assert {m["key"] for m in payload["mountains"]} == {m.key for m in cfg.mountains}
    stratton = next(m for m in payload["mountains"] if m["key"] == "stratton")
    assert "Wind Advisory" in stratton["alerts"]
    windows = conn.execute("SELECT count(*) FROM forecast_window").fetchone()[0]
    assert windows == 3 * 8 * 3  # one derive per report run


def test_scheduled_report_is_idempotent(cfg: config.Config) -> None:
    jobs.run_job(cfg, "report-build", DAY, options={"edition": "0700"})
    again = jobs.run_job(cfg, "report-build", DAY, options={"edition": "0700"})
    assert again["status"].startswith("skipped")
    assert reports(cfg) == [(1, "numbers_only", "0700")]
    forced = jobs.run_job(cfg, "report-build", DAY, options={"edition": "0700", "force": True})
    assert forced["version"] == 2


def test_0815_without_ecmwf_06z_says_so(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Next day's 8:15 edition built on data whose ECMWF run is 11-26 06Z (before 11-27 06Z).
    monkeypatch.setattr(jobs, "clock", lambda: datetime(2026, 11, 27, 13, 45, tzinfo=UTC))
    summary = jobs.run_job(cfg, "report-refresh", date(2026, 11, 27), options={"edition": "0815"})
    ecmwf = [n for n in summary["notes"] if n.startswith("ECMWF 06Z was not available")]
    assert ecmwf == ["ECMWF 06Z was not available by 8:45 AM; this edition uses the 06Z run."]


def test_report_build_without_ingest_fails(tmp_path: Path) -> None:
    c = config.load({"HAZELST_DB": str(tmp_path / "empty.db")})
    with pytest.raises(jobs.JobError):
        jobs.run_job(c, "report-build", DAY)
    conn = db.connect(c.settings.db_path)
    assert conn.execute("SELECT status FROM run").fetchone()[0] == "failed"


# --- startup catch-up ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (dtime(6, 30), None),
        (dtime(7, 5), "catch-up-0700"),
        (dtime(9, 0), "catch-up-0815"),
        (dtime(15, 0), "catch-up-1440"),
    ],
)
def test_catch_up_targets_latest_due_edition(
    tmp_path: Path, local: dtime, expected: str | None
) -> None:
    c = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    slot = scheduler.due_catch_up(c, datetime.combine(DAY, local, ET))
    assert (slot.id if slot else None) == expected
    if slot:
        assert [s.kind for s in slot.steps][:2] == ["ingest-forecast", "ingest-obs"]


def test_catch_up_skips_published_edition(cfg: config.Config) -> None:
    jobs.run_job(cfg, "report-build", DAY, options={"edition": "0700"})
    assert scheduler.due_catch_up(cfg, datetime.combine(DAY, dtime(7, 30), ET)) is None


def test_catch_up_runs_slot(cfg: config.Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "clock", lambda: datetime(2026, 11, 26, 12, 30, tzinfo=UTC))
    kinds: list[str] = []

    def runner(c: config.Config, kind: str, day: date, **kw: Any) -> dict[str, Any]:
        kinds.append(kind)
        return {"kind": kind}

    scheduler.catch_up(cfg, runner=runner)
    assert kinds == ["ingest-forecast", "ingest-obs", "report-build"]
