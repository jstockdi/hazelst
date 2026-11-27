"""J25 report-elevation: Open-Meteo elevation fill, migration 0004, `observed_elev_ft` per
source, the backfill edition, and the `verify summary --by elevation` banding."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from hazelst import cli, db, verify
from hazelst.sources import base, elevation, snow_report
from tests.unit.test_obs_sources import MOUNTAINS
from tests.unit.test_verify import DAY, EXPECTED_OBS, insert_resort, snow_day  # noqa: F401

BY_KEY = {m.key: m for m in MOUNTAINS}
HEADERS = base.make_client().headers  # the identifying User-Agent


class FakeElevation:
    """The Open-Meteo elevation API: metres = 1000 * latitude fraction; counts requests."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.queries: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url.copy_with(query=None)) == elevation.URL
        assert request.headers["User-Agent"].startswith("hazelst-forecast")
        query = dict(request.url.params.multi_items())
        self.queries.append(query)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": True, "reason": "down"})
        lats = [float(v) for v in query["latitude"].split(",")]
        return httpx.Response(200, json={"elevation": [round((x % 1) * 1000, 1) for x in lats]})

    def client(self) -> httpx.Client:
        return httpx.Client(headers=HEADERS, transport=httpx.MockTransport(self))


def report(
    lat: float, lon: float, *, snow_in: float = 2.0, hour: int = 12
) -> snow_report.SnowReportRow:
    return snow_report.make_row(
        product_id="202602071200-KBTV-NWUS51-LSRBTV",
        place="Somewhere",
        lat=lat,
        lon=lon,
        ending_at=datetime(2026, 2, 7, hour, tzinfo=UTC),
        snow_in=snow_in,
        hours=None,
        mountains=MOUNTAINS,
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "e.db")
    db.migrate(c)
    db.sync_mountains(c, MOUNTAINS)
    return c


# --- lookup and fill -----------------------------------------------------------------


def test_lookup_converts_metres_to_feet_in_batches_of_100() -> None:
    api = FakeElevation()
    points = [(44.0 + i / 1000, -72.0) for i in range(150)]
    found = elevation.lookup(api.client(), points)
    assert len(api.queries) == 2
    assert [len(q["latitude"].split(",")) for q in api.queries] == [100, 50]
    assert found[(44.0, -72.0)] == 0
    assert found[(44.1, -72.0)] == round(100 * elevation.FT_PER_M)  # 100 m = 328 ft


def test_lookup_paces_batches_and_stops_at_a_rate_limit(
    no_elevation_pacing: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hazelst.sources.base.time.sleep", lambda _s: None)  # retry backoff
    points = [(44.0 + i / 1000, -72.0) for i in range(250)]
    api = FakeElevation()
    elevation.lookup(api.client(), points)
    assert no_elevation_pacing == [elevation.BATCH_INTERVAL_S] * 2  # 3 batches

    limited = FakeElevation(status=429)
    found = elevation.lookup(limited.client(), points)
    assert len(limited.queries) == 3  # the first batch and its 2 retries, then stop
    assert set(found.values()) == {None} and len(found) == 250


def test_lookup_failure_is_none_not_an_error() -> None:
    found = elevation.lookup(FakeElevation(status=400).client(), [(44.5, -72.0)])
    assert found == {(44.5, -72.0): None}


def test_fill_sets_elev_ft_caches_by_rounded_point_and_counts_failures(
    conn: sqlite3.Connection,
) -> None:
    with db.transaction(conn):
        snow_report.store(conn, [report(44.3, -72.1), report(44.3001, -72.1, snow_in=3.0)], "lsr")
    api = FakeElevation()
    result = elevation.fill(conn, api.client())
    # Both rows round to the same 0.001° key: one point, one request.
    assert (result.rows, result.points, result.requested, result.filled) == (2, 1, 1, 2)
    elevs = {r[0] for r in conn.execute("SELECT elev_ft FROM snow_report")}
    assert elevs == {round(300 * elevation.FT_PER_M)}

    # A new report at a known point costs no request; a new point that fails stays NULL.
    with db.transaction(conn):
        snow_report.store(conn, [report(44.3, -72.1, hour=13), report(44.9, -72.1)], "lsr")
    down = FakeElevation(status=400)
    result = elevation.fill(conn, down.client())
    assert (result.cached, result.requested, result.filled, result.failed) == (1, 1, 1, 1)
    nulls = conn.execute("SELECT count(*) FROM snow_report WHERE elev_ft IS NULL").fetchone()[0]
    assert nulls == 1

    # The next fill retries the failed point only.
    again = FakeElevation()
    assert elevation.fill(conn, again.client()).filled == 1
    assert [q["latitude"] for q in again.queries] == ["44.900"]
    assert elevation.fill(conn, again.client()).rows == 0
    assert len(again.queries) == 1


def test_pns_copy_keeps_the_lsr_rows_elevation(conn: sqlite3.Connection) -> None:
    row = report(44.3, -72.1)
    with db.transaction(conn):
        snow_report.store(conn, [row], "lsr")
    elevation.fill(conn, FakeElevation().client())
    pns_copy = snow_report.SnowReportRow(
        **{**row.__dict__, "product_id": "202602071300-KBTV-NOUS41-PNSBTV", "place": "PNS place"}
    )
    with db.transaction(conn):
        assert snow_report.store(conn, [pns_copy], "pns") == 1
    stored = conn.execute("SELECT product_id, place, elev_ft FROM snow_report").fetchall()
    assert [tuple(r) for r in stored] == [
        ("202602071300-KBTV-NOUS41-PNSBTV", "PNS place", round(300 * elevation.FT_PER_M))
    ]


def test_ingest_lsr_fills_elevations(tmp_path: Path) -> None:
    from hazelst import config, jobs
    from tests.unit.test_obs_sources import LSR_FEB, body

    api = FakeElevation()

    def web(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(elevation.URL):
            return api(request)
        return httpx.Response(200, content=body(LSR_FEB))

    cfg = config.load({"HAZELST_DB": str(tmp_path / "i.db")})
    client = httpx.Client(headers=base.make_client().headers, transport=httpx.MockTransport(web))
    c = db.connect(cfg.settings.db_path)
    db.migrate(c)
    with db.job(c, cfg.settings.db_path, "ingest-obs", date(2026, 2, 7)) as run_id:
        ctx = jobs.JobContext(cfg, c, run_id, date(2026, 2, 7), client)
        out = jobs.ingest_lsr(ctx)
    assert out["elev_ft"]["failed"] == 0
    assert out["elev_ft"]["filled"] == out["snow_report"] > 0
    assert c.execute("SELECT count(*) FROM snow_report WHERE elev_ft IS NULL").fetchone()[0] == 0


def test_elevations_fill_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HAZELST_DB", str(tmp_path / "c.db"))
    c = db.connect(tmp_path / "c.db")
    db.migrate(c)
    with db.transaction(c):
        snow_report.store(c, [report(44.3, -72.1), report(44.4, -72.1)], "lsr")
    api = FakeElevation()
    monkeypatch.setattr(base, "make_client", lambda *a, **k: api.client())
    assert cli.main(["elevations", "fill"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["filled"] == 2
    assert out["snow_report"] == {"rows": 2, "without_elev_ft": 0}


# --- migration 0004 -----------------------------------------------------------------


def test_migration_0004_adds_observed_elev_ft(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "m.db")
    migrations = db.load_migrations()
    assert db.migrate(c, [m for m in migrations if m.version <= 3]) == [1, 2, 3]
    c.execute(
        "INSERT INTO verification (for_date, edition, mountain, window, method, result)"
        " VALUES ('2026-02-06', '0700', 'loon', '1d', 'raw_mean', 'HIT')"
    )
    assert db.migrate(c, [m for m in migrations if m.version <= 4]) == [4]
    columns = [r[1] for r in c.execute("PRAGMA table_info(verification)")]
    assert columns[-1] == "observed_elev_ft"
    assert c.execute("SELECT observed_elev_ft FROM verification").fetchone()[0] is None


def test_migration_0005_adds_context(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "m5.db")
    migrations = db.load_migrations()
    assert db.migrate(c, [m for m in migrations if m.version <= 4]) == [1, 2, 3, 4]
    c.execute(
        "INSERT INTO verification (for_date, edition, mountain, window, method, result)"
        " VALUES ('2026-02-06', '0700', 'loon', '1d', 'raw_mean', 'HIT')"
    )
    assert db.migrate(c) == [5]
    columns = [r[1] for r in c.execute("PRAGMA table_info(verification)")]
    assert columns[-2:] == ["observed_elev_ft", "context"]
    assert c.execute("SELECT context FROM verification").fetchone()[0] is None
    indexes = {r[1] for r in c.execute("PRAGMA index_list(snow_report)")}
    assert "snow_report_ending_at" in indexes


# --- elevations in the context ----------------------------------------------------------


def test_context_carries_report_and_pixel_elevations(snow_day: sqlite3.Connection) -> None:  # noqa: F811
    with db.transaction(snow_day):
        snow_day.execute("UPDATE snow_report SET elev_ft = 500 + CAST(snow_in * 100 AS INT)")
    rows = verify.verify_date(snow_day, MOUNTAINS, DAY)
    with db.transaction(snow_day):
        verify.store(snow_day, rows)
    # The score's own elevation is unknown: resort measurement sites are not published.
    assert {r.observed_elev_ft for r in rows} == {None}
    for mountain, context in snow_day.execute(
        "SELECT DISTINCT mountain, context FROM verification"
    ):
        items = json.loads(context)
        assert items[0]["source"] == "nohrsc"
        assert items[0]["elev_ft"] == BY_KEY[mountain].nohrsc_pixel_elev_ft
        for item in items[1:]:
            assert item["elev_ft"] == 500 + int(item["snow_in"] * 100)
            assert 0 <= item["distance_mi"] <= verify.CONTEXT_MI


# --- backfill edition -----------------------------------------------------------------


def insert_hourly(
    conn: sqlite3.Connection, mountain: str, model: str, day: date, per_h: float
) -> None:
    """A Previous Runs day-1 series: 24 summit hours from 6 AM local (11Z), lead_h 0."""
    fetch_id = conn.execute(
        "INSERT INTO fetch (source, url, http_status) VALUES ('t', ?, 200)", (f"{model}{day}",)
    ).lastrowid
    start = datetime(day.year, day.month, day.day, 11, tzinfo=UTC)
    conn.executemany(
        "INSERT INTO forecast_hourly (fetch_id, mountain, model, elevation, valid_at, lead_h,"
        " snowfall_in) VALUES (?, ?, ?, 'summit', ?, 0, ?)",
        [
            (fetch_id, mountain, model, (start + timedelta(hours=h)).isoformat(), per_h)
            for h in range(24)
        ],
    )


def test_backfill_forecasts_score_days_without_reports(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        insert_hourly(conn, "loon", "gfs_seamless", DAY, 0.25)  # 6 in
        insert_hourly(conn, "loon", "gem_seamless", DAY, 0.125)  # 3 in
        insert_resort(conn, "loon", "2026-02-07", "2026-02-07T12:00:00+00:00", 5.0)
    found = verify.backfill_forecasts(conn, DAY, DAY)
    (loon,) = found[DAY]
    assert (loon.edition, loon.mountain) == ("backfill", "loon")
    assert loon.methods == {"gfs_seamless": 6.0, "gem_seamless": 3.0, "raw_mean": 4.5}
    rows = verify.verify_date(conn, MOUNTAINS, DAY, forecasts=found[DAY])
    mean = next(r for r in rows if r.method == "raw_mean")
    assert (mean.edition, mean.observed_in, mean.observed_source, mean.result) == (
        "backfill",
        5.0,
        "resort",
        "HIT",
    )


# --- summary by elevation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("elev", "band"),
    [
        (None, "unknown"),
        (0, "< 1,500 ft"),
        (1499, "< 1,500 ft"),
        (1500, "1,500-2,500 ft"),
        (2500, "1,500-2,500 ft"),
        (2501, "> 2,500 ft"),
        (6288, "> 2,500 ft"),
    ],
)
def test_elevation_band(elev: int | None, band: str) -> None:
    assert verify.elevation_band(elev) == band


def insert_verification(
    conn: sqlite3.Connection,
    for_date: str,
    edition: str,
    mountain: str,
    forecast: float,
    observed: float | None,
    context: list[tuple[float, int | None]] = (),  # type: ignore[assignment]
    method: str = "raw_mean",
) -> None:
    """A row scored against a resort value, with context reports as (snow_in, elev_ft)."""
    error, ratio, result = verify.score(forecast, observed)
    items = [{"source": "ghcn", "place": "x", "snow_in": v, "elev_ft": e} for v, e in context]
    conn.execute(
        "INSERT INTO verification (for_date, edition, mountain, window, method, forecast_in,"
        " observed_in, observed_source, error_in, ratio, result, tags, context)"
        " VALUES (?, ?, ?, '1d', ?, ?, ?, 'resort', ?, ?, ?, '[]', ?)",
        (
            for_date,
            edition,
            mountain,
            method,
            forecast,
            observed,
            error,
            ratio,
            result,
            json.dumps(items),
        ),
    )


def test_summary_by_elevation_bands_and_earliest_edition(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        # Loon: the resort reported 2 in; a valley report had 1 in, a high site 3 and 2.5.
        insert_verification(
            conn, "2026-02-01", "0700", "loon", 4.0, 2.0, [(1.0, 900), (3.0, 3000), (2.5, 2600)]
        )
        insert_verification(conn, "2026-02-01", "0815", "loon", 9.0, 2.0)  # later edition
        # No resort report: not scored, but the context still compares.
        insert_verification(conn, "2026-02-02", "0700", "loon", 2.0, None, [(8.0, 900)])
        insert_verification(conn, "2026-02-03", "backfill", "loon", 1.0, 3.0, [(0.1, None)])
        insert_verification(conn, "2026-02-04", "0700", "loon", 0.2, 0.0, [(0.0, 900)])  # dry
        insert_verification(conn, "2026-02-01", "0700", "pico", 3.0, 6.0)  # MISS
        insert_verification(conn, "2026-02-01", "0700", "pico", 1.0, 6.0, method="gfs_seamless")
    cells = verify.summary_cells(conn, by="elevation")
    got = {(c.mountain, c.group): (c.n, c.bias_ratio, c.mae_in, c.hit_rate) for c in cells}
    assert got == {
        ("loon", "resort"): (2, 1.0, 2.0, 1.0),
        ("loon", "< 1,500 ft"): (2, 0.67, 4.5, 0.0),
        ("loon", "> 2,500 ft"): (1, 1.33, 1.0, 1.0),  # the highest report in the band
        ("loon", "unknown"): (1, 10.0, 0.9, 1.0),
        ("pico", "resort"): (1, 0.5, 3.0, 0.0),
        ("all", "resort"): (3, 0.73, 2.33, 0.67),
        ("all", "< 1,500 ft"): (2, 0.67, 4.5, 0.0),
        ("all", "> 2,500 ft"): (1, 1.33, 1.0, 1.0),
        ("all", "unknown"): (1, 10.0, 0.9, 1.0),
    }
    assert [c.mountain for c in cells][-1] == "all"  # totals come last
    # All days: the dry day joins the resort group and the low band; one edition only.
    every = verify.summary_cells(conn, by="elevation", snow_days_only=False)
    assert [(c.group, c.n) for c in every if c.mountain == "loon"][:2] == [
        ("resort", 3),
        ("< 1,500 ft", 3),
    ]
    later = verify.summary_cells(conn, by="elevation", edition="0815")
    assert next((c.mountain, c.group, c.n, c.bias_ratio) for c in later) == (
        "loon",
        "resort",
        1,
        4.5,
    )
    overall = verify.summary_cells(conn, by="mountain", end=date(2026, 2, 2))
    assert [(c.mountain, c.group, c.n) for c in overall] == [
        ("loon", "all", 1),
        ("pico", "all", 1),
        ("all", "all", 2),
    ]


def test_verify_summary_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HAZELST_DB", str(tmp_path / "s.db"))
    c = db.connect(tmp_path / "s.db")
    db.migrate(c)
    with db.transaction(c):
        insert_verification(c, "2026-02-01", "0700", "loon", 4.0, 2.0, [(1.0, 900)])
        insert_verification(c, "2026-02-02", "0700", "loon", 2.0, 8.0, [(6.0, 3000)])
    assert cli.main(["verify", "summary", "--by", "elevation"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("verification 1d: 2 dates (2026-02-01..2026-02-02)")
    assert "bands compare the forecast with context reports" in out[1]
    assert out[2].split() == ["mountain", "group", "n", "bias", "mae_in", "hit"]
    assert out[4].split() == ["loon", "resort", "2", "0.60", "4.00", "0.50"]
    assert out[5].split() == ["loon", "<", "1,500", "ft", "1", "4.00", "3.00", "0.00"]
    assert out[6].split() == ["loon", ">", "2,500", "ft", "1", "0.33", "4.00", "0.00"]


def test_forecast_verify_range_scores_backfilled_days(tmp_path: Path) -> None:
    from hazelst import config, jobs
    from tests.unit.test_verify import insert_editions

    cfg = config.load({"HAZELST_DB": str(tmp_path / "r.db")})
    c = db.connect(cfg.settings.db_path)
    db.migrate(c)
    db.sync_mountains(c, MOUNTAINS)
    before = DAY - timedelta(days=1)  # no report: scored from forecast_hourly
    with db.transaction(c):
        insert_editions(c, DAY)
        insert_hourly(c, "loon", "gfs_seamless", before, 0.25)
        insert_hourly(c, "loon", "gfs_seamless", DAY, 0.5)  # DAY has editions: ignored
        insert_resort(c, "loon", before.isoformat(), "2026-02-06T12:00:00+00:00", 5.0)
    out = jobs.run_job(cfg, "forecast-verify", DAY, options={"start": before, "end": DAY})
    assert out["rescored"]["days"] == 2
    assert out["rescored"]["backfill_days"] == 1
    editions = c.execute(
        "SELECT for_date, edition, count(*) FROM verification GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    assert [tuple(r) for r in editions] == [
        (before.isoformat(), "backfill", 2),  # gfs_seamless and raw_mean
        (DAY.isoformat(), "0700", 24),
        (DAY.isoformat(), "0815", 24),
        (DAY.isoformat(), "1440", 24),
    ]
    # The page footer reads report editions only.
    summary = verify.latest_summary(c, DAY + timedelta(days=1))
    assert summary is not None and summary.for_date == DAY
    assert verify.latest_summary(c, DAY) is None
    assert verify.summary_for(c, before) is None
    c.close()
