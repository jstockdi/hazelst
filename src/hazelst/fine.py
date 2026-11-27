"""The finer models' day-1 candidates in the live forecast (J29, decisions row 30).

forecast-method.md, "Snow amount" (finer models). Each ingest collects five
finer models (`open_meteo.FINE_MODELS`); they compete for day 1 only, because
HRRR and HRDPS end 18-48 h after their run:

- **Candidates.** `<short>_ratio`: one finer model's day-1 summit snow water
  by temperature band times its own resolved `snow_ratio` (with the
  `nw_upslope` split, as `resort_ratio`); `fine_ratio`: the mean of those over
  the models with at least 15 fit days at the mountain; `nbm_raw`: NBM's
  native snowfall as is (`ratio.fine_values`, the same rule the backtest
  scores).
- **Inputs.** A model counts when its run is at most 18 h old at `as_of` and
  it covers at least 23 hours of day 1 (6 AM -> 6 AM).
- **Stored.** `report-build` writes each candidate's 1d `forecast_window` row,
  so `forecast-verify` scores it beside `raw_mean` every day.
- **Shown.** Where the method choice picked a finer-model candidate for a
  mountain's day 1, day 1 takes its value and each window shifts by the same
  difference, so the days still sum to the windows; days 2-5 stay on the
  mountain's global method. The 1d range is the min-max of the finer models'
  ratio values (the candidate's own value for a single model).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hazelst import learn, ratio
from hazelst.derive import STALE_AFTER_H, confidence, nws_disagrees
from hazelst.models import Forecast, ForecastWindow, MountainForecast, WindowKey
from hazelst.sources import open_meteo
from hazelst.sources.open_meteo import HourlyRow

WINDOW: WindowKey = "1d"


@dataclass(frozen=True)
class FineDay:
    """One mountain's day-1 candidates."""

    values: dict[str, float]  # method -> day-1 snow
    by_model: dict[str, float]  # finer model -> its ratio value


def fresh_models(hourly: Iterable[HourlyRow], as_of: datetime) -> set[str]:
    """Finer models whose newest run is at most 18 h old at `as_of`."""
    newest: dict[str, datetime] = {}
    for row in hourly:
        if not open_meteo.is_fine(row.model) or row.issued_at is None:
            continue
        at = datetime.fromisoformat(row.issued_at)
        if row.model not in newest or at > newest[row.model]:
            newest[row.model] = at
    limit = timedelta(hours=STALE_AFTER_H)
    return {m for m, at in newest.items() if as_of - at <= limit}


def compute_mountain(
    mf: MountainForecast,
    rows: Iterable[HourlyRow],
    ratios: ratio.Ratios,
    *,
    anchor: datetime,
    models: set[str],
) -> FineDay | None:
    """Day-1 candidates from one ingest's summit rows; None when no finer model covers day 1."""
    start = anchor.astimezone(UTC)
    end = start + timedelta(hours=24)
    hours: dict[str, list[tuple[float, float | None]]] = defaultdict(list)
    for row in rows:
        if row.model not in models or row.elevation != "summit" or row.snowfall_in is None:
            continue
        if start <= datetime.fromisoformat(row.valid_at) < end:
            hours[row.model].append((row.snowfall_in, row.temp_f))
    waters = {
        m: ratio._day_water(values) for m, values in hours.items() if len(values) >= learn.MIN_HOURS
    }
    if not waters:
        return None
    day = mf.days[0]
    ld = ratio.LeadDay(
        mountain=mf.key, day=day.date, upslope="nw_upslope" in day.tags, models=waters
    )
    values = ratio.fine_values(ld, ratios)
    if not values:
        return None
    by_model = {
        m: round(values[ratio.fine_method(m)], 1)
        for m in open_meteo.FINE_MODELS
        if ratio.fine_method(m) in values
    }
    return FineDay(values={k: round(v, 1) for k, v in values.items()}, by_model=by_model)


def compute(
    forecast: Forecast, hourly: Iterable[HourlyRow], ratios: ratio.Ratios
) -> dict[str, FineDay]:
    """Mountain -> day-1 candidates from the rows derive read (empty without finer models)."""
    rows = list(hourly)
    models = fresh_models(rows, forecast.as_of)
    if not models:
        return {}
    by_mountain: dict[str, list[HourlyRow]] = defaultdict(list)
    for row in rows:
        by_mountain[row.mountain].append(row)
    out: dict[str, FineDay] = {}
    for mf in forecast.mountains:
        found = compute_mountain(
            mf, by_mountain[mf.key], ratios, anchor=forecast.anchor, models=models
        )
        if found is not None:
            out[mf.key] = found
    return out


def _range(method: str, fd: FineDay) -> tuple[float, float]:
    """The 1d range: the finer models' spread for `fine_ratio`, else the value itself."""
    value = fd.values[method]
    if method == ratio.FINE_RATIO and fd.by_model:
        spread = list(fd.by_model.values())
        return min(spread), max(spread)
    return value, value


def _by_model(method: str, fd: FineDay) -> dict[str, float | None]:
    if method == ratio.FINE_RATIO:
        return dict(fd.by_model)
    if method == ratio.NBM_RAW:
        return {ratio.NBM_MODEL: fd.values[method]}
    model = next(m for m in open_meteo.FINE_MODELS if ratio.fine_method(m) == method)
    return {model: fd.values[method]}


def store_rows(
    conn: sqlite3.Connection, run_id: int, forecast: Forecast, computed: Mapping[str, FineDay]
) -> int:
    """A 1d `forecast_window` row per candidate; NWS and base values copy `raw_mean`'s.

    Call with the raw (derive) forecast, so the copied values are the model mean's.
    """
    params = []
    for mf in forecast.mountains:
        fd = computed.get(mf.key)
        if fd is None:
            continue
        raw = mf.windows[WINDOW]
        for method in fd.values:
            lo, hi = _range(method, fd)
            params.append(
                (
                    run_id,
                    mf.key,
                    WINDOW,
                    method,
                    fd.values[method],
                    lo,
                    hi,
                    confidence(lo, hi),
                    json.dumps(_by_model(method, fd)),
                    raw.base_snow_in,
                    raw.nws_in,
                    raw.nws_ice_in,
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO forecast_window (run_id, mountain, window, method, snow_in,"
        " lo_in, hi_in, confidence, by_model, base_snow_in, nws_in, nws_ice_in)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def apply(
    forecast: Forecast, computed: Mapping[str, FineDay], chosen: Mapping[str, str]
) -> Forecast:
    """The forecast with each mountain's chosen finer-model candidate as day 1.

    `chosen` maps a mountain to its day-1 method; a mountain whose method is
    not a finer-model candidate, or has no value today, is unchanged. Day 1
    takes the candidate's value, every window shifts by the same difference
    (floored at 0), and the 1d range and models are the candidate's.
    """
    mountains = []
    for mf in forecast.mountains:
        method = chosen.get(mf.key)
        fd = computed.get(mf.key)
        if method not in ratio.FINE_METHODS or fd is None or method not in fd.values:
            mountains.append(mf)
            continue
        value = fd.values[method]
        delta = value - mf.days[0].snow_in
        windows: dict[WindowKey, ForecastWindow] = {}
        for key, w in mf.windows.items():
            snow = round(max(w.snow_in + delta, 0.0), 1)
            if key == WINDOW:
                lo, hi = _range(method, fd)
                by_model = _by_model(method, fd)
            else:
                lo, hi = (round(max(x + delta, 0.0), 1) for x in w.range)
                by_model = w.by_model
            windows[key] = w.model_copy(
                update={
                    "snow_in": snow,
                    "range": (lo, hi),
                    "confidence": confidence(lo, hi),
                    "by_model": by_model,
                    "nws_disagrees": nws_disagrees(snow, w.nws_in),
                    "raw_mean_in": w.raw_mean_in if w.raw_mean_in is not None else w.snow_in,
                }
            )
        days = [mf.days[0].model_copy(update={"snow_in": value}), *mf.days[1:]]
        mountains.append(
            mf.model_copy(update={"windows": windows, "days": days, "day1_method": method})
        )
    return forecast.model_copy(update={"mountains": mountains})
