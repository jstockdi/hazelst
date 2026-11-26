"""NWS api.weather.gov: grid points, gridpoint snow / ice / gust layers, and active alerts.

Gridpoint layers are values over ISO-8601 intervals (`2026-11-26T06:00:00+00:00/PT6H`)
in WMO units (`wmoUnit:mm`, `wmoUnit:km_h-1`); normalize stores them as given,
one row per interval. Proration into the 6 AM windows happens in `derive`.
See docs/design/data-sources.md.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

import httpx

from hazelst.config import Mountain
from hazelst.sources.base import Fetched, get

POINTS_SOURCE = "nws_points"
GRID_SOURCE = "nws_grid"
ALERTS_SOURCE = "nws_alerts"
API = "https://api.weather.gov"
GEO_JSON = {"Accept": "application/geo+json"}

LAYERS: tuple[str, ...] = ("snowfallAmount", "iceAccumulation", "windGust")
DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


class GridRow(NamedTuple):
    """One `nws_grid` row, in column order after `fetch_id`."""

    mountain: str
    layer: str
    valid_start: str
    hours: int
    value: float | None
    unit: str


class AlertRow(NamedTuple):
    """One `alert` row, without `first_seen_run` (set by `store_alerts`)."""

    id: str
    mountain: str
    event: str
    headline: str | None
    onset: str | None
    expires: str | None


# --- fetch --------------------------------------------------------------------------


def points_url(mountain: Mountain) -> str:
    return f"{API}/points/{mountain.lat},{mountain.lon}"


def grid_url(grid: str) -> str:
    """`grid` is `OFFICE/x,y` (for example `BTV/108,19`)."""
    return f"{API}/gridpoints/{grid}"


def parse_points(fetched: Fetched) -> str:
    """The `OFFICE/x,y` grid reference from a `/points` response."""
    try:
        props = fetched.json()["properties"]
        return f"{props['gridId']}/{int(props['gridX'])},{int(props['gridY'])}"
    except (KeyError, TypeError, ValueError) as exc:
        raise NormalizeError(f"points {fetched.url}: {exc}") from exc


def fetch_points(client: httpx.Client, mountain: Mountain) -> Fetched:
    return get(client, POINTS_SOURCE, points_url(mountain), meta={"mountain": mountain.key})


def fetch(
    client: httpx.Client,
    mountains: Sequence[Mountain],
    grids: Mapping[str, str] | None = None,
) -> list[Fetched]:
    """Points (only for mountains without a cached grid), gridpoints, and alerts.

    `grids` maps mountain key to a cached `OFFICE/x,y`; a mountain whose points
    lookup fails is skipped (its alerts are still fetched).
    """
    known = dict(grids or {})
    out: list[Fetched] = []
    for m in mountains:
        grid = known.get(m.key) or m.nws.grid
        if not grid:
            points = fetch_points(client, m)
            out.append(points)
            if points.http_status == 200:
                grid = parse_points(points)
        if grid:
            out.append(
                get(
                    client,
                    GRID_SOURCE,
                    grid_url(grid),
                    meta={"mountain": m.key, "grid": grid},
                    headers=GEO_JSON,
                )
            )
        out.append(
            get(
                client,
                ALERTS_SOURCE,
                f"{API}/alerts/active",
                {"point": f"{m.lat},{m.lon}"},
                meta={"mountain": m.key},
                headers=GEO_JSON,
            )
        )
    return out


def label(fetched: Fetched) -> str:
    kind = {POINTS_SOURCE: "points", GRID_SOURCE: "grid", ALERTS_SOURCE: "alerts"}[fetched.source]
    return f"{kind}-{fetched.meta['mountain']}"


# --- normalize ----------------------------------------------------------------------


def parse_interval(valid_time: str) -> tuple[datetime, int]:
    """`2026-11-25T12:00:00+00:00/PT6H` -> (start in UTC, hours); at least one hour."""
    start_text, _, duration = valid_time.partition("/")
    match = DURATION.match(duration)
    if not match:
        raise NormalizeError(f"unexpected interval {valid_time!r}")
    days, hours, minutes = (int(g or 0) for g in match.groups())
    total = days * 24 + hours + (1 if minutes else 0)
    start = datetime.fromisoformat(start_text).astimezone(UTC)
    return start, max(total, 1)


def _unit(uom: str) -> str:
    return uom.removeprefix("wmoUnit:")


def normalize_grid(fetched: Fetched) -> list[GridRow]:
    """One row per interval of each layer in `LAYERS`."""
    key = str(fetched.meta["mountain"])
    try:
        props: dict[str, Any] = fetched.json()["properties"]
    except (KeyError, TypeError) as exc:
        raise NormalizeError(f"gridpoint {fetched.url}: {exc}") from exc
    rows: list[GridRow] = []
    for layer in LAYERS:
        data = props.get(layer) or {}
        unit = _unit(str(data.get("uom", "")))
        for item in data.get("values", []):
            start, hours = parse_interval(item["validTime"])
            value = item.get("value")
            rows.append(
                GridRow(
                    mountain=key,
                    layer=layer,
                    valid_start=start.isoformat(timespec="seconds"),
                    hours=hours,
                    value=None if value is None else float(value),
                    unit=unit,
                )
            )
    return rows


def normalize_alerts(fetched: Fetched) -> list[AlertRow]:
    key = str(fetched.meta["mountain"])
    rows: list[AlertRow] = []
    for feature in fetched.json().get("features", []):
        props = feature.get("properties", {})
        alert_id = props.get("id") or feature.get("id")
        if not alert_id or not props.get("event"):
            continue
        rows.append(
            AlertRow(
                id=str(alert_id),
                mountain=key,
                event=str(props["event"]),
                headline=props.get("headline"),
                onset=props.get("onset") or props.get("effective"),
                expires=props.get("ends") or props.get("expires"),
            )
        )
    return rows


# --- store --------------------------------------------------------------------------


def store_grid(conn: sqlite3.Connection, fetch_id: int, rows: Iterable[GridRow]) -> int:
    params = [(fetch_id, *r) for r in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO nws_grid (fetch_id, mountain, layer, valid_start, hours, value,"
        " unit) VALUES (?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def store_alerts(conn: sqlite3.Connection, run_id: int, rows: Iterable[AlertRow]) -> int:
    """Insert alerts; an alert already seen keeps its `first_seen_run`."""
    params = [(*r, run_id) for r in rows]
    conn.executemany(
        "INSERT INTO alert (id, mountain, event, headline, onset, expires, first_seen_run)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (id, mountain) DO UPDATE SET headline = excluded.headline,"
        " onset = excluded.onset, expires = excluded.expires",
        params,
    )
    return len(params)


def cached_grids(conn: sqlite3.Connection) -> dict[str, str]:
    """Mountain key -> `OFFICE/x,y` from the `mountain` table (the points cache)."""
    rows = conn.execute("SELECT key, nws_grid FROM mountain WHERE nws_grid IS NOT NULL")
    return {r[0]: r[1] for r in rows}


def cache_grid(conn: sqlite3.Connection, mountain: str, grid: str) -> None:
    conn.execute("UPDATE mountain SET nws_grid = ? WHERE key = ?", (grid, mountain))
