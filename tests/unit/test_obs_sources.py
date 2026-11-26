"""Normalize tests for the ground-truth sources: NWS PNS, IEM LSR, NOHRSC (J00 fixtures)."""

from __future__ import annotations

import gzip
import json
import struct
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from hazelst import config, db, jobs
from hazelst.sources import base, geotiff, iem, nohrsc, pns, snow_report
from tests.support import FIXTURES

MOUNTAINS = config.load_mountains(config.DEFAULT_MOUNTAINS)
PNS_IDS = {
    "BTV": "202602071335-KBTV-NOUS41-PNSBTV",
    "ALY": "202602071323-KALY-NOUS41-PNSALY",
    "GYX": "202602071650-KGYX-NOUS41-PNSGYX",
}
LSR_FEB = FIXTURES / "iem" / "lsr_btv_aly_gyx_2026-02-01_2026-02-08.geojson.gz"
NOHRSC_24H = FIXTURES / "nohrsc" / "sfav2_CONUS_24h_2026020712.tif.gz"
# Nearest-pixel values at each mountain, from tests/fixtures/nohrsc/README.md.
NOHRSC_EXPECTED = {
    "killington": 3.17,
    "pico": 2.55,
    "sugarbush": 1.90,
    "stratton": 2.14,
    "loon": 1.88,
    "cranmore": 1.00,
    "sunday_river": 0.49,
    "sugarloaf": 0.15,
}


def body(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def fetched(
    source: str,
    path: Path,
    meta: dict[str, object] | None = None,
    *,
    url: str = "https://example.test/",
    status: int = 200,
) -> base.Fetched:
    return base.Fetched(
        source, url, {}, status, "2026-11-26T14:18:00+00:00", 0, body(path), dict(meta or {})
    )


def archive_text(office: str) -> base.Fetched:
    pid = PNS_IDS[office]
    path = FIXTURES / "iem" / f"nwstext_{pid}.txt.gz"
    return fetched(pns.ARCHIVE_SOURCE, path, {"product_id": pid, "office": office})


# --- PNS ---------------------------------------------------------------------------


def test_pns_btv_snow_24() -> None:
    product = pns.normalize(archive_text("BTV"), MOUNTAINS)
    assert product.product_id == PNS_IDS["BTV"]
    assert product.issued_at == "2026-02-07T13:35:00+00:00"
    assert len(product.reports) == 38
    assert all(r.hours == 24 for r in product.reports)
    first = product.reports[0]
    assert (first.place, first.lat, first.lon, first.snow_in) == (
        "Lake Placid, NY",
        44.3042,
        -73.9839,
        3.0,
    )
    # 0700 AM EST is 12Z.
    assert first.ending_at == "2026-02-07T12:00:00+00:00"
    assert first.elev_ft is None


def test_pns_aly_storm_totals_have_no_hours() -> None:
    reports = pns.normalize(archive_text("ALY"), MOUNTAINS).reports
    assert len(reports) == 20
    storm = [r for r in reports if r.hours is None]
    daily = [r for r in reports if r.hours == 24]
    assert storm and daily
    assert reports[0].place == "3 SE Pittsfield, MA"
    assert reports[0].ending_at == "2026-02-07T11:11:00+00:00"


def test_pns_gyx_cleans_null_place_tokens() -> None:
    reports = pns.normalize(archive_text("GYX"), MOUNTAINS).reports
    assert len(reports) == 65
    places = {r.place for r in reports}
    assert "Portland Jetport, ME" in places
    assert not any("null" in p.lower() for p in places)
    conway = next(r for r in reports if r.place == "North Conway, NH")
    assert (conway.nearest_mountain, conway.distance_mi, conway.snow_in) == ("cranmore", 1.1, 1.0)


def test_pns_metadata_skips_other_types_and_bad_lines() -> None:
    head = ":2/07/2026,0700 AM, VT, Rutland,"
    text = "\n".join(
        [
            "Lake Placid    3.0 in",
            "**METADATA**",
            f"{head} Killington, , , 43.6, -72.8, SNOW_24, T, Inch, COOP, x,",
            f"{head} Rutland, , , 43.6, -72.97, RAIN, 0.5, Inch, COOP, x,",
            f"{head} Pico, , , 43.66, -72.84, SNOWDEPTH, 9, Inch, COOP, x,",
            f"{head} Bad, , , north, -72.8, SNOW, 2, Inch, COOP, x,",
            f"{head} Sherburne, Pass, , , 43.66, -72.83, SNOW, 2, Inch, x,",
            "$$",
            f"{head} After, , , 43.6, -72.8, SNOW, 4, Inch, COOP, x,",
        ]
    )
    rows = pns.parse_metadata("id", text, MOUNTAINS)
    assert [(r.place, r.snow_in, r.hours) for r in rows] == [
        ("Killington, VT", 0.0, 24),  # trace
        ("Sherburne, Pass, VT", 2.0, None),  # a comma inside the place name
    ]


def test_pns_without_metadata_yields_no_reports() -> None:
    path = FIXTURES / "nws_pns" / "product_PNSALY_2026-11-25_non_snow.json.gz"
    product = pns.normalize(fetched(pns.SOURCE, path, {"office": "ALY"}), MOUNTAINS)
    assert product.product_id == "202611251408-KALY-NOUS41-PNSALY"
    assert product.office == "ALY"
    assert product.reports == []
    assert "Public Information Statement" in product.text


def test_pns_live_listing_uses_afos_ids() -> None:
    aly = fetched(
        pns.LIST_SOURCE, FIXTURES / "nws_pns" / "products_PNS_ALY_list.json.gz", {"office": "ALY"}
    )
    listed = pns.parse_list(aly)
    assert [(p.product_id, p.issued_at, p.source) for p in listed] == [
        ("202611251408-KALY-NOUS41-PNSALY", "2026-11-25T14:08:00+00:00", pns.SOURCE)
    ]
    assert listed[0].url.startswith("https://api.weather.gov/products/89659c91")
    btv = fetched(
        pns.LIST_SOURCE,
        FIXTURES / "nws_pns" / "products_PNS_BTV_list_empty.json.gz",
        {"office": "BTV"},
    )
    assert pns.parse_list(btv) == []


def test_pns_archive_listing() -> None:
    path = FIXTURES / "iem" / "afos_list_PNSBTV_2026-02-07.json.gz"
    listed = pns.parse_list(fetched(pns.ARCHIVE_LIST_SOURCE, path, {"office": "BTV"}))
    assert [p.product_id for p in listed] == [
        "202602071335-KBTV-NOUS41-PNSBTV",
        "202602071620-KBTV-NOUS41-PNSBTV",
        "202602072203-KBTV-NOUS41-PNSBTV",
    ]
    assert listed[0].url == pns.ARCHIVE_TEXT_URL.format(product_id=listed[0].product_id)
    assert listed[0].source == pns.ARCHIVE_SOURCE


# --- LSR ---------------------------------------------------------------------------


def test_lsr_keeps_snow_reports_only() -> None:
    rows = iem.normalize_lsr(fetched(iem.LSR_SOURCE, LSR_FEB), MOUNTAINS)
    assert len(rows) == 212  # 216 features: 212 snow, 4 other types
    first = rows[0]
    assert first.product_id == "202602071456-KALY-NWUS51-LSRALY"
    assert (first.place, first.snow_in, first.hours) == ("5 NNW Londonderry, VT", 1.5, None)
    assert first.ending_at == "2026-02-07T13:15:00+00:00"
    assert (first.nearest_mountain, first.distance_mi) == ("stratton", 12.8)


def test_lsr_empty_collection() -> None:
    path = FIXTURES / "iem" / "lsr_btv_aly_gyx_2026-11-19_2026-11-26_empty.geojson.gz"
    assert iem.normalize_lsr(fetched(iem.LSR_SOURCE, path), MOUNTAINS) == []


def test_lsr_params_are_utc() -> None:
    start = datetime(2026, 2, 6, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    end = datetime(2026, 2, 8, 5, 0, tzinfo=UTC)
    assert iem.lsr_params(start, end) == {
        "wfos": "BTV,ALY,GYX",
        "sts": "202602060500",
        "ets": "202602080500",
    }


def test_pns_replaces_lsr_copy_of_the_same_report(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    lsr = iem.normalize_lsr(fetched(iem.LSR_SOURCE, LSR_FEB), MOUNTAINS)
    btv = pns.normalize(archive_text("BTV"), MOUNTAINS)
    with db.transaction(conn):
        # 212 snow reports; 9 repeat in a later LSR product with the same id.
        assert snow_report.store(conn, lsr, "lsr") == 203
        pns.store(conn, btv)
        # Re-storing the LSR changes nothing: a PNS row is never replaced by an LSR.
        assert snow_report.store(conn, lsr, "lsr") == 0
    count = conn.execute("SELECT COUNT(*) FROM snow_report").fetchone()[0]
    from_pns = conn.execute(
        "SELECT COUNT(*) FROM snow_report WHERE product_id = ?", (PNS_IDS["BTV"],)
    ).fetchone()[0]
    assert from_pns == 38
    assert count == 203  # every BTV PNS line was also in an LSR
    assert pns.known_ids(conn) == {PNS_IDS["BTV"]}


def test_nearest_mountain() -> None:
    assert snow_report.nearest_mountain(43.604, -72.82, MOUNTAINS) == ("killington", 0.0)
    assert snow_report.distance_mi(43.604, -72.82, 43.662, -72.842) == pytest.approx(4.2, abs=0.1)


# --- NOHRSC ------------------------------------------------------------------------


def nohrsc_grid(product: nohrsc.Product, status: int = 200) -> base.Fetched:
    end = date(2026, 2, 7)
    return base.Fetched(
        nohrsc.SOURCE,
        nohrsc.grid_url(product, end),
        {},
        status,
        "2026-11-26T14:20:11+00:00",
        0,
        body(NOHRSC_24H) if status == 200 else b"not found",
        {"product": product, "end": end.isoformat()},
    )


def test_nohrsc_values_at_each_mountain() -> None:
    grids: dict[nohrsc.Product, base.Fetched] = {
        "24h": nohrsc_grid("24h"),
        "48h": nohrsc_grid("48h", 404),
        "season": nohrsc_grid("season", 404),
    }
    rows = nohrsc.normalize(grids, MOUNTAINS)
    assert {r.mountain: r.new_24h_in for r in rows} == NOHRSC_EXPECTED
    killington = rows[0]
    assert killington.obs_date == "2026-02-07"
    assert killington.reported_at == "2026-02-07T12:00:00+00:00"
    assert killington.new_48h_in is None and killington.season_in is None
    assert killington.raw["pixel"] == [284, 1329]


def test_nohrsc_without_24h_grid_yields_nothing() -> None:
    assert nohrsc.normalize({"24h": nohrsc_grid("24h", 404)}, MOUNTAINS) == []


def test_nohrsc_urls() -> None:
    end = date(2026, 11, 25)
    base_url = "https://www.nohrsc.noaa.gov/snowfall_v2/data/202611/"
    assert nohrsc.grid_url("24h", end) == base_url + "sfav2_CONUS_24h_2026112512.tif"
    assert nohrsc.grid_url("48h", end) == base_url + "sfav2_CONUS_48h_2026112512.tif"
    assert nohrsc.grid_url("season", end) == base_url + "sfav2_CONUS_2026093012_to_2026112512.tif"
    assert nohrsc.season_start(date(2027, 2, 1)) == date(2026, 9, 30)


def test_nohrsc_fetch_schedule(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    day = date(2026, 2, 8)
    morning = datetime(2026, 2, 8, 11, 50, tzinfo=UTC)
    # At 6:50 AM the grid ending 12Z today is not out; yesterday's and the re-read are.
    assert nohrsc.ends_to_fetch(conn, day, morning) == [date(2026, 2, 7), date(2026, 1, 31)]
    # 9 PM ET (02Z next day): today's grid is out too.
    evening = datetime(2026, 2, 9, 2, 0, tzinfo=UTC)
    assert date(2026, 2, 8) in nohrsc.ends_to_fetch(conn, day, evening)

    first_read = replace(nohrsc_grid("24h"), fetched_at="2026-02-08T00:30:00+00:00")
    grids: dict[nohrsc.Product, base.Fetched] = {"24h": first_read}
    with db.transaction(conn):
        ids: dict[nohrsc.Product, int] = {"24h": base.store_fetch(conn, None, first_read)}
        assert nohrsc.store(conn, nohrsc.normalize(grids, MOUNTAINS), ids) == 8
    # Stored before the reprocessing window: skipped now, read once more after 7 days.
    assert not nohrsc.needs_fetch(conn, date(2026, 2, 7), morning)
    late = datetime(2026, 11, 26, 12, tzinfo=UTC)
    assert nohrsc.needs_fetch(conn, date(2026, 2, 7), late)
    with db.transaction(conn):
        reread = replace(first_read, fetched_at=late.isoformat())
        refetched: dict[nohrsc.Product, int] = {"24h": base.store_fetch(conn, None, reread)}
        nohrsc.store(conn, nohrsc.normalize(grids, MOUNTAINS), refetched)
    count = conn.execute("SELECT COUNT(*) FROM observation WHERE source = 'nohrsc'").fetchone()[0]
    assert count == 8  # replaced, not duplicated
    assert not nohrsc.needs_fetch(conn, date(2026, 2, 7), late)


# --- GeoTIFF -----------------------------------------------------------------------


def test_geotiff_rejects_other_files() -> None:
    with pytest.raises(geotiff.TiffError):
        geotiff.read(b"GIF89a")


def test_geotiff_uncompressed_grid() -> None:
    """A 2 x 2 big-endian float32 grid with no compression, built by hand."""
    pixels = struct.pack(">4f", 1.0, 2.0, -99999.0, 4.5)
    entries = [
        (256, 3, 1, 2),
        (257, 3, 1, 2),
        (258, 3, 1, 32),
        (259, 3, 1, 1),
        (273, 4, 1, 0),  # patched below
        (277, 3, 1, 1),
        (278, 3, 1, 2),
        (279, 4, 1, len(pixels)),
        (339, 3, 1, 3),
    ]
    doubles = {33550: (0.5, 0.5, 0.0), 33922: (0.0, 0.0, 0.0, -71.0, 44.0, 0.0)}
    nodata = b"-99999\0"
    n = len(entries) + len(doubles) + 1
    ifd_size = 2 + 12 * n + 4
    data_at = 8 + ifd_size
    blobs = b""
    ifd = struct.pack(">H", n)
    extra_at = data_at + len(pixels)
    for tag, kind, count, value in entries:
        if tag == 273:
            value = data_at
        packed = struct.pack(">H", value) + b"\0\0" if kind == 3 else struct.pack(">I", value)
        ifd += struct.pack(">HHI", tag, kind, count) + packed
    for tag, values in doubles.items():
        ifd += struct.pack(">HHII", tag, 12, len(values), extra_at + len(blobs))
        blobs += struct.pack(f">{len(values)}d", *values)
    ifd += struct.pack(">HHII", 42113, 2, len(nodata), extra_at + len(blobs))
    blobs += nodata
    tiff = b"MM\0*" + struct.pack(">I", 8) + ifd + b"\0\0\0\0" + pixels + blobs
    grid = geotiff.read(tiff)
    assert grid.sample(43.9, -70.9) == 1.0
    assert grid.sample(43.9, -70.4) == 2.0
    assert grid.sample(43.4, -70.9) is None  # nodata
    assert grid.sample(43.4, -70.4) == 4.5
    assert grid.sample(45.0, -70.9) is None  # outside


# --- ingest-obs --------------------------------------------------------------------


def replay(request: httpx.Request) -> httpx.Response:
    """Serve the J00 fixtures by path (and AFOS `pil`); 404 for anything else."""
    url = request.url
    path = f"{url.host}{url.path}"
    if path == "mesonet.agron.iastate.edu/geojson/lsr.php":
        return httpx.Response(200, content=body(LSR_FEB))
    if path == "mesonet.agron.iastate.edu/api/1/nws/afos/list.json":
        if url.params.get("pil") == "PNSBTV":
            return httpx.Response(
                200, content=body(FIXTURES / "iem" / "afos_list_PNSBTV_2026-02-07.json.gz")
            )
        return httpx.Response(200, content=json.dumps({"data": []}).encode())
    if path.startswith("mesonet.agron.iastate.edu/api/1/nwstext/"):
        pid = url.path.rsplit("/", 1)[-1]
        fixture = FIXTURES / "iem" / f"nwstext_{pid}.txt.gz"
        if fixture.exists():
            return httpx.Response(200, content=body(fixture))
    if str(url) == nohrsc.grid_url("24h", date(2026, 2, 7)):
        return httpx.Response(200, content=body(NOHRSC_24H))
    return httpx.Response(404, text="not found")


def test_ingest_obs_on_a_snow_day(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    client = httpx.Client(transport=httpx.MockTransport(replay))
    summary = jobs.run_job(cfg, "ingest-obs", date(2026, 2, 7), client=client)
    assert summary["pns"]["status"] == "ok"
    assert summary["pns"]["listing"]["BTV"] == "iem archive"
    assert summary["pns"]["products"] == 1  # 1335Z; the 1620Z and 2203Z texts are not fixtures
    assert summary["lsr"]["status"] == "ok"
    assert summary["nohrsc"]["status"] == "ok"
    assert summary["nohrsc"]["ends"]["2026-02-07"]["observation"] == 8

    conn = db.connect(cfg.settings.db_path)
    obs = dict(
        conn.execute(
            "SELECT mountain, new_24h_in FROM observation"
            " WHERE source = 'nohrsc' AND obs_date = '2026-02-07'"
        ).fetchall()
    )
    assert obs == NOHRSC_EXPECTED
    near = dict(
        conn.execute(
            "SELECT nearest_mountain, COUNT(*) FROM snow_report WHERE distance_mi <= 15"
            " GROUP BY nearest_mountain"
        ).fetchall()
    )
    assert set(near) >= {"killington", "sugarbush", "stratton"}
    reports = conn.execute("SELECT COUNT(*) FROM snow_report").fetchone()[0]

    # A second run stores no duplicates and skips grids already read.
    again = jobs.run_job(cfg, "ingest-obs", date(2026, 2, 7), client=client)
    assert again["pns"]["products"] == 0
    assert conn.execute("SELECT COUNT(*) FROM snow_report").fetchone()[0] == reports
    assert "2026-02-07" not in again["nohrsc"]["ends"]
    count = conn.execute("SELECT COUNT(*) FROM observation WHERE source = 'nohrsc'").fetchone()[0]
    assert count == 8
    conn.close()


def test_ingest_obs_fails_only_when_every_source_fails(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(jobs.JobError):
        jobs.run_job(cfg, "ingest-obs", date(2026, 2, 7), client=down)
