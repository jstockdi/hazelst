"""The job schedule (agent-runs.md, "Jobs"): cron slots in America/New_York and startup catch-up.

Each slot runs its steps in order in one scheduler thread; a step waits up to
20 minutes for the job lock, so overlapping slots queue instead of failing.

| Slot | Steps |
|------|-------|
| 6:45 | ingest-forecast |
| 6:50 | ingest-obs |
| 7:00 | report-build (0700 edition) |
| 8:10 | ingest-forecast (waits for ECMWF 06Z until 8:45), ingest-obs, |
|      | report-refresh (0815 edition, not before 8:15) |
| 2:30 PM | ingest-forecast, ingest-obs |
| 2:40 PM | report-refresh (1440) |
| 9:00 PM | forecast-verify (scores yesterday's editions) |
| 7:15 to 9:00, every 15 min | report-build rebuild: rewrites today's newest edition |
|      | while it is `numbers_only` (no-op otherwise) |
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)
from apscheduler.triggers.combining import OrTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from hazelst import config, db, jobs

LOCK_WAIT_S = 20 * 60
MISFIRE_GRACE_S = 30 * 60


@dataclass(frozen=True)
class Step:
    kind: str
    edition: str | None = None
    ecmwf_gate_until: dtime | None = None  # local time; ingest waits for ECMWF 06Z until then
    not_before: dtime | None = None  # local time; the step starts no earlier
    rebuild: bool = False  # report-build: rewrite the newest edition if it is numbers_only


@dataclass(frozen=True)
class Slot:
    id: str
    at: dtime
    steps: tuple[Step, ...] = field(default_factory=tuple)


SLOTS: tuple[Slot, ...] = (
    Slot("ingest-forecast-0645", dtime(6, 45), (Step("ingest-forecast"),)),
    Slot("ingest-obs-0650", dtime(6, 50), (Step("ingest-obs"),)),
    Slot("report-build-0700", dtime(7, 0), (Step("report-build", edition="0700"),)),
    Slot(
        "refresh-0815",
        dtime(8, 10),
        (
            Step("ingest-forecast", ecmwf_gate_until=dtime(8, 45)),
            Step("ingest-obs"),
            Step("report-refresh", edition="0815", not_before=dtime(8, 15)),
        ),
    ),
    Slot("ingest-1430", dtime(14, 30), (Step("ingest-forecast"), Step("ingest-obs"))),
    Slot("report-refresh-1440", dtime(14, 40), (Step("report-refresh", edition="1440"),)),
    Slot("forecast-verify-2100", dtime(21, 0), (Step("forecast-verify"),)),
)
# A failed narrative is retried every 15 minutes until 9 AM (agent-runs.md, Failure behavior).
REBUILD = Slot("report-rebuild", dtime(7, 15), (Step("report-build", rebuild=True),))

# Edition -> the slot whose last step publishes it (for catch-up).
EDITION_SLOTS = {"0700": "report-build-0700", "0815": "refresh-0815", "1440": "report-refresh-1440"}

Runner = Callable[..., dict[str, Any]]


def trigger(slot: Slot, tz: str = config.TIMEZONE) -> CronTrigger:
    return CronTrigger(hour=slot.at.hour, minute=slot.at.minute, timezone=ZoneInfo(tz))


def rebuild_trigger(tz: str = config.TIMEZONE) -> OrTrigger:
    """7:15, 7:30, 7:45, 8:00, …, 8:45, 9:00 local."""
    zone = ZoneInfo(tz)
    return OrTrigger(
        [
            CronTrigger(hour=7, minute="15,30,45", timezone=zone),
            CronTrigger(hour=8, minute="0,15,30,45", timezone=zone),
            CronTrigger(hour=9, minute=0, timezone=zone),
        ]
    )


def _local(day: date, at: dtime, tz: str) -> datetime:
    return datetime.combine(day, at, ZoneInfo(tz))


def run_slot(
    cfg: config.Config,
    slot: Slot,
    *,
    day: date | None = None,
    runner: Runner = jobs.run_job,
) -> list[dict[str, Any]]:
    """Run a slot's steps in order; a failed step is logged and the next one still runs."""
    day = day or jobs.today(cfg)
    results: list[dict[str, Any]] = []
    for step in slot.steps:
        if step.not_before is not None:
            wait = (_local(day, step.not_before, cfg.timezone) - jobs.clock()).total_seconds()
            if wait > 0:
                jobs.sleep(wait)
        options: dict[str, Any] = {}
        if step.edition:
            options["edition"] = step.edition
        if step.rebuild:
            options["rebuild"] = True
        if step.ecmwf_gate_until is not None:
            options["ecmwf_gate_until"] = _local(day, step.ecmwf_gate_until, cfg.timezone)
        try:
            result = runner(cfg, step.kind, day, options=options, lock_timeout_s=LOCK_WAIT_S)
        except Exception as exc:  # the next step (e.g. the refresh) still runs
            result = {"kind": step.kind, "error": f"{type(exc).__name__}: {exc}"}
        jobs.log_event(slot=slot.id, **{k: v for k, v in result.items() if k != "slot"})
        results.append(result)
    return results


def due_catch_up(cfg: config.Config, now: datetime) -> Slot | None:
    """The slot to run at startup: the latest edition due today that has no report yet."""
    local_now = now.astimezone(ZoneInfo(cfg.timezone))
    day = local_now.date()
    conn = db.connect(cfg.settings.db_path)
    try:
        db.migrate(conn)
        done = jobs.edition_rows(conn, day)
    finally:
        conn.close()
    by_id = {s.id: s for s in SLOTS}
    due = [e for e, at in jobs.EDITIONS.items() if local_now.time() >= at]
    if not due or due[-1] in done:
        return None
    edition = due[-1]
    slot = by_id[EDITION_SLOTS[edition]]
    publish = slot.steps[-1]
    steps = (Step("ingest-forecast"), Step("ingest-obs"), Step(publish.kind, edition=edition))
    return Slot(f"catch-up-{edition}", local_now.time(), steps)


def catch_up(cfg: config.Config, runner: Runner = jobs.run_job) -> list[dict[str, Any]]:
    """At startup: publish today's latest due edition if it is missing (container down at 7)."""
    slot = due_catch_up(cfg, jobs.clock())
    if slot is None:
        return []
    return run_slot(cfg, slot, runner=runner)


def build(cfg: config.Config, runner: Runner = jobs.run_job) -> BackgroundScheduler:
    """A scheduler with every slot added (not started)."""
    scheduler = BackgroundScheduler(timezone=ZoneInfo(cfg.timezone))
    for slot in SLOTS:
        scheduler.add_job(
            run_slot,
            trigger(slot, cfg.timezone),
            args=(cfg, slot),
            kwargs={"runner": runner},
            id=slot.id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_S,
        )
    scheduler.add_job(
        run_slot,
        rebuild_trigger(cfg.timezone),
        args=(cfg, REBUILD),
        kwargs={"runner": runner},
        id=REBUILD.id,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=10 * 60,
    )
    scheduler.add_job(catch_up, args=(cfg,), kwargs={"runner": runner}, id="catch-up")
    return scheduler


def next_runs(cfg: config.Config, after: datetime) -> list[tuple[str, datetime]]:
    """Each slot's next fire time after `after` (for `hazelst schedule`)."""
    out = []
    for slot in SLOTS:
        fire = trigger(slot, cfg.timezone).get_next_fire_time(None, after)
        if fire is not None:
            out.append((slot.id, fire))
    fire = rebuild_trigger(cfg.timezone).get_next_fire_time(None, after)
    if fire is not None:
        out.append((REBUILD.id, fire))
    return sorted(out, key=lambda item: item[1])
