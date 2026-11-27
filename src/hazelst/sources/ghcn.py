"""GHCN-Daily station snowfall from the NCEI Access Data Service -> `snow_report`.

`www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries&stations=…
&dataTypes=SNOW,SNWD&startDate=…&endDate=…&format=json&units=standard` (no
token) returns one JSON object per station per date, values as strings in
inches. The stations are the ones in `config/stations.json` (generated once by
`scripts/make_stations.py`): GHCN stations within 20 mi of a mountain with
`SNOW` reported since 2024, plus Mount Washington for the eastern mountains.

Each date with `SNOW` becomes one `snow_report` row, `id = 'ghcn:{station}:{date}'`,
`product_id = 'GHCN'`, `hours = 24`, the station's elevation, and its nearest
listed mountain. Co-op (`USC`) and CoCoRaHS (`US1`) days end at the morning
observation, so `ending_at` is 7 AM local on the date; first-order stations
(`USW`, such as Mount Washington) report the calendar day, ending at midnight
after it. Verification then treats a co-op report within 5 mi like a 24 h PNS
report. See docs/design/data-sources.md, "GHCN-Daily stations".
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from hazelst import config
from hazelst.sources import snow_report
from hazelst.sources.base import Fetched, get
from hazelst.sources.snow_report import SnowReportRow

SOURCE = "ncei_ghcn"
URL = "https://www.ncei.noaa.gov/access/services/data/v1"
PRODUCT_ID = "GHCN"
DATA_TYPES = "SNOW,SNWD"
BATCH = 50  # stations per request; the service allows at most 100
MORNING_HOUR = 7  # co-op and CoCoRaHS observation time used for `ending_at`
CALENDAR_DAY_PREFIX = "USW"  # first-order stations report the calendar day
RECENT_DAYS = 10  # ingest-obs re-reads this many days (co-op reports arrive late)
STATIONS_FILE = "stations.json"


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


@dataclass(frozen=True)
class Station:
    """One `config/stations.json` entry: a station listed for one mountain."""

    id: str
    name: str
    lat: float
    lon: float
    elev_ft: int | None
    mountain: str
    distance_mi: float


def stations_path(settings: config.Settings) -> Path:
    """`stations.json` sits next to the mountain roster (`/app/config` in the image)."""
    return settings.mountains_path.with_name(STATIONS_FILE)


def load_stations(path: Path) -> tuple[Station, ...]:
    """Every entry in `stations.json` (a station near two mountains has two)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return tuple(Station(**entry) for entry in doc["stations"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise config.ConfigError(f"cannot read stations from {path}: {exc}") from exc


def nearest(stations: Iterable[Station]) -> dict[str, Station]:
    """Station id -> its entry for the closest listed mountain (the `snow_report` tag)."""
    out: dict[str, Station] = {}
    for s in stations:
        if s.id not in out or s.distance_mi < out[s.id].distance_mi:
            out[s.id] = s
    return out


def batches(ids: Iterable[str], size: int = BATCH) -> Iterator[list[str]]:
    ordered = sorted(set(ids))
    for i in range(0, len(ordered), size):
        yield ordered[i : i + size]


def params(ids: Sequence[str], start: date, end: date) -> dict[str, str]:
    return {
        "dataset": "daily-summaries",
        "stations": ",".join(ids),
        "dataTypes": DATA_TYPES,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "format": "json",
        "units": "standard",
    }


def fetch(client: httpx.Client, ids: Sequence[str], start: date, end: date) -> Fetched:
    return get(client, SOURCE, URL, params(ids, start, end))


def fetch_all(
    client: httpx.Client, stations: Iterable[Station], start: date, end: date
) -> list[Fetched]:
    """One request per batch of station ids for dates `start`..`end`."""
    return [fetch(client, ids, start, end) for ids in batches(s.id for s in stations)]


def ending_at(station_id: str, day: date, tz: str) -> datetime:
    """When a station's report for `day` ends (see the module docstring)."""
    zone = ZoneInfo(tz)
    if station_id.startswith(CALENDAR_DAY_PREFIX):
        return datetime.combine(day + timedelta(days=1), time(0), zone)
    return datetime.combine(day, time(MORNING_HOUR), zone)


def _inches(value: Any) -> float | None:
    try:
        out = float(str(value).strip())
    except ValueError:
        return None
    return out if out >= 0 else None


def normalize(
    fetched: Fetched, stations: dict[str, Station], tz: str = config.TIMEZONE
) -> list[SnowReportRow]:
    """One row per listed station per date with a `SNOW` value; others are skipped."""
    data: Any = fetched.json()
    if not isinstance(data, list):
        raise NormalizeError(f"{fetched.url}: expected a JSON list, got {type(data).__name__}")
    rows: list[SnowReportRow] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        station = stations.get(str(item.get("STATION", "")))
        snow = _inches(item.get("SNOW")) if "SNOW" in item else None
        if station is None or snow is None:
            continue
        try:
            day = date.fromisoformat(str(item.get("DATE")))
        except ValueError:
            continue
        rows.append(
            SnowReportRow(
                id=f"ghcn:{station.id}:{day.isoformat()}",
                product_id=PRODUCT_ID,
                place=station.name,
                lat=station.lat,
                lon=station.lon,
                elev_ft=station.elev_ft,
                ending_at=snow_report.isoformat(ending_at(station.id, day, tz)),
                snow_in=snow,
                hours=24,
                nearest_mountain=station.mountain,
                distance_mi=station.distance_mi,
            )
        )
    return rows


def store(conn: sqlite3.Connection, rows: Iterable[SnowReportRow]) -> int:
    """Insert or replace (a station's later, quality-checked value wins)."""
    return snow_report.store(conn, rows, "ghcn")


def recent_range(for_date: date) -> tuple[date, date]:
    """The dates `ingest-obs` reads for report date `for_date`."""
    return for_date - timedelta(days=RECENT_DAYS - 1), for_date


def record(client: httpx.Client, _mountains: Sequence[config.Mountain]) -> list[Fetched]:
    """`hazelst record ghcn`: the last 10 days for every listed station."""
    stations = load_stations(stations_path(config.settings_from_env()))
    start, end = recent_range(date.today())
    return fetch_all(client, stations, start, end)


def label(fetched: Fetched) -> str:
    ids = str(fetched.params.get("stations", "")).split(",")
    return f"{fetched.params.get('startDate')}_{ids[0]}_{len(ids)}"
