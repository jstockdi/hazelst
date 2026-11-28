"""forecast-verify and accumulation on the J00 fixture snow day (24 h ending 7 AM Sat 7 Feb 2026).

No snow fell during J14 (2026-11-26), so the build-plan Fallback applies: the
"Done when" is met on fixtures. Report date X = Fri 6 Feb 2026; its 1d window
(6 AM Feb 6 to 6 AM Feb 7) is verified against obs_date Feb 7.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from hazelst import config, db, jobs, verify, web
from hazelst.models import Accumulation, ContextReport
from hazelst.render import page
from hazelst.sources import base, iem, nohrsc, pns, snow_report
from tests.pages import recorded_narrative, recorded_payload
from tests.unit.test_obs_sources import (
    LSR_FEB,
    MOUNTAINS,
    NOHRSC_EXPECTED,
    archive_text,
    fetched,
    nohrsc_grid,
    replay,
)

DAY = date(2026, 2, 6)  # the report date being verified
OBS = date(2026, 2, 7)  # obs_date: the 24 h ending the morning after
EDITIONS = ("0700", "0815", "1440")
MODELS = ("gfs_global", "ecmwf_ifs025", "gem_global")
# The 1d model-mean forecast per mountain, and each edition's change to it.
FORECAST = {
    "killington": 4.0,
    "pico": 3.0,
    "sugarbush": 6.5,
    "stratton": 2.0,
    "loon": 1.0,
    "cranmore": 4.0,
    "sunday_river": 0.5,
    "sugarloaf": 0.0,
}
EDITION_SHIFT = {"0700": 0.0, "0815": 0.5, "1440": -0.5}
BY_KEY = {m.key: m for m in MOUNTAINS}


# --- scoring -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("forecast", "observed", "result"),
    [
        (4.0, 3.0, "HIT"),  # within 2 inches
        (5.0, 3.0, "HIT"),  # exactly 2 inches
        (5.5, 3.0, "MISS"),
        (13.0, 10.0, "HIT"),  # 3 inches off, but within 30%
        (14.0, 10.0, "MISS"),
        (2.0, 0.0, "HIT"),
        (3.0, 0.0, "MISS"),
        (3.0, None, "NO_OBS"),
    ],
)
def test_score(forecast: float, observed: float | None, result: str) -> None:
    assert verify.score(forecast, observed)[2] == result


def test_score_error_and_ratio() -> None:
    assert verify.score(4.0, 3.0) == (1.0, 1.333, "HIT")
    assert verify.score(2.0, 0.0) == (2.0, None, "HIT")
    assert verify.score(2.0, None) == (None, None, "NO_OBS")


# --- a database with the fixture day ----------------------------------------------


def insert_resort(
    conn: sqlite3.Connection,
    mountain: str,
    obs_date: str,
    reported_at: str,
    new_24h: float | None,
    *,
    source: str = "resort",
    stale: bool = False,
    season: float | None = None,
    base_in: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO observation (mountain, source, obs_date, reported_at, new_24h_in,"
        " season_in, base_in, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mountain,
            source,
            obs_date,
            reported_at,
            new_24h,
            season,
            base_in,
            json.dumps({"stale": stale}),
        ),
    )


def insert_editions(conn: sqlite3.Connection, day: date) -> None:
    """One report per edition (newest version wins), with 1d windows and day tags."""
    version = 0
    for run_id, edition in enumerate(EDITIONS, start=100):
        for stale_version in (True, False):  # an older version of the edition is ignored
            version += 1
            conn.execute(
                "INSERT INTO report (for_date, version, run_id, headline, payload, status)"
                " VALUES (?, ?, ?, ?, ?, 'published')",
                (
                    day.isoformat(),
                    version,
                    run_id + (1000 if stale_version else 0),
                    f"{edition} headline",
                    json.dumps({"edition": edition}),
                ),
            )
        for mountain, mean in FORECAST.items():
            value = max(mean + EDITION_SHIFT[edition], 0.0)
            by_model = {"gfs_global": value - 1, "ecmwf_ifs025": value, "gem_global": None}
            conn.execute(
                "INSERT INTO forecast_window (run_id, mountain, window, method, snow_in, lo_in,"
                " hi_in, by_model) VALUES (?, ?, '1d', 'raw_mean', ?, 0, 0, ?)",
                (run_id, mountain, value, json.dumps(by_model)),
            )
            conn.execute(
                "INSERT INTO forecast_window (run_id, mountain, window, method, snow_in, lo_in,"
                " hi_in, by_model) VALUES (?, ?, '3d', 'raw_mean', 99, 0, 0, '{}')",
                (run_id, mountain),
            )
            conn.execute(
                "INSERT INTO forecast_day (run_id, mountain, day, snow_in, tags)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, mountain, day.isoformat(), value, json.dumps(["nw_upslope"])),
            )


@pytest.fixture
def snow_day(tmp_path: Path) -> sqlite3.Connection:
    return build_snow_day(tmp_path)


def build_snow_day(tmp_path: Path) -> sqlite3.Connection:
    """The J00 observation fixtures stored as `ingest-obs` stores them, plus resort rows."""
    conn = db.connect(tmp_path / "v.db")
    db.migrate(conn)
    with db.transaction(conn):
        snow_report.store(
            conn, iem.normalize_lsr(fetched(iem.LSR_SOURCE, LSR_FEB), MOUNTAINS), "lsr"
        )
        for office in ("BTV", "ALY", "GYX"):
            pns.store(conn, pns.normalize(archive_text(office), MOUNTAINS))
        grids: dict[nohrsc.Product, base.Fetched] = {"24h": nohrsc_grid("24h")}
        ids: dict[nohrsc.Product, int] = {"24h": base.store_fetch(conn, None, grids["24h"])}
        nohrsc.store(conn, nohrsc.normalize(grids, MOUNTAINS), ids)
        # Killington's morning report (beats every other source); Pico's is stale.
        insert_resort(
            conn,
            "killington",
            "2026-02-07",
            "2026-02-07T11:30:00+00:00",
            3.0,
            season=83.0,
            base_in=None,
        )
        insert_resort(conn, "killington", "2026-02-07", "2026-02-07T19:30:00+00:00", 5.0)
        insert_resort(conn, "pico", "2026-02-07", "2026-02-07T11:30:00+00:00", 9.0, stale=True)
        # Sugarbush: only a conditions-extract value.
        insert_resort(
            conn, "sugarbush", "2026-02-07", "2026-02-07T12:10:00+00:00", 2.5, source="resort_llm"
        )
        insert_editions(conn, DAY)
    return conn


# Only resort-reported values score (decisions row 24); every other mountain is NO_OBS.
EXPECTED_OBS = {
    "killington": (3.0, "resort"),  # earliest report of the morning
    "sugarbush": (2.5, "resort_llm"),
}


def test_observed_is_resort_reported_only(snow_day: sqlite3.Connection) -> None:
    found = {m.key: verify.observed(snow_day, m.key, DAY) for m in MOUNTAINS}
    assert {k: (o.snow_in, o.source) for k, o in found.items() if o} == EXPECTED_OBS
    # Pico's resort report is stale: NOHRSC and spotter reports exist but never stand in.
    assert found["pico"] is None
    assert {c.source for c in verify.context(snow_day, BY_KEY["pico"], DAY)} == {
        "nohrsc",
        "pns",
        "lsr",
    }
    # NOHRSC values are stored rounded to 0.01 in, so they compare exactly.
    grid = verify.context(snow_day, BY_KEY["pico"], DAY)[0]
    assert (grid.source, grid.snow_in) == ("nohrsc", NOHRSC_EXPECTED["pico"])


def test_observed_never_returns_a_non_resort_source(tmp_path: Path) -> None:
    """Point reports at the summit and a NOHRSC value for every day still score nothing."""
    conn = db.connect(tmp_path / "o.db")
    db.migrate(conn)
    k = BY_KEY["killington"]
    rows = [
        snow_report.make_row(
            product_id=product,
            place="Killington summit",
            lat=k.lat,
            lon=k.lon,
            ending_at=datetime(2026, 2, 7, 12, 0, tzinfo=UTC),
            snow_in=8.0,
            hours=24,
            mountains=MOUNTAINS,
        )
        for product in ("202602071200-KBTV-NOUS41-PNSBTV", "202602071300-KBTV-NWUS51-LSRBTV")
    ]
    with db.transaction(conn):
        snow_report.store(conn, rows[:1], "pns")
        snow_report.store(conn, rows[1:], "lsr")
        insert_resort(
            conn, "killington", "2026-02-07", "2026-02-07T12:00:00+00:00", 5.0, source="nohrsc"
        )
        insert_resort(
            conn,
            "killington",
            "2026-02-07",
            "2026-02-07T11:00:00+00:00",
            6.0,
            source="model_analysis",
        )
    assert verify.observed(conn, "killington", DAY) is None
    assert {c.source for c in verify.context(conn, k, DAY)} == {"nohrsc", "pns"}
    with db.transaction(conn):
        insert_resort(
            conn, "killington", "2026-02-07", "2026-02-07T18:00:00+00:00", 2.0, source="snocountry"
        )
    obs = verify.observed(conn, "killington", DAY)
    assert obs is not None and (obs.snow_in, obs.source) == (2.0, "snocountry")
    assert obs.source in verify.RESORT_SOURCES


def test_context_per_source(snow_day: sqlite3.Connection) -> None:
    ctx = verify.context(snow_day, BY_KEY["cranmore"], DAY)
    assert ctx[0] == ContextReport(
        source="nohrsc", place="NOHRSC 24 h grid", snow_in=1.0, elev_ft=1063
    )
    points = ctx[1:]
    assert [c.distance_mi for c in points] == sorted(c.distance_mi or 0 for c in points)
    assert all(c.source == "pns" and c.hours == 24 for c in points)
    first = points[0]
    assert (first.place, first.snow_in, first.distance_mi) == ("North Conway, NH", 1.0, 1.1)
    # Every point report within 20 mi of the mountain's own point, whichever is nearest.
    sugarbush = verify.context(snow_day, BY_KEY["sugarbush"], DAY)
    assert max(c.distance_mi or 0 for c in sugarbush) == 20.0
    assert {c.source for c in sugarbush} == {"nohrsc", "pns", "lsr"}
    assert [c.source for c in verify.context(snow_day, BY_KEY["loon"], DAY)] == ["nohrsc"]


def test_context_keeps_the_morning_window_and_20_miles(snow_day: sqlite3.Connection) -> None:
    cranmore = BY_KEY["cranmore"]
    # Cranmore's 24 h reports end at 7 and 8 AM on Feb 7: they belong to Feb 6 only.
    for other in (date(2026, 2, 5), date(2026, 2, 7)):
        assert not [c for c in verify.context(snow_day, cranmore, other) if c.source == "pns"]
    with db.transaction(snow_day):
        snow_day.execute("UPDATE snow_report SET lat = lat + 1 WHERE place LIKE 'North Conway%'")
    places = {c.place for c in verify.context(snow_day, cranmore, DAY)}
    assert not any(p.startswith("North Conway") for p in places)


def test_lsr_of_unknown_span_is_context(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "l.db")
    db.migrate(conn)
    row = snow_report.make_row(
        product_id="202602062300-KBTV-NWUS51-LSRBTV",
        place="Killington, VT",
        lat=43.62,
        lon=-72.80,
        ending_at=datetime(2026, 2, 6, 23, 0, tzinfo=UTC),
        snow_in=4.2,
        hours=None,
        mountains=MOUNTAINS,
    )
    with db.transaction(conn):
        snow_report.store(conn, [row], "lsr")
    assert verify.observed(conn, "killington", DAY) is None
    (ctx,) = verify.context(conn, BY_KEY["killington"], DAY)
    assert (ctx.source, ctx.snow_in, ctx.hours) == ("lsr", 4.2, None)
    assert ctx.ending_at == "2026-02-06T23:00:00+00:00"
    # Before 6 AM on the day it belongs to the day before.
    assert verify.context(conn, BY_KEY["killington"], date(2026, 2, 7)) == []


def test_verification_rows_for_each_edition(snow_day: sqlite3.Connection) -> None:
    rows = verify.verify_date(snow_day, MOUNTAINS, DAY)
    with db.transaction(snow_day):
        assert verify.store(snow_day, rows) == len(rows)
    # 3 editions x 8 mountains x (raw_mean + GFS + ECMWF; GEM is null).
    assert len(rows) == 3 * 8 * 3
    assert {r.edition for r in rows} == set(EDITIONS)
    assert {r.method for r in rows} == {"raw_mean", "gfs_global", "ecmwf_ifs025"}
    assert {r.window for r in rows} == {"1d"}
    mean = {(r.edition, r.mountain): r for r in rows if r.method == "raw_mean"}
    k = mean[("0700", "killington")]
    assert (k.forecast_in, k.observed_in, k.observed_source) == (4.0, 3.0, "resort")
    assert (k.error_in, k.ratio, k.result) == (1.0, 1.333, "HIT")
    assert json.loads(k.tags) == ["nw_upslope"]
    s = mean[("0815", "sugarbush")]  # 7.0 forecast vs 2.5 extracted
    assert (s.forecast_in, s.observed_source, s.result) == (7.0, "resort_llm", "MISS")
    c = mean[("1440", "cranmore")]
    assert (c.observed_in, c.observed_source, c.result) == (None, None, "NO_OBS")
    assert {r.observed_source for r in rows} <= {*verify.RESORT_SOURCES, None}
    assert all(r.observed_elev_ft is None for r in rows)
    # The context is stored beside the score, the same for every edition and method.
    ctx = json.loads(c.context)
    assert ctx[0] == {
        "source": "nohrsc",
        "place": "NOHRSC 24 h grid",
        "snow_in": 1.0,
        "elev_ft": 1063,
        "distance_mi": None,
        "hours": None,
        "ending_at": None,
    }
    assert {r.context for r in rows if r.mountain == "cranmore"} == {c.context}
    assert json.loads(k.context)[0]["source"] == "nohrsc"
    # The newest version of each edition is scored (run ids 100-102, not 1100-1102).
    stored = snow_day.execute(
        "SELECT edition, count(*) FROM verification WHERE for_date = ? GROUP BY 1",
        (DAY.isoformat(),),
    ).fetchall()
    assert dict(stored) == {"0700": 24, "0815": 24, "1440": 24}
    assert (
        snow_day.execute(
            "SELECT count(*) FROM verification WHERE context IS NULL OR context = '[]'"
        ).fetchone()[0]
        == 0
    )
    # Killington hits and Sugarbush misses in every edition; the rest have no resort report.
    assert verify.summarize(rows) == {e: {"HIT": 1, "MISS": 1, "NO_OBS": 6} for e in EDITIONS}
    # Re-scoring replaces rows instead of adding them.
    with db.transaction(snow_day):
        verify.store(snow_day, verify.verify_date(snow_day, MOUNTAINS, DAY))
    assert snow_day.execute("SELECT count(*) FROM verification").fetchone()[0] == 72


def test_no_observation_is_no_obs(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "n.db")
    db.migrate(conn)
    with db.transaction(conn):
        insert_editions(conn, DAY)
    rows = verify.verify_date(conn, MOUNTAINS, DAY)
    assert rows and {r.result for r in rows} == {"NO_OBS"}
    assert all(r.observed_in is None and r.error_in is None for r in rows)


def test_summary_for_the_footer(snow_day: sqlite3.Connection) -> None:
    with db.transaction(snow_day):
        verify.store(snow_day, verify.verify_date(snow_day, MOUNTAINS, DAY))
    assert verify.latest_summary(snow_day, DAY) is None
    summary = verify.latest_summary(snow_day, date(2026, 2, 8))
    assert summary is not None and summary.for_date == DAY
    assert page.verification_counts(summary) == (
        "7:00 AM 1 HIT · 1 MISS · 6 no report; 8:15 AM 1 HIT · 1 MISS · 6 no report;"
        " 2:40 PM 1 HIT · 1 MISS · 6 no report"
    )


# --- accumulation ------------------------------------------------------------------


def test_accumulation_for_the_next_morning(snow_day: sqlite3.Connection) -> None:
    acc = verify.accumulation(snow_day, MOUNTAINS, date(2026, 2, 7))
    # Every mountain has context for Feb 6, so each is listed.
    assert set(acc) == {m.key for m in MOUNTAINS}
    k = acc["killington"]
    # The 7 days before Feb 7 are Jan 31 .. Feb 6; only Feb 6 has a resort report.
    assert k.last7_in == [None, None, None, None, None, None, 3.0]
    assert (k.source, k.season_in, k.base_in) == ("resort", 83.0, None)
    assert k.sources == ["resort"]
    assert [d.date for d in k.days] == [date(2026, 1, 31), *(date(2026, 2, n) for n in range(1, 7))]
    feb6 = k.days[-1]
    assert (feb6.snow_in, feb6.source) == (3.0, "resort")
    assert feb6.context[0].source == "nohrsc" and feb6.context[0].snow_in == 3.17
    # Cranmore has no resort report: no number, no season, the context beside it.
    c = acc["cranmore"]
    assert c.last7_in == [None] * 7 and (c.source, c.season_in, c.sources) == ("none", None, [])
    assert not c.has_resort_data
    assert c.days[-1].snow_in is None
    assert {x.source for x in c.days[-1].context} == {"nohrsc", "pns"}
    # No NOHRSC season fallback.
    assert acc["loon"].season_in is None and acc["loon"].last7_in[-1] is None


def _with_accumulation(snow_day: sqlite3.Connection, for_date: date):  # type: ignore[no-untyped-def]
    acc = verify.accumulation(snow_day, MOUNTAINS, for_date)
    payload = recorded_payload()
    return payload.model_copy(
        update={
            "mountains": [
                m.model_copy(update={"accumulation": acc.get(m.key)}) for m in payload.mountains
            ],
        }
    )


def test_sources_and_verification_stay_in_the_json(snow_day: sqlite3.Connection) -> None:
    """The footer is one line (J35); sources and verification are in the payload only."""
    with db.transaction(snow_day):
        verify.store(snow_day, verify.verify_date(snow_day, MOUNTAINS, DAY))
    payload = _with_accumulation(snow_day, date(2026, 2, 7)).model_copy(
        update={"verification": verify.latest_summary(snow_day, date(2026, 2, 7))}
    )
    html = page.render_report(payload, recorded_narrative())
    assert "Accumulation" not in html and "Verification" not in html and "<svg" not in html
    assert "NWS snowfall reports" not in html
    assert payload.verification is not None and payload.verification.for_date == date(2026, 2, 6)


def test_timeline_past_is_resort_reported_only(snow_day: sqlite3.Connection) -> None:
    payload = _with_accumulation(snow_day, date(2026, 2, 7)).model_copy(
        update={"date": date(2026, 2, 7)}
    )
    rows = {r.name: r for r in page.snow_timeline(payload).rows}
    # Cranmore has NOHRSC and PNS context on Fri 6 but no resort number: no report.
    feb6 = rows["Cranmore"].past[-1]
    assert (feb6.kind, feb6.text, feb6.title) == ("past", "", "Fri 6 · no resort report")
    killington = rows["Killington"].past
    assert killington[-1].text == "3\u2033"
    assert killington[-1].title == "Fri 6 · resort reported 3\u2033 (resort)"
    assert [c.text for c in killington[:2]] == ["", ""]
    html = page.render_report(payload, recorded_narrative())
    assert "NOHRSC 1" not in html  # context stays in the JSON


def test_timeline_without_resort_reports(snow_day: sqlite3.Connection) -> None:
    with db.transaction(snow_day):
        snow_day.execute("DELETE FROM observation WHERE source IN ('resort', 'resort_llm')")
    payload = _with_accumulation(snow_day, date(2026, 2, 7)).model_copy(
        update={"date": date(2026, 2, 7)}
    )
    killington = next(r for r in page.snow_timeline(payload).rows if r.name == "Killington")
    assert killington.past[-1].text == "" and not killington.past[-1].background
    assert "no resort report" in page.render_report(payload, recorded_narrative())


def test_accumulation_empty_without_observations(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "e.db")
    db.migrate(conn)
    assert verify.accumulation(conn, MOUNTAINS, date(2026, 2, 7)) == {}
    acc = Accumulation(source="resort", last7_in=[0.0] * 7, sources=["resort"])
    assert acc.days == [] and acc.has_resort_data


# --- the job -------------------------------------------------------------------------


def test_forecast_verify_job(tmp_path: Path) -> None:
    """ingest-obs on Feb 7, then forecast-verify on the evening of Feb 7 scores Feb 6."""
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    client = httpx.Client(transport=httpx.MockTransport(replay))
    jobs.run_job(cfg, "ingest-obs", OBS, client=client)
    conn = db.connect(cfg.settings.db_path)
    with db.transaction(conn):
        insert_editions(conn, DAY)
    summary = jobs.run_job(cfg, "forecast-verify", OBS, client=client)
    assert summary["kind"] == "forecast-verify"
    scored = summary["scored"]
    assert set(scored) == {"2026-02-06", "2026-01-30"}
    assert scored["2026-02-06"]["rows"] == 72
    assert set(scored["2026-02-06"]["model_mean"]) == set(EDITIONS)
    assert scored["2026-01-30"]["rows"] == 0
    # The recorded resort pages are unavailable, so nothing is scored; NOHRSC (read by
    # the job) and the spotter reports are stored as context only.
    rows = conn.execute(
        "SELECT edition, count(*) FROM verification WHERE for_date = '2026-02-06'"
        " AND result = 'NO_OBS' AND json_array_length(context) > 0 GROUP BY 1"
    ).fetchall()
    assert dict(rows) == {"0700": 24, "0815": 24, "1440": 24}
    run = conn.execute("SELECT kind, status FROM run WHERE id = ?", (summary["run_id"],)).fetchone()
    assert tuple(run) == ("forecast-verify", "ok")
    entries = web.archive_entries(conn)
    assert entries[0].date == DAY
    assert entries[0].verification == page.verification_counts(
        verify.summary_for(conn, DAY)  # type: ignore[arg-type]
    )
    conn.close()
