"""Job functions: each runs inside a `run` row while holding the job lock.

A job orchestrates sources, derive, and render; it never parses or computes
itself. A failed source degrades its section and is reported in the job
summary; the job fails only when no source succeeds.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from hazelst import config, db
from hazelst.sources import base, open_meteo


class JobError(RuntimeError):
    """A job could not complete."""


@dataclass
class JobContext:
    cfg: config.Config
    conn: sqlite3.Connection
    run_id: int
    for_date: date
    client: httpx.Client
    summary: dict[str, Any] = field(default_factory=dict)


def log_event(**fields: Any) -> None:
    """One JSON line on stdout (technical-design §13)."""
    print(json.dumps({"ts": base.utc_now(), **fields}, default=str), file=sys.stdout, flush=True)


def today(cfg: config.Config) -> date:
    return datetime.now(ZoneInfo(cfg.timezone)).date()


# --- ingest-forecast ------------------------------------------------------------


def ingest_open_meteo(ctx: JobContext) -> dict[str, Any]:
    """Fetch summit and base forecasts plus model run times; store raw and normalized rows."""
    fetched = open_meteo.fetch(ctx.client, ctx.cfg.mountains)
    for f in fetched:
        if f.source == open_meteo.SOURCE and f.http_status != 200:
            raise JobError(f"open_meteo {f.meta.get('elevation')}: HTTP {f.http_status}")
    metas = [f for f in fetched if f.source == open_meteo.META_SOURCE]
    runs = open_meteo.run_times(metas)
    rows = 0
    with db.transaction(ctx.conn):
        for f in fetched:
            fetch_id = base.store_fetch(ctx.conn, ctx.run_id, f)
            if f.source == open_meteo.SOURCE:
                rows += open_meteo.store(ctx.conn, fetch_id, open_meteo.normalize(f, runs))
    return {
        "fetches": len(fetched),
        "forecast_hourly": rows,
        "runs": {
            model: {
                "issued_at": open_meteo.isoformat(run.initialised_at),
                "estimated": run.estimated,
            }
            for model, run in runs.items()
        },
    }


SourceStep = Callable[[JobContext], dict[str, Any]]
FORECAST_SOURCES: dict[str, SourceStep] = {
    "open_meteo": ingest_open_meteo,
}


def run_sources(ctx: JobContext, steps: dict[str, SourceStep]) -> None:
    failures = 0
    for name, step in steps.items():
        started = time.monotonic()
        try:
            result = step(ctx)
            status = "ok"
        except Exception as exc:  # one source down never stops the others
            failures += 1
            result = {"error": f"{type(exc).__name__}: {exc}"}
            status = "failed"
        ms = round((time.monotonic() - started) * 1000)
        ctx.summary[name] = {"status": status, "ms": ms, **result}
        log_event(run_id=ctx.run_id, step=name, status=status, ms=ms, **result)
    if failures == len(steps):
        raise JobError("every source failed")


def ingest_forecast(ctx: JobContext) -> None:
    run_sources(ctx, FORECAST_SOURCES)


JOBS: dict[str, Callable[[JobContext], None]] = {
    "ingest-forecast": ingest_forecast,
}


def run_job(
    cfg: config.Config,
    kind: str,
    for_date: date | None = None,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Migrate, take the job lock, open a `run` row, and run `kind`; returns the summary."""
    if kind not in JOBS:
        raise JobError(f"unknown job {kind!r}; known: {sorted(JOBS)}")
    for_date = for_date or today(cfg)
    conn = db.connect(cfg.settings.db_path)
    own_client = client is None
    http = client or base.make_client(cfg.settings.nws_user_agent)
    try:
        db.migrate(conn)
        db.sync_mountains(conn, cfg.mountains)
        with db.job(conn, cfg.settings.db_path, kind, for_date) as run_id:
            ctx = JobContext(cfg=cfg, conn=conn, run_id=run_id, for_date=for_date, client=http)
            JOBS[kind](ctx)
        return {"run_id": run_id, "kind": kind, "for_date": for_date.isoformat(), **ctx.summary}
    finally:
        if own_client:
            http.close()
        conn.close()


# --- record ------------------------------------------------------------------------------


Recorder = Callable[[httpx.Client, Sequence[config.Mountain]], list[base.Fetched]]
RECORDERS: dict[str, tuple[Recorder, Callable[[base.Fetched], str]]] = {
    "open-meteo": (open_meteo.fetch, open_meteo.label),
}
