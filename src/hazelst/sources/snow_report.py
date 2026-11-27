"""Point snowfall reports (NWS PNS, IEM LSR, GHCN-Daily) and the `snow_report` table.

Each report is tagged with its nearest mountain and great-circle distance, so
`forecast-verify` can pick the reports within 5 mi of a mountain. The same
CoCoRaHS observation often appears in both an LSR and a PNS; both sources build
the same row id from the observation itself, and a PNS row replaces an LSR row
with the same id (docs/design/data-sources.md, "Choosing reports near each
mountain").
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from typing import Literal

from hazelst.config import Mountain

EARTH_RADIUS_MI = 3958.8

Kind = Literal["pns", "lsr", "ghcn"]


@dataclass(frozen=True)
class SnowReportRow:
    """One `snow_report` row, in column order."""

    id: str
    product_id: str
    place: str
    lat: float
    lon: float
    elev_ft: int | None
    ending_at: str
    snow_in: float
    hours: int | None
    nearest_mountain: str
    distance_mi: float


def distance_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def nearest_mountain(lat: float, lon: float, mountains: Sequence[Mountain]) -> tuple[str, float]:
    """The closest mountain to a point and its distance in miles (rounded to 0.1)."""
    best = min(mountains, key=lambda m: distance_mi(lat, lon, m.lat, m.lon))
    return best.key, round(distance_mi(lat, lon, best.lat, best.lon), 1)


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def report_id(lat: float, lon: float, ending_at: str, snow_in: float) -> str:
    """Row id shared by PNS and LSR: position to 0.01° (LSR precision), end time, amount."""
    return f"{lat:.2f},{lon:.2f},{ending_at},{snow_in:g}"


def make_row(
    *,
    product_id: str,
    place: str,
    lat: float,
    lon: float,
    ending_at: datetime,
    snow_in: float,
    hours: int | None,
    mountains: Sequence[Mountain],
) -> SnowReportRow:
    ending = isoformat(ending_at)
    nearest, miles = nearest_mountain(lat, lon, mountains)
    return SnowReportRow(
        id=report_id(lat, lon, ending, snow_in),
        product_id=product_id,
        place=place,
        lat=lat,
        lon=lon,
        elev_ft=None,  # neither PNS nor LSR gives an elevation
        ending_at=ending,
        snow_in=snow_in,
        hours=hours,
        nearest_mountain=nearest,
        distance_mi=miles,
    )


_COLUMNS = (
    "id, product_id, place, lat, lon, elev_ft, ending_at, snow_in, hours,"
    " nearest_mountain, distance_mi"
)


def store(conn: sqlite3.Connection, rows: Iterable[SnowReportRow], kind: Kind) -> int:
    """Write rows; returns how many were inserted or replaced.

    PNS rows replace any row with the same id (a later PNS, or the LSR copy of
    the same observation). LSR rows never replace an existing row. GHCN rows
    have their own ids (`ghcn:{station}:{date}`) and replace an earlier copy.
    """
    params = [astuple(r) for r in rows]
    verb = "INSERT OR IGNORE" if kind == "lsr" else "INSERT OR REPLACE"
    before = conn.total_changes
    conn.executemany(
        f"{verb} INTO snow_report ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params
    )
    return conn.total_changes - before
