"""GHCN-Daily stations: the station list, row mapping, storage, verification, and summary.

The fixture is one recorded NCEI response (2026-02-06..08) for five stations:
North Conway co-op and a CoCoRaHS gauge near Cranmore, Bondville near
Stratton, Lonesome Lake Hut near Loon, and Mount Washington.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest

from hazelst import config, db, jobs, stations, verify
from hazelst.sources import base, ghcn, snow_report
from tests.support import FIXTURES
from tests.unit.test_backfill import ghcn_body
from tests.unit.test_verify import insert_editions

GHCN_FEB = FIXTURES / "ghcn" / "2026-02-06.json.gz"
DAY = date(2026, 2, 6)  # report day; its 24 h ends the morning of Feb 7
CFG = config.load({})
MOUNTAINS = CFG.mountains
STATIONS = ghcn.load_stations(ghcn.stations_path(CFG.settings))
BY_ID = ghcn.nearest(STATIONS)


def rows() -> list[snow_report.SnowReportRow]:
    return ghcn.normalize(base.read_fixture(GHCN_FEB), BY_ID)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "g.db")
    db.migrate(c)
    return c


def test_station_list_covers_every_mountain() -> None:
    per_mountain = {m.key: [s for s in STATIONS if s.mountain == m.key] for m in MOUNTAINS}
    assert all(per_mountain.values())
    assert all(s.distance_mi <= 20 for s in STATIONS if s.id != "USW00014755")
    washington = {s.mountain for s in STATIONS if s.id == "USW00014755"}
    assert washington == {"loon", "cranmore", "sunday_river", "sugarloaf"}
    assert all(s.elev_ft is not None for s in STATIONS)
    # A station near Killington and Pico has one entry each; `snow_report` uses the nearer.
    shared = [s for s in STATIONS if s.mountain in ("killington", "pico")]
    assert len({s.id for s in shared}) < len(shared)
    assert BY_ID["USW00014755"].mountain == "cranmore"


def test_row_mapping() -> None:
    found = {r.id: r for r in rows()}
    assert len(found) == 15  # 5 stations x 3 days, each with SNOW
    r = found["ghcn:USC00274810:2026-02-07"]
    assert (r.product_id, r.place, r.snow_in, r.hours) == (
        "GHCN",
        "LONESOME LAKE HUT, NH",
        4.2,
        24,
    )
    assert (r.elev_ft, r.nearest_mountain, r.distance_mi) == (2746, "loon", 8.2)
    assert (r.lat, r.lon) == (BY_ID["USC00274810"].lat, BY_ID["USC00274810"].lon)


def test_ending_at_per_station_type() -> None:
    found = {r.id: r.ending_at for r in rows()}
    # Co-op and CoCoRaHS: 7 AM EST on the date.
    assert found["ghcn:USC00275995:2026-02-07"] == "2026-02-07T12:00:00+00:00"
    assert found["ghcn:US1NHCR0046:2026-02-07"] == "2026-02-07T12:00:00+00:00"
    # Mount Washington (first-order): the calendar day, ending at midnight after it.
    assert found["ghcn:USW00014755:2026-02-07"] == "2026-02-08T05:00:00+00:00"


def test_missing_and_bad_values_are_skipped() -> None:
    body = (
        b'[{"DATE":"2026-02-07","STATION":"USC00275995","SNWD":"16.0"},'
        b'{"DATE":"2026-02-07","STATION":"USC00274810","SNOW":"T"},'
        b'{"DATE":"2026-02-07","STATION":"USC99999999","SNOW":"2.0"},'
        b'{"DATE":"2026-02-07","STATION":"US1NHCR0046","SNOW":"0.0"}]'
    )
    fetched = base.Fetched(ghcn.SOURCE, ghcn.URL, {}, 200, "", 0, body)
    assert [(r.id, r.snow_in) for r in ghcn.normalize(fetched, BY_ID)] == [
        ("ghcn:US1NHCR0046:2026-02-07", 0.0)
    ]
    with pytest.raises(ghcn.NormalizeError):
        ghcn.normalize(base.Fetched(ghcn.SOURCE, ghcn.URL, {}, 200, "", 0, b"{}"), BY_ID)


def test_batches_stay_under_the_station_limit() -> None:
    got = list(ghcn.batches(BY_ID))
    assert all(len(b) <= ghcn.BATCH for b in got)
    assert sorted(i for b in got for i in b) == sorted(BY_ID)


def test_rerun_leaves_counts_unchanged(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        ghcn.store(conn, rows())
    first = conn.execute("SELECT count(*), sum(snow_in) FROM snow_report").fetchone()
    with db.transaction(conn):
        ghcn.store(conn, rows())
    assert conn.execute("SELECT count(*), sum(snow_in) FROM snow_report").fetchone() == first
    assert first[0] == 15


def test_ghcn_is_context_never_truth(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        ghcn.store(conn, rows())
        insert_editions(conn, DAY)
    assert all(verify.observed(conn, m.key, DAY) is None for m in MOUNTAINS)
    by_key = {m.key: m for m in MOUNTAINS}
    cranmore = verify.context(conn, by_key["cranmore"], DAY)
    assert [(c.source, c.place, c.snow_in, c.elev_ft, c.distance_mi) for c in cranmore] == [
        ("ghcn", "N CONWAY, NH", 1.0, 522, 0.9),
        ("ghcn", "NORTH CONWAY 1.8 SSE, NH", 1.5, 561, 1.8),
    ]
    # Lonesome Lake Hut (2,746 ft) is 8.2 mi from Loon: listed beside the score.
    (hut,) = verify.context(conn, by_key["loon"], DAY)
    assert (hut.place, hut.snow_in, hut.elev_ft, hut.distance_mi) == (
        "LONESOME LAKE HUT, NH",
        4.2,
        2746,
        8.2,
    )
    scored = verify.verify_date(conn, MOUNTAINS, DAY)
    assert {r.result for r in scored} == {"NO_OBS"}
    stratton = next(r for r in scored if r.mountain == "stratton")
    assert json.loads(stratton.context)[0]["place"] == "BONDVILLE 2.4 WNW, VT"


def test_ghcn_and_a_pns_copy_are_both_listed(conn: sqlite3.Connection) -> None:
    pns_copy = snow_report.make_row(
        product_id="202602071335-KGYX-NOUS41-PNSGYX",
        place="North Conway 1.8 SSE, NH",
        lat=BY_ID["US1NHCR0046"].lat,
        lon=BY_ID["US1NHCR0046"].lon,
        ending_at=ghcn.ending_at("US1NHCR0046", date(2026, 2, 7), config.TIMEZONE),
        snow_in=1.5,
        hours=24,
        mountains=MOUNTAINS,
    )
    with db.transaction(conn):
        snow_report.store(conn, [pns_copy], "pns")
        ghcn.store(conn, rows())
    cranmore = next(m for m in MOUNTAINS if m.key == "cranmore")
    assert verify.observed(conn, "cranmore", DAY) is None
    found = [(c.source, c.snow_in) for c in verify.context(conn, cranmore, DAY)]
    assert ("ghcn", 1.5) in found and ("pns", 1.5) in found


def test_calendar_day_station_is_outside_the_window(conn: sqlite3.Connection) -> None:
    """Mount Washington's day ends at midnight, outside the 4-10 AM window, even if near."""
    near = {"USW00014755": ghcn.Station(**{**BY_ID["USW00014755"].__dict__, "distance_mi": 1.0})}
    with db.transaction(conn):
        ghcn.store(conn, ghcn.normalize(base.read_fixture(GHCN_FEB), near))
    cranmore = next(m for m in MOUNTAINS if m.key == "cranmore")
    assert all(verify.context(conn, cranmore, d) == [] for d in (DAY, date(2026, 2, 7)))


def test_ingest_obs_reads_the_last_10_days(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    seen: list[dict[str, str]] = []

    def web(request: httpx.Request) -> httpx.Response:
        if str(request.url.copy_with(query=None)) == ghcn.URL:
            query = dict(request.url.params.multi_items())
            seen.append(query)
            return httpx.Response(200, json=ghcn_body(query))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(web))
    summary = jobs.run_job(cfg, "ingest-obs", date(2026, 2, 8), client=client)
    assert summary["ghcn"]["status"] == "ok"
    assert (summary["ghcn"]["from"], summary["ghcn"]["to"]) == ("2026-01-30", "2026-02-08")
    assert summary["ghcn"]["batches"] == len(seen) == 3
    assert summary["ghcn"]["snow_report"] == 15
    again = jobs.run_job(cfg, "ingest-obs", date(2026, 2, 8), client=client)
    assert again["ghcn"]["reports"] == 15
    c = db.connect(cfg.settings.db_path)
    assert c.execute("SELECT count(*) FROM snow_report").fetchone()[0] == 15
    c.close()


def test_stations_summary(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        ghcn.store(conn, rows())
    out = stations.summary(conn, MOUNTAINS, STATIONS, 2025)
    assert [r.mountain for r in out] == [m.key for m in MOUNTAINS]
    cranmore = next(r for r in out if r.mountain == "cranmore")
    assert cranmore.snocountry_in is None
    elevations = [s.elev_ft or 0 for s in cranmore.stations]
    assert elevations == sorted(elevations, reverse=True)
    top = cranmore.stations[0]
    assert (top.id, top.season_in, top.days) == ("USW00014755", 3.2, 3)
    conway = next(s for s in cranmore.stations if s.id == "USC00275995")
    assert (conway.season_in, conway.days) == (1.0, 3)
    text = stations.format_text(out, 2025)
    assert "Cranmore 2025-26: SnoCountry no rows" in text
    assert "MT WASHINGTON, NH (USW00014755)" in text
    assert stations.summary(conn, MOUNTAINS, STATIONS, 2024)[0].stations[0].days == 0


def test_stations_summary_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hazelst import cli

    monkeypatch.setenv("HAZELST_DB", str(tmp_path / "c.db"))
    assert cli.main(["stations", "summary", "--season", "2025"]) == 0
    out = capsys.readouterr().out
    assert all(f"{m.name} 2025-26: SnoCountry" in out for m in MOUNTAINS)


def test_backfill_sources_flag() -> None:
    from hazelst import cli

    args = cli.build_parser().parse_args(
        ["season-backfill", "--from", "2024-01-20", "--to", "2024-01-31", "--sources", "ghcn"]
    )
    assert args.steps == "ghcn"
