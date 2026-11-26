import json
import sqlite3
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry
from starlette.testclient import TestClient

from hazelst import db, web
from hazelst.models import ReportPayload
from tests.pages import recorded_payload, sample_narrative, sample_payload


def insert(
    conn: sqlite3.Connection,
    payload: ReportPayload,
    *,
    version: int = 1,
    status: str = "numbers_only",
    narrative: bool = False,
) -> None:
    n = sample_narrative() if narrative else None
    conn.execute(
        "INSERT INTO report (for_date, version, run_id, headline, best_bet, narrative_md,"
        " payload, status, published_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        (
            payload.date.isoformat(),
            version,
            n.headline if n else None,
            n.best_bet.model_dump_json() if n and n.best_bet else None,
            n.body_md if n else None,
            payload.model_dump_json(),
            status,
            payload.issued.isoformat(),
        ),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "hazelst.db"
    conn = db.connect(path)
    db.migrate(conn)
    insert(conn, sample_payload(), status="published", narrative=True)
    insert(conn, recorded_payload())
    conn.close()
    return path


def client(path: Path) -> TestClient:
    return TestClient(web.create_app(path, registry=CollectorRegistry()))


def test_today_falls_back_to_latest(db_path: Path) -> None:
    resp = client(db_path).get("/")
    assert resp.status_code == 200
    assert "Hazel St Forecast · 2026-11-26" in resp.text
    assert "numbers only" in resp.text


def test_report_by_date_with_narrative(db_path: Path) -> None:
    resp = client(db_path).get("/d/2026-11-25")
    assert resp.status_code == 200
    assert "Best bet" in resp.text and "<h2>The storm</h2>" in resp.text


def test_missing_and_invalid_dates(db_path: Path) -> None:
    c = client(db_path)
    assert c.get("/d/2026-01-01").status_code == 404
    assert c.get("/d/not-a-date").status_code == 404
    assert c.get("/api/report/2026-01-01.json").status_code == 404
    assert c.get("/api/report/x.json").status_code == 404


def test_report_json(db_path: Path) -> None:
    resp = client(db_path).get("/api/report/2026-11-26.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "numbers_only"
    killington = next(m for m in body["mountains"] if m["key"] == "killington")
    assert set(killington["forecast"]["windows"]) == {"1d", "3d", "5d"}
    assert ReportPayload.model_validate(body).date.isoformat() == "2026-11-26"


def test_archive_lists_dates_newest_first(db_path: Path) -> None:
    html = client(db_path).get("/archive").text
    assert html.index('href="/d/2026-11-26"') < html.index('href="/d/2026-11-25"')
    assert "Ski the cold, quiet Thanksgiving morning" in html


def test_new_version_replaces_cached_page(db_path: Path) -> None:
    c = client(db_path)
    assert "Best bet" not in c.get("/d/2026-11-26").text
    conn = db.connect(db_path)
    payload = recorded_payload().model_copy(update={"edition": "0815"})
    insert(conn, payload, version=2, status="published", narrative=True)
    conn.close()
    html = c.get("/d/2026-11-26").text
    assert "Best bet" in html and "8:15 AM edition" in html


def test_static_fonts_and_css(db_path: Path) -> None:
    c = client(db_path)
    css = c.get("/static/page.css")
    assert css.status_code == 200 and "@font-face" in css.text
    font = c.get("/static/fonts/fraunces-latin-500-normal.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"


def test_empty_database(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    resp = client(path).get("/")
    assert resp.status_code == 404 and "No report yet." in resp.text
    assert "No reports yet." in client(path).get("/archive").text
    assert json.loads(client(path).get("/health").text)["status"] == "ok"
