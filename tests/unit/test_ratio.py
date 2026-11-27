"""The `resort_ratio` method (ratio.py): fit, fallbacks, forecast, gate, backtest.

Fixtures are synthetic: lead-day-1 summit series in `forecast_hourly` and
resort reports in `observation` (SnoCountry rows, the resort's own number).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hazelst import config, db, jobs, ratio, verify
from hazelst.ratio import ALL_BANDS, ALL_MOUNTAINS, BOTH_SIDES, MEAN_MODEL, DayWater, LeadDay
from tests.support import replay_client
from tests.unit.test_learn import add_verification
from tests.unit.test_schedule import recorded

CFG = config.load({})
MOUNTAINS = CFG.mountains
DAY0 = date(2026, 1, 5)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "h.db")
    db.migrate(c)
    db.sync_mountains(c, MOUNTAINS)
    return c


def lead_day(
    mountain: str,
    day: date,
    water: dict[str, float],
    *,
    model: str = "gfs_seamless",
    upslope: bool = False,
) -> LeadDay:
    dw = DayWater(round(sum(water.values()) * 7, 3), water)
    return LeadDay(mountain, day, upslope, {model: dw, MEAN_MODEL: dw})


# --- bands ---------------------------------------------------------------------------------


def test_bands_have_inclusive_upper_bounds() -> None:
    assert [ratio.band(t) for t in (-5, 15, 15.1, 25, 25.5, 32, 32.1, None)] == [
        "<=15",
        "<=15",
        "15-25",
        "15-25",
        "25-32",
        "25-32",
        ">32",
        "",
    ]


# --- fit -----------------------------------------------------------------------------------


def test_fit_is_resort_snow_over_snow_water_per_cell() -> None:
    pairs = [
        (lead_day("loon", DAY0, {"15-25": 0.5}), 6.0),
        (lead_day("loon", DAY0 + timedelta(days=1), {"15-25": 0.25}), 4.0),
        # Split day: 8 in of resort snow shared 3:1 between two bands.
        (lead_day("loon", DAY0 + timedelta(days=2), {"<=15": 0.3, "25-32": 0.1}), 8.0),
    ]
    cells = {
        (r.mountain, r.model, r.temp_band, r.upslope): r
        for r in ratio.fit(pairs, DAY0 + timedelta(days=2))
    }
    mild = cells[("loon", "gfs_seamless", "15-25", BOTH_SIDES)]
    assert (mild.ratio, mild.n) == (round(10 / 0.75, 2), 2)
    assert cells[("loon", "gfs_seamless", "<=15", BOTH_SIDES)].ratio == 20.0  # 6 / 0.3
    assert cells[("loon", "gfs_seamless", "25-32", BOTH_SIDES)].ratio == 20.0  # 2 / 0.1
    every = cells[("loon", "gfs_seamless", ALL_BANDS, BOTH_SIDES)]
    assert (every.ratio, every.n) == (round(18 / 1.15, 2), 3)
    assert cells[("loon", "gfs_seamless", ALL_BANDS, "no")].n == 3
    assert ("loon", "gfs_seamless", ALL_BANDS, "yes") not in cells
    assert cells[(ALL_MOUNTAINS, MEAN_MODEL, ALL_BANDS, BOTH_SIDES)].n == 3
    assert {r.fitted_through for r in cells.values()} == {"2026-01-07"}


def test_fit_clamps_and_skips_dry_and_waterless_days() -> None:
    pairs = [
        (lead_day("loon", DAY0, {"15-25": 0.1}), 9.0),  # 90:1 -> 25
        (lead_day("stowe", DAY0, {"15-25": 0.2}), 0.0),  # forecast snow, none reported -> 4
        (lead_day("loon", DAY0 + timedelta(days=1), {}), 5.0),  # no snow water: no ratio
        (lead_day("loon", DAY0 + timedelta(days=2), {"15-25": 0.01}), 0.2),  # not a snow day
    ]
    cells = {(r.mountain, r.temp_band, r.upslope): r for r in ratio.fit(pairs, DAY0)}
    assert cells[("loon", "15-25", BOTH_SIDES)].ratio == 25.0
    assert cells[("loon", "15-25", BOTH_SIDES)].n == 1
    assert cells[("stowe", "15-25", BOTH_SIDES)].ratio == 4.0


# --- fallbacks -----------------------------------------------------------------------------


def days_at(
    mountain: str, n: int, water: dict[str, float], obs: float, **kw: object
) -> list[tuple[LeadDay, float]]:
    return [
        (lead_day(mountain, DAY0 + timedelta(days=i), water, **kw), obs)  # type: ignore[arg-type]
        for i in range(n)
    ]


def test_thin_band_uses_the_all_mountain_value_for_that_band() -> None:
    pairs = [
        *days_at("loon", 20, {"15-25": 0.5}, 6.0),  # loon 15-25: 12:1, n 20
        *days_at("loon", 3, {"<=15": 0.5}, 10.0),  # loon <=15: 20:1, n 3 (thin)
        *days_at("stowe", 14, {"<=15": 0.5}, 8.0),  # pooled <=15: (30 + 112) / 8.5
        *days_at("stowe", 14, {"25-32": 0.5}, 4.0),
    ]
    r = ratio.Ratios(ratio.fit(pairs, DAY0))
    assert r.ratio("loon", "gfs_seamless", "15-25", None) == 12.0
    assert r.ratio("loon", "gfs_seamless", "<=15", None) == round(142 / 8.5, 2)
    # Pooled 25-32 has only 14 days: the mountain's own all-band value.
    own = r.resolve("loon", "gfs_seamless", "25-32", None)
    assert own is not None and (own.mountain, own.temp_band) == ("loon", ALL_BANDS)
    # A model with no cells of its own (ECMWF) uses the models' mean.
    assert r.model_key("ecmwf_ifs025") == MEAN_MODEL
    assert r.ratio("loon", "ecmwf_ifs025", "15-25", None) == 12.0


def test_upslope_split_needs_fifteen_days_on_each_side() -> None:
    upslope = days_at("sugarbush", 15, {"<=15": 0.5}, 9.0, upslope=True)  # 18:1
    other = days_at("sugarbush", 14, {"<=15": 0.5}, 5.0)  # 10:1
    r = ratio.Ratios(ratio.fit([*upslope, *other], DAY0))
    assert not r.splits("sugarbush", "gfs_seamless")
    both = round(205 / 14.5, 2)
    assert r.ratio("sugarbush", "gfs_seamless", "<=15", "yes") == both
    more = days_at("sugarbush", 1, {"<=15": 0.5}, 5.0)
    r = ratio.Ratios(ratio.fit([*upslope, *other, *more], DAY0))
    assert r.splits("sugarbush", "gfs_seamless")
    assert r.ratio("sugarbush", "gfs_seamless", "<=15", "yes") == 18.0
    assert r.ratio("sugarbush", "gfs_seamless", "<=15", "no") == 10.0
    assert r.ratio("sugarbush", "gfs_seamless", "<=15", None) == 14.0  # temperature only


def test_split_side_with_a_thin_band_uses_the_mountains_band_value() -> None:
    pairs = [
        *days_at("sugarbush", 15, {"<=15": 0.5}, 9.0, upslope=True),
        *days_at("sugarbush", 15, {"15-25": 0.5}, 5.0),
        *days_at("sugarbush", 2, {"15-25": 0.5}, 7.0, upslope=True),
    ]
    r = ratio.Ratios(ratio.fit(pairs, DAY0))
    assert r.splits("sugarbush", "gfs_seamless")
    used = r.resolve("sugarbush", "gfs_seamless", "15-25", "yes")
    assert used is not None and (used.mountain, used.upslope, used.n) == ("sugarbush", "", 17)


# --- lead-day-1 forecasts from forecast_hourly ---------------------------------------------


def anchor(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 11, tzinfo=UTC)  # 6 AM EST


def add_hours(
    conn: sqlite3.Connection,
    day: date,
    mountain: str,
    *,
    snow: dict[str, float],
    temp: float,
    upslope: bool = False,
    lead_start: int = 0,
) -> None:
    run_id = db.start_run(conn, "season-backfill", None)
    fetch_id = conn.execute(
        "INSERT INTO fetch (run_id, source, url, http_status) VALUES (?, 'x', 'x', 200)",
        (run_id,),
    ).lastrowid
    rows = []
    for model, per_hour in snow.items():
        for i in range(24):
            valid = anchor(day) + timedelta(hours=i)
            rows.append(
                (
                    fetch_id,
                    mountain,
                    model,
                    "summit",
                    valid.isoformat(timespec="seconds"),
                    lead_start + i,
                    per_hour,
                    temp if i < 12 else temp + 10,
                    10.0 if upslope else 30.0,
                    90.0,
                    40.0,
                    300.0,
                )
            )
    conn.executemany(
        "INSERT INTO forecast_hourly (fetch_id, mountain, model, elevation, valid_at, lead_h,"
        " snowfall_in, temp_f, t850_f, rh850, ws850_mph, wd850)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def add_report(conn: sqlite3.Connection, mountain: str, day: date, snow_in: float) -> None:
    conn.execute(
        "INSERT INTO observation (mountain, source, obs_date, new_24h_in)"
        " VALUES (?, 'snocountry', ?, ?)",
        (mountain, (day + timedelta(days=1)).isoformat(), snow_in),
    )


def test_lead_days_split_water_by_band_and_tag_upslope(conn: sqlite3.Connection) -> None:
    add_hours(conn, DAY0, "loon", snow={"gfs_seamless": 0.14, "gem_seamless": 0.07}, temp=10)
    add_hours(conn, DAY0 + timedelta(days=1), "loon", snow={"gfs_seamless": 0.07}, temp=10,
              upslope=True)  # fmt: skip
    add_hours(conn, DAY0 + timedelta(days=2), "loon", snow={"gfs_seamless": 0.07}, temp=10,
              lead_start=30)  # fmt: skip
    days = ratio.lead_days(conn)
    assert set(days) == {("loon", DAY0), ("loon", DAY0 + timedelta(days=1))}  # lead day 2 left out
    first = days[("loon", DAY0)]
    assert not first.upslope
    gfs = first.models["gfs_seamless"]
    assert gfs.snow_in == pytest.approx(3.36)
    assert gfs.water == pytest.approx({"<=15": 0.24, "15-25": 0.24})
    assert first.models[MEAN_MODEL].snow_in == pytest.approx((3.36 + 1.68) / 2)
    assert days[("loon", DAY0 + timedelta(days=1))].upslope


# --- live forecast: days sum to windows, gate, page ----------------------------------------


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    monkeypatch.setattr(jobs, "clock", lambda: datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    c = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    jobs.run_job(c, "ingest-forecast", date(2026, 11, 26), client=replay_client(recorded()))
    return c


def flat_ratios(value: float) -> list[ratio.RatioRow]:
    return [
        ratio.RatioRow(m, MEAN_MODEL, b, BOTH_SIDES, value, 30, "2026-11-24")
        for m in (*(x.key for x in MOUNTAINS), ALL_MOUNTAINS)
        for b in (*ratio.BANDS, ALL_BANDS)
    ]


def test_report_build_stores_resort_ratio_and_shows_it_after_the_gate(
    built: config.Config,
) -> None:
    day = date(2026, 11, 26)
    conn = db.connect(built.settings.db_path)
    with db.transaction(conn):
        ratio.store(conn, flat_ratios(14.0))  # twice Open-Meteo's 7:1
    summary = jobs.run_job(built, "report-build", day, options={"force": True})
    assert summary["resort_ratio"]["shown"] == []
    assert len(summary["resort_ratio"]["mountains"]) == 8
    run_id = summary["run_id"]
    pairs = conn.execute(
        "SELECT r.mountain, r.window, r.snow_in, x.snow_in, x.lo_in, x.hi_in FROM forecast_window r"
        " JOIN forecast_window x ON x.run_id = r.run_id AND x.mountain = r.mountain"
        "  AND x.window = r.window AND x.method = 'resort_ratio'"
        " WHERE r.method = 'raw_mean' AND r.run_id = ?",
        (run_id,),
    ).fetchall()
    assert len(pairs) == 8 * 3
    for _m, _w, raw, scaled, lo, hi in pairs:
        assert scaled == pytest.approx(2 * raw, abs=0.15)
        assert lo <= scaled <= hi
    windows = {(m, w): x for m, w, _r, x, _lo, _hi in pairs}
    for mountain in {p[0] for p in pairs}:
        days = [
            r[0]
            for r in conn.execute(
                "SELECT resort_ratio_in FROM forecast_day WHERE run_id = ? AND mountain = ?"
                " ORDER BY day",
                (run_id, mountain),
            )
        ]
        assert days[0] == windows[(mountain, "1d")]
        assert sum(days[:3]) == pytest.approx(windows[(mountain, "3d")], abs=0.15)
        assert sum(days) == pytest.approx(windows[(mountain, "5d")], abs=0.25)
    payload = json.loads(conn.execute("SELECT payload FROM report").fetchone()[0])
    assert {m["forecast"].get("method", "raw_mean") for m in payload["mountains"]} == {"raw_mean"}

    with db.transaction(conn):
        for i in range(20):
            d = day - timedelta(days=30 - i)
            add_verification(conn, d, "killington", "raw_mean", 1.0, 4.0, edition="backfill")
            add_verification(conn, d, "killington", "resort_ratio", 3.5, 4.0, edition="backfill")
    summary = jobs.run_job(built, "report-build", day, options={"force": True})
    assert summary["resort_ratio"]["shown"] == ["killington"]
    payload = json.loads(
        conn.execute("SELECT payload FROM report ORDER BY version DESC LIMIT 1").fetchone()[0]
    )
    by_key = {m["key"]: m["forecast"] for m in payload["mountains"]}
    k = by_key["killington"]
    assert k["method"] == "resort_ratio"
    one = k["windows"]["1d"]
    assert one["snow_in"] == windows[("killington", "1d")]
    assert one["raw_mean_in"] is not None
    assert k["days"][0]["snow_in"] == one["snow_in"]  # chart and table agree
    assert sum(d["snow_in"] for d in k["days"]) == pytest.approx(
        k["windows"]["5d"]["snow_in"], abs=0.25
    )
    assert by_key["loon"]["method"] == "raw_mean"
    html = conn.execute("SELECT payload FROM report ORDER BY version DESC LIMIT 1").fetchone()[0]
    from hazelst.models import ReportPayload
    from hazelst.render import page

    rendered = page.render_report(ReportPayload.model_validate_json(html))
    assert "resort ratio" in rendered and "model mean" in rendered


def test_skill_learn_job_fits_snow_ratio(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    conn = db.connect(cfg.settings.db_path)
    db.migrate(conn)
    db.sync_mountains(conn, cfg.mountains)
    for i in range(16):
        d = DAY0 + timedelta(days=i)
        add_hours(conn, d, "loon", snow={"gfs_seamless": 0.07}, temp=10)  # 0.24 in water
        add_report(conn, "loon", d, 3.6)  # 15:1
    add_hours(conn, DAY0 + timedelta(days=30), "loon", snow={"gfs_seamless": 0.07}, temp=10)
    add_report(conn, "loon", DAY0 + timedelta(days=30), 9.0)  # after D - 2: not read
    conn.commit()
    summary = jobs.run_job(cfg, "skill-learn", DAY0 + timedelta(days=18))
    assert summary["snow_ratio_mountains"] == ["loon"]
    r = ratio.Ratios.load(conn)
    assert r.ratio("loon", "gfs_seamless", "<=15", None) == 15.0
    assert r.ratio("loon", "ecmwf_ifs025", "15-25", None) == 15.0
    assert {c.fitted_through for c in r.cells.values()} == {"2026-01-21"}


# --- backfill edition and backtest ---------------------------------------------------------


def two_seasons(conn: sqlite3.Connection) -> None:
    """Loon, 16 snow days a season: 10:1 in 2024-25, 20:1 in 2025-26."""
    for start, obs in ((date(2025, 1, 5), 2.4), (date(2026, 1, 5), 4.8)):
        for i in range(16):
            d = start + timedelta(days=i)
            add_hours(conn, d, "loon", snow={"gfs_seamless": 0.07}, temp=10)
            add_report(conn, "loon", d, obs)
    conn.commit()


def test_backfill_values_leave_the_days_season_out(conn: sqlite3.Connection) -> None:
    two_seasons(conn)
    values = ratio.backfill_values(conn, MOUNTAINS, date(2025, 1, 1), date(2026, 4, 30))
    assert values[("loon", date(2025, 1, 5))]["resort_ratio"] == pytest.approx(4.8)  # 20:1
    assert values[("loon", date(2026, 1, 5))]["resort_ratio"] == pytest.approx(2.4)  # 10:1
    forecasts = verify.backfill_forecasts(conn, date(2026, 1, 5), date(2026, 1, 5), extra=values)
    (f,) = forecasts[date(2026, 1, 5)]
    assert f.methods["resort_ratio"] == pytest.approx(2.4)
    assert f.methods["raw_mean"] == pytest.approx(1.68)


def test_backtest_fits_only_through_fit_to(conn: sqlite3.Connection) -> None:
    two_seasons(conn)
    result = ratio.backtest(
        conn,
        MOUNTAINS,
        fit_to=date(2025, 4, 30),
        test_from=date(2025, 11, 1),
        test_to=date(2026, 4, 30),
    )
    assert result.fit_days == 16
    cells = {(c.mountain, c.method): c for c in result.cells}
    assert set(ratio.BACKTEST_METHODS) == {m for _, m in cells}
    temp = cells[("loon", "ratio_temp")]
    assert (temp.n, temp.bias_ratio) == (16, 0.5)  # 10:1 fitted, 20:1 observed
    assert cells[("loon", "raw_mean")].bias_ratio == 0.35
    assert cells[("loon", "flat_10")].bias_ratio == 0.5
    assert cells[(ALL_MOUNTAINS, "ratio_upslope")].n == 16
    assert result.corrected_fallback_days == 0
