import json
import multiprocessing as mp
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from hazelst import cli, config, db

DATA_MODEL = config.PROJECT_ROOT / "docs" / "design" / "data-model.md"


def spec_schema() -> dict[str, list[tuple[str, str, int]]]:
    """Columns per table from the SQL block in data-model.md: (name, type, pk)."""
    text = DATA_MODEL.read_text()
    sql = re.search(r"```sql\n(.*?)```", text, re.S)
    assert sql is not None
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql.group(1))
    return schema(conn)


def schema(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str, int]]]:
    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    ]
    return {
        t: [(c[1], c[2], c[5]) for c in conn.execute(f"PRAGMA table_info({t})")] for t in tables
    }


def dump(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return list(conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name"))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "hazelst.db"


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    c = db.connect(db_path)
    db.migrate(c)
    return c


def test_migrate_from_empty_matches_data_model(db_path: Path) -> None:
    c = db.connect(db_path)
    assert db.user_version(c) == 0
    assert db.migrate(c) == [1]
    assert db.user_version(c) == 1
    assert schema(c) == spec_schema()
    assert len(schema(c)) == 17


def test_migrate_rerun_is_noop(db_path: Path) -> None:
    c = db.connect(db_path)
    db.migrate(c)
    before = dump(c)
    assert db.migrate(c) == []
    assert dump(c) == before
    c.close()
    c2 = db.connect(db_path)
    assert db.migrate(c2) == []
    assert db.user_version(c2) == 1


def test_verification_has_edition_in_key(conn: sqlite3.Connection) -> None:
    cols = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(verification)")}
    assert cols["edition"] == 2  # second column of the primary key


def test_connection_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_readonly_connection_cannot_write(conn: sqlite3.Connection, db_path: Path) -> None:
    ro = db.connect(db_path, readonly=True)
    assert ro.execute("SELECT count(*) FROM run").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO run (kind) VALUES ('ingest-obs')")


def test_readonly_connection_does_not_create_file(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        db.connect(tmp_path / "missing.db", readonly=True)
    assert not (tmp_path / "missing.db").exists()


def test_failed_migration_rolls_back(conn: sqlite3.Connection) -> None:
    bad = db.Migration(2, "0002_bad.sql", "CREATE TABLE extra (x INT);\nSELECT * FROM nope;")
    with pytest.raises(db.MigrationError, match=r"0002_bad\.sql"):
        db.migrate(conn, [*db.load_migrations(), bad])
    assert db.user_version(conn) == 1
    assert "extra" not in schema(conn)


def test_newer_database_is_left_alone(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA user_version = 7")
    assert db.migrate(conn) == []
    assert db.user_version(conn) == 7


def test_migrations_are_numbered_without_gaps() -> None:
    migrations = db.load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "0001_init.sql"


def test_two_connections_apply_once(db_path: Path) -> None:
    a, b = db.connect(db_path), db.connect(db_path)
    assert db.migrate(a) == [1]
    assert db.migrate(b) == []


def test_sync_mountains_is_idempotent(conn: sqlite3.Connection) -> None:
    roster = config.load({}).mountains
    assert db.sync_mountains(conn, roster) == 8
    assert db.sync_mountains(conn, roster) == 8
    row = conn.execute("SELECT * FROM mountain WHERE key = 'loon'").fetchone()
    assert row["nws_office"] == "GYX"
    assert json.loads(row["config"])["wind_hold_mph"] == {"watch": 35, "likely": 50}
    assert conn.execute("SELECT count(*) FROM mountain").fetchone()[0] == 8


# --- run rows and the job lock ---------------------------------------------------


def test_job_records_ok_run(conn: sqlite3.Connection, db_path: Path) -> None:
    with db.job(conn, db_path, "ingest-forecast", date(2026, 11, 26)) as run_id:
        row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "running"
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["kind"] == "ingest-forecast"
    assert row["for_date"] == "2026-11-26"
    assert row["finished_at"] >= row["started_at"]
    assert row["code_version"]


def test_job_records_failed_run(conn: sqlite3.Connection, db_path: Path) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        db.job(conn, db_path, "report-build", date(2026, 11, 26)),
    ):
        raise RuntimeError("boom")
    row = conn.execute("SELECT status, error FROM run").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "RuntimeError: boom"


def test_unknown_job_kind(conn: sqlite3.Connection, db_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown job kind"), db.job(conn, db_path, "nope", None):
        pass


def test_lock_is_exclusive(db_path: Path) -> None:
    with db.job_lock(db_path), pytest.raises(db.JobLocked), db.job_lock(db_path):
        pass
    with db.job_lock(db_path):  # released after the first holder exits
        pass


def test_locked_job_writes_no_run(conn: sqlite3.Connection, db_path: Path) -> None:
    with (
        db.job_lock(db_path),
        pytest.raises(db.JobLocked),
        db.job(conn, db_path, "skill-learn", None),
    ):
        pass
    assert conn.execute("SELECT count(*) FROM run").fetchone()[0] == 0


def _hold_lock(
    db_path: str, ready: "mp.synchronize.Event", release: "mp.synchronize.Event"
) -> None:
    with db.job_lock(db_path):
        ready.set()
        release.wait(10)


def test_lock_across_processes(db_path: Path) -> None:
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(db_path), ready, release))
    proc.start()
    try:
        assert ready.wait(10)
        with pytest.raises(db.JobLocked), db.job_lock(db_path, timeout_s=0.3):
            pass
    finally:
        release.set()
        proc.join(10)
    with db.job_lock(db_path, timeout_s=1):
        pass


def test_lock_released_when_holder_dies(db_path: Path) -> None:
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(db_path), ready, release))
    proc.start()
    assert ready.wait(10)
    proc.kill()
    proc.join(10)
    with db.job_lock(db_path, timeout_s=1):
        pass


# --- CLI ---------------------------------------------------------------------------


def test_cli_migrate_twice(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HAZELST_DB", str(db_path))
    assert cli.main(["migrate"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["user_version"] == {"before": 0, "after": 1}
    assert first["applied"] == [1]
    assert cli.main(["migrate"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["user_version"] == {"before": 1, "after": 1}
    assert second["applied"] == []
