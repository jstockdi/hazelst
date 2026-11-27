"""SnoCountry: resort-reported snow history, one JSON document per resort.

`GET https://good-cormorant-17.convex.site/api/resort-data?resortId={id}`
(fallback `https://snocountry.com/.netlify/functions/snowreport-resort`)
returns the resort's current report and its history. See
docs/design/data-sources.md, "SnoCountry".

Rows (`observation`, `source = 'snocountry'`, one per mountain per report date):

- history: every date in `snowarchive` (new snow, 2019-11 onward) or in the
  `jsonBaseDepth` series (base depth, 2012 onward). New snow is `snowMax`
  when it is above `snowMin` (a range), else `snowMin`. A base-depth date
  without a `snowarchive` entry is a zero day (`raw.inferred_zero`), but only
  in a season that has at least one `snowarchive` entry: before 2019-20, and
  in a season the archive is missing, a base-only date keeps `new_24h_in`
  NULL so a missing report is never scored as zero.
- current: the report itself (`reportDateTime`, `newSnowMin` / `newSnowMax`,
  `avgBaseDepthMin` / `avgBaseDepthMax`, `seasonTotal`, trails and lifts) for
  its local date, when it reports new snow or a base depth and the history
  has no reported value for that date. An empty new-snow field stays NULL
  (not zero): the current report alone does not say whether it was updated.

`obs_date` is the report date: the 24 h ending that morning, as for `resort`.
Writes upsert on (mountain, source, obs_date) (migration 0003), so a refetch
replaces values instead of adding rows; an inferred zero never replaces a
reported value.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from hazelst.config import TIMEZONE, Mountain
from hazelst.sources.base import Fetched, SourceError, get

SOURCE = "snocountry"
URL = "https://good-cormorant-17.convex.site/api/resort-data"
FALLBACK_URL = "https://snocountry.com/.netlify/functions/snowreport-resort"


class NormalizeError(ValueError):
    """A response does not have the expected shape."""


def params(resort_id: str) -> dict[str, str]:
    return {"resortId": resort_id}


def _usable(fetched: Fetched) -> bool:
    if fetched.http_status != 200:
        return False
    try:
        doc = fetched.json()
    except ValueError:
        return False
    return isinstance(doc, dict) and "snowarchive" in doc


def fetch_one(client: httpx.Client, mountain: Mountain) -> Fetched:
    """The resort document from the primary URL, else the fallback URL.

    Returns the last response when neither is usable (the caller stores it
    and reports its status); raises `SourceError` when neither answered.
    """
    if not mountain.snocountry_id:
        raise SourceError(f"{SOURCE}: {mountain.key} has no snocountry_id")
    meta = {"mountain": mountain.key, "resort_id": mountain.snocountry_id}
    query = params(mountain.snocountry_id)
    last: Fetched | None = None
    error: SourceError | None = None
    for url in (URL, FALLBACK_URL):
        try:
            fetched = get(client, SOURCE, url, query, meta=meta)
        except SourceError as exc:
            error = exc
            continue
        if _usable(fetched):
            return fetched
        last = fetched
    if last is not None:
        return last
    assert error is not None
    raise error


def fetch(client: httpx.Client, mountains: Sequence[Mountain]) -> list[Fetched]:
    """One document per mountain with a `snocountry_id` (the `record` entry point)."""
    return [fetch_one(client, m) for m in mountains if m.snocountry_id]


def label(fetched: Fetched) -> str:
    return str(fetched.meta.get("mountain", fetched.params.get("resortId", "resort")))


# --- normalize ----------------------------------------------------------------------


@dataclass(frozen=True)
class SnoCountryObservation:
    mountain: str
    obs_date: str
    reported_at: str | None
    new_24h_in: float | None
    base_in: float | None
    season_in: float | None
    trails_open: int | None
    trails_total: int | None
    lifts_open: int | None
    raw: dict[str, Any]

    @property
    def inferred_zero(self) -> bool:
        return bool(self.raw.get("inferred_zero"))


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(str(value).strip())
    except ValueError:
        return None
    return out if out >= 0 else None


def _int(value: object) -> int | None:
    n = _number(value)
    return None if n is None else int(n)


def new_snow(snow_min: object, snow_max: object) -> float | None:
    """`snowMax` when it is above `snowMin` (a range quotes the summit as max), else `snowMin`."""
    lo, hi = _number(snow_min), _number(snow_max)
    if lo is None:
        return hi
    if hi is not None and hi > lo:
        return hi
    return lo


def season_of(day: date) -> int:
    """The year a season starts in (July-June): 2025 for 2025-11-11 and 2026-03-01."""
    return day.year if day.month >= 7 else day.year - 1


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _archive(doc: dict[str, Any]) -> dict[date, tuple[float, dict[str, Any]]]:
    out: dict[date, tuple[float, dict[str, Any]]] = {}
    entries = doc.get("snowarchive") or []
    if not isinstance(entries, list):
        raise NormalizeError("snowarchive is not a list")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        day = _date(entry.get("date_of_new_snow"))
        value = new_snow(entry.get("snowMin"), entry.get("snowMax"))
        if day is None or value is None:
            continue
        # Duplicate dates keep the larger value (a range quoted twice).
        if day not in out or value > out[day][0]:
            out[day] = (value, entry)
    return out


def _base_depth(doc: dict[str, Any]) -> dict[date, tuple[float, dict[str, Any]]]:
    text = doc.get("jsonBaseDepth")
    if not text:
        return {}
    try:
        series = json.loads(text) if isinstance(text, str) else text
    except ValueError as exc:
        raise NormalizeError(f"jsonBaseDepth is not JSON: {exc}") from exc
    out: dict[date, tuple[float, dict[str, Any]]] = {}
    for points in series.get("data") or []:
        for point in points or []:
            if not isinstance(point, dict):
                continue
            day, value = _date(point.get("x")), _number(point.get("y"))
            if day is not None and value is not None:
                out[day] = (value, point)
    return out


def _current(
    doc: dict[str, Any], mountain: str, tz: str
) -> tuple[date, SnoCountryObservation] | None:
    stamp = str(doc.get("reportDateTime") or "")
    try:
        local = datetime.fromisoformat(stamp).replace(
            tzinfo=ZoneInfo(str(doc.get("timezone") or tz))
        )
    except ValueError:
        return None
    day = local.astimezone(ZoneInfo(tz)).date()
    base_lo, base_hi = _number(doc.get("avgBaseDepthMin")), _number(doc.get("avgBaseDepthMax"))
    keys = (
        "reportDateTime",
        "newSnowMin",
        "newSnowMax",
        "snowLast48Hours",
        "avgBaseDepthMin",
        "avgBaseDepthMax",
        "seasonTotal",
        "openDownHillTrails",
        "maxOpenDownHillTrails",
        "openDownHillLifts",
        "primarySurfaceCondition",
    )
    row = SnoCountryObservation(
        mountain=mountain,
        obs_date=day.isoformat(),
        reported_at=local.isoformat(timespec="seconds"),
        new_24h_in=new_snow(doc.get("newSnowMin"), doc.get("newSnowMax")),
        base_in=base_hi if base_hi is not None else base_lo,
        season_in=_number(doc.get("seasonTotal")),
        trails_open=_int(doc.get("openDownHillTrails")),
        trails_total=_int(doc.get("maxOpenDownHillTrails")),
        lifts_open=_int(doc.get("openDownHillLifts")),
        raw={"current": {k: doc.get(k) for k in keys}},
    )
    return day, row


def normalize(fetched: Fetched, tz: str = TIMEZONE) -> list[SnoCountryObservation]:
    """Every history date and the current report for one resort, oldest first."""
    mountain = str(fetched.meta.get("mountain") or "")
    if not mountain:
        raise NormalizeError("fetch has no mountain")
    if fetched.http_status != 200:
        raise NormalizeError(f"{SOURCE} {mountain}: HTTP {fetched.http_status}")
    try:
        doc = fetched.json()
    except ValueError as exc:
        raise NormalizeError(f"{SOURCE} {mountain}: not JSON: {exc}") from exc
    if not isinstance(doc, dict) or "snowarchive" not in doc:
        raise NormalizeError(f"{SOURCE} {mountain}: no snowarchive")
    archive = _archive(doc)
    base = _base_depth(doc)
    archived_seasons = {season_of(d) for d in archive}
    rows: dict[date, SnoCountryObservation] = {}
    for day in sorted(archive.keys() | base.keys()):
        raw: dict[str, Any] = {}
        new_in: float | None = None
        base_in: float | None = None
        if day in archive:
            new_in, raw["snowarchive"] = archive[day]
        if day in base:
            base_in, raw["base_depth"] = base[day]
        if day not in archive and season_of(day) in archived_seasons:
            new_in = 0.0
            raw["inferred_zero"] = True
        rows[day] = SnoCountryObservation(
            mountain=mountain,
            obs_date=day.isoformat(),
            reported_at=None,
            new_24h_in=new_in,
            base_in=base_in,
            season_in=None,
            trails_open=None,
            trails_total=None,
            lifts_open=None,
            raw=raw,
        )
    current = _current(doc, mountain, tz)
    if current is not None and (
        current[1].new_24h_in is not None or current[1].base_in is not None
    ):
        day, now_row = current
        past = rows.get(day)
        if past is None:
            rows[day] = now_row
        elif past.new_24h_in is None or (past.inferred_zero and now_row.new_24h_in):
            # The history has no reported value for the day: the current report fills it.
            rows[day] = SnoCountryObservation(
                mountain=mountain,
                obs_date=past.obs_date,
                reported_at=now_row.reported_at,
                new_24h_in=now_row.new_24h_in,
                base_in=past.base_in if past.base_in is not None else now_row.base_in,
                season_in=now_row.season_in,
                trails_open=now_row.trails_open,
                trails_total=now_row.trails_total,
                lifts_open=now_row.lifts_open,
                raw={**{k: v for k, v in past.raw.items() if k != "inferred_zero"}, **now_row.raw},
            )
    return [rows[d] for d in sorted(rows)]


# --- store ---------------------------------------------------------------------------

UPSERT = (
    "INSERT INTO observation (mountain, fetch_id, source, obs_date, reported_at, new_24h_in,"
    " base_in, season_in, trails_open, trails_total, lifts_open, raw)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (mountain, source, obs_date) WHERE source = 'snocountry' DO UPDATE SET"
    " fetch_id = excluded.fetch_id, reported_at = excluded.reported_at,"
    " new_24h_in = excluded.new_24h_in, base_in = excluded.base_in,"
    " season_in = excluded.season_in, trails_open = excluded.trails_open,"
    " trails_total = excluded.trails_total, lifts_open = excluded.lifts_open, raw = excluded.raw"
    # An inferred zero never replaces a reported value.
    " WHERE NOT (coalesce(json_extract(excluded.raw, '$.inferred_zero'), 0) = 1"
    "  AND observation.new_24h_in > 0"
    "  AND coalesce(json_extract(observation.raw, '$.inferred_zero'), 0) = 0)"
)


def store(conn: sqlite3.Connection, rows: Iterable[SnoCountryObservation], fetch_id: int) -> int:
    """Upsert each row on (mountain, source, obs_date); returns rows written."""
    params_list = [
        (
            r.mountain,
            fetch_id,
            SOURCE,
            r.obs_date,
            r.reported_at,
            r.new_24h_in,
            r.base_in,
            r.season_in,
            r.trails_open,
            r.trails_total,
            r.lifts_open,
            json.dumps(r.raw, sort_keys=True),
        )
        for r in rows
    ]
    conn.executemany(UPSERT, params_list)
    return len(params_list)
