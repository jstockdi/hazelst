import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hazelst import config, db, jobs
from hazelst.sources import base, discussions, nws
from tests.support import FIXTURES, load_fixtures, replay_client

DAY = "2026-11-26"
PROTOTYPE_RUN = config.PROJECT_ROOT / "prototype" / "out" / "2026-11-25.json"


@pytest.fixture(scope="module")
def recorded() -> list[base.Fetched]:
    return load_fixtures(FIXTURES / "nws" / DAY)


@pytest.fixture(scope="module")
def afds() -> list[base.Fetched]:
    return load_fixtures(FIXTURES / "discussions" / DAY)


def pick(recorded: list[base.Fetched], label: str, module: object = nws) -> base.Fetched:
    return next(f for f in recorded if module.label(f) == label)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("valid_time", "start", "hours"),
    [
        ("2026-11-25T12:00:00+00:00/PT6H", datetime(2026, 11, 25, 12, tzinfo=UTC), 6),
        ("2026-11-25T12:00:00+00:00/P1D", datetime(2026, 11, 25, 12, tzinfo=UTC), 24),
        ("2026-11-25T12:00:00+00:00/P1DT6H", datetime(2026, 11, 25, 12, tzinfo=UTC), 30),
        ("2026-11-25T07:00:00-05:00/PT1H", datetime(2026, 11, 25, 12, tzinfo=UTC), 1),
    ],
)
def test_parse_interval(valid_time: str, start: datetime, hours: int) -> None:
    assert nws.parse_interval(valid_time) == (start, hours)


def test_parse_interval_rejects_garbage() -> None:
    with pytest.raises(nws.NormalizeError):
        nws.parse_interval("2026-11-25T12:00:00+00:00/6 hours")


def test_parse_points(recorded: list[base.Fetched]) -> None:
    assert nws.parse_points(pick(recorded, "points-killington")).startswith("BTV/")
    assert nws.parse_points(pick(recorded, "points-stratton")).startswith("ALY/")
    assert nws.parse_points(pick(recorded, "points-sugarloaf")).startswith("GYX/")


def test_normalize_grid(recorded: list[base.Fetched]) -> None:
    rows = nws.normalize_grid(pick(recorded, "grid-killington"))
    layers = {r.layer for r in rows}
    assert layers == set(nws.LAYERS)
    units = {r.layer: r.unit for r in rows}
    assert units == {"snowfallAmount": "mm", "iceAccumulation": "mm", "windGust": "km_h-1"}
    assert all(r.valid_start.endswith("+00:00") and r.hours >= 1 for r in rows)
    snow = [r for r in rows if r.layer == "snowfallAmount"]
    # Snow and ice grids end after about three days (decisions row 5).
    first = datetime.fromisoformat(snow[0].valid_start)
    last = max(datetime.fromisoformat(r.valid_start) for r in snow)
    assert 2 <= (last - first).days <= 5
    # Intervals within a layer do not overlap.
    starts = [(datetime.fromisoformat(r.valid_start), r.hours) for r in snow]
    for (a, h), (b, _) in itertools.pairwise(starts):
        assert (b - a).total_seconds() >= h * 3600


def test_normalize_alerts(recorded: list[base.Fetched]) -> None:
    rows = nws.normalize_alerts(pick(recorded, "alerts-stratton"))
    assert sorted(r.event for r in rows) == ["Wind Advisory", "Winter Weather Advisory"]
    assert all(r.mountain == "stratton" and r.id.startswith("urn:oid:") for r in rows)
    assert all(r.expires and r.onset for r in rows)


def test_normalize_discussion(afds: list[base.Fetched]) -> None:
    row = discussions.normalize(pick(afds, "afd-BTV", discussions))
    assert row.office == "BTV"
    assert row.kind == "AFD"
    assert row.issued_at.startswith("2026-11-26")
    assert "Area Forecast Discussion" in row.text


def test_summary_keeps_forecaster_sections(afds: list[base.Fetched]) -> None:
    text = discussions.normalize(pick(afds, "afd-BTV", discussions)).text
    out = discussions.summary(text)
    assert ".KEY MESSAGES..." in out
    assert ".DISCUSSION..." in out
    assert ".AVIATION" not in out
    assert all("  " not in para and "\n" not in para for para in out.split("\n\n"))


def test_normalize_whitespace_joins_wrapped_lines() -> None:
    text = "A low will travel northward through the \neastern  United States.\n\n\nNext   para."
    assert discussions.normalize_whitespace(text) == (
        "A low will travel northward through the eastern United States.\n\nNext para."
    )


def test_storm_words_on_wrapped_afd() -> None:
    """The 2026-11-25 BTV AFD wraps "through the\\neastern"; the prototype missed it."""
    run = json.loads(PROTOTYPE_RUN.read_text())
    btv = next(d["text"] for d in run["discussions"] if d["office"] == "BTV")
    assert "northward through the \neastern" in btv
    words = discussions.storm_words(btv)
    assert "inland_runner" in words
    assert "freezing_rain" in words
    assert "low_level_jet" in words
    prototype = {w["tag"] for w in run["storm_words"] if "BTV" in w["offices"]}
    assert "inland_runner" not in prototype  # the miss this test guards against


def test_storm_words_none() -> None:
    assert discussions.storm_words("High pressure keeps quiet weather in place.") == []


def all_fixtures() -> list[base.Fetched]:
    return [
        *load_fixtures(FIXTURES / "open_meteo" / DAY),
        *load_fixtures(FIXTURES / "nws" / DAY),
        *load_fixtures(FIXTURES / "discussions" / DAY),
    ]


def test_ingest_forecast_stores_nws_and_discussions(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    recorded = all_fixtures()
    summary = jobs.run_job(cfg, "ingest-forecast", client=replay_client(recorded))
    assert summary["nws"]["status"] == "ok", summary["nws"]
    assert summary["nws"]["errors"] == {}
    assert summary["discussions"]["status"] == "ok"
    assert set(summary["discussions"]["discussion"]) == {"BTV", "ALY", "GYX"}
    conn = db.connect(cfg.settings.db_path)
    grids = nws.cached_grids(conn)
    assert len(grids) == 8
    per_mountain = conn.execute(
        "SELECT count(DISTINCT mountain) FROM nws_grid WHERE layer = 'snowfallAmount'"
    ).fetchone()[0]
    assert per_mountain == 8
    assert conn.execute("SELECT count(*) FROM discussion").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM alert").fetchone()[0] > 0

    # Second run: grids come from the cache (no points requests); alerts keep first_seen_run.
    first_run = summary["run_id"]
    again = jobs.run_job(cfg, "ingest-forecast", client=replay_client(recorded))
    points = conn.execute(
        "SELECT count(*) FROM fetch WHERE source = 'nws_points' AND run_id = ?",
        (again["run_id"],),
    ).fetchone()[0]
    assert points == 0
    seen = {r[0] for r in conn.execute("SELECT first_seen_run FROM alert")}
    assert seen == {first_run}
    assert conn.execute("SELECT count(*) FROM discussion").fetchone()[0] == 3


def test_one_mountain_failing_does_not_fail_nws(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    recorded = [f for f in all_fixtures() if f.meta.get("mountain") != "loon"]
    summary = jobs.run_job(cfg, "ingest-forecast", client=replay_client(recorded))
    assert summary["nws"]["status"] == "ok"
    assert set(summary["nws"]["errors"]) == {"loon"}
