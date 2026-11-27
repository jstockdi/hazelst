"""Ground elevation of a point from the Open-Meteo elevation API.

`GET https://api.open-meteo.com/v1/elevation?latitude=a,b,…&longitude=a,b,…`
returns `{"elevation": [metres, …]}` in request order (Copernicus 90 m DEM),
up to 100 points per call. Elevations are stored in feet.

`fill()` sets `snow_report.elev_ft` on rows that have none (PNS and LSR give
no elevation; GHCN rows carry the station's). Points are keyed by latitude and
longitude rounded to 0.001° (about the DEM's resolution): a key already known
from another `snow_report` row costs no request. A failed lookup leaves the
row NULL and is counted; the next fill retries it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import httpx

from hazelst import db
from hazelst.sources import base

SOURCE = "open_meteo_elevation"
URL = "https://api.open-meteo.com/v1/elevation"
MAX_POINTS = 100
FT_PER_M = 3.28084
KEY_DECIMALS = 3

Key = tuple[float, float]


def key(lat: float, lon: float) -> Key:
    """The cache key for a point: latitude and longitude rounded to 0.001°."""
    return round(lat, KEY_DECIMALS), round(lon, KEY_DECIMALS)


def to_ft(metres: float) -> int:
    return round(metres * FT_PER_M)


def _batches(points: Sequence[Key], size: int = MAX_POINTS) -> Iterable[Sequence[Key]]:
    for i in range(0, len(points), size):
        yield points[i : i + size]


def _coords(values: Iterable[float]) -> str:
    return ",".join(f"{v:.{KEY_DECIMALS}f}" for v in values)


def lookup(client: httpx.Client, points: Sequence[Key]) -> dict[Key, int | None]:
    """Elevation in feet for each point; None for a point whose request failed."""
    out: dict[Key, int | None] = {}
    for batch in _batches(list(dict.fromkeys(points))):
        params = {
            "latitude": _coords(p[0] for p in batch),
            "longitude": _coords(p[1] for p in batch),
        }
        values: list[object] = []
        try:
            fetched = base.get(client, SOURCE, URL, params)
            if fetched.http_status == 200:
                body = fetched.json()
                values = list(body.get("elevation") or []) if isinstance(body, dict) else []
        except (base.SourceError, ValueError):
            values = []
        if len(values) != len(batch):
            values = [None] * len(batch)
        for point, value in zip(batch, values, strict=True):
            out[point] = to_ft(value) if isinstance(value, int | float) else None
    return out


@dataclass(frozen=True)
class FillResult:
    """Counts from one `fill()`: rows and distinct points without an elevation."""

    rows: int  # rows with NULL elev_ft before the fill
    points: int  # distinct keys among them
    cached: int  # keys already known from other rows
    requested: int  # keys sent to the API
    filled: int  # rows given an elevation
    failed: int  # rows still NULL (failed lookups)

    def as_dict(self) -> dict[str, int]:
        return {
            "rows": self.rows,
            "points": self.points,
            "cached": self.cached,
            "requested": self.requested,
            "filled": self.filled,
            "failed": self.failed,
        }


def known(conn: sqlite3.Connection) -> dict[Key, int]:
    """Elevations already stored in `snow_report`, by key."""
    out: dict[Key, int] = {}
    for lat, lon, elev in conn.execute(
        "SELECT lat, lon, elev_ft FROM snow_report"
        " WHERE elev_ft IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL"
    ):
        out.setdefault(key(lat, lon), int(elev))
    return out


def fill(conn: sqlite3.Connection, client: httpx.Client) -> FillResult:
    """Set `snow_report.elev_ft` where it is NULL; never raises on an HTTP failure."""
    missing = conn.execute(
        "SELECT id, lat, lon FROM snow_report"
        " WHERE elev_ft IS NULL AND lat IS NOT NULL AND lon IS NOT NULL"
    ).fetchall()
    if not missing:
        return FillResult(0, 0, 0, 0, 0, 0)
    by_key: dict[Key, list[str]] = {}
    for row_id, lat, lon in missing:
        by_key.setdefault(key(lat, lon), []).append(row_id)
    cache = known(conn)
    found: dict[Key, int | None] = {k: cache[k] for k in by_key if k in cache}
    wanted = [k for k in by_key if k not in found]
    if wanted:
        found.update(lookup(client, wanted))
    updates = [
        (elev, row_id)
        for k, ids in by_key.items()
        if (elev := found.get(k)) is not None
        for row_id in ids
    ]
    with db.transaction(conn):
        conn.executemany("UPDATE snow_report SET elev_ft = ? WHERE id = ?", updates)
    return FillResult(
        rows=len(missing),
        points=len(by_key),
        cached=len(by_key) - len(wanted),
        requested=len(wanted),
        filled=len(updates),
        failed=len(missing) - len(updates),
    )


def safe_fill(conn: sqlite3.Connection, client: httpx.Client) -> Mapping[str, object]:
    """`fill()` for the ingest steps: an unexpected error is reported, not raised."""
    try:
        return fill(conn, client).as_dict()
    except (sqlite3.Error, httpx.HTTPError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
