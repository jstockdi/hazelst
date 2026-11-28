from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hazelst import config, db, jobs
from hazelst.sources import base, open_meteo
from tests.support import FIXTURES, load_fixtures, replay_client

DAY = FIXTURES / "open_meteo" / "2026-11-26"


@pytest.fixture(scope="module")
def recorded() -> list[base.Fetched]:
    return load_fixtures(DAY)


def by_label(recorded: list[base.Fetched], label: str) -> base.Fetched:
    return next(f for f in recorded if open_meteo.label(f) == label)


@pytest.fixture(scope="module")
def runs(recorded: list[base.Fetched]) -> dict[str, open_meteo.RunTime]:
    return open_meteo.run_times(f for f in recorded if f.source == open_meteo.META_SOURCE)


def test_fixture_params_match_current_request(recorded: list[base.Fetched]) -> None:
    mountains = config.load_mountains(config.DEFAULT_MOUNTAINS)
    summit = by_label(recorded, "forecast-summit")
    assert summit.params == open_meteo.forecast_params(mountains, "summit")
    assert summit.params["elevation"].split(",")[0] == "1293"  # Killington 4241 ft
    assert summit.meta["mountains"] == [m.key for m in mountains]


def test_run_times_from_meta(runs: dict[str, open_meteo.RunTime]) -> None:
    assert set(runs) == {"gfs_global", "ecmwf_ifs025", "gem_global", "hrrr"}
    assert runs["gfs_global"].initialised_at == datetime(2026, 11, 26, 6, tzinfo=UTC)
    assert not runs["gfs_global"].estimated
    assert runs["ecmwf_ifs025"].initialised_at == datetime(2026, 11, 26, 6, tzinfo=UTC)
    # cmc_gem_gdps meta.json stopped updating in May 2026: estimated from the GEM cycle.
    assert runs["gem_global"].estimated
    assert runs["gem_global"].initialised_at == datetime(2026, 11, 26, 0, tzinfo=UTC)


def test_effective_run_time_keeps_fresh_meta() -> None:
    run = open_meteo.RunTime(
        meta_id="x",
        initialised_at=datetime(2026, 11, 26, 6, tzinfo=UTC),
        available_at=datetime(2026, 11, 26, 9, tzinfo=UTC),
        data_end=datetime(2026, 12, 10, tzinfo=UTC),
        update_interval_s=21600,
    )
    assert open_meteo.effective_run_time(run, datetime(2026, 11, 26, 14, tzinfo=UTC)) == run


def test_effective_run_time_estimates_from_cycle() -> None:
    stale = open_meteo.RunTime(
        meta_id="x",
        initialised_at=datetime(2026, 5, 26, 0, tzinfo=UTC),
        available_at=datetime(2026, 5, 26, 5, tzinfo=UTC),
        data_end=datetime(2026, 6, 5, tzinfo=UTC),
        update_interval_s=43200,
    )
    # 16:59 - 5 h delay = 11:59 -> 00Z run; 17:00 -> 12Z run.
    early = open_meteo.effective_run_time(stale, datetime(2026, 11, 26, 16, 59, tzinfo=UTC))
    late = open_meteo.effective_run_time(stale, datetime(2026, 11, 26, 17, 0, tzinfo=UTC))
    assert early.initialised_at == datetime(2026, 11, 26, 0, tzinfo=UTC)
    assert late.initialised_at == datetime(2026, 11, 26, 12, tzinfo=UTC)
    assert early.estimated and late.estimated


def test_normalize_summit(
    recorded: list[base.Fetched], runs: dict[str, open_meteo.RunTime]
) -> None:
    rows = open_meteo.normalize(by_label(recorded, "forecast-summit"), runs)
    # 8 mountains x 3 models x 14 days of hours (7 past + 7 forecast)
    assert len(rows) == 8 * 3 * 336
    assert {r.elevation for r in rows} == {"summit"}
    assert {r.model for r in rows} == set(open_meteo.MODELS)
    first = rows[0]
    assert first.mountain == "killington"
    assert first.model == "gfs_global"
    assert first.valid_at == "2026-11-19T00:00:00+00:00"
    assert first.issued_at == "2026-11-26T06:00:00+00:00"
    assert first.lead_h == -174
    # Freezing level comes from GFS only (forecast-method.md).
    assert all(r.freezing_level_ft is not None for r in rows if r.model == "gfs_global")
    assert all(r.freezing_level_ft is None for r in rows if r.model != "gfs_global")
    # Units: snow depth arrives in feet and is stored in inches; freezing level in feet.
    gfs = [r for r in rows if r.model == "gfs_global"]
    assert max(r.freezing_level_ft or 0 for r in gfs) > 3000
    assert all(r.temp_f is not None and -40 < r.temp_f < 80 for r in rows)
    assert all(r.snowfall_in is None or 0 <= r.snowfall_in < 6 for r in rows)


def test_normalize_converts_snow_depth_feet_to_inches(
    recorded: list[base.Fetched], runs: dict[str, open_meteo.RunTime]
) -> None:
    summit = by_label(recorded, "forecast-summit")
    raw = summit.json()[0]
    assert raw["hourly_units"]["snow_depth_gfs_global"] == "ft"
    feet = raw["hourly"]["snow_depth_gfs_global"]
    rows = [
        r
        for r in open_meteo.normalize(summit, runs)
        if r.mountain == "killington" and r.model == "gfs_global"
    ]
    assert [r.snow_depth_in for r in rows] == [round(v * 12, 3) for v in feet]


def test_normalize_rejects_mountain_count_mismatch(
    recorded: list[base.Fetched], runs: dict[str, open_meteo.RunTime]
) -> None:
    summit = by_label(recorded, "forecast-summit")
    broken = base.Fetched(
        **{**summit.__dict__, "meta": {"elevation": "summit", "mountains": ["x"]}}
    )
    with pytest.raises(open_meteo.NormalizeError):
        open_meteo.normalize(broken, runs)


def test_store_fetch_dedupes_bodies(tmp_path: Path, recorded: list[base.Fetched]) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    meta = by_label(recorded, "meta-hrrr")
    first = base.store_fetch(conn, None, meta)
    second = base.store_fetch(conn, None, meta)
    bodies = conn.execute("SELECT id, body IS NOT NULL FROM fetch ORDER BY id").fetchall()
    assert [tuple(r) for r in bodies] == [(first, 1), (second, 0)]
    assert base.load_fetch(conn, second).body == meta.body
    assert base.load_fetch(conn, second).meta == meta.meta


def test_fixture_round_trip(tmp_path: Path, recorded: list[base.Fetched]) -> None:
    original = by_label(recorded, "meta-gfs_global")
    base.write_fixture(tmp_path / "x.json.gz", original)
    assert base.read_fixture(tmp_path / "x.json.gz") == original


def test_ingest_forecast_fills_forecast_hourly(
    tmp_path: Path, recorded: list[base.Fetched]
) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    summary = jobs.run_job(
        cfg, "ingest-forecast", date(2026, 11, 26), client=replay_client(recorded)
    )
    assert summary["open_meteo"]["status"] == "ok"
    # 336 hours requested (7 past days, 7 forecast days); stored from 6 AM EST (11Z) on
    # the ingest date: 13 hours of 26 Nov and 6 forecast days (decisions row 31).
    assert summary["open_meteo"]["forecast_hourly"] == 2 * 8 * 3 * 157
    conn = db.connect(cfg.settings.db_path)
    run = conn.execute("SELECT kind, status FROM run").fetchone()
    assert tuple(run) == ("ingest-forecast", "ok")
    counts = conn.execute(
        "SELECT elevation, count(*) FROM forecast_hourly GROUP BY elevation ORDER BY elevation"
    ).fetchall()
    assert [tuple(r) for r in counts] == [("base", 3768), ("summit", 3768)]
    sources = conn.execute(
        "SELECT source, count(*) FROM fetch WHERE source LIKE 'open_meteo%' GROUP BY source"
    ).fetchall()
    # The finer models' call and their meta.json files 404 in these fixtures (recorded
    # before J29): stored, reported, and not fatal.
    assert dict((r[0], r[1]) for r in sources) == {"open_meteo": 4, "open_meteo_meta": 8}
    assert summary["open_meteo"]["fine_forecast_hourly"] == 0
    assert summary["open_meteo"]["fine_errors"] == ["summit: HTTP 404", "base: HTTP 404"]


FINE_DAY = FIXTURES / "open_meteo" / "2026-11-27"


def test_fine_models_are_one_call_per_elevation() -> None:
    mountains = config.load_mountains(config.DEFAULT_MOUNTAINS)
    recorded = load_fixtures(FINE_DAY)
    summit = by_label(recorded, "forecast-summit-fine")
    assert summit.params == open_meteo.fine_params(mountains, "summit")
    assert summit.params["models"].split(",") == list(open_meteo.FINE_MODELS)
    assert open_meteo.is_fine_fetch(summit)
    assert not open_meteo.is_fine_fetch(by_label(recorded, "forecast-summit"))
    runs = open_meteo.run_times(f for f in recorded if f.source == open_meteo.META_SOURCE)
    assert set(runs) == set(open_meteo.MODELS) | set(open_meteo.FINE_MODELS)
    rows = open_meteo.normalize(summit, runs)
    assert {r.model for r in rows} == set(open_meteo.FINE_MODELS)
    hrrr = [r for r in rows if r.model == "ncep_hrrr_conus" and r.mountain == "killington"]
    assert hrrr[0].issued_at == "2026-11-27T10:00:00+00:00"  # hourly cycle
    # HRRR and HRDPS end within two days of the run; the others cover the call's 4 days.
    assert max(r.lead_h or 0 for r in hrrr) < 48
    assert all(r.snowfall_in is not None and r.temp_f is not None for r in rows)
    assert all(r.t850_f is None and r.gust_mph is None for r in rows)  # not requested


def test_ingest_forecast_stores_the_fine_models(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    summary = jobs.run_job(cfg, "ingest-forecast", client=replay_client(load_fixtures(FINE_DAY)))
    assert summary["open_meteo"]["status"] == "ok"
    assert summary["open_meteo"]["fine_forecast_hourly"] > 0
    assert "fine_errors" not in summary["open_meteo"]
    conn = db.connect(cfg.settings.db_path)
    models = {
        r[0]
        for r in conn.execute("SELECT DISTINCT model FROM forecast_hourly WHERE elevation = 'base'")
    }
    assert models == set(open_meteo.MODELS) | set(open_meteo.FINE_MODELS)


def test_ingest_forecast_fails_when_open_meteo_is_down(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    with pytest.raises(jobs.JobError):
        jobs.run_job(cfg, "ingest-forecast", client=replay_client([]))
    conn = db.connect(cfg.settings.db_path)
    assert conn.execute("SELECT status FROM run").fetchone()[0] == "failed"
