"""Starlette app. Public and read-only: the web never writes to the database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Info,
    generate_latest,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hazelst import __version__, db


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


def create_app(db_path: Path, registry: CollectorRegistry = REGISTRY) -> Starlette:
    build = Info("hazelst_build", "Running hazelst build", registry=registry)
    build.info({"version": __version__, "code_version": db.code_version()})

    async def health(request: Request) -> Response:
        healthy, body = health_status(db_path)
        return JSONResponse(body, status_code=200 if healthy else 503)

    async def metrics(request: Request) -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/metrics", metrics, methods=["GET"]),
        ]
    )
