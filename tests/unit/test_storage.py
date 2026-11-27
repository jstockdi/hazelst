"""Past hours (J30, decisions row 31): an ingest stores hours from 6 AM local on its date.

Each reader of `forecast_hourly` must give the same result as when every
hour the API returns is stored: report-build (derive, `resort_ratio`, the
finer models' day 1) for the morning and afternoon editions, skill-learn's
model forecasts, and the snow ratio's lead days.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hazelst import config, db, derive, fine, jobs, learn, ratio
from hazelst.ratio import MEAN_MODEL
from hazelst.sources import open_meteo
from tests.support import FIXTURES, load_fixtures, replay_client
from tests.unit.test_fine import flat

DAY = date(2026, 11, 27)
FIRST_HOUR = "2026-11-27T11:00:00+00:00"  # 6 AM EST
EDITIONS = (
    datetime(2026, 11, 27, 12, 0, tzinfo=UTC),  # 0700
    datetime(2026, 11, 27, 19, 40, tzinfo=UTC),  # 1440
)


def ingest(tmp_path: Path, name: str, *, keep_all: bool) -> sqlite3.Connection:
    (tmp_path / name).mkdir()
    cfg = config.load({"HAZELST_DB": str(tmp_path / name / "h.db")})
    with pytest.MonkeyPatch.context() as mp:
        if keep_all:  # every hour the API returned, as before J30
            mp.setattr(jobs, "keep_from", lambda for_date, tz: "")
        jobs.run_job(
            cfg,
            "ingest-forecast",
            DAY,
            client=replay_client(load_fixtures(FIXTURES / "open_meteo" / "2026-11-27")),
        )
    conn = db.connect(cfg.settings.db_path)
    rows = [r for m in (MEAN_MODEL, *open_meteo.FINE_MODELS) for r in flat(m, 14.0)]
    with db.transaction(conn):
        ratio.store(conn, rows)
    return conn


@pytest.fixture(scope="module")
def dbs(tmp_path_factory: pytest.TempPathFactory) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    tmp = tmp_path_factory.mktemp("storage")
    return ingest(tmp, "all", keep_all=True), ingest(tmp, "kept", keep_all=False)


def count(conn: sqlite3.Connection, where: str = "1") -> int:
    return int(conn.execute(f"SELECT count(*) FROM forecast_hourly WHERE {where}").fetchone()[0])


def test_ingest_stores_hours_from_6_am_on_its_date(
    dbs: tuple[sqlite3.Connection, sqlite3.Connection],
) -> None:
    full, kept = dbs
    assert jobs.keep_from(DAY, config.TIMEZONE) == FIRST_HOUR
    assert kept.execute("SELECT min(valid_at) FROM forecast_hourly").fetchone()[0] == FIRST_HOUR
    assert count(kept) == count(full, f"valid_at >= '{FIRST_HOUR}'")
    assert count(kept) < 0.6 * count(full)
    # The raw responses keep every hour.
    assert [tuple(r) for r in kept.execute("SELECT body_sha256 FROM fetch ORDER BY id")] == [
        tuple(r) for r in full.execute("SELECT body_sha256 FROM fetch ORDER BY id")
    ]


@pytest.mark.parametrize("as_of", EDITIONS, ids=["0700", "1440"])
def test_report_inputs_are_unchanged(
    dbs: tuple[sqlite3.Connection, sqlite3.Connection], as_of: datetime
) -> None:
    out = []
    for conn in dbs:
        run_id = derive.latest_ingest_run(conn)
        assert run_id is not None
        inputs = derive.load_inputs(conn, run_id, as_of)
        raw = derive.derive(config.load({}).mountains, inputs, for_date=DAY, as_of=as_of)
        ratios = ratio.Ratios.load(conn)
        out.append(
            (
                raw,
                ratio.compute(raw, inputs.hourly, ratios),
                fine.compute(raw, inputs.hourly, ratios),
            )
        )
    assert out[0] == out[1]
    raw, by_ratio, fine_days = out[1]
    assert raw.mountains and by_ratio and fine_days  # every reader had rows to read


def test_learned_inputs_are_unchanged(
    dbs: tuple[sqlite3.Connection, sqlite3.Connection],
) -> None:
    full, kept = dbs
    forecasts = learn.model_forecasts(kept)
    assert forecasts and forecasts == learn.model_forecasts(full)
    days = ratio.lead_days(kept)
    assert days and days == ratio.lead_days(full)


def test_report_for_an_earlier_date_needs_its_own_ingest(tmp_path: Path) -> None:
    cfg = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    jobs.run_job(
        cfg,
        "ingest-forecast",
        DAY,
        client=replay_client(load_fixtures(FIXTURES / "open_meteo" / "2026-11-27")),
    )
    with pytest.raises(jobs.JobError, match="has no hours from 2026-11-26T11:00:00"):
        jobs.run_job(cfg, "report-build", date(2026, 11, 26), options={"force": True})
