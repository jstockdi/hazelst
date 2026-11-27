"""`hazelst stations summary`: season snowfall at each GHCN station near a mountain.

Season Y runs from July 1 of Y to June 30 of Y + 1 (season 2025 is 2025-26).
For each mountain, every station listed for it in `stations.json` is shown
with its elevation, distance, season snowfall (the sum of its daily `SNOW`
rows in `snow_report`), and the number of days it reported, highest station
first, next to the SnoCountry season total (the sum of `observation` rows
with `source = 'snocountry'`) when that source has rows for the season.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from hazelst.config import Mountain
from hazelst.sources import ghcn


@dataclass(frozen=True)
class StationSeason:
    id: str
    name: str
    elev_ft: int | None
    distance_mi: float
    season_in: float | None
    days: int


@dataclass(frozen=True)
class MountainSeason:
    mountain: str
    name: str
    snocountry_in: float | None
    snocountry_days: int
    stations: list[StationSeason]


def season_range(season: int) -> tuple[date, date]:
    return date(season, 7, 1), date(season + 1, 6, 30)


def _station_totals(
    conn: sqlite3.Connection, first: date, last: date
) -> dict[str, tuple[float, int]]:
    """Station id -> (season snowfall, days reported) from GHCN `snow_report` rows."""
    out: dict[str, tuple[float, int]] = {}
    for row_id, snow_in in conn.execute(
        "SELECT id, snow_in FROM snow_report WHERE product_id = ?", (ghcn.PRODUCT_ID,)
    ):
        _, station, day = str(row_id).split(":")
        if first.isoformat() <= day <= last.isoformat():
            total, days = out.get(station, (0.0, 0))
            out[station] = (total + float(snow_in), days + 1)
    return out


def _snocountry(
    conn: sqlite3.Connection, mountain: str, first: date, last: date
) -> tuple[float | None, int]:
    row = conn.execute(
        "SELECT sum(new_24h_in), count(*) FROM observation WHERE mountain = ?"
        " AND source = 'snocountry' AND obs_date BETWEEN ? AND ? AND new_24h_in IS NOT NULL",
        (mountain, first.isoformat(), last.isoformat()),
    ).fetchone()
    return (None if row[0] is None else round(float(row[0]), 1)), int(row[1])


def summary(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    stations: Sequence[ghcn.Station],
    season: int,
) -> list[MountainSeason]:
    first, last = season_range(season)
    totals = _station_totals(conn, first, last)
    out = []
    for m in mountains:
        listed = []
        for s in stations:
            if s.mountain != m.key:
                continue
            total, days = totals.get(s.id, (None, 0))
            listed.append(
                StationSeason(
                    s.id,
                    s.name,
                    s.elev_ft,
                    s.distance_mi,
                    None if total is None else round(total, 1),
                    days,
                )
            )
        listed.sort(key=lambda s: (-(s.elev_ft or 0), s.distance_mi))
        sc_in, sc_days = _snocountry(conn, m.key, first, last)
        out.append(MountainSeason(m.key, m.name, sc_in, sc_days, listed))
    return out


def as_dict(rows: Sequence[MountainSeason]) -> list[dict[str, Any]]:
    return [
        {
            "mountain": r.mountain,
            "snocountry_in": r.snocountry_in,
            "snocountry_days": r.snocountry_days,
            "stations": [s.__dict__ for s in r.stations],
        }
        for r in rows
    ]


def format_text(rows: Sequence[MountainSeason], season: int) -> str:
    lines = []
    for r in rows:
        sc = (
            f"{r.snocountry_in:.1f} in ({r.snocountry_days} days)"
            if r.snocountry_in is not None
            else "no rows"
        )
        lines.append(f"{r.name} {season}-{(season + 1) % 100:02d}: SnoCountry {sc}")
        lines.append(f"  {'elev ft':>7} {'mi':>5} {'season in':>9} {'days':>4}  station")
        for s in r.stations:
            elev = f"{s.elev_ft:,}" if s.elev_ft is not None else "?"
            total = f"{s.season_in:.1f}" if s.season_in is not None else "-"
            lines.append(
                f"  {elev:>7} {s.distance_mi:>5.1f} {total:>9} {s.days:>4}  {s.name} ({s.id})"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
