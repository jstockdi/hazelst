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
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx

from hazelst import (
    backfill,
    config,
    corrected,
    db,
    derive,
    digest,
    knowledge,
    learn,
    method,
    narrative,
    ratio,
    verify,
)
from hazelst import backup as db_backup
from hazelst.agents import conditions_extract, knowledge_propose
from hazelst.agents.client import AgentCallFailed, AgentClient, AgentUnavailable
from hazelst.agents.schemas import ReportDraft
from hazelst.models import Narrative, ReportPayload, ReportStatus
from hazelst.render import page
from hazelst.render.payload import build_payload
from hazelst.sources import (
    base,
    discussions,
    elevation,
    ghcn,
    iem,
    nohrsc,
    nws,
    open_meteo,
    pns,
    snocountry,
    snow_report,
)
from hazelst.sources.resorts import source as resorts


class JobError(RuntimeError):
    """A job could not complete."""


ECMWF_POLL = timedelta(minutes=5)


def clock() -> datetime:
    """Now, in UTC (patched in tests)."""
    return datetime.now(UTC)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclass
class JobContext:
    cfg: config.Config
    conn: sqlite3.Connection
    run_id: int
    for_date: date
    client: httpx.Client
    summary: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


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


def ecmwf_06z(day: date) -> datetime:
    return datetime.combine(day, dtime(6), UTC)


def ecmwf_run(client: httpx.Client) -> datetime | None:
    """ECMWF's latest run from its meta.json, or None when the file cannot be read."""
    meta_id = open_meteo.META_IDS["ecmwf_ifs025"]
    try:
        fetched = base.get(
            client,
            open_meteo.META_SOURCE,
            open_meteo.META_URL.format(meta_id=meta_id),
            meta={"model": "ecmwf_ifs025", "meta_id": meta_id},
            retries=0,
        )
        if fetched.http_status != 200:
            return None
        return open_meteo.parse_meta(fetched).initialised_at
    except (base.SourceError, open_meteo.NormalizeError):
        return None


def wait_for_ecmwf_06z(ctx: JobContext, until: datetime) -> bool:
    """Poll ECMWF's meta.json every 5 minutes until the 06Z run is out or `until` passes."""
    target = ecmwf_06z(ctx.for_date)
    while True:
        run = ecmwf_run(ctx.client)
        if run is not None and run >= target:
            return True
        if clock() + ECMWF_POLL >= until:
            return False
        log_event(run_id=ctx.run_id, step="ecmwf_gate", waiting_for=target, latest=run)
        sleep(ECMWF_POLL.total_seconds())


def ingest_forecast(ctx: JobContext) -> None:
    """Fetch every forecast source. With option `ecmwf_gate_until` (the 8:10 ingest), wait
    for the ECMWF 06Z run first, polling every 5 minutes until that time."""
    until = ctx.options.get("ecmwf_gate_until")
    if until is not None:
        ready = wait_for_ecmwf_06z(ctx, until)
        ctx.summary["ecmwf_06z"] = "available" if ready else "not available by the gate"
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
        "elev_ft": elevation.safe_fill(ctx.conn, ctx.client),
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
    return {
        "reports": len(rows),
        "snow_report": written,
        "elev_ft": elevation.safe_fill(ctx.conn, ctx.client),
    }


def ingest_ghcn(ctx: JobContext) -> dict[str, Any]:
    """GHCN-Daily station snowfall for the last 10 days → `snow_report` (late reports fill in)."""
    stations = ghcn.load_stations(ghcn.stations_path(ctx.cfg.settings))
    start, end = ghcn.recent_range(ctx.for_date)
    fetched = ghcn.fetch_all(ctx.client, stations, start, end)
    ok = [f for f in fetched if f.http_status == 200]
    if not ok:
        statuses = sorted({f.http_status for f in fetched})
        raise JobError(f"ghcn: no batch succeeded (HTTP {statuses})")
    by_id = ghcn.nearest(stations)
    rows = [r for f in ok for r in ghcn.normalize(f, by_id, ctx.cfg.timezone)]
    with db.transaction(ctx.conn):
        for f in fetched:
            base.store_fetch(ctx.conn, ctx.run_id, f)
        written = ghcn.store(ctx.conn, rows)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "batches": len(fetched),
        "batch_errors": len(fetched) - len(ok),
        "reports": len(rows),
        "snow_report": written,
    }


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


def ingest_snocountry(ctx: JobContext) -> dict[str, Any]:
    """Each mountain's SnoCountry document → `observation` (source snocountry), upserted.

    The current report fills today's row when the resort feed fails; the
    history refreshes past rows. A failed mountain is reported; the step fails
    only when every mountain fails.
    """
    statuses: dict[str, Any] = {}
    ok = 0
    for m in ctx.cfg.mountains:
        if not m.snocountry_id:
            continue
        try:
            fetched = snocountry.fetch_one(ctx.client, m)
            rows = snocountry.normalize(fetched, ctx.cfg.timezone)
        except (base.SourceError, snocountry.NormalizeError) as exc:
            statuses[m.key] = {"status": "failed", "error": str(exc)}
            continue
        with db.transaction(ctx.conn):
            fetch_id = base.store_fetch(ctx.conn, ctx.run_id, fetched)
            written = snocountry.store(ctx.conn, rows, fetch_id)
        latest = rows[-1] if rows else None
        statuses[m.key] = {
            "status": "ok",
            "observation": written,
            "latest": latest.obs_date if latest else None,
            "fallback": fetched.url != snocountry.URL,
        }
        ok += 1
    if statuses and not ok:
        raise JobError("snocountry: every mountain failed")
    return {"ok": ok, "mountains": statuses}


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
    "ghcn": ingest_ghcn,
    "nohrsc": ingest_nohrsc,
    "resorts": ingest_resorts,
    "snocountry": ingest_snocountry,
}


def ingest_obs(ctx: JobContext) -> None:
    run_sources(ctx, OBS_SOURCES)


# --- report-build / report-refresh ---------------------------------------------------

EDITIONS: dict[str, dtime] = {"0700": dtime(7, 0), "0815": dtime(8, 15), "1440": dtime(14, 40)}
REFRESH_EDITIONS = ("0815", "1440")
STALE_INGEST = timedelta(hours=18)
SOURCE_NAMES = {
    open_meteo.SOURCE: "Open-Meteo",
    nws.GRID_SOURCE: "NWS gridpoints",
    nws.ALERTS_SOURCE: "NWS alerts",
    discussions.PRODUCT_SOURCE: "NWS forecast discussions",
}


def edition_rows(conn: sqlite3.Connection, for_date: date) -> dict[str, int]:
    """Edition -> newest report version for the date."""
    rows = conn.execute(
        "SELECT json_extract(payload, '$.edition'), max(version) FROM report"
        " WHERE for_date = ? AND status IN ('published', 'numbers_only') GROUP BY 1",
        (for_date.isoformat(),),
    )
    return {r[0]: r[1] for r in rows if r[0]}


def pick_edition(ctx: JobContext, kind: str) -> str:
    """The `edition` option, else 0700 for report-build and the next unpublished refresh."""
    chosen = ctx.options.get("edition")
    if chosen:
        if chosen not in EDITIONS:
            raise JobError(f"unknown edition {chosen!r}; known: {sorted(EDITIONS)}")
        return str(chosen)
    if kind == "report-build":
        return "0700"
    done = edition_rows(ctx.conn, ctx.for_date)
    return next((e for e in REFRESH_EDITIONS if e not in done), REFRESH_EDITIONS[-1])


def active_alerts(conn: sqlite3.Connection, as_of: datetime) -> dict[str, list[str]]:
    """Mountain -> events of alerts not yet expired at `as_of`."""
    out: dict[str, list[str]] = {}
    for mountain, event, expires in conn.execute("SELECT mountain, event, expires FROM alert"):
        if expires is None or datetime.fromisoformat(expires) > as_of:
            out.setdefault(mountain, []).append(event)
    return out


def unavailable_sources(conn: sqlite3.Connection, ingest_run_id: int) -> list[str]:
    ok = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT source FROM fetch WHERE run_id = ? AND http_status = 200",
            (ingest_run_id,),
        )
    }
    return [name for source, name in SOURCE_NAMES.items() if source not in ok]


def make_agents(ctx: JobContext) -> AgentClient:
    """The Claude client for a job (patched in tests with a stub backend)."""
    return AgentClient(ctx.conn, ctx.cfg.settings)


def morning_draft(conn: sqlite3.Connection, for_date: date) -> ReportDraft | None:
    """The newest draft for the date that passed `report-check` (from `agent_call`)."""
    row = conn.execute(
        "SELECT a.output FROM agent_call a JOIN run r ON r.id = a.run_id"
        " WHERE r.for_date = ? AND r.kind IN ('report-build', 'report-refresh')"
        " AND a.role IN ('report-write', 'report-revise') AND a.status = 'ok'"
        " AND a.check_errors = '[]' ORDER BY a.id DESC LIMIT 1",
        (for_date.isoformat(),),
    ).fetchone()
    return ReportDraft.model_validate_json(row[0]) if row else None


def write_narrative(ctx: JobContext, payload: ReportPayload, kind: str) -> Narrative | None:
    """The checked written report for a payload; None publishes the page as `numbers_only`.

    report-build: report-write -> report-check -> (report-revise -> report-check).
    report-refresh: report-check the newest checked draft against the new numbers;
    report-revise only if it now fails.
    """
    report_digest = digest.build(ctx.conn, payload)
    agents = make_agents(ctx)
    knowledge = narrative.accepted_knowledge(ctx.conn)
    if kind == "report-refresh":
        draft = morning_draft(ctx.conn, ctx.for_date)
        if draft is None:
            result = narrative.Narrative("numbers_only", reason="no checked narrative today")
        else:
            result = narrative.recheck_narrative(
                agents, report_digest, draft, knowledge, run_id=ctx.run_id
            )
    else:
        result = narrative.build_narrative(agents, report_digest, knowledge, run_id=ctx.run_id)
    ctx.summary["narrative"] = {
        "status": result.status,
        "reason": result.reason,
        "check_errors": result.errors,
        "agent_calls": result.call_ids,
    }
    log_event(run_id=ctx.run_id, step="narrative", **ctx.summary["narrative"])
    return result.page


def rebuild_target(ctx: JobContext) -> tuple[str | None, str]:
    """The edition a 15-minute rebuild should rewrite, or None and why not.

    Today's newest edition, when its newest version is `numbers_only` and a
    Claude call can still be made (agent-runs.md, Failure behavior).
    """
    if not ctx.cfg.settings.enable_agents:
        return None, "agents disabled"
    rows = ctx.conn.execute(
        "SELECT json_extract(payload, '$.edition'), status FROM report WHERE for_date = ?"
        " ORDER BY version DESC",
        (ctx.for_date.isoformat(),),
    ).fetchall()
    newest: dict[str, str] = {}
    for edition, status in rows:
        newest.setdefault(edition, status)
    editions = [e for e in EDITIONS if e in newest]
    if not editions:
        return None, "no edition published yet"
    edition = editions[-1]
    if newest[edition] != "numbers_only":
        return None, f"{edition} already has a checked narrative"
    agents = make_agents(ctx)
    if agents.calls_today() >= ctx.cfg.settings.agent_daily_calls:
        return None, "daily call cap reached"
    return edition, "numbers_only"


def build_report(ctx: JobContext, kind: str) -> None:
    """Derive from the latest ingest and publish a new report version for one edition."""
    if ctx.options.get("rebuild"):
        target, why = rebuild_target(ctx)
        if target is None:
            ctx.summary.update({"status": f"skipped ({why})"})
            return
        ctx.options.update({"edition": target, "force": True})
    now = clock()
    edition = pick_edition(ctx, kind)
    if not ctx.options.get("force") and edition in edition_rows(ctx.conn, ctx.for_date):
        ctx.summary.update({"edition": edition, "status": "skipped (already published)"})
        return
    ingest_run = derive.latest_ingest_run(ctx.conn)
    if ingest_run is None:
        raise JobError("no successful ingest-forecast run to build from")
    started = ctx.conn.execute("SELECT started_at FROM run WHERE id = ?", (ingest_run,)).fetchone()
    inputs = derive.load_inputs(ctx.conn, ingest_run, now)
    if not inputs.hourly:
        raise JobError(f"ingest run {ingest_run} has no forecast rows")
    raw = derive.derive(ctx.cfg.mountains, inputs, for_date=ctx.for_date, as_of=now)
    corrections = corrected.correct(raw, corrected.load_cells(ctx.conn))
    ratio_forecasts = ratio.compute(raw, inputs.hourly, ratio.Ratios.load(ctx.conn))
    choice = method.load(ctx.conn)
    passed = [m for m in choice.shows(corrected.METHOD) if m in corrections]
    ratio_passed = [m for m in choice.uses(ratio.METHOD) if m in ratio_forecasts]
    forecast = ratio.apply(corrected.apply(raw, corrections, passed), ratio_forecasts, ratio_passed)
    notes: list[str] = []
    ingested = datetime.fromisoformat(started[0])
    if now - ingested > STALE_INGEST:
        local = ingested.astimezone(ZoneInfo(ctx.cfg.timezone))
        notes.append(f"Forecast data is from {local:%a %d %b %-I:%M %p}; later ingests failed.")
    ecmwf = next((r for r in forecast.models if r.model == "ecmwf_ifs025"), None)
    if (
        edition == "0815"
        and ecmwf
        and ecmwf.issued_at
        and ecmwf.issued_at < ecmwf_06z(ctx.for_date)
    ):
        notes.append(
            f"ECMWF 06Z was not available by 8:45 AM; this edition uses the"
            f" {ecmwf.issued_at:%HZ} run."
        )
    payload = build_payload(
        forecast,
        ctx.cfg.mountains,
        issued=now,
        status="numbers_only",
        edition=edition,
        alerts=active_alerts(ctx.conn, now),
        discussion_texts=inputs.discussions,
        sources_unavailable=unavailable_sources(ctx.conn, ingest_run),
        notes=notes,
        accumulation=verify.accumulation(
            ctx.conn, ctx.cfg.mountains, ctx.for_date, ctx.cfg.timezone
        ),
        verification=verify.latest_summary(ctx.conn, ctx.for_date),
    )
    written = write_narrative(ctx, payload, kind)
    status: ReportStatus = "published" if written else "numbers_only"
    payload = payload.model_copy(update={"status": status})
    with db.transaction(ctx.conn):
        derive.store(ctx.conn, ctx.run_id, raw)
        corrected.store(ctx.conn, ctx.run_id, raw, corrections)
        ratio.store_rows(ctx.conn, ctx.run_id, raw, ratio_forecasts)
        version = ctx.conn.execute(
            "SELECT coalesce(max(version), 0) + 1 FROM report WHERE for_date = ?",
            (ctx.for_date.isoformat(),),
        ).fetchone()[0]
        ctx.conn.execute(
            "INSERT INTO report (for_date, version, run_id, headline, best_bet, narrative_md,"
            " payload, status, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ctx.for_date.isoformat(),
                version,
                ctx.run_id,
                written.headline if written else page.numbers_only_headline(payload),
                written.best_bet.model_dump_json() if written and written.best_bet else None,
                written.body_md if written else None,
                payload.model_dump_json(),
                status,
                now.isoformat(timespec="seconds"),
            ),
        )
    ctx.summary.update(
        {
            "edition": edition,
            "version": version,
            "status": status,
            "ingest_run": ingest_run,
            "models": {
                r.model: {"issued_at": r.issued_at, "stale": r.stale} for r in forecast.models
            },
            "notes": notes,
            "corrected": {"mountains": sorted(corrections), "shown": sorted(passed)},
            "resort_ratio": {"mountains": sorted(ratio_forecasts), "shown": sorted(ratio_passed)},
        }
    )
    log_event(run_id=ctx.run_id, step=kind, edition=edition, version=version, status=status)


def report_build(ctx: JobContext) -> None:
    build_report(ctx, "report-build")


def report_refresh(ctx: JobContext) -> None:
    build_report(ctx, "report-refresh")


# --- forecast-verify -------------------------------------------------------------------

# Re-score this many days back too: NOHRSC grids are re-read once after 7 days.
RESCORE_DAYS = 7


def forecast_verify(ctx: JobContext) -> None:
    """Score yesterday's editions (and the day a week before that) against observations.

    Runs at 9 PM: the NOHRSC grid ending 12Z today (the 24 h ending this
    morning) is published by then, so it is read first; resort and point
    reports for this morning came in with today's `ingest-obs`. The run's
    date is the evening it runs: `hazelst run forecast-verify --date D` scores D - 1.

    With options `start` and `end` (`hazelst forecast-verify --from --to`), it
    re-scores every report date in that range from the stored observations
    instead, fetching nothing.
    """
    start, end = ctx.options.get("start"), ctx.options.get("end")
    if start is not None or end is not None:
        if not isinstance(start, date) or not isinstance(end, date) or end < start:
            raise JobError("forecast-verify range needs start <= end")
        rescore_range(ctx, start, end)
        return
    try:
        ctx.summary["nohrsc"] = {"status": "ok", **ingest_nohrsc(ctx)}
    except Exception as exc:  # a missing grid leaves the resort and point reports
        ctx.summary["nohrsc"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    yesterday = ctx.for_date - timedelta(days=1)
    days = [yesterday, yesterday - timedelta(days=RESCORE_DAYS)]
    scored: dict[str, Any] = {}
    for day in days:
        rows = verify.verify_date(ctx.conn, ctx.cfg.mountains, day, ctx.cfg.timezone)
        with db.transaction(ctx.conn):
            written = verify.store(ctx.conn, rows)
        scored[day.isoformat()] = {"rows": written, "model_mean": verify.summarize(rows)}
    ctx.summary["scored"] = scored
    log_event(run_id=ctx.run_id, step="forecast-verify", scored=scored)


def rescore_range(ctx: JobContext, start: date, end: date) -> None:
    """Re-score each date `start`..`end`: its stored report editions, or, for a day with
    none (a season backfill), the `backfill` edition built from `forecast_hourly`."""
    extra = ratio.backfill_values(ctx.conn, ctx.cfg.mountains, start, end, ctx.cfg.timezone)
    backfilled = verify.backfill_forecasts(ctx.conn, start, end, ctx.cfg.timezone, extra)
    days = rows_total = backfill_days = 0
    sources: dict[str, int] = {}
    context_sources: dict[str, int] = {}
    day = start
    while day <= end:
        rows = verify.verify_date(ctx.conn, ctx.cfg.mountains, day, ctx.cfg.timezone)
        if not rows and day in backfilled:
            rows = verify.verify_date(
                ctx.conn, ctx.cfg.mountains, day, ctx.cfg.timezone, forecasts=backfilled[day]
            )
            backfill_days += 1 if rows else 0
        if rows:
            with db.transaction(ctx.conn):
                rows_total += verify.store(ctx.conn, rows)
            days += 1
            for r in rows:
                if r.method == verify.RAW_MEAN:
                    key = r.observed_source or "none"
                    sources[key] = sources.get(key, 0) + 1
                    for name in sorted({c["source"] for c in json.loads(r.context)}):
                        context_sources[name] = context_sources.get(name, 0) + 1
        day += timedelta(days=1)
    ctx.summary["rescored"] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": days,
        "backfill_days": backfill_days,
        "rows": rows_total,
        "model_mean_observed_source": dict(sorted(sources.items())),
        "model_mean_with_context": dict(sorted(context_sources.items())),
    }
    log_event(run_id=ctx.run_id, step="forecast-verify", rescored=ctx.summary["rescored"])


# --- backup ------------------------------------------------------------------------------


def backup(ctx: JobContext) -> None:
    """Online copy of the database to `backups/hazelst-<date>.db`; keeps the newest 14."""
    dest = db_backup.backup_dir(ctx.cfg.settings.db_path)
    started = time.monotonic()
    result = db_backup.snapshot(ctx.conn, dest, ctx.for_date)
    ms = int((time.monotonic() - started) * 1000)
    ctx.summary["backup"] = {
        "path": str(result.path),
        "bytes": result.bytes,
        "pruned": result.pruned,
        "ms": ms,
    }
    log_event(run_id=ctx.run_id, step="backup", status="ok", **ctx.summary["backup"])


# --- skill-learn ---------------------------------------------------------------------------


def skill_learn(ctx: JobContext) -> None:
    """Rebuild `skill` (learn.py), refit `snow_ratio` (ratio.py), and re-choose each
    mountain's method (`method_gate`, method.py) from verified days."""
    found = learn.samples(ctx.conn, ctx.cfg.mountains, ctx.for_date, ctx.cfg.timezone)
    rows = learn.build(found)
    ratios = ratio.refit(ctx.conn, ctx.cfg.mountains, ctx.for_date, ctx.cfg.timezone)
    with db.transaction(ctx.conn):
        written = learn.store(ctx.conn, rows, clock())
        ratio_rows = ratio.store(ctx.conn, ratios)
    gate = method.choose(ctx.conn, ctx.cfg.mountains, ctx.for_date, ctx.cfg.timezone)
    with db.transaction(ctx.conn):
        method.store(ctx.conn, gate, clock())
    ctx.summary.update(
        {
            "samples": len(found),
            "snow_day_samples": sum(1 for s in found if learn.is_snow_day(s)),
            "skill": written,
            **learn.summarize(rows, corrected.MIN_N),
            "snow_ratio": ratio_rows,
            "snow_ratio_mountains": sorted(
                {r.mountain for r in ratios if r.mountain != ratio.ALL_MOUNTAINS}
            ),
            "method_gate": {
                r.mountain: r.method for r in gate if r.chosen and r.method not in method.BESIDE
            },
        }
    )
    log_event(
        run_id=ctx.run_id,
        step="skill-learn",
        samples=len(found),
        skill=written,
        snow_ratio=ratio_rows,
    )


# --- season-backfill ---------------------------------------------------------------------


def season_backfill(ctx: JobContext) -> None:
    """Load a past range (options `start`, `end`; optional `steps`) into the live tables."""
    start, end = ctx.options.get("start"), ctx.options.get("end")
    if not isinstance(start, date) or not isinstance(end, date) or end < start:
        raise JobError("season-backfill needs start and end dates: hazelst season-backfill")
    steps = tuple(ctx.options.get("steps") or backfill.STEPS)
    bf = backfill.Backfill(ctx.conn, ctx.client, ctx.run_id, ctx.cfg, log_event)
    result = backfill.run(bf, start, end, steps)
    ctx.summary.update(result)
    if all(
        result[s]["error_count"] and not result[s]["rows"] and not result[s]["cached"]
        for s in steps
    ):
        raise JobError(f"season-backfill {start}..{end}: every step failed")


# --- knowledge-review ----------------------------------------------------------------------


def knowledge_review(ctx: JobContext) -> None:
    """Weekly: the week's evidence -> `knowledge-propose` -> proposed `knowledge` and
    `config_change` rows. Nothing proposed takes effect until the owner accepts it."""
    data = knowledge_propose.gather(ctx.conn, ctx.cfg, ctx.for_date)
    ctx.summary.update(
        {
            "week": [data.week_start.isoformat(), data.week_end.isoformat()],
            "skill_cells": len(data.skill_changes),
            "misses": len(data.misses),
        }
    )
    if not data.skill_changes and not data.misses:
        ctx.summary["status"] = "skipped (no skill cells or misses to review)"
        return
    try:
        result = knowledge_propose.propose(make_agents(ctx), data, run_id=ctx.run_id)
    except AgentUnavailable as exc:
        ctx.summary["status"] = f"skipped ({exc})"
        return
    except AgentCallFailed as exc:
        raise JobError(str(exc)) from exc
    valid = knowledge.validate(result.output, data, ctx.cfg, ctx.conn)
    with db.transaction(ctx.conn):
        ids = knowledge.store(
            ctx.conn, valid, call_id=result.call_id, run_id=ctx.run_id, now=db.utc_now()
        )
    ctx.summary.update(
        {
            "status": "proposed",
            "agent_call": result.call_id,
            "proposed": ids,
            "dropped": valid.dropped,
        }
    )
    log_event(run_id=ctx.run_id, step="knowledge-review", proposed=ids, dropped=valid.dropped)


JOBS: dict[str, Callable[[JobContext], None]] = {
    "ingest-forecast": ingest_forecast,
    "ingest-obs": ingest_obs,
    "report-build": report_build,
    "report-refresh": report_refresh,
    "forecast-verify": forecast_verify,
    "backup": backup,
    "skill-learn": skill_learn,
    "season-backfill": season_backfill,
    "knowledge-review": knowledge_review,
}


def run_job(
    cfg: config.Config,
    kind: str,
    for_date: date | None = None,
    *,
    client: httpx.Client | None = None,
    options: Mapping[str, Any] | None = None,
    lock_timeout_s: float = 0.0,
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
        # Re-read accepted config_change rows: a long-running scheduler holds its cfg.
        cfg = config.effective(cfg, conn)
        db.sync_mountains(conn, cfg.mountains)
        with db.job(
            conn, cfg.settings.db_path, kind, for_date, lock_timeout_s=lock_timeout_s
        ) as run_id:
            ctx = JobContext(
                cfg=cfg,
                conn=conn,
                run_id=run_id,
                for_date=for_date,
                client=http,
                options=dict(options or {}),
            )
            log_event(run_id=run_id, step="config", config_changes=list(cfg.config_changes))
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
    "snocountry": (snocountry.fetch, snocountry.label),
    "ghcn": (ghcn.record, ghcn.label),
}
