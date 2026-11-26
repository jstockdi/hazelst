"""SQLite access: connections, numbered migrations, `run` rows, and the job lock.

Migrations are `migrations/NNNN_<name>.sql`, applied in order inside one
transaction each; `PRAGMA user_version` records the last one applied.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import IO, Literal

from hazelst import __version__
from hazelst.config import Mountain

BUSY_TIMEOUT_MS = 5000
MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

JOB_KINDS = frozenset(
    {
        "ingest-forecast",
        "ingest-obs",
        "report-build",
        "report-refresh",
        "forecast-verify",
        "skill-learn",
        "knowledge-review",
        "season-backfill",
        "backup",
    }
)
RunStatus = Literal["running", "ok", "failed"]


class MigrationError(RuntimeError):
    """Raised when the migration files are inconsistent."""


class JobLocked(RuntimeError):
    """Raised when another job holds the job lock."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def code_version() -> str:
    """Image git SHA when set at build time, else the package version."""
    return os.environ.get("HAZELST_CODE_VERSION") or __version__


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database with WAL, foreign keys, and a busy timeout.

    Connections use autocommit; callers group writes with `transaction()`.
    A read-only connection never creates the file and cannot write.
    """
    if readonly:
        uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, autocommit=True)
    else:
        conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT, rolled back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_migrations() -> list[Migration]:
    """Read the packaged migrations, checking they are numbered 1..N without gaps."""
    found: list[Migration] = []
    for entry in resources.files("hazelst.migrations").iterdir():
        match = MIGRATION_NAME.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
        elif entry.name.endswith(".sql"):
            raise MigrationError(f"badly named migration {entry.name}")
    found.sort(key=lambda m: m.version)
    expected = list(range(1, len(found) + 1))
    if [m.version for m in found] != expected:
        raise MigrationError(f"migrations must be numbered 1..N: {[m.name for m in found]}")
    return found


def user_version(conn: sqlite3.Connection) -> int:
    value = conn.execute("PRAGMA user_version").fetchone()[0]
    return int(value)


def migrate(conn: sqlite3.Connection, migrations: Iterable[Migration] | None = None) -> list[int]:
    """Apply every migration newer than `user_version`; return the versions applied.

    A database newer than the code (after a rollback) is left untouched:
    migrations are additive, so older code can still read and write it.
    """
    applied: list[int] = []
    for migration in load_migrations() if migrations is None else migrations:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Checked inside the write lock so concurrent migrators apply each file once.
            if migration.version <= user_version(conn):
                conn.execute("ROLLBACK")
                continue
            conn.executescript(f"{migration.sql}\n;PRAGMA user_version = {migration.version};")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"{migration.name}: {exc}") from exc
        applied.append(migration.version)
    return applied


def sync_mountains(conn: sqlite3.Connection, mountains: Iterable[Mountain]) -> int:
    """Upsert the roster into `mountain`; returns the number of rows written."""
    rows = [
        (
            m.key,
            m.name,
            m.state,
            m.lat,
            m.lon,
            m.summit_ft,
            m.base_ft,
            m.nws.office,
            m.nws.grid,
            m.season_open.isoformat() if m.season_open else None,
            json.dumps(
                {
                    "wind_hold_mph": m.wind_hold_mph.model_dump(),
                    "favored_by": list(m.favored_by),
                    "adapter": m.report_adapter.model_dump() if m.report_adapter else None,
                }
            ),
        )
        for m in mountains
    ]
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO mountain (key, name, state, lat, lon, summit_ft, base_ft,
                                  nws_office, nws_grid, season_open, config)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET
              name = excluded.name, state = excluded.state, lat = excluded.lat,
              lon = excluded.lon, summit_ft = excluded.summit_ft, base_ft = excluded.base_ft,
              nws_office = excluded.nws_office,
              nws_grid = COALESCE(excluded.nws_grid, mountain.nws_grid),
              season_open = excluded.season_open, config = excluded.config
            """,
            rows,
        )
    return len(rows)


# --- run rows -----------------------------------------------------------------


def start_run(conn: sqlite3.Connection, kind: str, for_date: date | None) -> int:
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    cur = conn.execute(
        "INSERT INTO run (kind, for_date, started_at, status, code_version)"
        " VALUES (?, ?, ?, 'running', ?)",
        (kind, for_date.isoformat() if for_date else None, utc_now(), code_version()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: RunStatus, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ?, error = ? WHERE id = ?",
        (utc_now(), status, error, run_id),
    )


# --- job lock -----------------------------------------------------------------


def lock_path(db_path: Path | str) -> Path:
    return Path(f"{db_path}.lock")


@contextmanager
def job_lock(db_path: Path | str, *, timeout_s: float = 0.0) -> Iterator[None]:
    """Hold the exclusive job lock (one job at a time across processes).

    An advisory `flock` on `<db>.lock`: the kernel releases it when the holder
    exits, so a crashed job never leaves a stale lock. Raises `JobLocked` when
    the lock is still held after `timeout_s`.
    """
    path = lock_path(db_path)
    deadline = time.monotonic() + timeout_s
    handle: IO[str] = open(path, "a+")  # noqa: SIM115 - closed in finally
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise JobLocked(f"another job holds {path}") from None
                time.sleep(0.1)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        handle.close()  # closing the descriptor releases the lock


@contextmanager
def job(
    conn: sqlite3.Connection,
    db_path: Path | str,
    kind: str,
    for_date: date | None,
    *,
    lock_timeout_s: float = 0.0,
) -> Iterator[int]:
    """Run a job: take the job lock, open a `run` row, and close it as ok or failed."""
    with job_lock(db_path, timeout_s=lock_timeout_s):
        run_id = start_run(conn, kind, for_date)
        try:
            yield run_id
        except BaseException as exc:
            finish_run(conn, run_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        finish_run(conn, run_id, "ok")
