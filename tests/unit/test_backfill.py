"""season-backfill on the J00 fixture snow day (report day Fri 6 Feb 2026), with HTTP mocked.

Previous Runs responses are synthetic (the J00 fixtures are single-location
and local-time); LSR, PNS, and NOHRSC replay the recorded J00 fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from hazelst import backfill, config, db, jobs
from hazelst.sources import (
    base,
    elevation,
    ghcn,
    historical_forecast,
    iem,
    nohrsc,
    open_meteo,
    pns,
    previous_runs,
    snocountry,
)
from tests.support import FIXTURES
from tests.unit.test_obs_sources import LSR_FEB, NOHRSC_24H, PNS_IDS, body
from tests.unit.test_snocountry import SNOCOUNTRY_BODIES

GHCN_FEB = FIXTURES / "ghcn" / "2026-02-06.json.gz"


def ghcn_body(query: dict[str, str]) -> list[dict[str, Any]]:
    """The recorded GHCN response, limited to the requested stations and dates."""
    wanted = set(query["stations"].split(","))
    return [
        r
        for r in base.read_fixture(GHCN_FEB).json()
        if r["STATION"] in wanted and query["startDate"] <= r["DATE"] <= query["endDate"]
    ]


DAY = date(2026, 2, 6)
SNOW_PER_HOUR = 0.1
FINE_SNOW_PER_HOUR = 0.2


def previous_runs_body(query: dict[str, str]) -> list[dict[str, Any]]:
    """Every mountain, the requested models (and an all-null ECMWF), for the requested dates.

    Global models carry 0.1 x lead in/h, finer models `FINE_SNOW_PER_HOUR`.
    """
    lead = int(query["hourly"].split(",")[0].rsplit("previous_day", 1)[1])
    pressure = "temperature_850hPa" in query["hourly"].split(",")
    start = datetime.fromisoformat(query["start_date"]).replace(tzinfo=UTC)
    days = (date.fromisoformat(query["end_date"]) - start.date()).days + 1
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24 * days)]
    out = []
    for _ in query["latitude"].split(","):
        hourly: dict[str, list[Any]] = {"time": times}
        units: dict[str, str] = {"time": "iso8601"}
        for model in (*query["models"].split(","), "ecmwf_ifs025"):
            snow = FINE_SNOW_PER_HOUR if open_meteo.is_fine(model) else SNOW_PER_HOUR * lead
            for var, unit, value in (
                ("snowfall", "inch", snow),
                ("precipitation", "inch", 0.01),
                ("temperature_2m", "°F", 20.0),
            ):
                name = f"{var}_previous_day{lead}_{model}"
                missing = model == "ecmwf_ifs025" and var == "snowfall"
                hourly[name] = [None if missing else value for _ in times]
                units[name] = unit
            if pressure:  # unsuffixed 850 hPa variables (the API has no _previous_dayN form)
                for var, unit, value in (
                    ("temperature_850hPa", "°F", 10.0),
                    ("relative_humidity_850hPa", "%", 90.0),
                    ("wind_speed_850hPa", "mp/h", 35.0),
                    ("wind_direction_850hPa", "°", 300.0),
                ):
                    hourly[f"{var}_{model}"] = [value for _ in times]
                    units[f"{var}_{model}"] = unit
        out.append({"utc_offset_seconds": 0, "hourly": hourly, "hourly_units": units})
    return out


def historical_forecast_body(query: dict[str, str]) -> list[dict[str, Any]]:
    """Every mountain, one finer model (unsuffixed variables), for the requested dates."""
    start = datetime.fromisoformat(query["start_date"]).replace(tzinfo=UTC)
    days = (date.fromisoformat(query["end_date"]) - start.date()).days + 1
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24 * days)]
    out = []
    for _ in query["latitude"].split(","):
        hourly: dict[str, list[Any]] = {"time": times}
        units: dict[str, str] = {"time": "iso8601"}
        for var, unit, value in (
            ("snowfall", "inch", FINE_SNOW_PER_HOUR),
            ("precipitation", "inch", 0.03),
            ("temperature_2m", "°F", 20.0),
        ):
            hourly[var] = [value for _ in times]
            units[var] = unit
        out.append({"utc_offset_seconds": 0, "hourly": hourly, "hourly_units": units})
    return out


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backfill's paced waits instead of sleeping."""
    waits: list[float] = []
    monkeypatch.setattr(backfill, "sleep", waits.append)
    monkeypatch.setattr(backfill, "_last_paced", None)
    return waits


class FakeWeb:
    """Serves the backfill's requests and counts them."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        query = dict(request.url.params.multi_items())
        self.requests.append(url)
        assert request.headers["User-Agent"].startswith("hazelst-forecast")
        if url == previous_runs.URL:
            return httpx.Response(200, json=previous_runs_body(query))
        if url == historical_forecast.URL:
            return httpx.Response(200, json=historical_forecast_body(query))
        if url == iem.LSR_URL:
            return httpx.Response(200, content=body(LSR_FEB))
        if url == pns.ARCHIVE_LIST_URL:
            office = query["pil"][3:]
            listed = [PNS_IDS[office]] if query["date"] == "2026-02-07" else []
            return httpx.Response(200, json={"data": [{"product_id": p} for p in listed]})
        for pid in PNS_IDS.values():
            if url == pns.ARCHIVE_TEXT_URL.format(product_id=pid):
                path = FIXTURES / "iem" / f"nwstext_{pid}.txt.gz"
                return httpx.Response(200, content=body(path))
        if url == snocountry.URL:
            return httpx.Response(200, content=SNOCOUNTRY_BODIES[query["resortId"]])
        if url == nohrsc.grid_url("24h", date(2026, 2, 7)):
            return httpx.Response(200, content=body(NOHRSC_24H))
        if url == ghcn.URL:
            return httpx.Response(200, json=ghcn_body(query))
        if url == elevation.URL:
            points = query["latitude"].split(",")
            return httpx.Response(200, json={"elevation": [300.0] * len(points)})
        return httpx.Response(404, text="not found")


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    return config.load({"HAZELST_DB": str(tmp_path / "h.db")})


def run_backfill(cfg: config.Config, web: FakeWeb) -> dict[str, Any]:
    headers = base.make_client().headers  # the identifying User-Agent
    client = httpx.Client(headers=headers, transport=httpx.MockTransport(web))
    return jobs.run_job(
        cfg, "season-backfill", DAY, client=client, options={"start": DAY, "end": DAY}
    )


def counts(cfg: config.Config) -> dict[str, int]:
    conn = db.connect(cfg.settings.db_path)
    tables = ("fetch", "forecast_hourly", "snow_report", "discussion", "observation")
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def test_chunks_follow_calendar_months() -> None:
    assert list(backfill.chunks(date(2025, 11, 15), date(2026, 1, 10))) == [
        (date(2025, 11, 15), date(2025, 11, 30)),
        (date(2025, 12, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 10)),
    ]


def test_previous_runs_normalize_lead_and_models() -> None:
    mountains = config.load({}).mountains[:2]
    query = {
        k: str(v)
        for k, v in previous_runs.params(mountains, 3, DAY, DAY + timedelta(days=1)).items()
    }
    fetched = base.Fetched(
        previous_runs.SOURCE,
        previous_runs.URL,
        query,
        200,
        "2026-11-26T00:00:00+00:00",
        0,
        json.dumps(previous_runs_body(query)).encode(),
        {"mountains": [m.key for m in mountains], "elevation": "summit", "lead_day": 3},
    )
    rows = previous_runs.normalize(fetched)
    assert {r.model for r in rows} == {"gfs_global", "gem_global"}  # no ECMWF snowfall
    assert len(rows) == 2 * 2 * 48
    first = rows[0]
    assert (first.elevation, first.issued_at, first.lead_h) == ("summit", None, 48)
    assert (first.snowfall_in, first.precip_in, first.temp_f) == (0.3, 0.01, 20.0)
    assert first.valid_at == "2026-02-06T00:00:00+00:00"


def _fetched(
    lead: int,
    mountains: Sequence[config.Mountain],
    models: Sequence[str] = previous_runs.MODELS,
) -> base.Fetched:
    query = {
        k: str(v)
        for k, v in previous_runs.params(
            mountains, lead, DAY, DAY + timedelta(days=1), models
        ).items()
    }
    return base.Fetched(
        previous_runs.SOURCE,
        previous_runs.URL,
        query,
        200,
        "2026-11-26T00:00:00+00:00",
        0,
        json.dumps(previous_runs_body(query)).encode(),
        {
            "mountains": [m.key for m in mountains],
            "elevation": "summit",
            "lead_day": lead,
            "models": list(models),
        },
    )


def test_previous_runs_requests_global_and_finer_models() -> None:
    assert previous_runs.requests() == [
        (("gfs_global", "gem_global"), 1),
        (("gfs_global", "gem_global"), 2),
        (("gfs_global", "gem_global"), 3),
        (("gfs_global", "gem_global"), 4),
        (("gfs_global", "gem_global"), 5),
        (("ncep_hrrr_conus", "gem_hrdps_continental", "ncep_nbm_conus"), 1),
    ]
    mountains = config.load({}).mountains[:2]
    fine = previous_runs.params(mountains, 1, DAY, DAY, previous_runs.FINE_MODELS)
    assert fine["models"] == "ncep_hrrr_conus,gem_hrdps_continental,ncep_nbm_conus"
    assert "850hPa" not in fine["hourly"] and "wind_speed_unit" not in fine
    rows = previous_runs.normalize(_fetched(1, mountains, previous_runs.FINE_MODELS))
    assert {r.model for r in rows} == set(previous_runs.FINE_MODELS)
    assert {(r.lead_h, r.snowfall_in, r.t850_f) for r in rows} == {(0, 0.2, None)}


def test_previous_runs_lead_day_1_adds_the_850_hpa_variables() -> None:
    mountains = config.load({}).mountains[:2]
    one = previous_runs.params(mountains, 1, DAY, DAY)
    assert "temperature_850hPa" in one["hourly"].split(",")
    assert one["wind_speed_unit"] == "mph"
    assert "850hPa" not in previous_runs.params(mountains, 2, DAY, DAY)["hourly"]
    rows = previous_runs.normalize(_fetched(1, mountains))
    first = rows[0]
    assert (first.t850_f, first.rh850, first.ws850_mph, first.wd850) == (10.0, 90.0, 35.0, 300.0)
    assert previous_runs.normalize(_fetched(2, mountains))[0].t850_f is None


def _store(
    conn: Any, run_id: int, fetched: base.Fetched, mountains: Sequence[config.Mountain]
) -> tuple[int, int]:
    rows = previous_runs.normalize(fetched)
    with db.transaction(conn):
        fetch_id = base.store_fetch(conn, run_id, fetched)
        dropped = previous_runs.drop_superseded(
            conn,
            fetch_id,
            models=str(fetched.params["models"]).split(","),
            lead_day=int(fetched.meta["lead_day"]),
            mountains=[m.key for m in mountains],
            start=DAY,
            end=DAY + timedelta(days=1),
        )
        open_meteo.store(conn, fetch_id, rows)
    return fetch_id, dropped


def test_previous_runs_replace_rows_of_an_earlier_fetch(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "h.db")
    db.migrate(conn)
    db.sync_mountains(conn, config.load({}).mountains)
    mountains = config.load({}).mountains[:2]
    run_id = db.start_run(conn, "season-backfill", None)
    ids = [
        _store(conn, run_id, _fetched(lead, mountains, models), mountains)
        for lead, models in (
            (1, previous_runs.MODELS),
            (1, previous_runs.MODELS),
            (2, previous_runs.MODELS),
            (1, previous_runs.FINE_MODELS),
        )
    ]
    # The second lead-1 fetch replaces the first; lead 2 and the finer models leave it alone.
    assert [d for _, d in ids] == [0, 2 * 2 * 48, 0, 0]
    per_fetch = dict(
        conn.execute("SELECT fetch_id, count(*) FROM forecast_hourly GROUP BY 1").fetchall()
    )
    assert per_fetch == {ids[1][0]: 2 * 2 * 48, ids[2][0]: 2 * 2 * 48, ids[3][0]: 3 * 2 * 48}


def test_previous_runs_replace_a_finer_models_historical_forecast(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "h.db")
    db.migrate(conn)
    db.sync_mountains(conn, config.load({}).mountains)
    mountains = config.load({}).mountains[:2]
    run_id = db.start_run(conn, "season-backfill", None)
    stitched = []
    for model in ("ncep_hrrr_conus", "ecmwf_ifs"):
        # Historical Forecast rows as J29 stored them (any model then), at nominal lead 0.
        query = {"models": model, "start_date": DAY.isoformat(), "end_date": DAY.isoformat()}
        fetched = base.Fetched(
            historical_forecast.SOURCE,
            historical_forecast.URL,
            query,
            200,
            "2026-11-26T00:00:00+00:00",
            0,
            json.dumps(historical_forecast_body({**query, "latitude": "1,2"})).encode(),
            {"mountains": [m.key for m in mountains], "elevation": "summit", "model": model},
        )
        with db.transaction(conn):
            fetch_id = base.store_fetch(conn, run_id, fetched)
            open_meteo.store(conn, fetch_id, historical_forecast.normalize(fetched))
        stitched.append(fetch_id)
    _, dropped = _store(conn, run_id, _fetched(1, mountains, previous_runs.FINE_MODELS), mountains)
    assert dropped == 2 * 24  # HRRR's stitched day at both mountains
    left = dict(
        conn.execute(
            "SELECT model, count(*) FROM forecast_hourly WHERE fetch_id IN (?, ?) GROUP BY 1",
            stitched,
        ).fetchall()
    )
    assert left == {"ecmwf_ifs": 2 * 24}  # no lead-day-1 archive: stays on Historical Forecast


def test_backfill_loads_every_source_and_is_idempotent(cfg: config.Config) -> None:
    web = FakeWeb()
    first = run_backfill(cfg, web)
    # GFS and GEM global at lead days 1-5, then HRRR, HRDPS, and NBM at lead day 1.
    assert first["previous_runs"]["rows"] == {"forecast_hourly": (5 * 2 + 3) * 8 * 48}
    fine = first["historical_forecast"]
    assert fine["requests"] == 2  # ECMWF IFS 9 km and AIFS
    assert fine["rows"]["forecast_hourly"] == 2 * 8 * 48
    assert fine["rows"]["forecast_hourly:ecmwf_ifs"] == 8 * 48
    assert first["lsr"]["rows"]["snow_report"] > 0
    assert first["pns"]["rows"]["discussion"] == 3
    assert first["nohrsc"]["rows"] == {"observation": 8}
    assert first["nohrsc"]["requests"] == 1
    assert first["snocountry"]["requests"] == 8
    assert first["snocountry"]["rows"]["observation"] > 8 * 1000  # whole histories
    assert first["ghcn"]["requests"] == 3  # 132 stations in batches of 50
    assert first["ghcn"]["rows"] == {"snow_report": 10}  # 5 stations x Feb 6-7
    assert first["lsr"]["rows"]["elev_ft"] == first["lsr"]["rows"]["snow_report"]
    stored = counts(cfg)
    sent = len(web.requests)

    again = FakeWeb()
    second = run_backfill(cfg, again)
    assert again.requests == []  # every response is already stored
    assert all(
        second[s]["requests"] == 0 and second[s]["rows"] in ({}, {"elev_ft": 0})
        for s in backfill.STEPS
    )
    assert counts(cfg) == stored
    conn = db.connect(cfg.settings.db_path)
    assert conn.execute("SELECT count(*) FROM snow_report WHERE elev_ft IS NULL").fetchone()[0] == 0
    # Leads, finer models, LSR, listings for 2 days, texts, NOHRSC, one SnoCountry document
    # per mountain, GHCN batches; elevation: 2 calls for LSR, 1 for PNS.
    assert sent == 6 + 2 + 1 + 2 * 3 + 3 + 1 + 8 + 3 + 3


def test_failed_requests_are_retried_on_the_next_run(cfg: config.Config) -> None:
    class Flaky(FakeWeb):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if "nwstext" in str(request.url) or str(request.url).startswith(iem.LSR_URL):
                self.requests.append(str(request.url))
                return httpx.Response(404)
            return super().__call__(request)

    first = run_backfill(cfg, Flaky())
    assert first["pns"]["error_count"] == 3 and first["lsr"]["error_count"] == 1
    web = FakeWeb()
    second = run_backfill(cfg, web)
    assert second["pns"]["rows"]["discussion"] == 3
    assert second["lsr"]["rows"]["snow_report"] > 0
    assert second["previous_runs"]["requests"] == 0 and second["nohrsc"]["requests"] == 0


def test_backfill_seeds_skill_per_mountain_and_model(cfg: config.Config) -> None:
    run_backfill(cfg, FakeWeb())
    summary = jobs.run_job(cfg, "skill-learn", date(2026, 11, 26))
    conn = db.connect(cfg.settings.db_path)
    cells = conn.execute(
        "SELECT mountain, model, lead_bucket, n FROM skill WHERE tag = '' ORDER BY 1, 2, 3"
    ).fetchall()
    keys = {m.key for m in cfg.mountains}
    assert {(r[0], r[1]) for r in cells} == {
        (m, model) for m in keys for model in ("gfs_global", "gem_global", *open_meteo.FINE_MODELS)
    }
    assert {r[2] for r in cells if r[1] in ("gfs_global", "gem_global")} == {
        "1d",
        "2-3d",
        "4-5d",
    }
    # The finer models are backfilled for lead day 1 only.
    assert {r[2] for r in cells if open_meteo.is_fine(r[1])} == {"1d"}
    # Lead day N carries 0.1 x N in/h: day 1 forecasts 2.4 in, verified against the
    # resort-reported 6 in from SnoCountry (ahead of NOHRSC's 3.17 in).
    killington = conn.execute(
        "SELECT bias_ratio FROM skill WHERE mountain = 'killington' AND model = 'gfs_global'"
        " AND lead_bucket = '1d' AND tag = ''"
    ).fetchone()[0]
    assert killington == pytest.approx(2.4 / 6.0, abs=0.01)
    assert summary["skill"] == len(cells)


def test_season_backfill_needs_a_range(cfg: config.Config) -> None:
    with pytest.raises(jobs.JobError, match="start and end"):
        jobs.run_job(cfg, "season-backfill", DAY)


def test_historical_forecast_window_starts_at_each_model_archive() -> None:
    assert historical_forecast.MODELS == ("ecmwf_ifs", "ecmwf_aifs025_single")
    first = date(2025, 2, 1)
    assert historical_forecast.window("ecmwf_aifs025_single", first, date(2025, 2, 28)) == (
        date(2025, 2, 20),
        date(2025, 2, 28),
    )
    assert historical_forecast.window("ecmwf_aifs025_single", first, date(2025, 2, 10)) is None
    assert historical_forecast.window("ecmwf_ifs", first, first) == (first, first)


def test_historical_forecast_rows_stand_in_for_lead_day_1() -> None:
    mountains = config.load({}).mountains[:2]
    query = {
        k: str(v)
        for k, v in historical_forecast.params(
            mountains, "ecmwf_ifs", DAY, DAY + timedelta(days=1)
        ).items()
    }
    fetched = base.Fetched(
        historical_forecast.SOURCE,
        historical_forecast.URL,
        query,
        200,
        "2026-11-27T00:00:00+00:00",
        0,
        json.dumps(historical_forecast_body(query)).encode(),
        {
            "mountains": [m.key for m in mountains],
            "elevation": "summit",
            "model": "ecmwf_ifs",
        },
    )
    rows = historical_forecast.normalize(fetched)
    assert len(rows) == 2 * 48
    first = rows[0]
    assert (first.model, first.elevation, first.issued_at, first.lead_h) == (
        "ecmwf_ifs",
        "summit",
        None,
        0,
    )
    assert (first.snowfall_in, first.precip_in, first.temp_f) == (0.2, 0.03, 20.0)


def test_historical_forecast_paces_requests_and_stops_at_429(
    cfg: config.Config, no_pacing: list[float]
) -> None:
    class Limited(FakeWeb):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url.copy_with(query=None))
            if url == historical_forecast.URL and "aifs" in str(request.url):
                self.requests.append(url)
                return httpx.Response(429, text="Too many requests")
            return super().__call__(request)

    web = Limited()
    headers = base.make_client().headers
    client = httpx.Client(headers=headers, transport=httpx.MockTransport(web))
    summary = jobs.run_job(
        cfg,
        "season-backfill",
        DAY,
        client=client,
        options={"start": DAY, "end": DAY, "steps": ("historical_forecast",)},
    )
    fine = summary["historical_forecast"]
    # ecmwf_ifs loads; AIFS answers 429 and the step stops.
    assert fine["requests"] == 2 and fine["rate_limited"] is True
    assert fine["rows"]["forecast_hourly"] == 8 * 48
    assert len(no_pacing) == 1  # a wait before the second request
    assert all(0 < w <= historical_forecast.REQUEST_INTERVAL_S for w in no_pacing)
    resumed = jobs.run_job(
        cfg,
        "season-backfill",
        DAY,
        client=httpx.Client(headers=headers, transport=httpx.MockTransport(FakeWeb())),
        options={"start": DAY, "end": DAY, "steps": ("historical_forecast",)},
    )
    assert resumed["historical_forecast"]["requests"] == 1  # AIFS
    assert resumed["historical_forecast"]["cached"] == 1
