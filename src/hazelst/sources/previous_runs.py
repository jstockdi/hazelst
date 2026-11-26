"""Open-Meteo Previous Runs: past summit forecasts at a fixed lead, for `season-backfill`.

`{var}_previous_day{N}` is each hour's value from the model run N days
before it, stitched over the date range (data-sources.md, "Open-Meteo
historical models"). It scores lead day N of the report: `previous_day1` for
the 1d window, and so on (decisions row 17). One request per lead day covers
every mountain (comma-separated coordinates, a list in the same order) and
GFS and GEM; ECMWF has no snowfall at lead and is not requested.

The rows go into `forecast_hourly` like a live ingest, at summit elevation,
with `issued_at` NULL (a stitched series has no single run) and a nominal
`lead_h` = 24 x (N - 1), so `skill-learn` places each day at lead day N.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx

from hazelst.config import Mountain
from hazelst.sources.base import Fetched, get
from hazelst.sources.open_meteo import FT_PER_M, HourlyRow, NormalizeError, _converter, isoformat

SOURCE = "open_meteo_previous_runs"
URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODELS: tuple[str, ...] = ("gfs_seamless", "gem_seamless")
LEAD_DAYS: tuple[int, ...] = (1, 2, 3, 4, 5)
ELEVATION = "summit"
# Open-Meteo variable (before `_previous_dayN`) -> forecast_hourly column.
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


def nominal_lead_h(lead_day: int) -> int:
    return 24 * (lead_day - 1)


def params(mountains: Sequence[Mountain], lead_day: int, start: date, end: date) -> dict[str, Any]:
    """Query for one lead day, every mountain at summit elevation, `start`..`end` (UTC dates)."""
    return {
        "latitude": ",".join(str(m.lat) for m in mountains),
        "longitude": ",".join(str(m.lon) for m in mountains),
        "elevation": ",".join(str(round(m.summit_ft / FT_PER_M)) for m in mountains),
        "hourly": ",".join(f"{v}_previous_day{lead_day}" for v in VARIABLES),
        "models": ",".join(MODELS),
        "timezone": "GMT",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }


def fetch(
    client: httpx.Client, mountains: Sequence[Mountain], lead_day: int, start: date, end: date
) -> Fetched:
    return get(
        client,
        SOURCE,
        URL,
        params(mountains, lead_day, start, end),
        meta={
            "mountains": [m.key for m in mountains],
            "elevation": ELEVATION,
            "lead_day": lead_day,
        },
    )


def normalize(fetched: Fetched) -> list[HourlyRow]:
    """Rows for every mountain, model, and hour with a value."""
    data = fetched.json()
    locations: list[dict[str, Any]] = data if isinstance(data, list) else [data]
    keys = [str(k) for k in fetched.meta.get("mountains", [])]
    lead_day = int(fetched.meta["lead_day"])
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
        for model in MODELS:
            columns: dict[str, list[float | None]] = {}
            for var, column in VARIABLES.items():
                name = f"{var}_previous_day{lead_day}_{model}"
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
