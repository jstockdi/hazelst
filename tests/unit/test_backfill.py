"""season-backfill on the J00 fixture snow day (report day Fri 6 Feb 2026), with HTTP mocked.

Previous Runs responses are synthetic (the J00 fixtures are single-location
and local-time); LSR, PNS, and NOHRSC replay the recorded J00 fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from hazelst import backfill, config, db, jobs
from hazelst.sources import base, iem, nohrsc, pns, previous_runs, snocountry
from tests.support import FIXTURES
from tests.unit.test_obs_sources import LSR_FEB, NOHRSC_24H, PNS_IDS, body
from tests.unit.test_snocountry import SNOCOUNTRY_BODIES

DAY = date(2026, 2, 6)
SNOW_PER_HOUR = 0.1


def previous_runs_body(query: dict[str, str]) -> list[dict[str, Any]]:
    """Every mountain, GFS and GEM (and an all-null ECMWF), for the requested dates."""
    lead = int(query["hourly"].split(",")[0].rsplit("previous_day", 1)[1])
    start = datetime.fromisoformat(query["start_date"]).replace(tzinfo=UTC)
    days = (date.fromisoformat(query["end_date"]) - start.date()).days + 1
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24 * days)]
    out = []
    for _ in query["latitude"].split(","):
        hourly: dict[str, list[Any]] = {"time": times}
        units: dict[str, str] = {"time": "iso8601"}
        for model in ("gfs_seamless", "gem_seamless", "ecmwf_ifs025"):
            for var, unit, value in (
                ("snowfall", "inch", SNOW_PER_HOUR * lead),
                ("precipitation", "inch", 0.01),
                ("temperature_2m", "°F", 20.0),
            ):
                name = f"{var}_previous_day{lead}_{model}"
                missing = model == "ecmwf_ifs025" and var == "snowfall"
                hourly[name] = [None if missing else value for _ in times]
                units[name] = unit
        out.append({"utc_offset_seconds": 0, "hourly": hourly, "hourly_units": units})
    return out


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
    assert {r.model for r in rows} == {"gfs_seamless", "gem_seamless"}  # no ECMWF snowfall
    assert len(rows) == 2 * 2 * 48
    first = rows[0]
    assert (first.elevation, first.issued_at, first.lead_h) == ("summit", None, 48)
    assert (first.snowfall_in, first.precip_in, first.temp_f) == (0.3, 0.01, 20.0)
    assert first.valid_at == "2026-02-06T00:00:00+00:00"


def test_backfill_loads_every_source_and_is_idempotent(cfg: config.Config) -> None:
    web = FakeWeb()
    first = run_backfill(cfg, web)
    assert first["previous_runs"]["rows"] == {"forecast_hourly": 5 * 8 * 2 * 48}
    assert first["lsr"]["rows"]["snow_report"] > 0
    assert first["pns"]["rows"]["discussion"] == 3
    assert first["nohrsc"]["rows"] == {"observation": 8}
    assert first["nohrsc"]["requests"] == 1
    assert first["snocountry"]["requests"] == 8
    assert first["snocountry"]["rows"]["observation"] > 8 * 1000  # whole histories
    stored = counts(cfg)
    sent = len(web.requests)

    again = FakeWeb()
    second = run_backfill(cfg, again)
    assert again.requests == []  # every response is already stored
    assert all(second[s]["requests"] == 0 and second[s]["rows"] == {} for s in backfill.STEPS)
    assert counts(cfg) == stored
    # Leads, LSR, listings for 2 days, texts, NOHRSC, one SnoCountry document per mountain.
    assert sent == 5 + 1 + 2 * 3 + 3 + 1 + 8


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
        (m, model) for m in keys for model in ("gfs_seamless", "gem_seamless")
    }
    assert {r[2] for r in cells} == {"1d", "2-3d", "4-5d"}
    # Lead day N carries 0.1 x N in/h: day 1 forecasts 2.4 in, verified against the
    # resort-reported 6 in from SnoCountry (ahead of NOHRSC's 3.17 in).
    killington = conn.execute(
        "SELECT bias_ratio FROM skill WHERE mountain = 'killington' AND model = 'gfs_seamless'"
        " AND lead_bucket = '1d' AND tag = ''"
    ).fetchone()[0]
    assert killington == pytest.approx(2.4 / 6.0, abs=0.01)
    assert summary["skill"] == len(cells)


def test_season_backfill_needs_a_range(cfg: config.Config) -> None:
    with pytest.raises(jobs.JobError, match="start and end"):
        jobs.run_job(cfg, "season-backfill", DAY)
