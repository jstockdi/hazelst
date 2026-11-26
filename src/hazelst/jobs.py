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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx

from hazelst import config, db
from hazelst.agents import conditions_extract
from hazelst.agents.client import AgentCallFailed, AgentClient, AgentUnavailable
from hazelst.sources import base, discussions, iem, nohrsc, nws, open_meteo, pns, snow_report
from hazelst.sources.resorts import source as resorts


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


def ingest_nws(ctx: JobContext) -> dict[str, Any]:
    """Gridpoint snow / ice / gust and active alerts per mountain; resolves grids once."""
    grids = nws.cached_grids(ctx.conn)

    def one(m: config.Mountain) -> list[base.Fetched] | str:
        try:
            return nws.fetch(ctx.client, [m], grids)
        except base.SourceError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(zip(ctx.cfg.mountains, pool.map(one, ctx.cfg.mountains), strict=True))
    errors = {m.key: r for m, r in results.items() if isinstance(r, str)}
    counts = {"nws_grid": 0, "alert": 0}
    with db.transaction(ctx.conn):
        for m, result in results.items():
            if isinstance(result, str):
                continue
            for f in result:
                fetch_id = base.store_fetch(ctx.conn, ctx.run_id, f)
                if f.http_status != 200:
                    errors[m.key] = f"{f.source}: HTTP {f.http_status}"
                    continue
                if f.source == nws.POINTS_SOURCE:
                    nws.cache_grid(ctx.conn, m.key, nws.parse_points(f))
                elif f.source == nws.GRID_SOURCE:
                    counts["nws_grid"] += nws.store_grid(ctx.conn, fetch_id, nws.normalize_grid(f))
                elif f.source == nws.ALERTS_SOURCE:
                    counts["alert"] += nws.store_alerts(
                        ctx.conn, ctx.run_id, nws.normalize_alerts(f)
                    )
    if len(errors) == len(ctx.cfg.mountains):
        raise JobError(f"nws failed for every mountain: {errors}")
    return {**counts, "errors": errors}


def ingest_discussions(ctx: JobContext) -> dict[str, Any]:
    """The latest AFD from each office."""
    stored: dict[str, str] = {}
    errors: dict[str, str] = {}
    for office in discussions.OFFICES:
        try:
            fetched = discussions.fetch(ctx.client, [office])
        except (base.SourceError, discussions.NormalizeError) as exc:
            errors[office] = str(exc)
            continue
        with db.transaction(ctx.conn):
            for f in fetched:
                base.store_fetch(ctx.conn, ctx.run_id, f)
                if f.http_status != 200:
                    errors[office] = f"{f.source}: HTTP {f.http_status}"
                elif f.source == discussions.PRODUCT_SOURCE:
                    row = discussions.normalize(f)
                    discussions.store(ctx.conn, [row])
                    stored[office] = row.issued_at
    if not stored:
        raise JobError(f"no discussion stored: {errors}")
    return {"discussion": stored, "errors": errors}


SourceStep = Callable[[JobContext], dict[str, Any]]
FORECAST_SOURCES: dict[str, SourceStep] = {
    "open_meteo": ingest_open_meteo,
    "nws": ingest_nws,
    "discussions": ingest_discussions,
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


# --- ingest-obs -------------------------------------------------------------------------


def local_day_start(cfg: config.Config, day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), ZoneInfo(cfg.timezone))


def obs_window(ctx: JobContext) -> tuple[datetime, datetime]:
    """Reports from the start of the day before `for_date` to the end of it (or now)."""
    start = local_day_start(ctx.cfg, ctx.for_date - timedelta(days=1))
    end = min(local_day_start(ctx.cfg, ctx.for_date + timedelta(days=1)), datetime.now(UTC))
    return start, end


def ingest_pns(ctx: JobContext) -> dict[str, Any]:
    """New PNS products from api.weather.gov (IEM archive when that fails) → `snow_report`."""
    start, end = obs_window(ctx)
    known = pns.known_ids(ctx.conn)
    fetched: list[base.Fetched] = []
    wanted: list[pns.ListedProduct] = []
    listing: dict[str, str] = {}
    # api.weather.gov keeps PNS products for a short time; past dates use the IEM archive.
    live = ctx.for_date >= today(ctx.cfg) - timedelta(days=1)
    for office in pns.OFFICES:
        products: list[pns.ListedProduct] | None = None
        if live:
            try:
                listed = pns.fetch_list(ctx.client, office)
                fetched.append(listed)
                if listed.http_status != 200:
                    raise base.SourceError(f"HTTP {listed.http_status}")
                products = pns.parse_list(listed)
                listing[office] = "api.weather.gov"
            except (base.SourceError, pns.NormalizeError) as exc:
                listing[office] = f"iem archive (api.weather.gov: {exc})"
        if products is None:
            archive = [
                pns.fetch_archive_list(ctx.client, office, day)
                for day in (ctx.for_date - timedelta(days=1), ctx.for_date)
            ]
            fetched.extend(archive)
            ok = [f for f in archive if f.http_status == 200]
            products = [p for f in ok for p in pns.parse_list(f)]
            if ok:
                listing.setdefault(office, "iem archive")
            else:
                listing[office] = "unavailable"
        for p in products:
            issued = datetime.fromisoformat(p.issued_at)
            if p.product_id not in known and start <= issued <= end:
                known.add(p.product_id)
                wanted.append(p)
    if all(v == "unavailable" for v in listing.values()):
        raise JobError("pns: no office listing could be read")
    texts = [pns.fetch_product(ctx.client, p) for p in wanted]
    products_ok = [f for f in texts if f.http_status == 200]
    reports = 0
    with db.transaction(ctx.conn):
        for f in [*fetched, *texts]:
            base.store_fetch(ctx.conn, ctx.run_id, f)
        for f in products_ok:
            reports += pns.store(ctx.conn, pns.normalize(f, ctx.cfg.mountains))
    return {
        "listing": listing,
        "products": len(products_ok),
        "product_errors": len(texts) - len(products_ok),
        "snow_report": reports,
    }


def ingest_lsr(ctx: JobContext) -> dict[str, Any]:
    """IEM Local Storm Reports for the window → `snow_report` (a PNS copy wins)."""
    start, end = obs_window(ctx)
    fetched = iem.fetch_lsr(ctx.client, start, end)
    if fetched.http_status != 200:
        raise JobError(f"iem_lsr: HTTP {fetched.http_status}")
    rows = iem.normalize_lsr(fetched, ctx.cfg.mountains)
    with db.transaction(ctx.conn):
        base.store_fetch(ctx.conn, ctx.run_id, fetched)
        written = snow_report.store(ctx.conn, rows, "lsr")
    return {"reports": len(rows), "snow_report": written}


def ingest_nohrsc(ctx: JobContext) -> dict[str, Any]:
    """NOHRSC 24 h / 48 h / season grids at each mountain → `observation` (source nohrsc)."""
    now = datetime.now(UTC)
    ends = nohrsc.ends_to_fetch(ctx.conn, ctx.for_date, now)
    out: dict[str, Any] = {}
    for end in ends:
        fetched = nohrsc.fetch(ctx.client, end)
        grids = {cast(nohrsc.Product, f.meta["product"]): f for f in fetched}
        rows = nohrsc.normalize(grids, ctx.cfg.mountains)
        with db.transaction(ctx.conn):
            ids = {p: base.store_fetch(ctx.conn, ctx.run_id, f) for p, f in grids.items()}
            written = nohrsc.store(ctx.conn, rows, ids)
        out[end.isoformat()] = {
            "http": {p: f.http_status for p, f in grids.items()},
            "observation": written,
        }
    latest = nohrsc.latest_end(ctx.for_date, now)
    if not nohrsc.stored_fetch_times(ctx.conn, latest):
        raise JobError(f"nohrsc: the grid ending {latest.isoformat()} 12Z is unavailable")
    return {"latest": latest.isoformat(), "ends": out or "up to date"}


def ingest_resorts(ctx: JobContext, agents: AgentClient | None = None) -> dict[str, Any]:
    """Resort report adapters: fetch within the daily budget, store `observation` rows.

    Each resort's status (ok, failed with a reason, skipped_budget, disabled)
    goes in the summary, and the step fails when no adapter succeeds. A
    failed parse also logs a `resort_adapter_failed` line, the live contract
    check. A failed non-Boyne page goes to `conditions-extract` (at most once
    per resort per day); its values are stored as `resort_llm` rows.
    """
    now = datetime.now(UTC)
    statuses: dict[str, dict[str, Any]] = {}
    for mountain in ctx.cfg.mountains:
        outcome = resorts.ingest_one(ctx.conn, ctx.client, ctx.run_id, mountain, now)
        statuses[mountain.key] = outcome.summary()
        if outcome.status != "failed":
            continue
        log_event(
            run_id=ctx.run_id,
            step="resorts",
            event="resort_adapter_failed",
            mountain=mountain.key,
            preset=outcome.preset,
            fetch_id=outcome.fetch_id,
            reason=outcome.reason,
        )
        if resorts.extraction_allowed(mountain) and outcome.fetched is not None:
            agents = agents or AgentClient(ctx.conn, ctx.cfg.settings)
            statuses[mountain.key]["extract"] = extract_failed_page(
                ctx, agents, mountain, outcome, now
            )
    counts = {
        status: sum(1 for s in statuses.values() if s["status"] == status)
        for status in ("ok", "failed", "skipped_budget", "disabled")
    }
    if counts["failed"] and not counts["ok"]:
        reasons = "; ".join(
            f"{key}: {s.get('reason')}" for key, s in statuses.items() if s["status"] == "failed"
        )
        raise JobError(f"resorts: every adapter failed ({reasons})")
    return {**counts, "resorts": statuses}


def extract_failed_page(
    ctx: JobContext,
    agents: AgentClient,
    mountain: config.Mountain,
    outcome: resorts.ResortOutcome,
    now: datetime,
) -> dict[str, Any]:
    """Run `conditions-extract` on one failed page and store a `resort_llm` row."""
    assert outcome.fetched is not None
    if conditions_extract.called_today(ctx.conn, mountain.key, now):
        return {"status": "skipped", "reason": "already called today"}
    try:
        result = conditions_extract.extract(agents, mountain, outcome.fetched, run_id=ctx.run_id)
    except conditions_extract.ExtractionNotAllowed as exc:
        return {"status": "skipped", "reason": str(exc)}
    except AgentUnavailable as exc:
        return {"status": "unavailable", "reason": str(exc)}
    except AgentCallFailed as exc:
        return {"status": "failed", "call_id": exc.call_id, "reason": str(exc)}
    values = result.output.model_dump()
    if all(values[k] is None for k in values if k != "confidence"):
        return {"status": "no_values", "call_id": result.call_id}
    row = resorts.llm_observation(outcome.fetched, values, call_id=result.call_id)
    with db.transaction(ctx.conn):
        observation_id = resorts.store(ctx.conn, outcome.fetch_id, row)
    return {"status": "ok", "call_id": result.call_id, "observation_id": observation_id}


OBS_SOURCES: dict[str, SourceStep] = {
    "pns": ingest_pns,
    "lsr": ingest_lsr,
    "nohrsc": ingest_nohrsc,
    "resorts": ingest_resorts,
}


def ingest_obs(ctx: JobContext) -> None:
    run_sources(ctx, OBS_SOURCES)


JOBS: dict[str, Callable[[JobContext], None]] = {
    "ingest-forecast": ingest_forecast,
    "ingest-obs": ingest_obs,
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
    "nws": (nws.fetch, nws.label),
    "discussions": (lambda client, _mountains: discussions.fetch(client), discussions.label),
    "resorts": (resorts.record, resorts.label),
}
