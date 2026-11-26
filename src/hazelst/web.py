"""Starlette app. Public and read-only: the web never writes to the database."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Info,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from hazelst import __version__, db, verify
from hazelst.config import TIMEZONE
from hazelst.models import READ_STORED, BestBet, Narrative, ReportPayload
from hazelst.render import page

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAGE_STATUSES = ("published", "numbers_only")


def health_status(db_path: Path) -> tuple[bool, dict[str, object]]:
    """Healthy when the database opens, is migrated, and the last `report-build` did not fail."""
    try:
        conn = db.connect(db_path, readonly=True)
    except sqlite3.Error as exc:
        return False, {"status": "error", "error": f"database unavailable: {exc}"}
    try:
        version = db.user_version(conn)
        if version < 1:
            return False, {"status": "error", "error": "database not migrated", "user_version": 0}
        row = conn.execute(
            "SELECT status FROM run WHERE kind = 'report-build' AND status != 'running'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        return False, {"status": "error", "error": f"database query failed: {exc}"}
    finally:
        conn.close()
    last_build = row["status"] if row else None
    healthy = last_build != "failed"
    body: dict[str, object] = {
        "status": "ok" if healthy else "error",
        "user_version": version,
        "last_report_build": last_build,
    }
    return healthy, body


class FreshnessCollector(Collector):
    """Report and backup freshness, read from the database at scrape time.

    frank's Prometheus alerts on these (report age > 26 h, technical-design §13).
    A metric is omitted while its row does not exist yet.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            conn = db.connect(self.db_path, readonly=True)
        except sqlite3.Error:
            return
        try:
            report = conn.execute(
                "SELECT max(published_at) FROM report WHERE status IN (?, ?)", PAGE_STATUSES
            ).fetchone()[0]
            backup = conn.execute(
                "SELECT max(finished_at) FROM run WHERE kind = 'backup' AND status = 'ok'"
            ).fetchone()[0]
        except sqlite3.Error:
            return
        finally:
            conn.close()
        for name, doc, value in (
            (
                "hazelst_report_published_timestamp_seconds",
                "Unix time the newest page-visible report was published",
                report,
            ),
            (
                "hazelst_backup_last_success_timestamp_seconds",
                "Unix time the newest successful database backup finished",
                backup,
            ),
        ):
            if value:
                yield GaugeMetricFamily(name, doc, value=datetime.fromisoformat(value).timestamp())


# --- reports -----------------------------------------------------------------------


def _latest(conn: sqlite3.Connection, for_date: str | None) -> sqlite3.Row | None:
    """The newest version of a date's report (or of the newest date when `for_date` is None)."""
    marks = ",".join("?" for _ in PAGE_STATUSES)
    where = f"status IN ({marks})"
    params: list[str] = list(PAGE_STATUSES)
    if for_date is not None:
        where += " AND for_date = ?"
        params.append(for_date)
    row: sqlite3.Row | None = conn.execute(
        "SELECT for_date, version, headline, best_bet, narrative_md, payload, status FROM report"
        f" WHERE {where} ORDER BY for_date DESC, version DESC LIMIT 1",
        params,
    ).fetchone()
    return row


def narrative_of(row: sqlite3.Row) -> Narrative | None:
    """The written report, when the row has one (`numbers_only` rows do not)."""
    if row["status"] != "published" or not row["narrative_md"]:
        return None
    best = json.loads(row["best_bet"]) if row["best_bet"] else None
    return Narrative(
        headline=row["headline"] or "",
        best_bet=BestBet.model_validate(best, context=READ_STORED) if best else None,
        body_md=row["narrative_md"],
    )


def render_row(row: sqlite3.Row) -> str:
    payload = ReportPayload.model_validate_json(row["payload"], context=READ_STORED)
    return page.render_report(payload, narrative_of(row))


def archive_entries(conn: sqlite3.Connection) -> list[page.ArchiveEntry]:
    marks = ",".join("?" for _ in PAGE_STATUSES)
    rows = conn.execute(
        "SELECT r.for_date, r.headline, r.status, r.payload FROM report r"
        " JOIN (SELECT for_date, max(version) AS version FROM report"
        f"       WHERE status IN ({marks}) GROUP BY for_date) latest"
        " ON latest.for_date = r.for_date AND latest.version = r.version"
        " ORDER BY r.for_date DESC",
        PAGE_STATUSES,
    ).fetchall()
    entries = []
    for r in rows:
        headline = r["headline"]
        if not headline:
            headline = page.numbers_only_headline(
                ReportPayload.model_validate_json(r["payload"], context=READ_STORED)
            )
        day = date.fromisoformat(r["for_date"])
        summary = verify.summary_for(conn, day)
        entries.append(
            page.ArchiveEntry(
                date=day,
                headline=headline,
                status=r["status"],
                verification=page.verification_counts(summary) if summary else None,
            )
        )
    return entries


class PageCache:
    """Rendered responses, kept until the `report` table changes (a job commits)."""

    def __init__(self) -> None:
        self._stamp: tuple[object, ...] | None = None
        self._items: dict[str, tuple[int, str, str]] = {}

    def get(
        self, conn: sqlite3.Connection, key: str, build: Callable[[], tuple[int, str, str]]
    ) -> tuple[int, str, str]:
        stamp = tuple(
            conn.execute(
                "SELECT count(*), max(published_at), (SELECT max(rowid) FROM verification)"
                " FROM report"
            ).fetchone()
        )
        if stamp != self._stamp:
            self._stamp, self._items = stamp, {}
        if key not in self._items:
            self._items[key] = build()
        return self._items[key]


def create_app(db_path: Path, registry: CollectorRegistry = REGISTRY) -> Starlette:
    build = Info("hazelst_build", "Running hazelst build", registry=registry)
    build.info({"version": __version__, "code_version": db.code_version()})
    registry.register(FreshnessCollector(db_path))

    async def health(request: Request) -> Response:
        healthy, body = health_status(db_path)
        return JSONResponse(body, status_code=200 if healthy else 503)

    async def metrics(request: Request) -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    cache = PageCache()

    def serve(key: str, build: Callable[[sqlite3.Connection], tuple[int, str, str]]) -> Response:
        try:
            conn = db.connect(db_path, readonly=True)
        except sqlite3.Error:
            return HTMLResponse(page.render_not_found("The forecast is unavailable."), 503)
        try:
            status, media_type, body = cache.get(conn, key, lambda: build(conn))
        finally:
            conn.close()
        return Response(body, status_code=status, media_type=media_type)

    def html_report(
        for_date: str | None, fallback_latest: bool
    ) -> Callable[..., tuple[int, str, str]]:
        def build(conn: sqlite3.Connection) -> tuple[int, str, str]:
            row = _latest(conn, for_date)
            if row is None and fallback_latest:
                row = _latest(conn, None)
            if row is None:
                message = "No report yet." if fallback_latest else f"No report for {for_date}."
                return 404, "text/html", page.render_not_found(message)
            return 200, "text/html", render_row(row)

        return build

    async def today(request: Request) -> Response:
        day = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
        return serve(f"/:{day}", html_report(day, fallback_latest=True))

    async def by_date(request: Request) -> Response:
        day = request.path_params["day"]
        if not DATE.match(day):
            return HTMLResponse(page.render_not_found("Not a date."), 404)
        return serve(f"/d/{day}", html_report(day, fallback_latest=False))

    async def archive(request: Request) -> Response:
        return serve(
            "/archive", lambda conn: (200, "text/html", page.render_archive(archive_entries(conn)))
        )

    async def report_json(request: Request) -> Response:
        day = request.path_params["day"]
        if not DATE.match(day):
            return JSONResponse({"error": "not a date"}, 404)

        def build(conn: sqlite3.Connection) -> tuple[int, str, str]:
            row = _latest(conn, day)
            if row is None:
                return 404, "application/json", json.dumps({"error": f"no report for {day}"})
            return 200, "application/json", row["payload"]

        return serve(f"/api/report/{day}.json", build)

    return Starlette(
        routes=[
            Route("/", today, methods=["GET"]),
            Route("/d/{day}", by_date, methods=["GET"]),
            Route("/archive", archive, methods=["GET"]),
            Route("/api/report/{day}.json", report_json, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/metrics", metrics, methods=["GET"]),
            Mount("/static", StaticFiles(packages=[("hazelst.render", "static")]), name="static"),
        ]
    )
