"""Method choice per mountain (method.py): the held-out season gate, live days, the CLI.

Fixtures are synthetic: lead-day-1 summit series and resort reports from
tests/unit/test_ratio.py, and `verification` rows for the live days.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hazelst import cli, config, db, jobs, method
from tests.unit.test_learn import add_verification
from tests.unit.test_ratio import add_hours, add_report

CFG = config.load({})
MOUNTAINS = CFG.mountains
AS_OF = date(2026, 11, 27)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "h.db")
    db.migrate(c)
    db.sync_mountains(c, MOUNTAINS)
    return c


def stored(conn: sqlite3.Connection, *methods: str) -> None:
    """A `forecast_window` row per method, so `method choose` treats it as a candidate."""
    run_id = db.start_run(conn, "report-build", AS_OF)
    conn.executemany(
        "INSERT INTO forecast_window (run_id, mountain, window, method, snow_in)"
        " VALUES (?, 'loon', '1d', ?, 1.0)",
        [(run_id, m) for m in methods],
    )


def two_seasons(conn: sqlite3.Connection) -> None:
    """Loon, 16 snow days a season: 10:1 in 2024-25, 20:1 in the held-out 2025-26."""
    for start, obs in ((date(2025, 1, 5), 2.4), (date(2026, 1, 5), 4.8)):
        for i in range(16):
            d = start + timedelta(days=i)
            add_hours(conn, d, "loon", snow={"gfs_seamless": 0.07}, temp=10)
            add_report(conn, "loon", d, obs)
    conn.commit()


# --- seasons -----------------------------------------------------------------------------------


def test_held_out_season_is_the_newest_complete_one() -> None:
    assert method.held_out_season(AS_OF) == (date(2025, 11, 1), date(2026, 4, 30))
    assert method.held_out_season(date(2026, 5, 1)) == (date(2025, 11, 1), date(2026, 4, 30))
    assert method.held_out_season(date(2026, 4, 30)) == (date(2024, 11, 1), date(2025, 4, 30))
    assert method.current_season(AS_OF) == (date(2026, 11, 1), date(2027, 4, 30))
    assert method.current_season(date(2027, 2, 1)) == (date(2026, 11, 1), date(2027, 4, 30))


# --- the rule ----------------------------------------------------------------------------------


def test_a_method_needs_a_margin_of_five_hundredths() -> None:
    assert method.decide({"raw_mean": 1.0, "resort_ratio": 0.95}) == ("resort_ratio", set())
    assert method.decide({"raw_mean": 1.0, "resort_ratio": 0.951}) == ("raw_mean", set())
    assert method.decide({"raw_mean": 1.0, "resort_ratio": 0.993}) == ("raw_mean", set())


def test_the_lowest_candidate_wins_and_a_tie_keeps_the_mean() -> None:
    maes = {"raw_mean": 2.0, "resort_ratio": 1.5, "fine_ratio": 1.2}
    assert method.decide(maes) == ("fine_ratio", set())
    # The lowest misses the margin: the mean stays, even though another beats it.
    assert method.decide({"raw_mean": 1.0, "a": 0.97, "b": 0.99}) == ("raw_mean", set())
    assert method.decide({"raw_mean": 2.0, "a": 1.5, "b": 1.5}) == ("raw_mean", set())


def test_corrected_is_shown_beside_never_as_the_headline() -> None:
    assert method.decide({"raw_mean": 2.0, "resort_ratio": 1.8, "corrected": 1.0}) == (
        "resort_ratio",
        {"corrected"},
    )
    assert method.decide({"raw_mean": 2.0, "corrected": 1.97}) == ("raw_mean", set())


# --- held-out season ---------------------------------------------------------------------------


def test_choose_scores_the_held_out_season(conn: sqlite3.Connection) -> None:
    two_seasons(conn)
    stored(conn, "raw_mean", "resort_ratio")
    rows = {(r.mountain, r.method): r for r in method.choose(conn, MOUNTAINS, AS_OF)}
    ratio_row = rows[("loon", "resort_ratio")]
    raw_row = rows[("loon", "raw_mean")]
    # Fit 10:1 on 2024-25: 2.4 in vs 4.8 observed; raw mean 1.68 (7:1).
    assert (ratio_row.n, ratio_row.mae_in, ratio_row.raw_mean_mae_in) == (16, 2.4, 3.12)
    assert (ratio_row.test_from, ratio_row.test_to) == (date(2025, 11, 1), date(2026, 4, 30))
    assert ratio_row.chosen and not raw_row.chosen
    # Every mountain gets a row; one without test days keeps the mean.
    assert {m.key for m in MOUNTAINS} == {k[0] for k in rows}
    assert rows[("killington", "raw_mean")].chosen and rows[("killington", "raw_mean")].n == 0


def test_candidates_are_the_methods_report_build_stores(conn: sqlite3.Connection) -> None:
    two_seasons(conn)
    rows = method.choose(conn, MOUNTAINS, AS_OF)
    assert {r.method for r in rows} == {"raw_mean"}  # nothing stored yet
    stored(conn, "raw_mean", "resort_ratio", "corrected")
    loon = {r.method for r in method.choose(conn, MOUNTAINS, AS_OF) if r.mountain == "loon"}
    assert loon == {"raw_mean", "resort_ratio", "corrected"}


# --- live days ---------------------------------------------------------------------------------


def live_days(conn: sqlite3.Connection, count: int, *, edition: str = "0700") -> None:
    """Loon's live days this season: the mean misses by 2 in, resort_ratio by 1 in."""
    for i in range(count):
        d = date(2026, 11, 2) + timedelta(days=i)
        add_verification(conn, d, "loon", "raw_mean", 2.0, 4.0, edition=edition)
        add_verification(conn, d, "loon", "resort_ratio", 5.0, 4.0, edition=edition)
    conn.commit()


def test_twenty_live_snow_days_replace_the_held_out_season(conn: sqlite3.Connection) -> None:
    two_seasons(conn)
    stored(conn, "raw_mean", "resort_ratio")
    live_days(conn, 25, edition="backfill")  # backfilled days are not live
    live_days(conn, 19)
    rows = {r.method: r for r in method.choose(conn, MOUNTAINS, AS_OF) if r.mountain == "loon"}
    assert rows["resort_ratio"].test_from == date(2025, 11, 1)
    live_days(conn, 20)
    rows = {r.method: r for r in method.choose(conn, MOUNTAINS, AS_OF) if r.mountain == "loon"}
    r = rows["resort_ratio"]
    assert (r.test_from, r.test_to, r.n) == (date(2026, 11, 2), date(2026, 11, 21), 20)
    assert (r.mae_in, r.raw_mean_mae_in, r.chosen) == (1.0, 2.0, True)


def test_live_days_need_every_candidate_and_snow(conn: sqlite3.Connection) -> None:
    stored(conn, "raw_mean", "resort_ratio")
    live_days(conn, 20)
    day = date(2026, 11, 25)
    add_verification(conn, day, "loon", "raw_mean", 0.0, 0.0)  # dry
    add_verification(conn, day, "loon", "resort_ratio", 0.1, 0.0)
    add_verification(conn, day + timedelta(days=1), "loon", "raw_mean", 3.0, 4.0)  # one method
    span = method.current_season(AS_OF)
    days, errors = method.live_errors(conn, "loon", {"raw_mean", "resort_ratio"}, span)
    assert len(days) == 20 and set(errors) == {"raw_mean", "resort_ratio"}


def test_a_days_editions_are_averaged(conn: sqlite3.Connection) -> None:
    d = date(2026, 11, 2)
    add_verification(conn, d, "loon", "raw_mean", 2.0, 4.0, edition="0700")
    add_verification(conn, d, "loon", "raw_mean", 3.0, 4.0, edition="0815")
    days, errors = method.live_errors(conn, "loon", {"raw_mean"}, method.current_season(AS_OF))
    assert days == [d] and errors["raw_mean"] == [1.5]


# --- store, load, jobs, CLI --------------------------------------------------------------------


def test_store_replaces_the_table_and_load_reads_the_choice(conn: sqlite3.Connection) -> None:
    rows = [
        method.GateRow("loon", "raw_mean", None, None, 3, 1.0, 1.0, False),
        method.GateRow("loon", "resort_ratio", None, None, 3, 0.5, 1.0, True),
        method.GateRow("loon", "corrected", None, None, 3, 0.6, 1.0, True),
        method.GateRow("pico", "raw_mean", None, None, 3, 1.0, 1.0, True),
    ]
    now = datetime(2026, 11, 27, 7, tzinfo=UTC)
    method.store(conn, [method.GateRow("old", "raw_mean", None, None, 0, None, None, True)], now)
    assert method.store(conn, rows, now) == 4
    choice = method.load(conn)
    assert choice.headline == {"loon": "resort_ratio", "pico": "raw_mean"}
    assert choice.uses("resort_ratio") == ["loon"] and choice.shows("corrected") == ["loon"]


def test_skill_learn_rewrites_method_gate(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    conn = db.connect(cfg.settings.db_path)
    db.migrate(conn)
    db.sync_mountains(conn, cfg.mountains)
    two_seasons(conn)
    stored(conn, "raw_mean", "resort_ratio")
    conn.commit()
    summary = jobs.run_job(cfg, "skill-learn", AS_OF)
    assert summary["method_gate"]["loon"] == "resort_ratio"
    assert summary["method_gate"]["pico"] == "raw_mean"
    n = conn.execute("SELECT count(DISTINCT mountain) FROM method_gate").fetchone()[0]
    assert n == len(cfg.mountains)


def test_cli_method_choose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "h.db"
    monkeypatch.setenv("HAZELST_DB", str(path))
    conn = db.connect(path)
    db.migrate(conn)
    db.sync_mountains(conn, MOUNTAINS)
    two_seasons(conn)
    stored(conn, "raw_mean", "resort_ratio")
    conn.commit()
    assert cli.main(["method", "choose", "--date", AS_OF.isoformat(), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "loon" in out and "dry run" in out
    assert conn.execute("SELECT count(*) FROM method_gate").fetchone()[0] == 0
    assert cli.main(["method", "choose", "--date", AS_OF.isoformat()]) == 0
    chosen = conn.execute("SELECT method FROM method_gate WHERE mountain = 'loon' AND chosen = 1")
    assert [r[0] for r in chosen] == ["resort_ratio"]


def test_cli_schedule_prints_every_slot(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["schedule"]) == 0
    out = json.loads(capsys.readouterr().out)
    slots = {s["slot"]: s["steps"] for s in out["slots"]}
    assert slots["report-rebuild"] == ["report-build"]
    assert len(slots) == 11
