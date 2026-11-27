from datetime import date
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry
from starlette.testclient import TestClient

from hazelst import db, web


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "hazelst.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


def client(path: Path) -> TestClient:
    return TestClient(web.create_app(path, registry=CollectorRegistry()))


def test_health_ok_on_fresh_db(db_path: Path) -> None:
    resp = client(db_path).get("/health")
    assert resp.status_code == 200
    latest = len(db.load_migrations())
    assert resp.json() == {"status": "ok", "user_version": latest, "last_report_job": None}


@pytest.mark.parametrize("kind", ["report-build", "report-refresh"])
def test_health_and_metric_fail_after_a_failed_report_job(db_path: Path, kind: str) -> None:
    conn = db.connect(db_path)
    with pytest.raises(RuntimeError), db.job(conn, db_path, kind, date(2026, 11, 26)):
        raise RuntimeError("report-check failed after 3 revisions")
    resp = client(db_path).get("/health")
    assert resp.status_code == 503
    assert resp.json()["last_report_job"] == "failed"
    assert "hazelst_report_job_failed 1.0" in client(db_path).get("/metrics").text
    with db.job(conn, db_path, kind, date(2026, 11, 26)):
        pass
    assert client(db_path).get("/health").status_code == 200
    assert "hazelst_report_job_failed 0.0" in client(db_path).get("/metrics").text


def test_health_fails_without_db(tmp_path: Path) -> None:
    resp = client(tmp_path / "missing.db").get("/health")
    assert resp.status_code == 503
    assert not (tmp_path / "missing.db").exists()


def test_health_fails_on_unmigrated_db(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    db.connect(path).close()
    resp = client(path).get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"


def test_metrics(db_path: Path) -> None:
    resp = client(db_path).get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "hazelst_build_info{" in resp.text


def test_web_does_not_write(db_path: Path) -> None:
    before = db_path.stat().st_mtime_ns
    c = client(db_path)
    c.get("/health")
    c.get("/metrics")
    assert db_path.stat().st_mtime_ns == before


def test_metrics_report_and_backup_freshness(db_path: Path) -> None:
    assert "hazelst_report_published_timestamp_seconds" not in client(db_path).get("/metrics").text
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO report (for_date, version, status, published_at)"
        " VALUES ('2026-11-26', 1, 'published', '2026-11-26T12:00:00+00:00')"
    )
    conn.execute(  # a row from before J33 without prose is not a page
        "INSERT INTO report (for_date, version, status, published_at)"
        " VALUES ('2026-11-26', 2, 'numbers_only', '2026-11-26T13:00:00+00:00')"
    )
    with db.job(conn, db_path, "backup", date(2026, 11, 26)):
        pass
    conn.close()
    text = client(db_path).get("/metrics").text
    assert "hazelst_report_published_timestamp_seconds 1.7956944e+09" in text
    assert "hazelst_backup_last_success_timestamp_seconds " in text
