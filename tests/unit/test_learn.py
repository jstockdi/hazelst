"""skill-learn and the corrected method, on synthetic verification data.

- a `corrected` value exists only for skill cells with n >= 10 verified snow days
  (data-model.md, "How the data compounds", step 4);
- the page shows it only where `method_gate` chose it (method.py; tests/unit/test_method.py).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hazelst import config, corrected, db, jobs, learn, method
from hazelst.models import Forecast, ForecastWindow, MountainForecast
from tests.support import replay_client
from tests.unit.test_schedule import recorded

CFG = config.load({})
MOUNTAINS = CFG.mountains
DAY0 = date(2026, 1, 5)
MODELS = ("gfs_seamless", "ecmwf_ifs025", "gem_seamless")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "h.db")
    db.migrate(c)
    db.sync_mountains(c, MOUNTAINS)
    return c


def anchor(day: date) -> datetime:
    """6 AM EST on `day` (January: UTC-5)."""
    return datetime(day.year, day.month, day.day, 11, tzinfo=UTC)


def add_fetch(conn: sqlite3.Connection) -> int:
    run_id = db.start_run(conn, "ingest-forecast", None)
    cur = conn.execute(
        "INSERT INTO fetch (run_id, source, url, http_status) VALUES (?, 'open_meteo', 'x', 200)",
        (run_id,),
    )
    return int(cur.lastrowid or 0)


def add_series(
    conn: sqlite3.Connection,
    fetch_id: int,
    mountain: str,
    model: str,
    issued: datetime,
    start: datetime,
    hours: int,
    per_hour: float,
    elevation: str = "summit",
) -> None:
    rows = []
    for i in range(hours):
        valid = start + timedelta(hours=i)
        lead = round((valid - issued).total_seconds() / 3600)
        rows.append(
            (
                fetch_id,
                mountain,
                model,
                elevation,
                issued.isoformat(timespec="seconds"),
                valid.isoformat(timespec="seconds"),
                lead,
                per_hour,
            )
        )
    conn.executemany(
        "INSERT INTO forecast_hourly (fetch_id, mountain, model, elevation, issued_at, valid_at,"
        " lead_h, snowfall_in) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def add_verification(
    conn: sqlite3.Connection,
    day: date,
    mountain: str,
    method: str,
    forecast_in: float,
    observed_in: float | None,
    *,
    edition: str = "0700",
    tags: tuple[str, ...] = (),
) -> None:
    from hazelst import verify

    error, ratio, result = verify.score(forecast_in, observed_in)
    conn.execute(
        "INSERT OR REPLACE INTO verification (for_date, edition, mountain, window, method,"
        " forecast_in, observed_in, observed_source, error_in, ratio, result, tags)"
        " VALUES (?, ?, ?, '1d', ?, ?, ?, 'resort', ?, ?, ?, ?)",
        (
            day.isoformat(),
            edition,
            mountain,
            method,
            forecast_in,
            observed_in,
            error,
            ratio,
            result,
            json.dumps(list(tags)),
        ),
    )


# --- forecasts at lead --------------------------------------------------------------------


def test_model_forecasts_lead_days_coverage_and_averaging(conn: sqlite3.Connection) -> None:
    day = DAY0
    a = anchor(day)
    # Morning run (06Z on the day): lead 5 h at 6 AM -> lead day 1; 0.25 in/h -> 6 in.
    f1 = add_fetch(conn)
    add_series(conn, f1, "killington", "gfs_seamless", a - timedelta(hours=5), a, 24, 0.25)
    # A second ingest of the same lead day (00Z run, lead 11 h): 0.5 in/h -> 12 in; averaged.
    f2 = add_fetch(conn)
    add_series(conn, f2, "killington", "gfs_seamless", a - timedelta(hours=11), a, 24, 0.5)
    # Two days earlier (lead 53 h) -> lead day 3.
    f3 = add_fetch(conn)
    add_series(conn, f3, "killington", "gfs_seamless", a - timedelta(hours=53), a, 24, 0.1)
    # A partial day (12 h) and past hours (negative lead) are skipped.
    f4 = add_fetch(conn)
    add_series(conn, f4, "pico", "gfs_seamless", a - timedelta(hours=5), a, 12, 1.0)
    f5 = add_fetch(conn)
    add_series(conn, f5, "loon", "gfs_seamless", a + timedelta(hours=30), a, 24, 1.0)
    out = learn.model_forecasts(conn)
    assert out == {
        ("killington", "gfs_seamless", 1, day): pytest.approx(9.0),
        ("killington", "gfs_seamless", 3, day): pytest.approx(2.4),
    }


def test_samples_use_scored_truth_and_wait_for_observations(conn: sqlite3.Connection) -> None:
    d1, d2, d3 = DAY0, DAY0 + timedelta(days=1), DAY0 + timedelta(days=2)
    for d in (d1, d2, d3):
        f = add_fetch(conn)
        add_series(
            conn,
            f,
            "killington",
            "gfs_seamless",
            anchor(d) - timedelta(hours=5),
            anchor(d),
            24,
            0.25,
        )
    add_verification(conn, d1, "killington", "raw_mean", 5.0, 4.0, tags=("nw_upslope",))
    add_verification(conn, d1, "killington", "raw_mean", 5.0, 9.9, edition="1440")
    # d2 was never verified; d2's own observation (obs_date d2 + 1) is a resort report.
    conn.execute(
        "INSERT INTO observation (mountain, source, obs_date, reported_at, new_24h_in, raw)"
        " VALUES ('killington', 'resort', ?, ?, 3.0, '{}')",
        (d3.isoformat(), f"{d3.isoformat()}T12:00:00+00:00"),
    )
    found = learn.samples(conn, MOUNTAINS, d3 + timedelta(days=1))  # learns through d2
    assert [(s.day, s.observed_in, s.tags, s.bucket) for s in found] == [
        (d1, 4.0, ("nw_upslope",), "1d"),  # the earliest edition's observation and tags
        (d2, 3.0, (), "1d"),  # verify.observed fallback
    ]


# --- skill cells ------------------------------------------------------------------------


def sample(day: int, f: float, o: float, tags: tuple[str, ...] = (), lead: int = 1) -> learn.Sample:
    return learn.Sample("sugarbush", "gfs_seamless", lead, DAY0 + timedelta(days=day), f, o, tags)


def test_build_counts_snow_days_and_slices_by_tag() -> None:
    rows = learn.build(
        [
            sample(0, 4.0, 2.0, ("nw_upslope",)),
            sample(1, 6.0, 3.0, ("nw_upslope",)),
            sample(2, 1.0, 0.0),
            sample(3, 0.0, 0.0),  # dry and forecast dry: not a snow day
            sample(4, 0.2, 0.4),  # under half an inch either way
            sample(5, 1.0, 2.0, lead=2),
            sample(6, 3.0, 4.0, lead=3),
        ]
    )
    by_key = {(r.lead_bucket, r.tag): r for r in rows}
    assert set(by_key) == {("1d", ""), ("1d", "nw_upslope"), ("2-3d", "")}
    all_days = by_key[("1d", "")]
    assert (all_days.n, all_days.bias_ratio, all_days.mae_in) == (3, 2.2, 2.0)
    assert all_days.hit_rate == pytest.approx(0.667)  # 6 vs 3 misses
    upslope = by_key[("1d", "nw_upslope")]
    assert (upslope.n, upslope.bias_ratio) == (2, 2.0)
    pooled = by_key[("2-3d", "")]
    assert (pooled.n, pooled.bias_ratio, pooled.mae_in, pooled.hit_rate) == (2, 0.667, 1.0, 1.0)


def test_build_bias_is_null_when_nothing_fell() -> None:
    (row,) = learn.build([sample(0, 2.0, 0.0)])
    assert row.bias_ratio is None and row.hit_rate == 1.0


# --- gate 1: n >= 10 verified snow days ---------------------------------------------------


def window(snow: float, by_model: dict[str, float | None]) -> ForecastWindow:
    values = [v for v in by_model.values() if v is not None]
    return ForecastWindow(
        snow_in=snow,
        range=(min(values), max(values)),
        confidence="high",
        by_model=by_model,
        base_snow_in=0.0,
        nws_in=None,
        nws_ice_in=None,
        nws_disagrees=False,
    )


def forecast(models: tuple[str, ...] = ("gfs_seamless", "gem_seamless")) -> Forecast:
    mf = MountainForecast(
        key="sugarbush",
        windows={
            "1d": window(5.0, {"gfs_seamless": 6.0, "gem_seamless": 4.0}),
            "3d": window(9.0, {"gfs_seamless": 10.0, "gem_seamless": 8.0}),
            "5d": window(11.0, {"gfs_seamless": 12.0, "gem_seamless": 10.0}),
        },
        days=[],
        storm_type="none",
    )
    return Forecast(
        date=DAY0,
        anchor=anchor(DAY0),
        as_of=anchor(DAY0),
        ingest_run_id=None,
        models=[],
        used_models=list(models),
        storm_words={},
        mountains=[mf],
    )


def skill_rows(n: int, buckets: tuple[str, ...] = learn.BUCKETS) -> list[learn.SkillRow]:
    out = []
    for bucket in buckets:
        out.append(learn.SkillRow("sugarbush", "gfs_seamless", bucket, "", n, 2.0, 1.0, 0.5))
        out.append(learn.SkillRow("sugarbush", "gem_seamless", bucket, "", n, 0.5, 3.0, 0.5))
    return out


def test_no_corrected_value_below_ten_snow_days(conn: sqlite3.Connection) -> None:
    learn.store(conn, skill_rows(9), datetime.now(UTC))
    cells = corrected.load_cells(conn)
    assert cells == {}
    assert corrected.correct(forecast(), cells) == {}


def test_corrected_value_from_ten_snow_days(conn: sqlite3.Connection) -> None:
    learn.store(conn, skill_rows(10), datetime.now(UTC))
    out = corrected.correct(forecast(), corrected.load_cells(conn))["sugarbush"]
    # 1d: GFS 6 / 2 = 3 (weight 1), GEM 4 / 0.5 = 8 (weight 1/3) -> (3 + 8/3) / (4/3) = 4.25.
    assert out["1d"].snow_in == 4.2  # round(4.25, 1) rounds half to even
    assert out["1d"].by_model == {"gfs_seamless": 3.0, "gem_seamless": 8.0}
    assert (out["1d"].lo_in, out["1d"].hi_in) == (3.0, 8.0)
    # 3d adds days 2-3: GFS 4 / 2 = 2, GEM 4 / 0.5 = 8 -> 3.5; 4.25 + 3.5 = 7.75.
    assert out["3d"].snow_in == pytest.approx(7.8)
    # 5d adds days 4-5: GFS 2 / 2 = 1, GEM 2 / 0.5 = 4 -> 1.75; 9.5.
    assert out["5d"].snow_in == 9.5


def test_window_needs_every_segment_and_skips_stale_models(conn: sqlite3.Connection) -> None:
    learn.store(conn, skill_rows(12, buckets=("1d",)), datetime.now(UTC))
    cells = corrected.load_cells(conn)
    assert set(corrected.correct(forecast(), cells)["sugarbush"]) == {"1d"}
    # GEM is stale (not in used_models): only GFS is blended.
    only_gfs = corrected.correct(forecast(models=("gfs_seamless",)), cells)["sugarbush"]
    assert only_gfs["1d"].snow_in == 3.0
    assert only_gfs["1d"].by_model == {"gfs_seamless": 3.0}


def test_tag_cells_do_not_drive_the_correction(conn: sqlite3.Connection) -> None:
    rows = [learn.SkillRow("sugarbush", "gfs_seamless", "1d", "nw_upslope", 30, 2.0, 1.0, 0.5)]
    learn.store(conn, rows, datetime.now(UTC))
    assert corrected.load_cells(conn) == {}


def test_bias_scale_is_clamped(conn: sqlite3.Connection) -> None:
    rows = [learn.SkillRow("sugarbush", "gfs_seamless", "1d", "", 10, 40.0, 1.0, 0.1)]
    learn.store(conn, rows, datetime.now(UTC))
    out = corrected.correct(forecast(("gfs_seamless",)), corrected.load_cells(conn))
    assert out["sugarbush"]["1d"].snow_in == 1.5  # 6 / 4, not 6 / 40


# --- shown beneath the headline only where method_gate chose it ---------------------------


def test_apply_sets_corrected_only_for_passed_mountains(conn: sqlite3.Connection) -> None:
    learn.store(conn, skill_rows(10), datetime.now(UTC))
    fc = forecast()
    corr = corrected.correct(fc, corrected.load_cells(conn))
    hidden = corrected.apply(fc, corr, [])
    assert all(w.corrected_in is None for w in hidden.mountains[0].windows.values())
    shown = corrected.apply(fc, corr, ["sugarbush"])
    assert shown.mountains[0].windows["5d"].corrected_in == 9.5
    assert shown.mountains[0].windows["5d"].snow_in == 11.0  # the mean stays the headline


# --- the jobs -------------------------------------------------------------------------------


def test_skill_learn_job_rebuilds_skill(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    conn = db.connect(cfg.settings.db_path)
    db.migrate(conn)
    db.sync_mountains(conn, cfg.mountains)
    for i in range(12):
        d = DAY0 + timedelta(days=i)
        f = add_fetch(conn)
        for model, rate in (("gfs_seamless", 0.5), ("gem_seamless", 0.25)):
            add_series(
                conn, f, "sugarbush", model, anchor(d) - timedelta(hours=5), anchor(d), 24, rate
            )
        add_verification(conn, d, "sugarbush", "raw_mean", 9.0, 6.0, tags=("nw_upslope",))
    conn.execute(
        "INSERT INTO skill (mountain, model, lead_bucket, tag, n) VALUES ('old', 'x', '1d', '', 1)"
    )
    conn.commit()
    summary = jobs.run_job(cfg, "skill-learn", DAY0 + timedelta(days=20))
    assert summary["samples"] == 24 and summary["skill"] == 4
    assert summary["ready"] == ["sugarbush/gem_seamless/1d", "sugarbush/gfs_seamless/1d"]
    rows = {
        (r[0], r[1]): r[2:]
        for r in conn.execute(
            "SELECT model, tag, n, bias_ratio, mae_in, hit_rate FROM skill ORDER BY 1, 2"
        )
    }
    assert rows == {
        ("gem_seamless", ""): (12, 1.0, 0.0, 1.0),
        ("gem_seamless", "nw_upslope"): (12, 1.0, 0.0, 1.0),
        ("gfs_seamless", ""): (12, 2.0, 6.0, 0.0),
        ("gfs_seamless", "nw_upslope"): (12, 2.0, 6.0, 0.0),
    }


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    monkeypatch.setattr(jobs, "clock", lambda: datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    c = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    jobs.run_job(c, "ingest-forecast", date(2026, 11, 26), client=replay_client(recorded()))
    return c


def test_report_build_stores_corrected_rows_and_shows_them_after_the_gate(
    built: config.Config,
) -> None:
    day = date(2026, 11, 26)
    conn = db.connect(built.settings.db_path)
    rows = [
        learn.SkillRow(m.key, model, bucket, "", 15, 2.0, 1.0, 0.5)
        for m in built.mountains
        for model in MODELS
        for bucket in learn.BUCKETS
    ]
    with db.transaction(conn):
        learn.store(conn, rows, datetime.now(UTC))
    summary = jobs.run_job(built, "report-build", day, options={"force": True})
    assert summary["corrected"]["shown"] == []
    assert len(summary["corrected"]["mountains"]) == 8
    pairs = conn.execute(
        "SELECT r.snow_in, c.snow_in FROM forecast_window r JOIN forecast_window c"
        " ON c.run_id = r.run_id AND c.mountain = r.mountain AND c.window = r.window"
        " WHERE r.method = 'raw_mean' AND c.method = 'corrected' AND r.run_id = ?",
        (summary["run_id"],),
    ).fetchall()
    assert len(pairs) == 8 * 3
    for raw, fixed in pairs:  # every model halved, so the blend is about half the mean
        assert fixed == pytest.approx(raw / 2, abs=0.2)
    payload = json.loads(conn.execute("SELECT payload FROM report").fetchone()[0])
    assert all(
        w.get("corrected_in") is None
        for m in payload["mountains"]
        for w in m["forecast"]["windows"].values()
    )
    with db.transaction(conn):
        method.store(
            conn,
            [
                method.GateRow("killington", "raw_mean", None, None, 20, 2.0, 2.0, True),
                method.GateRow("killington", "corrected", None, None, 20, 0.5, 2.0, True),
                method.GateRow("loon", "corrected", None, None, 20, 1.99, 2.0, False),
            ],
            datetime.now(UTC),
        )
    summary = jobs.run_job(built, "report-build", day, options={"force": True})
    assert summary["corrected"]["shown"] == ["killington"]
    payload = json.loads(
        conn.execute("SELECT payload FROM report ORDER BY version DESC LIMIT 1").fetchone()[0]
    )
    shown = {
        m["key"]: m["forecast"]["windows"]["1d"].get("corrected_in") for m in payload["mountains"]
    }
    assert shown["killington"] is not None
    assert {k for k, v in shown.items() if v is not None} == {"killington"}
