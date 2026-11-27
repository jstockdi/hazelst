"""The finer models (J29): lead days, ratio fit, candidates, backtest, live day 1.

Fixtures are synthetic lead-day-1 summit series in `forecast_hourly` (one fetch
for the global models, one for the finer models, as the backfill stores them)
and resort reports in `observation`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hazelst import config, db, fine, jobs, ratio, verify
from hazelst.models import ReportPayload
from hazelst.ratio import ALL_BANDS, BOTH_SIDES, MEAN_MODEL, DayWater, LeadDay
from hazelst.render import page
from hazelst.sources import open_meteo
from tests.drafts import report_config
from tests.pages import recorded_narrative
from tests.support import FIXTURES, load_fixtures, replay_client
from tests.unit import test_derive as td
from tests.unit.test_ratio import add_hours, add_report

CFG = config.load({})
MOUNTAINS = CFG.mountains
DAY0 = date(2026, 1, 5)
HRRR, NBM = "ncep_hrrr_conus", "ncep_nbm_conus"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "h.db")
    db.migrate(c)
    db.sync_mountains(c, MOUNTAINS)
    return c


def test_lead_days_keep_the_mean_and_upslope_on_the_global_models(
    conn: sqlite3.Connection,
) -> None:
    add_hours(conn, DAY0, "loon", snow={"gfs_seamless": 0.07, "gem_seamless": 0.14}, temp=10,
              upslope=True)  # fmt: skip
    # The finer models' fetch: twice as much snow, no 850 hPa tag of their own.
    add_hours(conn, DAY0, "loon", snow={HRRR: 0.28, NBM: 0.21}, temp=10)
    (day,) = ratio.lead_days(conn).values()
    assert day.upslope  # the fine fetch casts no vote
    assert day.models[MEAN_MODEL].snow_in == pytest.approx((1.68 + 3.36) / 2)
    assert set(day.raw_models()) == {"gfs_seamless", "gem_seamless"}
    assert list(day.fine_models()) == [HRRR, NBM]
    assert day.models[HRRR].snow_in == pytest.approx(6.72)


def fine_day(mountain: str, day: date, water: float, *, hrrr: float, nbm_snow: float) -> LeadDay:
    g = DayWater(round(water * 7, 3), {"<=15": water})
    h = DayWater(round(hrrr * 7, 3), {"<=15": hrrr})
    n = DayWater(nbm_snow, {"<=15": round(nbm_snow / 7, 4)})
    return LeadDay(mountain, day, False, {"gfs_seamless": g, MEAN_MODEL: g, HRRR: h, NBM: n})


def test_each_fine_model_gets_its_own_ratio_and_candidates() -> None:
    # Resort snow 6 in: global water 0.3 (20:1), HRRR water 0.5 (12:1), NBM 4 in native.
    pairs = [
        (fine_day("loon", DAY0 + timedelta(days=i), 0.3, hrrr=0.5, nbm_snow=4.0), 6.0)
        for i in range(16)
    ]
    r = ratio.Ratios(ratio.fit(pairs, DAY0))
    assert r.ratio("loon", HRRR, "<=15", None) == 12.0
    assert r.ratio("loon", NBM, "<=15", None) == 10.5  # 6 / (4 / 7)
    assert r.ratio("loon", "gfs_seamless", "<=15", None) == 20.0
    assert ratio.fit_days(r, "loon", HRRR) == 16
    # ECMWF 9 km, HRDPS, AIFS have no fit days: thin, out of the method choice.
    assert ratio.thin_methods(r, "loon") == {"ifs9_ratio", "hrdps_ratio", "aifs_ratio"}
    test = fine_day("loon", DAY0 + timedelta(days=40), 0.1, hrrr=0.2, nbm_snow=1.5)
    values = ratio.fine_values(test, r)
    assert values["hrrr_ratio"] == pytest.approx(2.4)
    assert values["nbm_ratio"] == pytest.approx(1.5 / 7 * 10.5, abs=0.01)
    assert values["fine_ratio"] == pytest.approx((2.4 + 2.25) / 2, abs=0.01)
    assert values["nbm_raw"] == 1.5
    assert "ifs9_ratio" not in values


def test_fine_ratio_uses_every_model_when_all_are_thin() -> None:
    pairs = [(fine_day("loon", DAY0, 0.3, hrrr=0.5, nbm_snow=4.0), 6.0)]
    r = ratio.Ratios(ratio.fit(pairs, DAY0))
    assert ratio.FINE_RATIO in ratio.thin_methods(r, "loon")
    values = ratio.fine_values(fine_day("loon", DAY0, 0.1, hrrr=0.2, nbm_snow=1.5), r)
    assert values["fine_ratio"] == pytest.approx((values["hrrr_ratio"] + values["nbm_ratio"]) / 2)


def two_seasons(conn: sqlite3.Connection) -> None:
    """Loon, 16 snow days a season; HRRR forecasts twice the global snow water."""
    for start, obs in ((date(2025, 1, 5), 2.4), (date(2026, 1, 5), 4.8)):
        for i in range(16):
            d = start + timedelta(days=i)
            add_hours(conn, d, "loon", snow={"gfs_seamless": 0.07}, temp=10)
            add_hours(conn, d, "loon", snow={HRRR: 0.14, NBM: 0.1}, temp=10)
            add_report(conn, "loon", d, obs)
    conn.commit()


def test_backtest_scores_every_fine_candidate(conn: sqlite3.Connection) -> None:
    two_seasons(conn)
    result = ratio.backtest(
        conn, MOUNTAINS, fit_to=date(2025, 4, 30), test_from=date(2025, 11, 1),
        test_to=date(2026, 4, 30),
    )  # fmt: skip
    cells = {(c.mountain, c.method): c for c in result.cells}
    assert {m for _, m in cells} == set(ratio.BACKTEST_METHODS)
    # Fitted at 2.4 in from HRRR's 0.48 in of water (5:1); 2026 reports 4.8 in.
    hrrr = cells[("loon", "hrrr_ratio")]
    assert (hrrr.n, hrrr.bias_ratio) == (16, 0.5)
    assert cells[("loon", "nbm_raw")].bias_ratio == 0.5  # 2.4 in native vs 4.8
    assert cells[("loon", "raw_mean")].bias_ratio == 0.35  # unchanged by the fine models
    assert result.fine_fit_days[("loon", HRRR)] == 16
    assert result.fine_fit_days[("loon", "ecmwf_ifs")] == 0
    assert "hrrr_ratio" not in result.thin["loon"]
    assert {"ifs9_ratio", "hrdps_ratio", "aifs_ratio"} <= set(result.thin["loon"])
    # ECMWF 9 km, HRDPS, AIFS have no data: the raw mean stands in on every test day.
    assert result.fine_fallback_days["ifs9_ratio"] == 16
    assert result.fine_fallback_days["hrrr_ratio"] == 0


def test_backfill_edition_scores_fine_models_and_keeps_raw_mean_global(
    conn: sqlite3.Connection,
) -> None:
    two_seasons(conn)
    values = ratio.backfill_values(conn, MOUNTAINS, date(2026, 1, 5), date(2026, 1, 5))
    forecasts = verify.backfill_forecasts(conn, date(2026, 1, 5), date(2026, 1, 5), extra=values)
    (f,) = forecasts[date(2026, 1, 5)]
    assert f.methods["raw_mean"] == pytest.approx(1.68)  # GFS only, not the finer models
    assert f.methods[HRRR] == pytest.approx(3.36)
    assert f.methods["hrrr_ratio"] == pytest.approx(2.4)  # 2024-25 fit (5:1), season left out
    assert f.methods["nbm_raw"] == pytest.approx(2.4)


# --- live day 1 ------------------------------------------------------------------------------

FINE_DAY = FIXTURES / "open_meteo" / "2026-11-27"


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    monkeypatch.setattr(jobs, "clock", lambda: datetime(2026, 11, 27, 12, 0, tzinfo=UTC))
    c = report_config(monkeypatch, tmp_path / "h.db")
    jobs.run_job(
        c, "ingest-forecast", date(2026, 11, 27), client=replay_client(load_fixtures(FINE_DAY))
    )
    return c


def flat(model: str, value: float) -> list[ratio.RatioRow]:
    return [
        ratio.RatioRow(m, model, b, BOTH_SIDES, value, 30, "2026-11-24")
        for m in (*(x.key for x in MOUNTAINS), ratio.ALL_MOUNTAINS)
        for b in (*ratio.BANDS, ALL_BANDS)
    ]


def test_report_build_stores_fine_candidates_and_applies_the_chosen_day_1(
    built: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = date(2026, 11, 27)
    conn = db.connect(built.settings.db_path)
    rows = [r for m in (MEAN_MODEL, *open_meteo.FINE_MODELS) for r in flat(m, 14.0)]
    with db.transaction(conn):
        ratio.store(conn, rows)
    summary = jobs.run_job(built, "report-build", day, options={"force": True})
    assert len(summary["fine"]["mountains"]) == 8
    run_id = summary["run_id"]
    stored = conn.execute(
        "SELECT mountain, method, snow_in, lo_in, hi_in FROM forecast_window"
        " WHERE run_id = ? AND method NOT IN ('raw_mean', 'resort_ratio')",
        (run_id,),
    ).fetchall()
    methods = {r[1] for r in stored}
    assert methods == set(ratio.FINE_METHODS)
    assert {r[0] for r in stored} == {m.key for m in MOUNTAINS}
    fine_rows = {(r[0], r[1]): r for r in stored}
    k = fine_rows[("killington", "fine_ratio")]
    assert k[3] <= k[2] <= k[4]

    # Pick HRRR x ratio for Killington's day 1.
    real_apply = fine.apply
    monkeypatch.setattr(
        fine, "apply", lambda f, c, chosen: real_apply(f, c, {"killington": "hrrr_ratio"})
    )
    summary = jobs.run_job(built, "report-build", day, options={"force": True})
    payload = ReportPayload.model_validate_json(
        conn.execute("SELECT payload FROM report ORDER BY version DESC LIMIT 1").fetchone()[0]
    )
    by_key = {m.key: m.forecast for m in payload.mountains}
    kf = by_key["killington"]
    assert kf.day1_method == "hrrr_ratio"
    assert kf.days[0].snow_in == fine_rows[("killington", "hrrr_ratio")][2]
    assert kf.windows["1d"].snow_in == kf.days[0].snow_in  # chart and table agree
    assert sum(d.snow_in for d in kf.days[:3]) == pytest.approx(kf.windows["3d"].snow_in, abs=0.2)
    assert sum(d.snow_in for d in kf.days) == pytest.approx(kf.windows["5d"].snow_in, abs=0.3)
    assert by_key["loon"].day1_method is None
    html = page.render_report(payload, recorded_narrative())
    assert "day 1: HRRR ratio" in html


def test_apply_shifts_windows_by_the_day_1_difference() -> None:
    rows = [r for m in open_meteo.MODELS for r in td.hourly(m, snowfall_in=0.05)]
    rows += td.hourly(HRRR, snowfall_in=0.1)[:30]  # HRRR ends after 30 h
    rows += td.hourly(NBM, issued=td.AS_OF - timedelta(hours=19), snowfall_in=0.3)  # stale
    forecast = td.run(rows)
    ratios = ratio.Ratios(r for m in (MEAN_MODEL, HRRR, NBM) for r in flat(m, 14.0))
    computed = fine.compute(forecast, rows, ratios)
    k = computed["killington"]
    assert k.values == {"hrrr_ratio": 4.8, "fine_ratio": 4.8}  # 2.4 in / 7 x 14; NBM stale
    applied = fine.apply(forecast, computed, {"killington": "hrrr_ratio"})
    before, after = forecast.mountain("killington"), applied.mountain("killington")
    assert [d.snow_in for d in after.days] == [4.8, 1.2, 1.2, 1.2, 1.2]
    assert after.windows["1d"].snow_in == 4.8
    assert after.windows["1d"].by_model == {HRRR: 4.8}
    assert after.windows["3d"].snow_in == pytest.approx(before.windows["3d"].snow_in + 3.6)
    assert after.windows["5d"].snow_in == pytest.approx(sum(d.snow_in for d in after.days))
    assert after.windows["5d"].raw_mean_in == before.windows["5d"].snow_in
    assert after.day1_method == "hrrr_ratio"
    # A global method, or a candidate without a value today, leaves the mountain as it was.
    assert fine.apply(forecast, computed, {"killington": "resort_ratio"}) == forecast
    assert fine.apply(forecast, computed, {"killington": "nbm_raw"}) == forecast
