import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hazelst import config, db, derive, jobs, tags
from hazelst.models import Forecast
from hazelst.sources.nws import GridRow
from hazelst.sources.open_meteo import MODELS, HourlyRow
from tests.support import FIXTURES, load_fixtures, replay_client

MOUNTAINS = config.load_mountains(config.DEFAULT_MOUNTAINS)
KILLINGTON = MOUNTAINS[0]
DAY = date(2026, 1, 10)  # EST: 6 AM local = 11:00 UTC
START = datetime(2026, 1, 10, 11, tzinfo=UTC)
AS_OF = START + timedelta(hours=1)


def hourly(
    model: str = "gfs_seamless",
    elevation: str = "summit",
    issued: datetime = START - timedelta(hours=5),
    values: dict[str, float | None] | None = None,
    **kw: float | None,
) -> list[HourlyRow]:
    base: dict[str, float | None] = {
        "snowfall_in": 0.0,
        "precip_in": 0.0,
        "temp_f": 20.0,
        "gust_mph": 10.0,
        "freezing_level_ft": 2000.0,
        "snow_depth_in": 0.0,
        "t850_f": 10.0,
        "rh850": 50.0,
        "ws850_mph": 10.0,
        "wd850": 200.0,
    }
    base.update(values or {})
    base.update(kw)
    return [
        HourlyRow(
            mountain="killington",
            model=model,
            elevation=elevation,
            issued_at=issued.isoformat(),
            valid_at=(START + timedelta(hours=i)).isoformat(),
            lead_h=None,
            **base,
        )
        for i in range(120)
    ]


def three_models(**values: float | None) -> list[HourlyRow]:
    rows: list[HourlyRow] = []
    for m in MODELS:
        rows += hourly(m, "summit", values=values)
        rows += hourly(m, "base", values={**values, "temp_f": values.get("base_temp_f", 25.0)})
    return rows


def run(rows: list[HourlyRow], nws: list[GridRow] | None = None, **kw: object) -> Forecast:
    return derive.derive(
        [KILLINGTON],
        derive.Inputs(rows, nws or [], kw.pop("afd", {})),  # type: ignore[arg-type]
        for_date=DAY,
        as_of=kw.pop("as_of", AS_OF),  # type: ignore[arg-type]
    )


# --- windows and days ---------------------------------------------------------------


def test_anchor_is_6am_local_across_dst() -> None:
    assert derive.anchor_for(date(2026, 1, 10)).astimezone(UTC).hour == 11
    assert derive.anchor_for(date(2026, 7, 10)).astimezone(UTC).hour == 10


def test_daily_buckets_sum_to_windows() -> None:
    rows: list[HourlyRow] = []
    for i, m in enumerate(MODELS):  # 0.05, 0.10, 0.15 in/h
        rows += hourly(m, snowfall_in=0.05 * (i + 1))
        rows += hourly(m, "base", snowfall_in=0.01)
    k = run(rows).mountain("killington")
    assert [d.snow_in for d in k.days] == [2.4] * 5
    assert k.windows["1d"].snow_in == 2.4
    assert k.windows["3d"].snow_in == pytest.approx(sum(d.snow_in for d in k.days[:3]))
    assert k.windows["5d"].snow_in == pytest.approx(sum(d.snow_in for d in k.days))
    assert k.windows["5d"].by_model == {
        "gfs_seamless": 6.0,
        "ecmwf_ifs025": 12.0,
        "gem_seamless": 18.0,
    }
    assert k.windows["5d"].range == (6.0, 18.0)
    assert k.windows["1d"].range == (1.2, 3.6)
    assert k.windows["5d"].base_snow_in == 1.2


def test_daily_buckets_sum_to_windows_on_recorded_day() -> None:
    forecast = recorded_forecast()
    for m in forecast.mountains:
        assert m.windows["1d"].snow_in == m.days[0].snow_in
        assert abs(sum(d.snow_in for d in m.days[:3]) - m.windows["3d"].snow_in) <= 0.15
        assert abs(sum(d.snow_in for d in m.days) - m.windows["5d"].snow_in) <= 0.25


@pytest.mark.parametrize(
    ("lo", "hi", "expected"),
    [(0, 3, "high"), (1, 7, "medium"), (2, 8.5, "low")],
)
def test_confidence(lo: float, hi: float, expected: str) -> None:
    assert derive.confidence(lo, hi) == expected


def test_stale_model_excluded_from_mean_and_range() -> None:
    rows = (
        hourly("gfs_seamless", snowfall_in=0.1)
        + hourly("ecmwf_ifs025", snowfall_in=0.1)
        + hourly("gem_seamless", snowfall_in=0.5, issued=AS_OF - timedelta(hours=19))
    )
    forecast = run(rows)
    assert forecast.used_models == ["gfs_seamless", "ecmwf_ifs025"]
    assert [r.stale for r in forecast.models] == [False, False, True]
    w = forecast.mountain("killington").windows["1d"]
    assert w.snow_in == 2.4
    assert w.range == (2.4, 2.4)
    assert w.by_model["gem_seamless"] == 12.0  # still listed


def test_all_models_stale_keeps_all() -> None:
    old = AS_OF - timedelta(hours=30)
    rows = hourly("gfs_seamless", issued=old) + hourly("ecmwf_ifs025", issued=old)
    forecast = run(rows)
    assert forecast.used_models == ["gfs_seamless", "ecmwf_ifs025"]
    assert all(r.stale for r in forecast.models)


def test_day_fields() -> None:
    rows = three_models(temp_f=18.0, gust_mph=30.0)
    k = run(rows).mountain("killington")
    d = k.days[0]
    assert (d.date, d.label) == (DAY, "Sat 10")
    assert (d.hi_f, d.lo_f, d.gust_mph, d.freezing_level_ft) == (18, 18, 30, 2000)
    assert [x.date for x in k.days] == [DAY + timedelta(days=i) for i in range(5)]


# --- NWS --------------------------------------------------------------------------------


def grid(layer: str, start: datetime, hours: int, mm: float) -> GridRow:
    return GridRow("killington", layer, start.isoformat(), hours, mm, "mm")


def test_prorated_sum_splits_intervals_at_window_edges() -> None:
    rows = [
        grid("snowfallAmount", START - timedelta(hours=3), 6, 25.4),  # half inside
        grid("snowfallAmount", START + timedelta(hours=3), 24, 25.4),  # 21 of 24 h inside
        grid("snowfallAmount", START + timedelta(hours=27), 6, 25.4),  # ends the coverage
    ]
    total, covered = derive.prorated_sum(rows, START, START + timedelta(hours=24))
    assert total == pytest.approx(0.5 + 21 / 24)
    assert covered
    _, covered_3d = derive.prorated_sum(rows, START, START + timedelta(hours=72))
    assert not covered_3d


def test_nws_cross_check_1d_3d_only_and_disagreement() -> None:
    # 11 x 6 h = 66 h of grid: covers 1d, ends before the 3d window does.
    nws = [grid("snowfallAmount", START + timedelta(hours=6 * i), 6, 12.7) for i in range(11)]
    nws += [grid("iceAccumulation", START + timedelta(hours=6 * i), 6, 0.0) for i in range(11)]
    rows = three_models(snowfall_in=0.02)  # 0.48" per day
    k = run(rows, nws).mountain("killington")
    assert k.windows["1d"].nws_in == 2.0  # 4 x 0.5"
    assert k.windows["1d"].nws_disagrees  # 0.48 vs 2.0
    assert k.windows["1d"].nws_ice_in == 0.0
    assert k.windows["3d"].nws_in is None
    assert k.windows["5d"].nws_in is None


def test_nws_disagrees_rule() -> None:
    assert not derive.nws_disagrees(0.4, 0.9)  # both under 1"
    assert derive.nws_disagrees(1.0, 3.0)
    assert not derive.nws_disagrees(4.0, 5.0)
    assert not derive.nws_disagrees(4.0, None)


# --- tags -------------------------------------------------------------------------------


def day(**values: float | None) -> list[tags.Hour]:
    return [tags.Hour(**values) for _ in range(24)]


def tag(hours: list[tags.Hour], nws_ice_in: float | None = None) -> list[str]:
    return tags.day_tags(hours, wind_watch_mph=40, wind_likely_mph=55, nws_ice_in=nws_ice_in)


def test_tag_nw_upslope() -> None:
    upslope = {"wd850": 300.0, "ws850_mph": 25.0, "rh850": 80.0, "t850_f": 10.0}
    hours = [tags.Hour(**upslope)] * 6 + [tags.Hour()] * 18
    assert tag(hours) == ["nw_upslope"]
    assert tag([tags.Hour(**upslope)] * 5 + [tags.Hour()] * 19) == []
    assert tag(day(**{**upslope, "wd850": 200.0})) == []
    assert tag(day(**{**upslope, "t850_f": 20.0})) == []


def test_tag_mixing_base_and_rain_summit() -> None:
    assert tag(day(precip_in=0.02, temp_f=30.0, base_temp_f=35.0, t850_f=20.0)) == ["mixing_base"]
    assert tag(day(precip_in=0.02, temp_f=35.0, base_temp_f=36.0, t850_f=34.0)) == [
        "mixing_base",
        "rain_summit",
    ]
    assert tag(day(precip_in=0.005, temp_f=35.0, base_temp_f=36.0)) == []  # not wet


def test_tag_freezing_rain() -> None:
    assert tag(day(precip_in=0.02, temp_f=30.0, base_temp_f=31.0, t850_f=35.0)) == ["freezing_rain"]
    assert tag(day(precip_in=0.0, temp_f=30.0, t850_f=35.0)) == []
    assert tag(day(temp_f=30.0, t850_f=20.0), nws_ice_in=0.05) == ["freezing_rain"]
    assert tag(day(temp_f=30.0, t850_f=20.0), nws_ice_in=0.0) == []


def test_tag_wind_hold() -> None:
    assert tag(day(gust_mph=39.0)) == []
    assert tag(day(gust_mph=40.0)) == ["wind_hold_watch"]
    assert tag(day(gust_mph=55.0)) == ["wind_hold_likely"]


@pytest.mark.parametrize(
    ("snow", "precip", "day_tags", "expected"),
    [
        (0.1, 0.04, ["mixing_base"], "dry"),
        (2.0, 0.3, ["rain_summit"], "snow"),
        (0.5, 0.4, ["rain_summit", "freezing_rain"], "rain"),
        (0.0, 0.9, ["mixing_base", "freezing_rain"], "ice"),
        (0.9, 0.24, ["mixing_base"], "mix"),
    ],
)
def test_precip_type(snow: float, precip: float, day_tags: list[str], expected: str) -> None:
    assert tags.precip_type(snow, precip, day_tags) == expected


def test_storm_type_from_mountain_office() -> None:
    afd = {"BTV": "A low will travel northward through the \neastern United States."}
    assert run(three_models(), afd=afd).mountain("killington").storm_type == "inland_runner"
    assert run(three_models(), afd={}).mountain("killington").storm_type == "none"
    assert tags.storm_type(["freezing_rain", "coastal", "clipper"]) == "clipper"


# --- recorded days -----------------------------------------------------------------------


def recorded_forecast() -> Forecast:
    from hazelst.sources import open_meteo

    recorded = load_fixtures(FIXTURES / "open_meteo" / "2026-11-26")
    runs = open_meteo.run_times(f for f in recorded if f.source == open_meteo.META_SOURCE)
    rows = [
        r for f in recorded if f.source == open_meteo.SOURCE for r in open_meteo.normalize(f, runs)
    ]
    return derive.derive(
        MOUNTAINS,
        derive.Inputs(rows, [], {}),
        for_date=date(2026, 11, 26),
        as_of=datetime(2026, 11, 26, 14, 30, tzinfo=UTC),
    )


SAMPLE = config.PROJECT_ROOT / "prototype" / "out" / "2026-11-25.json"


@pytest.fixture(scope="module")
def sample_day() -> tuple[Forecast, dict[str, dict]]:  # type: ignore[type-arg]
    """The 2026-11-25 sample run rebuilt from the same model runs (Single Runs API).

    The prototype fetched at 10:44 AM EST from the live API: GFS 06Z blended
    with the latest HRRR, ECMWF 06Z, GEM 00Z. The fixtures hold GFS 06Z
    (gfs_seamless with its own 06Z HRRR blend), ECMWF 06Z, and GEM 00Z, so
    ECMWF matches exactly and GFS differs in the first two days. NWS grids
    from that morning were not saved, so NWS values are not compared.
    """
    from hazelst.sources import open_meteo

    rows = [
        r
        for f in load_fixtures(FIXTURES / "open_meteo" / "2026-11-25")
        for r in open_meteo.normalize(f, {})
    ]
    forecast = derive.derive(
        MOUNTAINS,
        derive.Inputs(rows, [], {}),
        for_date=date(2026, 11, 25),
        as_of=datetime(2026, 11, 25, 15, 44, tzinfo=UTC),
    )
    sample = {m["key"]: m for m in json.loads(SAMPLE.read_text())["mountains"]}
    return forecast, sample


def test_sample_run_ecmwf_windows_match_exactly(sample_day: tuple[Forecast, dict]) -> None:  # type: ignore[type-arg]
    forecast, sample = sample_day
    for key, m in sample.items():
        for w in ("1d", "3d", "5d"):
            expected = m["forecast"]["windows"][w]["by_model"]["ecmwf_ifs025"]
            assert forecast.mountain(key).windows[w].by_model["ecmwf_ifs025"] == expected


def test_sample_run_precip_types_and_tags(sample_day: tuple[Forecast, dict]) -> None:  # type: ignore[type-arg]
    forecast, sample = sample_day
    mismatches = []
    for key, m in sample.items():
        for old, new in zip(m["forecast"]["days"], forecast.mountain(key).days, strict=True):
            assert old["label"] == new.label
            if (old["precip_type"], old["tags"]) != (new.precip_type, new.tags):
                mismatches.append((key, new.label, old["precip_type"], new.precip_type))
    # One of 40 mountain-days differs: Sugarbush Fri 27 is rain in the sample and
    # ice here: no hour is both wet and above 33 F at the summit in these inputs
    # (no rain_summit tag); the sample's GFS series was blended with a later HRRR.
    assert mismatches == [("sugarbush", "Fri 27", "rain", "ice")]


def test_sample_run_1d_windows_and_temperatures(sample_day: tuple[Forecast, dict]) -> None:  # type: ignore[type-arg]
    forecast, sample = sample_day
    for key, m in sample.items():
        mine = forecast.mountain(key)
        assert mine.windows["1d"].snow_in == m["forecast"]["windows"]["1d"]["snow_in"] == 0.0
        for old, new in zip(m["forecast"]["days"], mine.days, strict=True):
            assert new.hi_f is not None and new.lo_f is not None
            assert abs(old["hi_f"] - new.hi_f) <= 2
            assert abs(old["lo_f"] - new.lo_f) <= 2


# --- database ---------------------------------------------------------------------------


def test_load_derive_store_from_ingest(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    recorded = [
        *load_fixtures(FIXTURES / "open_meteo" / "2026-11-26"),
        *load_fixtures(FIXTURES / "nws" / "2026-11-26"),
        *load_fixtures(FIXTURES / "discussions" / "2026-11-26"),
    ]
    summary = jobs.run_job(
        cfg, "ingest-forecast", date(2026, 11, 26), client=replay_client(recorded)
    )
    conn = db.connect(cfg.settings.db_path)
    as_of = datetime(2026, 11, 26, 14, 30, tzinfo=UTC)
    run_id = derive.latest_ingest_run(conn)
    assert run_id is not None and run_id == summary["run_id"]
    inputs = derive.load_inputs(conn, run_id, as_of)
    assert len(inputs.hourly) == 16128
    assert set(inputs.discussions) == {"BTV", "ALY", "GYX"}
    forecast = derive.derive(cfg.mountains, inputs, for_date=date(2026, 11, 26), as_of=as_of)
    assert forecast.used_models == list(MODELS)
    for m in forecast.mountains:
        assert m.windows["1d"].nws_in is not None
        assert m.windows["5d"].nws_in is None
    assert forecast.mountain("killington").storm_type == "none"
    derive.store(conn, run_id, forecast)
    assert conn.execute("SELECT count(*) FROM forecast_window").fetchone()[0] == 8 * 3
    row = conn.execute(
        "SELECT precip_type, tags FROM forecast_day WHERE mountain = 'stratton' AND day = ?",
        ("2026-11-26",),
    ).fetchone()
    assert row[0] in {"snow", "ice", "rain", "mix", "dry"}
    assert isinstance(json.loads(row[1]), list)
