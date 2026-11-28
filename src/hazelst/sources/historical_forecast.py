"""Open-Meteo Historical Forecast: past summit series of the finer models with no lead
archive, for `season-backfill`.

The Historical Forecast API stitches the first hours of successive runs into
one hourly series (data-sources.md, "Open-Meteo historical models"), so a
day's values are short-lead output. They stand in for the day-1 forecast of
ECMWF IFS 9 km and AIFS only (`MODELS`; decisions rows 30 and 35): Previous
Runs has no `snowfall_previous_day1` for them before 2025-10. HRRR, HRDPS,
and NBM come from Previous Runs at lead day 1, and the global models keep
Previous Runs at the matching lead (decisions row 17).

One request per model and range covers every mountain (comma-separated
coordinates, a list in the same order). A model is requested only for dates
on or after its first archived day (`FIRST_DAY`).

The rows go into `forecast_hourly` like a live ingest, at summit elevation,
with `issued_at` NULL (a stitched series has no single run) and nominal
`lead_h` 0, so `skill-learn` and the `resort_ratio` fit place each day at
lead day 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx

from hazelst.config import Mountain
from hazelst.sources.base import Fetched, get
from hazelst.sources.open_meteo import (
    FINE_MODELS,
    FT_PER_M,
    HourlyRow,
    NormalizeError,
    _converter,
    isoformat,
)

SOURCE = "open_meteo_historical_forecast"
URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ELEVATION = "summit"
LEAD_H = 0  # nominal: a stitched short-lead series stands in for lead day 1
# The finer models backfilled here (J34); the others come from Previous Runs at lead day 1.
MODELS: tuple[str, ...] = ("ecmwf_ifs", "ecmwf_aifs025_single")
# First archived day per model (Open-Meteo Historical Forecast docs, checked 2026-11-27).
FIRST_DAY: dict[str, date] = {
    "ecmwf_ifs": date(2017, 1, 1),
    "ecmwf_aifs025_single": date(2025, 2, 20),
}
# Open-Meteo variable -> forecast_hourly column.
VARIABLES: dict[str, str] = {
    "snowfall": "snowfall_in",
    "precipitation": "precip_in",
    "temperature_2m": "temp_f",
}
COLUMNS: tuple[str, ...] = (
    "snowfall_in",
    "precip_in",
    "temp_f",
    "gust_mph",
    "freezing_level_ft",
    "snow_depth_in",
    "t850_f",
    "rh850",
    "ws850_mph",
    "wd850",
)
# Seconds between requests: one request is one model x 8 mountains x up to 32 days x
# 3 variables, about 18 of Open-Meteo's fractional calls (more than two weeks per
# location counts extra); 10 s apart keeps a month's backfill near 110 calls a minute,
# under the 600-a-minute and 5,000-an-hour limits.
REQUEST_INTERVAL_S = 10.0

assert set(FIRST_DAY) == set(MODELS) <= set(FINE_MODELS)


def window(model: str, start: date, end: date) -> tuple[date, date] | None:
    """UTC dates `start`..`end` clipped to the model's archive; None if it has none."""
    first = max(start, FIRST_DAY[model])
    return None if first > end else (first, end)


def params(mountains: Sequence[Mountain], model: str, start: date, end: date) -> dict[str, Any]:
    """Query for one model, every mountain at summit elevation, `start`..`end` (UTC dates)."""
    return {
        "latitude": ",".join(str(m.lat) for m in mountains),
        "longitude": ",".join(str(m.lon) for m in mountains),
        "elevation": ",".join(str(round(m.summit_ft / FT_PER_M)) for m in mountains),
        "hourly": ",".join(VARIABLES),
        "models": model,
        "timezone": "GMT",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }


def fetch(
    client: httpx.Client, mountains: Sequence[Mountain], model: str, start: date, end: date
) -> Fetched:
    return get(
        client,
        SOURCE,
        URL,
        params(mountains, model, start, end),
        meta={"mountains": [m.key for m in mountains], "elevation": ELEVATION, "model": model},
    )


def normalize(fetched: Fetched) -> list[HourlyRow]:
    """Rows for every mountain and hour with a snowfall value (one model per response)."""
    data = fetched.json()
    locations: list[dict[str, Any]] = data if isinstance(data, list) else [data]
    keys = [str(k) for k in fetched.meta.get("mountains", [])]
    model = str(fetched.meta.get("model", ""))
    if model not in FINE_MODELS:  # earlier fetches also hold HRRR, HRDPS, and NBM
        raise NormalizeError(f"unknown model {model!r}")
    if len(locations) != len(keys):
        raise NormalizeError(f"{len(locations)} locations for {len(keys)} mountains")
    rows: list[HourlyRow] = []
    for key, loc in zip(keys, locations, strict=True):
        if loc.get("utc_offset_seconds", 0) != 0:
            raise NormalizeError("expected GMT times")
        hourly: dict[str, list[Any]] = loc["hourly"]
        units: dict[str, str] = loc.get("hourly_units", {})
        times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in hourly["time"]]
        columns: dict[str, list[float | None]] = {}
        for var, column in VARIABLES.items():
            # A single-model request names variables without the model suffix.
            values = hourly.get(var, hourly.get(f"{var}_{model}"))
            convert = _converter(units.get(var, units.get(f"{var}_{model}", "undefined")), column)
            if values is None or convert is None:
                columns[column] = [None] * len(times)
                continue
            columns[column] = [None if v is None else round(convert(v), 3) for v in values]
        for i, valid in enumerate(times):
            if columns["snowfall_in"][i] is None:
                continue
            values_at = {c: columns[c][i] if c in columns else None for c in COLUMNS}
            rows.append(
                HourlyRow(
                    mountain=key,
                    model=model,
                    elevation=ELEVATION,
                    issued_at=None,
                    valid_at=isoformat(valid),
                    lead_h=LEAD_H,
                    **values_at,
                )
            )
    return rows
