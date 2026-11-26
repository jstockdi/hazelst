"""skill-learn: rebuild `skill` from verified days and the stored model forecasts.

A sample is one model's summit snow forecast for one mountain-day (6 AM to
6 AM local), paired with the snow observed that day:

- Forecasts come from `forecast_hourly`: each fetch's summit series that
  covers the whole day, at the lead it had when the day began. Lead day
  N = floor(lead at 6 AM / 24) + 1, so the morning ingest's day 1 is lead
  day 1. Rows before their run (`lead_h` < 0, the API's past days) and models
  without a run time are skipped. Several fetches of the same model at the
  same lead day (the three daily ingests) are averaged into one sample.
- Lead days are pooled into the report's window segments: `1d` (day 1),
  `2-3d` (days 2 and 3), `4-5d` (days 4 and 5), so the corrected method can
  rebuild the 1d / 3d / 5d windows from them.
- The observation is the one `forecast-verify` scored for that day
  (`verification`, model mean row of the earliest edition), so the learned
  truth is the scored truth. Days without a `verification` row (a season
  backfill, or a day that was never reported) use `verify.observed`, the same
  priority rule. Tags are the ones that fired on the day, copied from
  `verification`.
- Only snow days count: the observation or the model's forecast is at least
  0.5 inch. A dry day forecast dry says nothing about bias.

Each `skill` cell (mountain x model x lead bucket x tag; tag '' = every day):
`n` snow days, `bias_ratio` = sum of forecasts / sum of observations (NULL
when nothing was observed), `mae_in`, and `hit_rate` by the verification rule.
A day is a verifiable sample once its observations are in: day X is scored at
9 PM on X + 1, so a run on date D learns from days up to D - 2.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from hazelst import verify
from hazelst.config import TIMEZONE, Mountain

SNOW_DAY_IN = 0.5
ALL_TAGS = ""
MAX_LEAD_DAY = 5
LEAD_BUCKETS: dict[int, str] = {1: "1d", 2: "2-3d", 3: "2-3d", 4: "4-5d", 5: "4-5d"}
BUCKETS: tuple[str, ...] = ("1d", "2-3d", "4-5d")
ANCHOR_HOUR = 6
MIN_HOURS = 23  # a full day; 23 allows the spring DST day


@dataclass(frozen=True)
class Sample:
    """One model's forecast for one mountain-day at one lead day, and what fell."""

    mountain: str
    model: str
    lead_day: int
    day: date
    forecast_in: float
    observed_in: float
    tags: tuple[str, ...] = ()

    @property
    def bucket(self) -> str:
        return LEAD_BUCKETS[self.lead_day]


@dataclass(frozen=True)
class SkillRow:
    """One `skill` cell, in column order (without `updated_at`)."""

    mountain: str
    model: str
    lead_bucket: str
    tag: str
    n: int
    bias_ratio: float | None
    mae_in: float
    hit_rate: float


# --- forecasts ------------------------------------------------------------------------


def _local_day_fn(tz: str) -> Callable[[str], str]:
    zone = ZoneInfo(tz)

    @lru_cache(maxsize=65536)
    def local_day(valid_at: str) -> str:
        """The 6 AM -> 6 AM local day an hour (UTC ISO) belongs to."""
        local = datetime.fromisoformat(valid_at).astimezone(zone)
        return (local - timedelta(hours=ANCHOR_HOUR)).date().isoformat()

    return local_day


def model_forecasts(
    conn: sqlite3.Connection, tz: str = TIMEZONE, *, through: date | None = None
) -> dict[tuple[str, str, int, date], float]:
    """(mountain, model, lead day, day) -> mean summit snow over the fetches covering the day."""
    conn.create_function("hazelst_local_day", 1, _local_day_fn(tz), deterministic=True)
    sql = (
        "SELECT mountain, model, day, min_lead, total FROM ("
        " SELECT mountain, model, hazelst_local_day(valid_at) AS day,"
        "  min(lead_h) AS min_lead, sum(snowfall_in) AS total, count(*) AS hours"
        " FROM forecast_hourly"
        " WHERE elevation = 'summit' AND lead_h IS NOT NULL AND lead_h >= 0"
        "  AND lead_h < ? AND snowfall_in IS NOT NULL"
        " GROUP BY fetch_id, mountain, model, day)"
        " WHERE hours >= ?"
    )
    params: list[object] = [24 * (MAX_LEAD_DAY + 1), MIN_HOURS]
    sums: dict[tuple[str, str, int, date], list[float]] = defaultdict(list)
    for mountain, model, day, min_lead, total in conn.execute(sql, params):
        lead_day = int(min_lead) // 24 + 1
        when = date.fromisoformat(day)
        if lead_day > MAX_LEAD_DAY or (through is not None and when > through):
            continue
        sums[(mountain, model, lead_day, when)].append(float(total))
    return {key: sum(v) / len(v) for key, v in sums.items()}


# --- observations ---------------------------------------------------------------------


def verified_days(
    conn: sqlite3.Connection,
) -> dict[tuple[str, date], tuple[float | None, tuple[str, ...]]]:
    """(mountain, day) -> (observed, tags) from the earliest edition's model-mean row."""
    out: dict[tuple[str, date], tuple[float | None, tuple[str, ...]]] = {}
    for for_date, mountain, observed_in, tags in conn.execute(
        "SELECT for_date, mountain, observed_in, tags FROM verification"
        " WHERE window = ? AND method = ? ORDER BY for_date, mountain, edition",
        (verify.WINDOW, verify.RAW_MEAN),
    ):
        key = (mountain, date.fromisoformat(for_date))
        if key not in out:
            out[key] = (observed_in, tuple(json.loads(tags or "[]")))
    return out


def samples(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    for_date: date,
    tz: str = TIMEZONE,
) -> list[Sample]:
    """Every verifiable sample as of a run on `for_date` (days up to `for_date` - 2)."""
    through = for_date - timedelta(days=2)
    keys = {m.key for m in mountains}
    forecasts = model_forecasts(conn, tz, through=through)
    verified = verified_days(conn)
    truth: dict[tuple[str, date], tuple[float, tuple[str, ...]] | None] = {}
    out: list[Sample] = []
    for (mountain, model, lead_day, day), forecast_in in sorted(forecasts.items()):
        if mountain not in keys:
            continue
        key = (mountain, day)
        if key not in truth:
            scored = verified.get(key)
            tags: tuple[str, ...] = scored[1] if scored else ()
            value = scored[0] if scored else None
            if value is None:
                found = verify.observed(conn, mountain, day, tz)
                value = found.snow_in if found else None
            truth[key] = None if value is None else (float(value), tags)
        known = truth[key]
        if known is None:
            continue
        out.append(
            Sample(
                mountain=mountain,
                model=model,
                lead_day=lead_day,
                day=day,
                forecast_in=round(forecast_in, 2),
                observed_in=known[0],
                tags=known[1],
            )
        )
    return out


# --- skill ------------------------------------------------------------------------------


def is_snow_day(s: Sample) -> bool:
    return s.observed_in >= SNOW_DAY_IN or s.forecast_in >= SNOW_DAY_IN


def cell(mountain: str, model: str, bucket: str, tag: str, group: Sequence[Sample]) -> SkillRow:
    forecast = sum(s.forecast_in for s in group)
    observed = sum(s.observed_in for s in group)
    hits = sum(1 for s in group if verify.score(s.forecast_in, s.observed_in)[2] == "HIT")
    return SkillRow(
        mountain=mountain,
        model=model,
        lead_bucket=bucket,
        tag=tag,
        n=len(group),
        bias_ratio=round(forecast / observed, 3) if observed > 0 else None,
        mae_in=round(sum(abs(s.forecast_in - s.observed_in) for s in group) / len(group), 2),
        hit_rate=round(hits / len(group), 3),
    )


def build(all_samples: Iterable[Sample]) -> list[SkillRow]:
    """Skill cells from samples: snow days only, per tag and for every day (tag '')."""
    groups: dict[tuple[str, str, str, str], list[Sample]] = defaultdict(list)
    for s in all_samples:
        if not is_snow_day(s):
            continue
        for tag in (ALL_TAGS, *sorted(set(s.tags))):
            groups[(s.mountain, s.model, s.bucket, tag)].append(s)
    return [cell(*key, group) for key, group in sorted(groups.items())]


def store(conn: sqlite3.Connection, rows: Iterable[SkillRow], updated_at: datetime) -> int:
    """Replace the whole `skill` table (it is rebuilt from scratch every night)."""
    stamp = updated_at.astimezone(UTC).isoformat(timespec="seconds")
    params = [
        (r.mountain, r.model, r.lead_bucket, r.tag, r.n, r.bias_ratio, r.mae_in, r.hit_rate, stamp)
        for r in rows
    ]
    conn.execute("DELETE FROM skill")
    conn.executemany(
        "INSERT INTO skill (mountain, model, lead_bucket, tag, n, bias_ratio, mae_in, hit_rate,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def summarize(rows: Sequence[SkillRow], min_n: int) -> dict[str, object]:
    """Counts for the job summary: cells, and all-day cells with at least `min_n` snow days."""
    ready = [r for r in rows if r.tag == ALL_TAGS and r.n >= min_n]
    return {
        "cells": len(rows),
        "cells_all_tags": sum(1 for r in rows if r.tag == ALL_TAGS),
        "ready_cells": len(ready),
        "ready": sorted({f"{r.mountain}/{r.model}/{r.lead_bucket}" for r in ready}),
    }
