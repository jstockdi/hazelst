"""Open-Meteo Previous Runs: past summit forecasts at a fixed lead, for `season-backfill`.

`{var}_previous_day{N}` is each hour's value from the model run N days
before it, stitched over the date range (data-sources.md, "Open-Meteo
historical models"). It scores lead day N of the report: `previous_day1` for
the 1d window, and so on (decisions row 17). Two request sets, each one
request per lead day covering every mountain (comma-separated coordinates, a
list in the same order):

- the global models (`MODELS`: GFS and GEM global; ECMWF has no snowfall at
  lead and is not requested), lead days 1-5;
- the finer models with a lead-day-1 archive over the backfill (`FINE_MODELS`:
  HRRR, HRDPS, NBM; J34), lead day 1 only, so their day-1 candidates are
  scored at the same lead as the global models.

The rows go into `forecast_hourly` like a live ingest, at summit elevation,
with `issued_at` NULL (a stitched series has no single run) and a nominal
`lead_h` = 24 x (N - 1), so `skill-learn` places each day at lead day N.

The API has no `_previous_dayN` form of the 850 hPa variables (it rejects
`temperature_850hPa_previous_day1`); it serves them unsuffixed, from the
newest run before each hour (lead under 24 h). The global lead-day-1 request
adds those, so `nw_upslope` can be tagged on backfilled days for the
`resort_ratio` fit (data-sources.md, "Open-Meteo historical models").
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from hazelst.config import Mountain
from hazelst.sources import historical_forecast
from hazelst.sources.base import Fetched, get
from hazelst.sources.open_meteo import FT_PER_M, HourlyRow, NormalizeError, _converter, isoformat

SOURCE = "open_meteo_previous_runs"
URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
# Pure global models (J34): `gfs_seamless` / `gem_seamless` at lead day 1 are HRRR / HRDPS.
MODELS: tuple[str, ...] = ("gfs_global", "gem_global")
LEAD_DAYS: tuple[int, ...] = (1, 2, 3, 4, 5)
# Finer models with `snowfall_previous_day1` over the backfill (checked 2026-11-27 at
# Killington: HRDPS from 2024-01-20, HRRR and NBM from 2024-12-06).
FINE_MODELS: tuple[str, ...] = ("ncep_hrrr_conus", "gem_hrdps_continental", "ncep_nbm_conus")
FINE_LEAD_DAYS: tuple[int, ...] = (1,)
# Seconds between requests. A month is six requests of 24-48 of Open-Meteo's fractional
# calls (8 locations x 3 two-week blocks, x 2 over 10 variables): about 170 calls, so
# 2023-11..2026-04 is about 5,000, the hourly limit. 25 s apart keeps it near 4,000 an hour.
REQUEST_INTERVAL_S = 25.0
# A month of the global lead-day-1 request (14 variables) took over 40 s on 2026-11-28.
TIMEOUT_S = 180.0
ELEVATION = "summit"
# Open-Meteo variable (before `_previous_dayN`) -> forecast_hourly column.
VARIABLES: dict[str, str] = {
    "snowfall": "snowfall_in",
    "precipitation": "precip_in",
    "temperature_2m": "temp_f",
}
# 850 hPa variables, unsuffixed (newest run, lead under 24 h); requested with these lead days.
PRESSURE_VARIABLES: dict[str, str] = {
    "temperature_850hPa": "t850_f",
    "relative_humidity_850hPa": "rh850",
    "wind_speed_850hPa": "ws850_mph",
    "wind_direction_850hPa": "wd850",
}
PRESSURE_LEAD_DAYS: frozenset[int] = frozenset({1})
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


def nominal_lead_h(lead_day: int) -> int:
    return 24 * (lead_day - 1)


def requests() -> list[tuple[tuple[str, ...], int]]:
    """(models, lead day) of every request for one date range: the global set, then the finer."""
    return [(MODELS, n) for n in LEAD_DAYS] + [(FINE_MODELS, n) for n in FINE_LEAD_DAYS]


def _pressure(models: Sequence[str], lead_day: int) -> bool:
    """The 850 hPa variables go with the global models' lead-day-1 request only."""
    return tuple(models) == MODELS and lead_day in PRESSURE_LEAD_DAYS


def params(
    mountains: Sequence[Mountain],
    lead_day: int,
    start: date,
    end: date,
    models: Sequence[str] = MODELS,
) -> dict[str, Any]:
    """Query for one lead day, every mountain at summit elevation, `start`..`end` (UTC dates)."""
    hourly = [f"{v}_previous_day{lead_day}" for v in VARIABLES]
    pressure = _pressure(models, lead_day)
    if pressure:
        hourly.extend(PRESSURE_VARIABLES)
    out: dict[str, Any] = {
        "latitude": ",".join(str(m.lat) for m in mountains),
        "longitude": ",".join(str(m.lon) for m in mountains),
        "elevation": ",".join(str(round(m.summit_ft / FT_PER_M)) for m in mountains),
        "hourly": ",".join(hourly),
        "models": ",".join(models),
        "timezone": "GMT",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }
    if pressure:
        out["wind_speed_unit"] = "mph"
    return out


def fetch(
    client: httpx.Client,
    mountains: Sequence[Mountain],
    lead_day: int,
    start: date,
    end: date,
    models: Sequence[str] = MODELS,
) -> Fetched:
    return get(
        client,
        SOURCE,
        URL,
        params(mountains, lead_day, start, end, models),
        meta={
            "mountains": [m.key for m in mountains],
            "elevation": ELEVATION,
            "lead_day": lead_day,
            "models": list(models),
        },
        timeout_s=TIMEOUT_S,
    )


def normalize(fetched: Fetched) -> list[HourlyRow]:
    """Rows for every mountain, model, and hour with a value (models and variables as the
    request named them)."""
    data = fetched.json()
    locations: list[dict[str, Any]] = data if isinstance(data, list) else [data]
    keys = [str(k) for k in fetched.meta.get("mountains", [])]
    lead_day = int(fetched.meta["lead_day"])
    models = str(fetched.params.get("models", ",".join(MODELS))).split(",")
    pressure = set(PRESSURE_VARIABLES) <= set(str(fetched.params.get("hourly", "")).split(","))
    if len(locations) != len(keys):
        raise NormalizeError(f"{len(locations)} locations for {len(keys)} mountains")
    lead_h = nominal_lead_h(lead_day)
    rows: list[HourlyRow] = []
    for key, loc in zip(keys, locations, strict=True):
        if loc.get("utc_offset_seconds", 0) != 0:
            raise NormalizeError("expected GMT times")
        hourly: dict[str, list[Any]] = loc["hourly"]
        units: dict[str, str] = loc.get("hourly_units", {})
        times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in hourly["time"]]
        for model in models:
            columns: dict[str, list[float | None]] = {}
            names = {f"{var}_previous_day{lead_day}_{model}": c for var, c in VARIABLES.items()}
            if pressure:
                names.update({f"{var}_{model}": c for var, c in PRESSURE_VARIABLES.items()})
            for name, column in names.items():
                values = hourly.get(name)
                convert = _converter(units.get(name, "undefined"), column)
                if values is None or convert is None:
                    columns[column] = [None] * len(times)
                    continue
                columns[column] = [None if v is None else round(convert(v), 3) for v in values]
            for i, valid in enumerate(times):
                at = {c: columns[c][i] for c in columns}
                if at["snowfall_in"] is None:
                    continue
                values_at = {c: at.get(c) for c in COLUMNS}
                rows.append(
                    HourlyRow(
                        mountain=key,
                        model=model,
                        elevation=ELEVATION,
                        issued_at=None,
                        valid_at=isoformat(valid),
                        lead_h=lead_h,
                        **values_at,
                    )
                )
    return rows


def drop_superseded(
    conn: sqlite3.Connection,
    fetch_id: int,
    *,
    models: Sequence[str],
    lead_day: int,
    mountains: Sequence[str],
    start: date,
    end: date,
) -> int:
    """Delete stored rows that fetch `fetch_id` replaces: the same models, lead, and
    mountains over its UTC dates `start`..`end`, from earlier Previous Runs fetches (a
    re-run with a changed query) or Historical Forecast fetches (a finer model's
    short-lead series, which this lead-day-1 series replaces; J34). Without this the
    older rows would count twice, or at the wrong lead.
    """
    cur = conn.execute(
        "DELETE FROM forecast_hourly WHERE fetch_id IN"
        " (SELECT id FROM fetch WHERE source IN (?, ?) AND id != ?)"
        " AND elevation = ? AND lead_h = ? AND valid_at >= ? AND valid_at < ?"
        " AND model IN (SELECT value FROM json_each(?))"
        " AND mountain IN (SELECT value FROM json_each(?))",
        (
            SOURCE,
            historical_forecast.SOURCE,
            fetch_id,
            ELEVATION,
            nominal_lead_h(lead_day),
            isoformat(datetime.combine(start, datetime.min.time(), UTC)),
            isoformat(datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC)),
            json.dumps(sorted(models)),
            json.dumps(sorted(mountains)),
        ),
    )
    return cur.rowcount
