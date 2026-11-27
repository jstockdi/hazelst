"""SnoCountry resort snow history: normalize, upsert, verification order, ingest, backfill.

Fixtures: one real response per mountain, recorded 2026-11-26 with
`hazelst record snocountry --name 2026-11-26`.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from hazelst import config, db, jobs, verify
from hazelst.sources import base, snocountry
from tests.support import FIXTURES
from tests.unit.test_obs_sources import MOUNTAINS, replay
from tests.unit.test_verify import DAY, OBS, build_snow_day, insert_editions, insert_resort

FOLDER = FIXTURES / "snocountry" / "2026-11-26"
RECORDED = {p.stem.removesuffix(".json"): base.read_fixture(p) for p in FOLDER.glob("*.json.gz")}
SNOCOUNTRY_BODIES = {str(f.meta["resort_id"]): f.body for f in RECORDED.values()}

# Days with reported new snow per season (start year), read from each fixture's
# `snowarchive`; data-sources.md "SnoCountry" coverage.
SNOW_DAYS = {
    "killington": {2019: 42, 2020: 41, 2021: 53, 2022: 53, 2023: 45, 2024: 58, 2025: 62},
    "sugarbush": {2019: 39, 2021: 49, 2022: 44, 2023: 62, 2024: 60, 2025: 57},
    "sunday_river": {2019: 21, 2025: 23},
    "cranmore": {2019: 16, 2020: 14, 2021: 23, 2022: 24, 2023: 14, 2024: 20, 2025: 20},
}
INFERRED_ZEROS = {
    "killington": 753,
    "pico": 416,
    "sugarbush": 594,
    "stratton": 701,
    "loon": 709,
    "cranmore": 658,
    "sunday_river": 193,
    "sugarloaf": 855,
}


def rows_for(mountain: str) -> list[snocountry.SnoCountryObservation]:
    return snocountry.normalize(RECORDED[mountain])


def season(row: snocountry.SnoCountryObservation) -> int:
    return snocountry.season_of(date.fromisoformat(row.obs_date))


def doc_fetch(doc: dict[str, Any], mountain: str = "killington") -> base.Fetched:
    return base.Fetched(
        snocountry.SOURCE,
        snocountry.URL,
        {"resortId": "802007"},
        200,
        "2026-11-26T21:00:00+00:00",
        0,
        json.dumps(doc).encode(),
        {"mountain": mountain, "resort_id": "802007"},
    )


def history_doc(
    archive: list[tuple[str, str, str]], base_points: list[tuple[str, str]], **current: str
) -> dict[str, Any]:
    return {
        "snowarchive": [
            {"date_of_new_snow": d, "snowMin": lo, "snowMax": hi} for d, lo, hi in archive
        ],
        "jsonBaseDepth": json.dumps(
            {"titles": ["season"], "data": [[{"x": d, "y": y} for d, y in base_points]]}
        ),
        "timezone": "America/New_York",
        **current,
    }


# --- normalize ---------------------------------------------------------------------


def test_every_mountain_has_a_fixture_and_an_id() -> None:
    assert set(RECORDED) == {m.key for m in MOUNTAINS}
    for m in MOUNTAINS:
        assert RECORDED[m.key].params == {"resortId": m.snocountry_id}


@pytest.mark.parametrize(
    ("lo", "hi", "expected"),
    [
        ("3", "0", 3.0),  # a single value: snowMax is 0
        ("2", "6", 6.0),  # a range stores the max (the summit)
        ("4", "4", 4.0),
        ("0", "0", 0.0),  # a reported zero
        ("", "5", 5.0),
        ("", "", None),
        ("n/a", None, None),
    ],
)
def test_new_snow_range_takes_the_max(lo: str, hi: str | None, expected: float | None) -> None:
    assert snocountry.new_snow(lo, hi) == expected


@pytest.mark.parametrize("mountain", sorted(SNOW_DAYS))
def test_snow_days_per_season_match_the_source(mountain: str) -> None:
    rows = rows_for(mountain)
    archived = Counter(season(r) for r in rows if "snowarchive" in r.raw)
    assert dict(archived) == SNOW_DAYS[mountain]
    # Every archive entry became exactly one row.
    doc = RECORDED[mountain].json()
    assert len({e["date_of_new_snow"] for e in doc["snowarchive"]}) == sum(archived.values())


def test_rows_map_archive_and_base_depth() -> None:
    by_date = {r.obs_date: r for r in rows_for("killington")}
    snow = by_date["2026-04-07"]  # snowarchive {snowMin: 3, snowMax: 0}
    assert (snow.new_24h_in, snow.inferred_zero, snow.reported_at) == (3.0, False, None)
    assert snow.raw["snowarchive"] == {
        "date_of_new_snow": "2026-04-07",
        "snowMin": "3",
        "snowMax": "0",
    }
    dry = by_date["2025-11-12"]  # base 12 reported, no snowarchive entry
    assert (dry.new_24h_in, dry.base_in, dry.inferred_zero) == (0.0, 12.0, True)
    assert by_date["2025-11-11"].new_24h_in == 2.0
    old = by_date["2012-11-04"]  # base depth only, years before the archive
    assert (old.new_24h_in, old.base_in, old.inferred_zero) == (None, 20.0, False)


@pytest.mark.parametrize("mountain", sorted(INFERRED_ZEROS))
def test_inferred_zeros_only_on_base_depth_dates(mountain: str) -> None:
    rows = rows_for(mountain)
    zeros = [r for r in rows if r.inferred_zero]
    assert len(zeros) == INFERRED_ZEROS[mountain]
    archived_seasons = {season(r) for r in rows if "snowarchive" in r.raw}
    for r in zeros:
        assert r.new_24h_in == 0.0
        assert "base_depth" in r.raw and "snowarchive" not in r.raw
        assert season(r) in archived_seasons
    # A base-only date outside the archived seasons is never scored as zero.
    for r in rows:
        if (
            "snowarchive" not in r.raw
            and season(r) not in archived_seasons
            and not r.raw.get("current")
        ):
            assert r.new_24h_in is None


def test_missing_archive_season_has_no_zeros() -> None:
    sugarbush = [r for r in rows_for("sugarbush") if season(r) == 2020]
    assert sugarbush and all(r.new_24h_in is None for r in sugarbush)
    sunday_river = {season(r) for r in rows_for("sunday_river") if r.inferred_zero}
    assert sunday_river == {2019, 2025}


def test_current_report_row() -> None:
    today = rows_for("killington")[-1]
    assert today.obs_date == "2026-11-26"
    assert today.reported_at == "2026-11-26T16:41:00-05:00"
    assert (today.new_24h_in, today.base_in, today.season_in) == (None, 20.0, 5.0)
    assert (today.trails_open, today.trails_total, today.lifts_open) == (3, 155, 2)
    # Cranmore's current report has neither new snow nor a base depth: no row.
    assert rows_for("cranmore")[-1].obs_date == "2026-04-04"


def test_current_report_fills_but_never_replaces_reported_history() -> None:
    doc = history_doc(
        [("2026-01-10", "4", "8")],
        [("2026-01-09", "20"), ("2026-01-10", "24"), ("2026-01-11", "24")],
        reportDateTime="2026-01-11 07:30:00",
        newSnowMin="2",
        newSnowMax="",
        avgBaseDepthMin="20",
        avgBaseDepthMax="26",
    )
    rows = {r.obs_date: r for r in snocountry.normalize(doc_fetch(doc))}
    assert rows["2026-01-10"].new_24h_in == 8.0
    assert rows["2026-01-09"].inferred_zero
    jan11 = rows["2026-01-11"]  # history infers zero; the current report says 2
    assert (jan11.new_24h_in, jan11.inferred_zero, jan11.base_in) == (2.0, False, 24.0)
    doc["reportDateTime"] = "2026-01-10 15:00:00"  # history already reports this date
    rows = {r.obs_date: r for r in snocountry.normalize(doc_fetch(doc))}
    assert rows["2026-01-10"].new_24h_in == 8.0 and rows["2026-01-10"].reported_at is None


def test_bad_documents_raise() -> None:
    with pytest.raises(snocountry.NormalizeError):
        snocountry.normalize(doc_fetch({"error": "not found"}))
    bad = doc_fetch({"snowarchive": []})
    with pytest.raises(snocountry.NormalizeError):
        snocountry.normalize(
            base.Fetched(bad.source, bad.url, bad.params, 404, bad.fetched_at, 0, b"", bad.meta)
        )


# --- store ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "s.db")
    db.migrate(c)
    return c


def store_fixture(c: sqlite3.Connection, mountain: str) -> int:
    fetched = RECORDED[mountain]
    with db.transaction(c):
        fetch_id = base.store_fetch(c, None, fetched)
        return snocountry.store(c, snocountry.normalize(fetched), fetch_id)


def count(c: sqlite3.Connection, mountain: str | None = None) -> int:
    sql = "SELECT count(*) FROM observation WHERE source = 'snocountry'"
    if mountain is None:
        return int(c.execute(sql).fetchone()[0])
    return int(c.execute(sql + " AND mountain = ?", (mountain,)).fetchone()[0])


def test_upsert_is_idempotent(conn: sqlite3.Connection) -> None:
    written = store_fixture(conn, "killington")
    assert written == count(conn, "killington") == len(rows_for("killington"))
    before = conn.execute(
        "SELECT obs_date, new_24h_in, base_in, raw FROM observation ORDER BY obs_date"
    ).fetchall()
    store_fixture(conn, "killington")
    assert count(conn, "killington") == written
    after = conn.execute(
        "SELECT obs_date, new_24h_in, base_in, raw FROM observation ORDER BY obs_date"
    ).fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_upsert_replaces_values_but_not_with_an_inferred_zero(conn: sqlite3.Connection) -> None:
    doc = history_doc([("2026-01-10", "4", "0")], [("2026-01-10", "20"), ("2026-01-11", "20")])
    with db.transaction(conn):
        snocountry.store(conn, snocountry.normalize(doc_fetch(doc)), 1)
    # The source revises Jan 11 to 3 in and drops Jan 10 from the archive.
    doc = history_doc([("2026-01-11", "3", "0")], [("2026-01-10", "20"), ("2026-01-11", "20")])
    with db.transaction(conn):
        snocountry.store(conn, snocountry.normalize(doc_fetch(doc)), 2)
    rows = dict(
        conn.execute("SELECT obs_date, new_24h_in FROM observation ORDER BY obs_date").fetchall()
    )
    assert rows == {"2026-01-10": 4.0, "2026-01-11": 3.0}
    assert count(conn) == 2


def test_unique_index_only_covers_snocountry(conn: sqlite3.Connection) -> None:
    for _ in range(2):
        insert_resort(conn, "killington", "2026-02-07", "2026-02-07T11:30:00+00:00", 3.0)
    with pytest.raises(sqlite3.IntegrityError):
        for _ in range(2):
            conn.execute(
                "INSERT INTO observation (mountain, source, obs_date) VALUES"
                " ('killington', 'snocountry', '2026-02-07')"
            )


# --- verification order ----------------------------------------------------------------


def insert_snocountry(
    c: sqlite3.Connection, mountain: str, new_24h: float | None, *, inferred: bool = False
) -> None:
    row = snocountry.SnoCountryObservation(
        mountain=mountain,
        obs_date=OBS.isoformat(),
        reported_at=None,
        new_24h_in=new_24h,
        base_in=30.0,
        season_in=None,
        trails_open=None,
        trails_total=None,
        lifts_open=None,
        raw={"inferred_zero": True} if inferred else {},
    )
    snocountry.store(c, [row], 0)


def test_verification_order_resort_snocountry_points_nohrsc(
    snow_day: sqlite3.Connection,
) -> None:
    with db.transaction(snow_day):
        insert_snocountry(snow_day, "killington", 7.0)  # resort 3.0 still wins
        insert_snocountry(snow_day, "stratton", 4.0)  # beats the LSR (1.9)
        insert_snocountry(snow_day, "cranmore", 2.0)  # beats the PNS (1.3)
        insert_snocountry(snow_day, "loon", 5.0)  # beats NOHRSC (1.88)
        insert_snocountry(snow_day, "sugarloaf", 0.0, inferred=True)  # a zero day counts
        insert_snocountry(snow_day, "sunday_river", None)  # base only: falls through
    found = {m.key: verify.observed(snow_day, m.key, DAY) for m in MOUNTAINS}
    assert {k: (o.snow_in, o.source) for k, o in found.items() if o} == {
        "killington": (3.0, "resort"),
        "pico": (2.55, "nohrsc"),
        "sugarbush": (2.5, "resort_llm"),
        "stratton": (4.0, "snocountry"),
        "loon": (5.0, "snocountry"),
        "cranmore": (2.0, "snocountry"),
        "sunday_river": (0.49, "nohrsc"),
        "sugarloaf": (0.0, "snocountry"),
    }


@pytest.fixture
def snow_day(tmp_path: Path) -> sqlite3.Connection:
    return build_snow_day(tmp_path)


# --- jobs ------------------------------------------------------------------------------


def serve(request: httpx.Request, *, down: frozenset[str] = frozenset()) -> httpx.Response:
    url = str(request.url.copy_with(query=None))
    resort = request.url.params.get("resortId", "")
    if url == snocountry.URL and resort not in down:
        return httpx.Response(200, content=SNOCOUNTRY_BODIES[resort])
    return replay(request)


def test_ingest_obs_adds_snocountry(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    client = httpx.Client(transport=httpx.MockTransport(serve))
    summary = jobs.run_job(cfg, "ingest-obs", OBS, client=client)
    assert summary["snocountry"]["status"] == "ok"
    assert summary["snocountry"]["ok"] == 8
    assert summary["snocountry"]["mountains"]["killington"]["latest"] == "2026-11-26"
    c = db.connect(cfg.settings.db_path)
    stored = count(c)
    assert stored == sum(len(rows_for(m)) for m in RECORDED)
    jobs.run_job(cfg, "ingest-obs", OBS, client=client)
    assert count(c) == stored  # a second run upserts
    c.close()


def test_snocountry_failure_degrades_only_that_source(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    # Killington's primary URL fails; the fallback serves it. Pico fails on both.
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        resort = request.url.params.get("resortId", "")
        calls.append(f"{url}?{resort}")
        if url == snocountry.FALLBACK_URL and resort == "802007":
            return httpx.Response(200, content=SNOCOUNTRY_BODIES[resort])
        return serve(request, down=frozenset({"802007", "802014"}))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    summary = jobs.run_job(cfg, "ingest-obs", OBS, client=client)
    sc = summary["snocountry"]
    assert sc["status"] == "ok" and sc["ok"] == 7
    assert sc["mountains"]["killington"]["fallback"] is True
    assert sc["mountains"]["pico"]["status"] == "failed"
    assert summary["nohrsc"]["status"] == "ok"

    down = httpx.Client(
        transport=httpx.MockTransport(lambda r: serve(r, down=frozenset(SNOCOUNTRY_BODIES)))
    )
    summary = jobs.run_job(cfg, "ingest-obs", OBS, client=down)
    assert summary["snocountry"]["status"] == "failed"
    assert summary["nohrsc"]["status"] == "ok"


def test_backfill_snocountry_only_loads_each_mountain_once(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return serve(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    options = {"start": date(2019, 11, 1), "end": date(2019, 11, 30), "steps": ("snocountry",)}
    first = jobs.run_job(cfg, "season-backfill", date(2019, 11, 1), client=client, options=options)
    assert len(sent) == 8 and all(s.startswith(snocountry.URL) for s in sent)
    assert set(first) & {"previous_runs", "lsr", "pns", "nohrsc"} == set()
    assert first["snocountry"]["rows"]["inferred_zero"] == sum(INFERRED_ZEROS.values())
    c = db.connect(cfg.settings.db_path)
    stored = count(c)
    # The next month (and a rerun) sends nothing and changes nothing.
    options = {"start": date(2019, 12, 1), "end": date(2019, 12, 31), "steps": ("snocountry",)}
    second = jobs.run_job(cfg, "season-backfill", date(2019, 12, 1), client=client, options=options)
    assert len(sent) == 8
    assert second["snocountry"]["cached"] == 8 and second["snocountry"]["rows"] == {}
    assert count(c) == stored
    c.close()


def test_forecast_verify_range_rescores_with_snocountry(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    c = db.connect(cfg.settings.db_path)
    db.migrate(c)
    with db.transaction(c):
        insert_editions(c, DAY)
    first = jobs.run_job(cfg, "forecast-verify", DAY, options={"start": DAY, "end": DAY})
    assert first["rescored"]["model_mean_observed_source"] == {"none": 24}
    with db.transaction(c):
        for m in MOUNTAINS:
            insert_snocountry(c, m.key, 2.0)
    second = jobs.run_job(
        cfg, "forecast-verify", OBS, options={"start": date(2026, 2, 1), "end": OBS}
    )
    assert second["rescored"]["days"] == 1
    assert second["rescored"]["model_mean_observed_source"] == {"snocountry": 24}
    sources = c.execute(
        "SELECT DISTINCT observed_source FROM verification WHERE for_date = ?", (DAY.isoformat(),)
    ).fetchall()
    assert [r[0] for r in sources] == ["snocountry"]
    assert "nohrsc" not in second  # a range re-score fetches nothing
    c.close()


def test_cli_backfill_with_sources_is_one_run_and_rerun_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hazelst import cli

    monkeypatch.setenv("HAZELST_DB", str(tmp_path / "c.db"))
    monkeypatch.setattr(
        base, "make_client", lambda *a, **k: httpx.Client(transport=httpx.MockTransport(serve))
    )
    argv = ["season-backfill", "--from", "2019-11-01", "--to", "2026-04-30"]
    argv += ["--sources", "snocountry", "--no-learn"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.rindex("\n{") :])["months"] == 1
    c = db.connect(tmp_path / "c.db")
    stored = count(c)
    assert cli.main(argv) == 0
    assert count(c) == stored
    assert cli.main(["forecast-verify", "--from", "2024-01-20", "--to", "2024-01-31"]) == 0
    assert '"days": 0' in capsys.readouterr().out
    c.close()
