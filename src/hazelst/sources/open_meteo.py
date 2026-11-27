"""Open-Meteo forecast: summit and base hourly series for every mountain.

Two model sets: the three global models (`MODELS`), which feed every window,
and the finer models (`FINE_MODELS`, J29), which compete for day 1 only. One
call per elevation and set covers all mountains (comma-separated coordinates;
the response is a list in the same order). Each ingest also fetches each
model's `meta.json` so `forecast_hourly.issued_at` is the model run time, not
the fetch time. See docs/design/data-sources.md.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple

import httpx

from hazelst.config import Mountain
from hazelst.sources.base import Fetched, get

SOURCE = "open_meteo"
META_SOURCE = "open_meteo_meta"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# One past model run per call (`run=`); used to rebuild a past day's inputs as fixtures.
SINGLE_RUN_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
META_URL = "https://api.open-meteo.com/data/{meta_id}/static/meta.json"

MODELS: tuple[str, ...] = ("gfs_seamless", "ecmwf_ifs025", "gem_seamless")
# Finer models (J29, decisions row 30): collected every run, candidates for day 1 only.
FINE_MODELS: tuple[str, ...] = (
    "ecmwf_ifs",  # ECMWF IFS HRES 9 km
    "ncep_hrrr_conus",  # NCEP HRRR CONUS 3 km
    "gem_hrdps_continental",  # GEM HRDPS Continental 2.5 km
    "ncep_nbm_conus",  # NCEP NBM CONUS (native snowfall)
    "ecmwf_aifs025_single",  # ECMWF AIFS 0.25 degree
)
# meta.json id that carries each model's run time. gfs_seamless blends HRRR
# into the first hours; its run time is the GFS run.
META_IDS: dict[str, str] = {
    "gfs_seamless": "ncep_gfs013",
    "ecmwf_ifs025": "ecmwf_ifs025",
    "gem_seamless": "cmc_gem_gdps_15km",
    "ecmwf_ifs": "ecmwf_ifs",
    "ncep_hrrr_conus": "ncep_hrrr_conus",
    "gem_hrdps_continental": "cmc_gem_hrdps",
    "ncep_nbm_conus": "ncep_nbm_conus",
    "ecmwf_aifs025_single": "ecmwf_aifs025_single",
}

Elevation = Literal["summit", "base"]
ELEVATIONS: tuple[Elevation, ...] = ("summit", "base")
FT_PER_M = 3.28084

# Open-Meteo hourly variable -> forecast_hourly column.
VARIABLES: dict[str, str] = {
    "snowfall": "snowfall_in",
    "precipitation": "precip_in",
    "temperature_2m": "temp_f",
    "wind_gusts_10m": "gust_mph",
    "freezing_level_height": "freezing_level_ft",
    "snow_depth": "snow_depth_in",
    "temperature_850hPa": "t850_f",
    "relative_humidity_850hPa": "rh850",
    "wind_speed_850hPa": "ws850_mph",
    "wind_direction_850hPa": "wd850",
}

# The fine-model call: day 1 needs snow and summit temperature only (ratio bands),
# and fewer variables and days keep the call's weight under Open-Meteo's limits.
FINE_VARIABLES: tuple[str, ...] = ("snowfall", "precipitation", "temperature_2m")
FINE_PAST_DAYS = 1
FINE_FORECAST_DAYS = 3

PAST_DAYS = 7
FORECAST_DAYS = 7


def is_fine(model: str) -> bool:
    return model in FINE_MODELS


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


class HourlyRow(NamedTuple):
    """One `forecast_hourly` row, in column order after `fetch_id`."""

    mountain: str
    model: str
    elevation: str
    issued_at: str | None
    valid_at: str
    lead_h: int | None
    snowfall_in: float | None
    precip_in: float | None
    temp_f: float | None
    gust_mph: float | None
    freezing_level_ft: float | None
    snow_depth_in: float | None
    t850_f: float | None
    rh850: float | None
    ws850_mph: float | None
    wd850: float | None


@dataclass(frozen=True)
class RunTime:
    """A model's latest run as reported by its meta.json."""

    meta_id: str
    initialised_at: datetime
    available_at: datetime
    data_end: datetime
    update_interval_s: int
    estimated: bool = False


# --- fetch --------------------------------------------------------------------------


def forecast_params(
    mountains: Sequence[Mountain], elevation: Elevation, models: Sequence[str] = MODELS
) -> dict[str, Any]:
    feet = [m.summit_ft if elevation == "summit" else m.base_ft for m in mountains]
    return {
        "latitude": ",".join(str(m.lat) for m in mountains),
        "longitude": ",".join(str(m.lon) for m in mountains),
        "elevation": ",".join(str(round(ft / FT_PER_M)) for ft in feet),
        "hourly": ",".join(VARIABLES),
        "models": ",".join(models),
        "timezone": "GMT",
        "past_days": PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
    }


def fine_params(mountains: Sequence[Mountain], elevation: Elevation) -> dict[str, Any]:
    """The finer models' call: day-1 variables, one past day, three forecast days."""
    params = forecast_params(mountains, elevation, FINE_MODELS)
    params.update(
        {
            "hourly": ",".join(FINE_VARIABLES),
            "past_days": FINE_PAST_DAYS,
            "forecast_days": FINE_FORECAST_DAYS,
        }
    )
    return params


def fetch(client: httpx.Client, mountains: Sequence[Mountain]) -> list[Fetched]:
    """Each model's meta.json, then the summit and base forecast for every mountain:
    the global models, then the finer models (meta `models`)."""
    out: list[Fetched] = []
    for model, meta_id in META_IDS.items():
        out.append(
            get(
                client,
                META_SOURCE,
                META_URL.format(meta_id=meta_id),
                meta={"model": model, "meta_id": meta_id},
            )
        )
    keys = [m.key for m in mountains]
    for elevation in ELEVATIONS:
        out.append(
            get(
                client,
                SOURCE,
                FORECAST_URL,
                forecast_params(mountains, elevation),
                meta={"elevation": elevation, "mountains": keys},
            )
        )
    for elevation in ELEVATIONS:
        out.append(
            get(
                client,
                SOURCE,
                FORECAST_URL,
                fine_params(mountains, elevation),
                meta={"elevation": elevation, "mountains": keys, "models": list(FINE_MODELS)},
            )
        )
    return out


def is_fine_fetch(fetched: Fetched) -> bool:
    """A forecast fetch of the finer models (its failure does not fail the ingest)."""
    return bool(fetched.meta.get("models")) and all(is_fine(str(m)) for m in fetched.meta["models"])


def fetch_single_runs(
    client: httpx.Client, mountains: Sequence[Mountain], runs: Mapping[str, datetime]
) -> list[Fetched]:
    """The summit and base forecast from one given run of each model (Single Runs API).

    No meta.json is needed: each response records its run time in `meta.run`.
    """
    out: list[Fetched] = []
    keys = [m.key for m in mountains]
    for model, run in runs.items():
        if model not in MODELS:
            raise ValueError(f"unknown model {model!r}")
        for elevation in ELEVATIONS:
            params = forecast_params(mountains, elevation, [model])
            del params["past_days"]
            params["run"] = run.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")
            out.append(
                get(
                    client,
                    SOURCE,
                    SINGLE_RUN_URL,
                    params,
                    meta={
                        "elevation": elevation,
                        "mountains": keys,
                        "model": model,
                        "run": isoformat(run),
                    },
                )
            )
    return out


def label(fetched: Fetched) -> str:
    """A short, stable name for a recorded response (fixture file name)."""
    if fetched.source == META_SOURCE:
        return f"meta-{fetched.meta['model']}"
    if "model" in fetched.meta:
        return f"forecast-{fetched.meta['elevation']}-{fetched.meta['model']}"
    if is_fine_fetch(fetched):
        return f"forecast-{fetched.meta['elevation']}-fine"
    return f"forecast-{fetched.meta['elevation']}"


# --- run times --------------------------------------------------------------------------


def _ts(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), UTC)


def parse_meta(fetched: Fetched) -> RunTime:
    data = fetched.json()
    try:
        return RunTime(
            meta_id=str(fetched.meta.get("meta_id", "")),
            initialised_at=_ts(data["last_run_initialisation_time"]),
            available_at=_ts(data["last_run_availability_time"]),
            data_end=_ts(data["data_end_time"]),
            update_interval_s=int(data["update_interval_seconds"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NormalizeError(f"meta.json {fetched.url}: {exc}") from exc


def effective_run_time(run: RunTime, fetched_at: datetime) -> RunTime:
    """The run time to record for data fetched at `fetched_at`.

    A meta.json whose data ends before the fetch cannot describe the data
    served (seen for `cmc_gem_gdps`, which stopped updating on 2026-05-26
    while GEM data stays current). The run is then estimated from the model's
    own cycle: the latest initialisation, on its update interval, that would
    be available by the fetch given the meta file's last observed delay.
    """
    if run.data_end >= fetched_at:
        return run
    delay = run.available_at - run.initialised_at
    interval = run.update_interval_s
    latest = (fetched_at - delay).timestamp()
    init = datetime.fromtimestamp(math.floor(latest / interval) * interval, UTC)
    return RunTime(
        meta_id=run.meta_id,
        initialised_at=init,
        available_at=init + delay,
        data_end=run.data_end,
        update_interval_s=interval,
        estimated=True,
    )


def run_times(metas: Iterable[Fetched]) -> dict[str, RunTime]:
    """Model -> effective run time, from the meta.json fetches of one ingest."""
    out: dict[str, RunTime] = {}
    for fetched in metas:
        if fetched.http_status != 200:
            continue
        run = parse_meta(fetched)
        fetched_at = datetime.fromisoformat(fetched.fetched_at)
        out[str(fetched.meta["model"])] = effective_run_time(run, fetched_at)
    return out


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


# --- normalize ----------------------------------------------------------------------


def _converter(unit: str, column: str) -> Any:
    """A function converting a value in `unit` to the column's unit, or None if undefined."""
    if unit in ("undefined", ""):
        return None
    table: dict[tuple[str, str], Any] = {
        ("inch", "in"): lambda v: v,
        ("mm", "in"): lambda v: v / 25.4,
        ("cm", "in"): lambda v: v / 2.54,
        ("ft", "in"): lambda v: v * 12,
        ("m", "in"): lambda v: v * 39.3701,
        ("ft", "ft"): lambda v: v,
        ("m", "ft"): lambda v: v * FT_PER_M,
        ("°F", "f"): lambda v: v,
        ("°C", "f"): lambda v: v * 9 / 5 + 32,
        ("mp/h", "mph"): lambda v: v,
        ("km/h", "mph"): lambda v: v / 1.609344,
        ("%", "pct"): lambda v: v,
        ("°", "deg"): lambda v: v,
    }
    target = {
        "snowfall_in": "in",
        "precip_in": "in",
        "snow_depth_in": "in",
        "freezing_level_ft": "ft",
        "temp_f": "f",
        "t850_f": "f",
        "gust_mph": "mph",
        "ws850_mph": "mph",
        "rh850": "pct",
        "wd850": "deg",
    }[column]
    try:
        return table[(unit, target)]
    except KeyError:
        raise NormalizeError(f"unexpected unit {unit!r} for {column}") from None


def _valid_at(time: str) -> datetime:
    return datetime.fromisoformat(time).replace(tzinfo=UTC)


def normalize(
    fetched: Fetched, runs: Mapping[str, RunTime], *, since: str | None = None
) -> list[HourlyRow]:
    """Rows for every mountain, model, and hour with at least one value.

    `fetched` is one forecast response (one elevation; its models are
    `meta.models`, else the global `MODELS`); `runs` maps each model to its
    run time (a model missing from `runs` gets `issued_at` NULL). A single-run
    response names its model and run in `fetched.meta` and has unsuffixed
    variable names. `since` (`valid_at` text) drops earlier hours.
    """
    data = fetched.json()
    locations: list[dict[str, Any]] = data if isinstance(data, list) else [data]
    keys: list[str] = list(fetched.meta.get("mountains", []))
    elevation = str(fetched.meta.get("elevation", ""))
    if elevation not in ELEVATIONS:
        raise NormalizeError(f"unknown elevation {elevation!r}")
    if len(locations) != len(keys):
        raise NormalizeError(f"{len(locations)} locations for {len(keys)} mountains")
    single = fetched.meta.get("model")
    listed = fetched.meta.get("models")
    models: tuple[str, ...] = (
        (str(single),) if single else tuple(str(m) for m in listed) if listed else MODELS
    )
    if single and "run" in fetched.meta:
        at = datetime.fromisoformat(str(fetched.meta["run"]))
        runs = {str(single): RunTime(str(single), at, at, at, 0)}
    rows: list[HourlyRow] = []
    for key, loc in zip(keys, locations, strict=True):
        if loc.get("utc_offset_seconds", 0) != 0:
            raise NormalizeError("expected GMT times")
        hourly: dict[str, list[Any]] = loc["hourly"]
        units: dict[str, str] = loc.get("hourly_units", {})
        times = [_valid_at(t) for t in hourly["time"]]
        for model in models:
            columns: dict[str, list[float | None]] = {}
            for var, column in VARIABLES.items():
                name = var if single else f"{var}_{model}"
                values = hourly.get(name)
                convert = _converter(units.get(name, "undefined"), column)
                if values is None or convert is None:
                    columns[column] = [None] * len(times)
                    continue
                columns[column] = [None if v is None else round(convert(v), 3) for v in values]
            run = runs.get(model)
            issued = isoformat(run.initialised_at) if run else None
            for i, valid in enumerate(times):
                values_at = {c: series[i] for c, series in columns.items()}
                if all(v is None for v in values_at.values()):
                    continue
                if since is not None and isoformat(valid) < since:
                    continue
                lead = round((valid - run.initialised_at).total_seconds() / 3600) if run else None
                rows.append(
                    HourlyRow(
                        mountain=key,
                        model=model,
                        elevation=elevation,
                        issued_at=issued,
                        valid_at=isoformat(valid),
                        lead_h=lead,
                        **values_at,
                    )
                )
    return rows


def store(conn: sqlite3.Connection, fetch_id: int, rows: Iterable[HourlyRow]) -> int:
    """Insert normalized rows for one fetch; returns the number written."""
    params = [(fetch_id, *r) for r in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO forecast_hourly (fetch_id, mountain, model, elevation, issued_at,"
        " valid_at, lead_h, snowfall_in, precip_in, temp_f, gust_mph, freezing_level_ft,"
        " snow_depth_in, t850_f, rh850, ws850_mph, wd850)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)
