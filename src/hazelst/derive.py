"""Forecast numbers from normalized rows: windows, days, tags, precip type.

`derive()` is pure over rows (forecast-method.md):

- Windows start at 6 AM local on the report day: 1d / 3d / 5d = 24 / 72 / 120 h.
- Headline = mean of the models' summit snowfall (additive, so the daily
  buckets sum to the windows); range = min-max of the per-model totals;
  confidence from the range width.
- A model whose run is more than 18 h old at `as_of` is stale: listed, but
  left out of the mean and the range (unless every model is stale).
- NWS gridpoint snow and ice (mm over ISO-8601 intervals) are prorated into
  the 1d and 3d windows; the grids end after about three days.
- Freezing level comes from GFS only; 850 hPa direction too (directions do
  not average).

`load_inputs()` reads those rows for one ingest run, and `store()` writes
`forecast_window` and `forecast_day`.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hazelst import tags as tagging
from hazelst.config import TIMEZONE, Mountain
from hazelst.models import (
    Confidence,
    Forecast,
    ForecastDay,
    ForecastWindow,
    ModelRun,
    MountainForecast,
    WindowKey,
)
from hazelst.sources import discussions, open_meteo
from hazelst.sources.nws import GridRow
from hazelst.sources.open_meteo import HourlyRow

WINDOWS: dict[WindowKey, int] = {"1d": 24, "3d": 72, "5d": 120}
NWS_WINDOWS: frozenset[str] = frozenset({"1d", "3d"})  # NWS snow / ice grids end at ~3 days
DAYS = 5
ANCHOR_HOUR = 6
STALE_AFTER_H = 18.0
FREEZING_LEVEL_MODEL = "gfs_global"
DIRECTION_MODEL = "gfs_global"
MM_PER_IN = 25.4
DAY_MIN_HOURS = 23  # a model's day counts when its run covers a full day (23 for spring DST)


@dataclass(frozen=True)
class Inputs:
    """The normalized rows one derive reads."""

    hourly: Sequence[HourlyRow]
    nws: Sequence[GridRow]
    discussions: Mapping[str, str]  # office -> latest AFD text
    ingest_run_id: int | None = None


# --- small helpers ----------------------------------------------------------------


def anchor_for(day: date, tz: str = TIMEZONE) -> datetime:
    """6 AM local on `day`, as an aware datetime."""
    return datetime.combine(day, time(ANCHOR_HOUR), ZoneInfo(tz))


def confidence(lo: float, hi: float) -> Confidence:
    width = hi - lo
    return "high" if width <= 3 else "medium" if width <= 6 else "low"


def nws_disagrees(mean_in: float, nws_in: float | None) -> bool:
    """NWS and the model mean differ by > 50% while either is >= 1 inch."""
    if nws_in is None:
        return False
    big = max(mean_in, nws_in)
    return big >= 1 and abs(nws_in - mean_in) > 0.5 * big


def _total(values: Iterable[float | None]) -> float:
    return sum(v for v in values if v is not None)


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _round_int(value: float | None) -> int | None:
    return None if value is None else round(value)


def _to_inches(value: float, unit: str) -> float | None:
    if unit == "mm":
        return value / MM_PER_IN
    if unit == "cm":
        return value / 2.54
    if unit in ("in", "inch"):
        return value
    return None


def prorated_sum(rows: Iterable[GridRow], start: datetime, end: datetime) -> tuple[float, bool]:
    """Inches over [start, end) from interval rows, prorating intervals that cross an edge.

    Returns (total, covered): `covered` is false when the layer ends before
    `end`, so a short grid never reads as a small total.
    """
    total = 0.0
    last_end = start
    for row in rows:
        row_start = datetime.fromisoformat(row.valid_start)
        row_end = row_start + timedelta(hours=row.hours)
        last_end = max(last_end, row_end)
        overlap_h = (min(row_end, end) - max(row_start, start)).total_seconds() / 3600
        if overlap_h <= 0 or row.value is None:
            continue
        inches = _to_inches(row.value, row.unit)
        if inches is not None:
            total += inches * overlap_h / row.hours
    return total, last_end >= end


# --- per-mountain series -----------------------------------------------------------


class _Series:
    """Hourly values over the 120 h after the anchor for one mountain.

    `get(model, elevation, column)` returns a list aligned with the hours,
    with None where the model has no value.
    """

    def __init__(self, rows: Iterable[HourlyRow], hours: Sequence[datetime]) -> None:
        index = {h: i for i, h in enumerate(hours)}
        self.n = len(hours)
        self._data: dict[tuple[str, str], list[HourlyRow | None]] = defaultdict(
            lambda: [None] * self.n
        )
        for row in rows:
            i = index.get(datetime.fromisoformat(row.valid_at))
            if i is not None:
                self._data[(row.model, row.elevation)][i] = row

    def models(self) -> set[str]:
        return {model for model, _ in self._data}

    def get(self, model: str, elevation: str, column: str) -> list[float | None]:
        rows = self._data.get((model, elevation))
        if rows is None:
            return [None] * self.n
        return [None if r is None else getattr(r, column) for r in rows]

    def mean(self, models: Sequence[str], elevation: str, column: str) -> list[float | None]:
        per_model = [self.get(m, elevation, column) for m in models]
        return [_mean(values) for values in zip(*per_model, strict=True)] if per_model else []


# --- derive ------------------------------------------------------------------------


def model_runs(hourly: Iterable[HourlyRow], as_of: datetime) -> tuple[list[ModelRun], list[str]]:
    """Each model's run age at `as_of`, and the models used in the mean (fresh ones)."""
    issued: dict[str, datetime | None] = {}
    for row in hourly:
        at = datetime.fromisoformat(row.issued_at) if row.issued_at else None
        prev = issued.get(row.model)
        if row.model not in issued or (at is not None and (prev is None or at > prev)):
            issued[row.model] = at
    runs: list[ModelRun] = []
    for model in open_meteo.MODELS:
        if model not in issued:
            continue
        at = issued[model]
        age = None if at is None else round((as_of - at).total_seconds() / 3600, 1)
        runs.append(
            ModelRun(
                model=model, issued_at=at, age_h=age, stale=age is not None and age > STALE_AFTER_H
            )
        )
    used = [r.model for r in runs if not r.stale] or [r.model for r in runs]
    return runs, used


def derive_mountain(
    mountain: Mountain,
    rows: Iterable[HourlyRow],
    nws: Iterable[GridRow],
    *,
    anchor: datetime,
    models: Sequence[str],
    storm_words: Iterable[str] = (),
) -> MountainForecast:
    start = anchor.astimezone(UTC)
    hours = [start + timedelta(hours=i) for i in range(WINDOWS["5d"])]
    s = _Series(rows, hours)
    all_models = [m for m in open_meteo.MODELS if m in s.models()]
    snow = {m: s.get(m, "summit", "snowfall_in") for m in all_models}
    day_models = [m for m in (*open_meteo.MODELS, *open_meteo.FINE_MODELS) if m in s.models()]
    day_snow = {m: s.get(m, "summit", "snowfall_in") for m in day_models}
    snow_mean = s.mean(models, "summit", "snowfall_in")
    base_snow_mean = s.mean(models, "base", "snowfall_in")
    precip = s.mean(models, "summit", "precip_in")
    temp = s.mean(models, "summit", "temp_f")
    base_temp = s.mean(models, "base", "temp_f")
    gust = s.mean(models, "summit", "gust_mph")
    t850 = s.mean(models, "summit", "t850_f")
    rh850 = s.mean(models, "summit", "rh850")
    ws850 = s.mean(models, "summit", "ws850_mph")
    wd850 = s.get(DIRECTION_MODEL, "summit", "wd850")
    freezing = s.get(FREEZING_LEVEL_MODEL, "summit", "freezing_level_ft")

    grid: dict[str, list[GridRow]] = defaultdict(list)
    for row in nws:
        grid[row.layer].append(row)

    def nws_total(layer: str, a: datetime, b: datetime) -> float | None:
        if layer not in grid:
            return None
        total, covered = prorated_sum(grid[layer], a, b)
        return round(total, 1) if covered else None

    windows: dict[WindowKey, ForecastWindow] = {}
    for key, length in WINDOWS.items():
        per_model: dict[str, float | None] = {
            m: (_total(v[:length]) if any(x is not None for x in v[:length]) else None)
            for m, v in snow.items()
        }
        used_totals = [v for m, v in per_model.items() if m in models and v is not None]
        mean = _total(snow_mean[:length])
        lo, hi = (min(used_totals), max(used_totals)) if used_totals else (0.0, 0.0)
        end = start + timedelta(hours=length)
        nws_in = nws_total("snowfallAmount", start, end) if key in NWS_WINDOWS else None
        nws_ice = nws_total("iceAccumulation", start, end) if key in NWS_WINDOWS else None
        windows[key] = ForecastWindow(
            snow_in=round(mean, 1),
            range=(round(lo, 1), round(hi, 1)),
            confidence=confidence(lo, hi),
            by_model={m: None if v is None else round(v, 1) for m, v in per_model.items()},
            base_snow_in=round(_total(base_snow_mean[:length]), 1),
            nws_in=nws_in,
            nws_ice_in=nws_ice,
            nws_disagrees=nws_disagrees(mean, nws_in),
        )

    days: list[ForecastDay] = []
    for d in range(DAYS):
        a, z = 24 * d, 24 * (d + 1)
        hourly = [
            tagging.Hour(
                precip_in=precip[i],
                temp_f=temp[i],
                base_temp_f=base_temp[i],
                t850_f=t850[i],
                rh850=rh850[i],
                ws850_mph=ws850[i],
                wd850=wd850[i],
                gust_mph=gust[i],
            )
            for i in range(a, z)
        ]
        ice = nws_total("iceAccumulation", hours[a], hours[a] + timedelta(hours=24))
        day_tags = tagging.day_tags(
            hourly,
            wind_watch_mph=mountain.wind_hold_mph.watch,
            wind_likely_mph=mountain.wind_hold_mph.likely,
            nws_ice_in=ice,
        )
        snow_in = _total(snow_mean[a:z])
        precip_in = _total(precip[a:z])
        kinds = tagging.day_precip(
            hourly,
            snow_in=snow_in,
            base_snow_in=_total(base_snow_mean[a:z]),
            precip_in=precip_in,
            nws_ice_in=ice,
        )
        temps = [v for v in temp[a:z] if v is not None]
        gusts = [v for v in gust[a:z] if v is not None]
        levels = [v for v in freezing[a:z] if v is not None]
        local = (anchor + timedelta(days=d)).date()
        by_model = {
            m: round(_total(v[a:z]), 1)
            for m, v in day_snow.items()
            if sum(x is not None for x in v[a:z]) >= DAY_MIN_HOURS
        }
        days.append(
            ForecastDay(
                date=local,
                label=f"{local:%a} {local.day:02d}",
                snow_in=round(snow_in, 1),
                precip_in=round(precip_in, 2),
                precip_type=kinds.precip_type,
                hi_f=_round_int(max(temps)) if temps else None,
                lo_f=_round_int(min(temps)) if temps else None,
                gust_mph=_round_int(max(gusts)) if gusts else None,
                freezing_level_ft=_round_int(max(levels)) if levels else None,
                tags=day_tags,
                base_precip_type=kinds.base_precip_type,
                freezing_rain_at=list(kinds.freezing_rain_at),
                by_model=by_model,
            )
        )
    return MountainForecast(
        key=mountain.key,
        windows=windows,
        days=days,
        storm_type=tagging.storm_type(storm_words),
    )


def derive(
    mountains: Sequence[Mountain],
    inputs: Inputs,
    *,
    for_date: date,
    as_of: datetime,
    tz: str = TIMEZONE,
) -> Forecast:
    """The forecast for `for_date` from one ingest's rows, as known at `as_of`."""
    anchor = anchor_for(for_date, tz)
    runs, used = model_runs(inputs.hourly, as_of)
    words = {office: discussions.storm_words(text) for office, text in inputs.discussions.items()}
    by_mountain: dict[str, list[HourlyRow]] = defaultdict(list)
    for row in inputs.hourly:
        by_mountain[row.mountain].append(row)
    nws_by_mountain: dict[str, list[GridRow]] = defaultdict(list)
    for grid_row in inputs.nws:
        nws_by_mountain[grid_row.mountain].append(grid_row)
    out = [
        derive_mountain(
            m,
            by_mountain[m.key],
            nws_by_mountain[m.key],
            anchor=anchor,
            models=used,
            storm_words=words.get(m.nws.office, []),
        )
        for m in mountains
    ]
    return Forecast(
        date=for_date,
        anchor=anchor,
        as_of=as_of,
        ingest_run_id=inputs.ingest_run_id,
        models=runs,
        used_models=used,
        storm_words=words,
        mountains=out,
    )


# --- database ------------------------------------------------------------------------


def latest_ingest_run(conn: sqlite3.Connection, before: datetime | None = None) -> int | None:
    """The newest `ingest-forecast` run that finished ok (optionally started before `before`)."""
    sql = "SELECT id FROM run WHERE kind = 'ingest-forecast' AND status = 'ok'"
    params: list[str] = []
    if before is not None:
        sql += " AND started_at <= ?"
        params.append(before.astimezone(UTC).isoformat(timespec="seconds"))
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return None if row is None else int(row[0])


def load_inputs(conn: sqlite3.Connection, ingest_run_id: int, as_of: datetime) -> Inputs:
    """Rows from one ingest run, plus each office's latest AFD issued by `as_of`."""
    hourly = [
        HourlyRow(*r)
        for r in conn.execute(
            "SELECT h.mountain, h.model, h.elevation, h.issued_at, h.valid_at, h.lead_h,"
            " h.snowfall_in, h.precip_in, h.temp_f, h.gust_mph, h.freezing_level_ft,"
            " h.snow_depth_in, h.t850_f, h.rh850, h.ws850_mph, h.wd850"
            " FROM forecast_hourly h JOIN fetch f ON f.id = h.fetch_id WHERE f.run_id = ?",
            (ingest_run_id,),
        )
    ]
    nws = [
        GridRow(*r)
        for r in conn.execute(
            "SELECT g.mountain, g.layer, g.valid_start, g.hours, g.value, g.unit"
            " FROM nws_grid g JOIN fetch f ON f.id = g.fetch_id WHERE f.run_id = ?"
            " ORDER BY g.mountain, g.layer, g.valid_start",
            (ingest_run_id,),
        )
    ]
    texts: dict[str, str] = {}
    for office in discussions.OFFICES:
        row = conn.execute(
            "SELECT text FROM discussion WHERE office = ? AND kind = 'AFD' AND issued_at <= ?"
            " ORDER BY issued_at DESC LIMIT 1",
            (office, as_of.astimezone(UTC).isoformat(timespec="seconds")),
        ).fetchone()
        if row is not None:
            texts[office] = row[0]
    return Inputs(hourly=hourly, nws=nws, discussions=texts, ingest_run_id=ingest_run_id)


def store(conn: sqlite3.Connection, run_id: int, forecast: Forecast) -> None:
    """Write `forecast_window` (method `raw_mean`) and `forecast_day` rows for `run_id`."""
    windows = []
    days = []
    for m in forecast.mountains:
        for key, w in m.windows.items():
            windows.append(
                (
                    run_id,
                    m.key,
                    key,
                    "raw_mean",
                    w.snow_in,
                    w.range[0],
                    w.range[1],
                    w.confidence,
                    json.dumps(w.by_model),
                    w.base_snow_in,
                    w.nws_in,
                    w.nws_ice_in,
                )
            )
        for d in m.days:
            days.append(
                (
                    run_id,
                    m.key,
                    d.date.isoformat(),
                    d.snow_in,
                    d.precip_in,
                    d.precip_type,
                    d.hi_f,
                    d.lo_f,
                    d.gust_mph,
                    d.freezing_level_ft,
                    json.dumps(d.tags),
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO forecast_window (run_id, mountain, window, method, snow_in,"
        " lo_in, hi_in, confidence, by_model, base_snow_in, nws_in, nws_ice_in)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        windows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO forecast_day (run_id, mountain, day, snow_in, precip_in,"
        " precip_type, hi_f, lo_f, gust_mph, freezing_level_ft, tags)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        days,
    )
