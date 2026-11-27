import sqlite3
from datetime import date
from pathlib import Path

from hazelst import backup, config, db, jobs


def make_db(path: Path) -> None:
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO report (for_date, version, status) VALUES ('2026-11-26', 1, 'numbers_only')"
    )
    conn.close()


def test_snapshot_is_a_complete_copy(tmp_path: Path) -> None:
    src = tmp_path / "hazelst.db"
    make_db(src)
    conn = db.connect(src)
    result = backup.snapshot(conn, backup.backup_dir(src), date(2026, 11, 26))
    conn.close()
    assert result.path == tmp_path / "backups" / "hazelst-2026-11-26.db"
    assert result.bytes > 0
    assert not list((tmp_path / "backups").glob("*.partial"))
    copy = sqlite3.connect(result.path)
    assert copy.execute("SELECT count(*) FROM report").fetchone()[0] == 1
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()


def test_snapshot_keeps_the_newest_four(tmp_path: Path) -> None:
    src = tmp_path / "h.db"
    conn = db.connect(src)
    db.migrate(conn)
    dest = backup.backup_dir(src)
    dest.mkdir()
    for day in (1, 8, 15, 22, 29):  # five weekly copies already there
        (dest / f"hazelst-2026-10-{day:02d}.db").write_bytes(b"x")
    result = backup.snapshot(conn, dest, date(2026, 11, 5))
    assert backup.KEEP == 4
    assert result.pruned == ["hazelst-2026-10-01.db", "hazelst-2026-10-08.db"]
    assert sorted(p.name for p in dest.iterdir()) == [
        "hazelst-2026-10-15.db",
        "hazelst-2026-10-22.db",
        "hazelst-2026-10-29.db",
        "hazelst-2026-11-05.db",
    ]


def test_prune_keeps_newest(tmp_path: Path) -> None:
    for day in range(1, 6):
        (tmp_path / f"hazelst-2026-11-0{day}.db").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("kept")
    assert backup.prune(tmp_path, keep=2) == [
        "hazelst-2026-11-01.db",
        "hazelst-2026-11-02.db",
        "hazelst-2026-11-03.db",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "hazelst-2026-11-04.db",
        "hazelst-2026-11-05.db",
        "notes.txt",
    ]


def test_backup_job_records_a_run(tmp_path: Path) -> None:
    src = tmp_path / "hazelst.db"
    make_db(src)
    cfg = config.load({"HAZELST_DB": str(src)})
    summary = jobs.run_job(cfg, "backup", date(2026, 11, 26))
    assert Path(summary["backup"]["path"]).exists()
    conn = db.connect(src)
    row = conn.execute("SELECT kind, status FROM run ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert tuple(row) == ("backup", "ok")
