"""Nightly database backup with SQLite's online backup API (no `sqlite3` CLI on frank).

A backup is a consistent copy of the live database, written next to it under
`backups/` as `hazelst-<date>.db`. The copy is written to a temporary name and
renamed, so a backup file that exists is always complete. The newest `KEEP`
backups are kept; older ones are removed.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

KEEP = 14
NAME = re.compile(r"^hazelst-\d{4}-\d{2}-\d{2}\.db$")


@dataclass(frozen=True)
class Backup:
    path: Path
    bytes: int
    pruned: list[str]


def backup_dir(db_path: Path) -> Path:
    return db_path.parent / "backups"


def snapshot(source: sqlite3.Connection, dest_dir: Path, day: date, keep: int = KEEP) -> Backup:
    """Copy the database behind `source` to `dest_dir/hazelst-<day>.db`, then prune."""
    dest_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    final = dest_dir / f"hazelst-{day.isoformat()}.db"
    partial = final.with_name(final.name + ".partial")
    partial.unlink(missing_ok=True)
    dest = sqlite3.connect(partial)
    try:
        source.backup(dest)
    finally:
        dest.close()
    os.replace(partial, final)
    return Backup(path=final, bytes=final.stat().st_size, pruned=prune(dest_dir, keep))


def prune(dest_dir: Path, keep: int = KEEP) -> list[str]:
    """Remove all but the newest `keep` backups (by the date in the name)."""
    names = sorted(p.name for p in dest_dir.iterdir() if NAME.match(p.name))
    stale = names[:-keep] if keep > 0 else names
    for name in stale:
        (dest_dir / name).unlink()
    return stale
