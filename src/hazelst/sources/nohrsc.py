"""NOAA NOHRSC National Snowfall Analysis v2: gridded snowfall at each mountain.

`www.nohrsc.noaa.gov/snowfall_v2/data/{YYYYMM}/sfav2_CONUS_{24h|48h}_{YYYYMMDD}12.tif`
and the season-to-date grid `sfav2_CONUS_{SEASON_START}12_to_{YYYYMMDD}12.tif`.
Each grid ends at 12Z on its date and appears about 12 hours later (00Z the
next day). Values are inches; the nearest pixel (the 0.04° cell containing the
mountain's coordinates) is read. See docs/design/data-sources.md, "NOHRSC
National Snowfall Analysis".

One `observation` row per mountain per end date (`obs_date` = the end date,
`reported_at` = 12Z on it), combining the 24 h, 48 h, and season grids.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

import httpx

from hazelst.config import Mountain
from hazelst.sources import geotiff
from hazelst.sources.base import Fetched, get

SOURCE = "nohrsc"
BASE_URL = "https://www.nohrsc.noaa.gov/snowfall_v2/data"

Product = Literal["24h", "48h", "season"]
PRODUCTS: tuple[Product, ...] = ("24h", "48h", "season")
COLUMNS: dict[Product, str] = {"24h": "new_24h_in", "48h": "new_48h_in", "season": "season_in"}

# A grid ending 12Z on day D is published about 00:06Z on D+1.
AVAILABLE_AFTER = timedelta(hours=12, minutes=30)
# Grids are reprocessed later (2026-02-07 was regenerated on 2026-02-12), so a
# grid is read once more when it is this old.
REPROCESS_AFTER_DAYS = 7


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


def season_start(end: date) -> date:
    """Start of the snowfall season containing `end` (Sept 30, 12Z)."""
    return date(end.year if end.month >= 10 else end.year - 1, 9, 30)


def grid_url(product: Product, end: date) -> str:
    if product == "season":
        name = f"sfav2_CONUS_{season_start(end):%Y%m%d}12_to_{end:%Y%m%d}12.tif"
    else:
        name = f"sfav2_CONUS_{product}_{end:%Y%m%d}12.tif"
    return f"{BASE_URL}/{end:%Y%m}/{name}"


def ends_at(end: date) -> datetime:
    return datetime.combine(end, time(12), UTC)


def available(end: date, now: datetime) -> bool:
    """Whether the grids ending 12Z on `end` should be published by `now`."""
    return now >= ends_at(end) + AVAILABLE_AFTER


def fetch(client: httpx.Client, end: date, products: Sequence[Product] = PRODUCTS) -> list[Fetched]:
    """One GET per product; a 404 (not published yet) is returned, not raised."""
    return [
        get(client, SOURCE, grid_url(p, end), meta={"product": p, "end": end.isoformat()})
        for p in products
    ]


@dataclass(frozen=True)
class NohrscObservation:
    mountain: str
    obs_date: str
    reported_at: str
    new_24h_in: float | None
    new_48h_in: float | None
    season_in: float | None
    raw: dict[str, object]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def normalize(
    grids: Mapping[Product, Fetched], mountains: Sequence[Mountain]
) -> list[NohrscObservation]:
    """Sample each grid (HTTP 200 only) at every mountain; one row per mountain."""
    ok = {p: f for p, f in grids.items() if f.http_status == 200}
    if "24h" not in ok:
        return []
    ends = {str(f.meta["end"]) for f in ok.values()}
    if len(ends) != 1:
        raise NormalizeError(f"grids for different end dates: {sorted(ends)}")
    end = date.fromisoformat(ends.pop())
    try:
        parsed = {p: geotiff.read(f.body) for p, f in ok.items()}
    except (geotiff.TiffError, IndexError, ValueError) as exc:
        raise NormalizeError(f"{SOURCE} {end}: {exc}") from exc
    rows = []
    for m in mountains:
        values: dict[Product, float | None] = {
            p: _round(g.sample(m.lat, m.lon)) for p, g in parsed.items()
        }
        rows.append(
            NohrscObservation(
                mountain=m.key,
                obs_date=end.isoformat(),
                reported_at=ends_at(end).isoformat(timespec="seconds"),
                new_24h_in=values.get("24h"),
                new_48h_in=values.get("48h"),
                season_in=values.get("season"),
                raw={
                    "pixel": list(parsed["24h"].pixel_at(m.lat, m.lon) or ()),
                    "grids": {p: ok[p].url for p in parsed},
                },
            )
        )
    return rows


# --- store ------------------------------------------------------------------------


def stored_fetch_times(conn: sqlite3.Connection, end: date) -> list[str]:
    """`fetched_at` of the fetches behind the stored rows for `end` (empty if none)."""
    rows = conn.execute(
        "SELECT DISTINCT f.fetched_at FROM observation o JOIN fetch f ON f.id = o.fetch_id"
        " WHERE o.source = ? AND o.obs_date = ?",
        (SOURCE, end.isoformat()),
    ).fetchall()
    return [r[0] for r in rows]


def needs_fetch(conn: sqlite3.Connection, end: date, now: datetime) -> bool:
    """True when `end` has no rows yet, or its rows predate the reprocessing window."""
    if not available(end, now):
        return False
    times = stored_fetch_times(conn, end)
    if not times:
        return True
    reprocessed_by = ends_at(end) + timedelta(days=REPROCESS_AFTER_DAYS)
    if now < reprocessed_by:
        return False
    return all(datetime.fromisoformat(t) < reprocessed_by for t in times)


def latest_end(for_date: date, now: datetime) -> date:
    """The newest end date that should be published for an ingest on `for_date`."""
    return for_date if available(for_date, now) else for_date - timedelta(days=1)


def ends_to_fetch(conn: sqlite3.Connection, for_date: date, now: datetime) -> list[date]:
    """End dates an `ingest-obs` for `for_date` reads: the latest grids and one re-read."""
    candidates = [for_date, for_date - timedelta(days=1)]
    candidates.append(for_date - timedelta(days=REPROCESS_AFTER_DAYS + 1))
    return [e for e in candidates if needs_fetch(conn, e, now)]


def store(
    conn: sqlite3.Connection,
    rows: Iterable[NohrscObservation],
    fetch_ids: Mapping[Product, int],
) -> int:
    """Replace the rows for each mountain and end date; returns rows written."""
    written = 0
    for r in rows:
        conn.execute(
            "DELETE FROM observation WHERE source = ? AND mountain = ? AND obs_date = ?",
            (SOURCE, r.mountain, r.obs_date),
        )
        raw = {**r.raw, "fetch_ids": dict(fetch_ids)}
        conn.execute(
            "INSERT INTO observation (mountain, fetch_id, source, obs_date, reported_at,"
            " new_24h_in, new_48h_in, season_in, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.mountain,
                fetch_ids.get("24h"),
                SOURCE,
                r.obs_date,
                r.reported_at,
                r.new_24h_in,
                r.new_48h_in,
                r.season_in,
                json.dumps(raw, sort_keys=True),
            ),
        )
        written += 1
    return written
