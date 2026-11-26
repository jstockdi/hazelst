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
    assert resp.json() == {"status": "ok", "user_version": 1, "last_report_build": None}


def test_health_fails_after_failed_report_build(db_path: Path) -> None:
    conn = db.connect(db_path)
    with pytest.raises(RuntimeError), db.job(conn, db_path, "report-build", date(2026, 11, 26)):
        raise RuntimeError("no data")
    resp = client(db_path).get("/health")
    assert resp.status_code == 503
    assert resp.json()["last_report_build"] == "failed"
    with db.job(conn, db_path, "report-build", date(2026, 11, 26)):
        pass
    assert client(db_path).get("/health").status_code == 200


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
