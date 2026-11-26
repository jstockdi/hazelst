"""Resort reports: fetch within the daily budget, normalize to `observation`.

`fetch_resort()` makes the HTTP requests and writes raw `fetch` rows (the data
URL, plus the `mtnpowder` widget config at most once per `cache_hours`).
`normalize()` is pure: it reads one stored response and returns an
`observation` row, so it reruns over history. See docs/design/data-sources.md,
"Resort adapters".
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from hazelst import db
from hazelst.config import TIMEZONE, Mountain
from hazelst.sources import base
from hazelst.sources.resorts import adapter

SOURCE = "resort"  # fetch.source of a data response; observation.source of its row
CONFIG_SOURCE = "resort_config"  # fetch.source of a prefetched config document
LLM_SOURCE = "resort_llm"  # observation.source of a conditions-extract result
DAILY_FETCHES = 3  # data fetches per resort per America/New_York day
STALE_AFTER = timedelta(hours=36)
REDACTED = "[from prefetch]"

# Boyne resorts publish no `ai-input` content signal: code-only, never sent to Claude.
BOYNE_PRESET = "boyne_reportpal"
BOYNE_KEYS = frozenset({"loon", "sunday_river", "sugarloaf"})

Status = Literal["ok", "failed", "skipped_budget", "disabled"]


class ResortError(RuntimeError):
    """A resort request failed before a report could be parsed."""


@dataclass(frozen=True)
class ObservationRow:
    mountain: str
    source: str
    obs_date: str
    reported_at: str
    new_24h_in: float | None
    new_48h_in: float | None
    new_7d_in: float | None
    base_in: float | None
    season_in: float | None
    trails_open: int | None
    trails_total: int | None
    lifts_open: int | None
    lifts_hold: int | None
    raw: dict[str, Any]

    @property
    def stale(self) -> bool:
        return bool(self.raw.get("stale"))


@dataclass
class ResortOutcome:
    """What happened to one resort in one ingest."""

    mountain: str
    preset: str | None
    status: Status
    reason: str | None = None
    fetch_id: int | None = None
    observation_id: int | None = None
    duplicate: bool = False
    reported_at: str | None = None
    stale: bool | None = None
    fetched: base.Fetched | None = None  # the data response, for conditions-extract

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "preset": self.preset}
        for key in ("reason", "fetch_id", "observation_id", "reported_at", "stale"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.duplicate:
            out["duplicate"] = True
        return out


def extraction_allowed(mountain: Mountain) -> bool:
    """Whether a failed page may go to `conditions-extract` (never for Boyne resorts)."""
    spec = mountain.report_adapter
    return spec is not None and spec.preset != BOYNE_PRESET and mountain.key not in BOYNE_KEYS


# --- time helpers ------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local_date(moment: datetime, tz: str = TIMEZONE) -> str:
    return moment.astimezone(ZoneInfo(tz)).date().isoformat()


def day_bounds_utc(now: datetime, tz: str = TIMEZONE) -> tuple[str, str]:
    """UTC ISO bounds of the local day containing `now`."""
    zone = ZoneInfo(tz)
    day = now.astimezone(zone).date()
    start = datetime.combine(day, time(0), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
    return _iso(start), _iso(end)


# --- fetch -------------------------------------------------------------------------


def fetches_today(conn: sqlite3.Connection, mountain: str, now: datetime) -> int:
    """Data fetches already made for `mountain` in the local day of `now`."""
    start, end = day_bounds_utc(now)
    row = conn.execute(
        "SELECT count(*) FROM fetch WHERE source = ? AND fetched_at >= ? AND fetched_at < ?"
        " AND json_extract(params, '$.meta.mountain') = ?",
        (SOURCE, start, end, mountain),
    ).fetchone()
    return int(row[0])


def cached_config(
    conn: sqlite3.Connection, url: str, now: datetime, max_age: timedelta
) -> tuple[int, base.Fetched] | None:
    """The newest successful config fetch of `url` younger than `max_age`."""
    row = conn.execute(
        "SELECT id FROM fetch WHERE source = ? AND url = ? AND http_status = 200"
        " AND fetched_at >= ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (CONFIG_SOURCE, url, _iso(now - max_age)),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), base.load_fetch(conn, int(row[0]))


def config_vars(prefetch: adapter.Prefetch, fetched: base.Fetched) -> dict[str, str]:
    """Values the data URL needs from a config document (for example the feed token)."""
    if fetched.http_status != 200:
        raise ResortError(f"config {fetched.url}: HTTP {fetched.http_status}")
    try:
        doc = fetched.json()
    except ValueError:
        raise ResortError(f"config {fetched.url} is not JSON") from None
    values: dict[str, str] = {}
    for name, path in prefetch.vars.items():
        value = adapter.resolve_path(doc, path)
        if value in (None, ""):
            raise ResortError(f"config {fetched.url} has no {path}")
        values[name] = str(value)
    return values


def data_request(
    spec: adapter.Preset, params: Mapping[str, Any], variables: Mapping[str, str]
) -> tuple[str, dict[str, str]]:
    """The data URL (without query) and its query parameters."""
    url = httpx.URL(adapter.format_url(spec.url, {**params, **variables}))
    return str(url.copy_with(query=None)), dict(url.params.multi_items())


def _redact(fetched: base.Fetched, secrets: Iterable[str]) -> base.Fetched:
    """Replace prefetched values (the public feed token) in the stored query."""
    hidden = set(secrets)
    params = {k: (REDACTED if v in hidden else v) for k, v in fetched.params.items()}
    return dataclasses.replace(fetched, params=params)


def fetch_resort(
    conn: sqlite3.Connection,
    client: httpx.Client,
    run_id: int | None,
    mountain: Mountain,
    now: datetime,
) -> tuple[int, base.Fetched]:
    """Fetch one resort's report and store the raw rows; returns the data fetch."""
    spec_ref = mountain.report_adapter
    assert spec_ref is not None
    spec = adapter.preset(spec_ref.preset)
    params = dict(spec_ref.params)
    meta: dict[str, Any] = {"mountain": mountain.key, "preset": spec.name, "params": params}
    variables: dict[str, str] = {}
    if spec.prefetch is not None:
        config_url = adapter.format_url(spec.prefetch.url, params)
        cached = cached_config(conn, config_url, now, timedelta(hours=spec.prefetch.cache_hours))
        if cached is None:
            fetched_config = base.get(
                client, CONFIG_SOURCE, config_url, meta={"mountain": mountain.key}
            )
            with db.transaction(conn):
                config_id = base.store_fetch(conn, run_id, fetched_config)
        else:
            config_id, fetched_config = cached
        variables = config_vars(spec.prefetch, fetched_config)
        meta["prefetch_fetch_id"] = config_id
    url, query = data_request(spec, params, variables)
    fetched = _redact(base.get(client, SOURCE, url, query, meta=meta), variables.values())
    with db.transaction(conn):
        fetch_id = base.store_fetch(conn, run_id, fetched)
    return fetch_id, fetched


# --- normalize ---------------------------------------------------------------------


def normalize(fetched: base.Fetched) -> ObservationRow:
    """One resort response -> one `observation` row; raises `AdapterError` if it fails."""
    mountain = fetched.meta.get("mountain")
    preset_name = fetched.meta.get("preset")
    if not mountain or not preset_name:
        raise adapter.AdapterError("fetch row has no mountain or preset")
    if fetched.http_status != 200:
        raise adapter.AdapterError(f"HTTP {fetched.http_status}")
    report = adapter.parse(adapter.preset(preset_name), fetched.body)
    assert report.reported_at is not None
    fetched_at = _parse_iso(fetched.fetched_at)
    values = report.values
    return ObservationRow(
        mountain=mountain,
        source=SOURCE,
        obs_date=local_date(report.reported_at),
        reported_at=_iso(report.reported_at),
        new_24h_in=_float(values["new_24h_in"]),
        new_48h_in=_float(values["new_48h_in"]),
        new_7d_in=_float(values["new_7d_in"]),
        base_in=_float(values["base_in"]),
        season_in=_float(values["season_in"]),
        trails_open=_int(values["trails_open"]),
        trails_total=_int(values["trails_total"]),
        lifts_open=_int(values["lifts_open"]),
        lifts_hold=_int(values["lifts_hold"]),
        raw={
            "preset": preset_name,
            "fetched_at": fetched.fetched_at,
            "stale": fetched_at - report.reported_at > STALE_AFTER,
            "lifts_total": values["lifts_total"],
            "values": report.raw,
        },
    )


def llm_observation(
    fetched: base.Fetched, values: dict[str, Any], *, call_id: int
) -> ObservationRow:
    """A `conditions-extract` result as a `resort_llm` row.

    The page gives no report time the adapter could read, so `reported_at` is
    the fetch time. Readers prefer a `resort` row over a `resort_llm` row.
    """
    fetched_at = _parse_iso(fetched.fetched_at)
    return ObservationRow(
        mountain=str(fetched.meta["mountain"]),
        source=LLM_SOURCE,
        obs_date=local_date(fetched_at),
        reported_at=_iso(fetched_at),
        new_24h_in=_float(values.get("new_24h_in")),
        new_48h_in=None,
        new_7d_in=None,
        base_in=_float(values.get("base_in")),
        season_in=None,
        trails_open=_int(values.get("trails_open")),
        trails_total=_int(values.get("trails_total")),
        lifts_open=_int(values.get("lifts_open")),
        lifts_hold=None,
        raw={
            "preset": fetched.meta.get("preset"),
            "fetched_at": fetched.fetched_at,
            "agent_call_id": call_id,
            "confidence": values.get("confidence"),
            "reported_at_is_fetch_time": True,
        },
    )


def _float(value: float | int | None) -> float | None:
    return None if value is None else float(value)


def _int(value: float | int | None) -> int | None:
    return None if value is None else int(value)


# --- store ---------------------------------------------------------------------------


def find_observation(
    conn: sqlite3.Connection, mountain: str, source: str, reported_at: str
) -> int | None:
    row = conn.execute(
        "SELECT id FROM observation WHERE mountain = ? AND source = ? AND reported_at = ?"
        " ORDER BY id LIMIT 1",
        (mountain, source, reported_at),
    ).fetchone()
    return None if row is None else int(row[0])


def store(conn: sqlite3.Connection, fetch_id: int | None, row: ObservationRow) -> int | None:
    """Insert `row` unless the same (mountain, source, reported_at) is stored; returns its id."""
    if find_observation(conn, row.mountain, row.source, row.reported_at) is not None:
        return None
    cur = conn.execute(
        "INSERT INTO observation (mountain, fetch_id, source, obs_date, reported_at,"
        " new_24h_in, new_48h_in, new_7d_in, base_in, season_in,"
        " trails_open, trails_total, lifts_open, lifts_hold, surface, raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            row.mountain,
            fetch_id,
            row.source,
            row.obs_date,
            row.reported_at,
            row.new_24h_in,
            row.new_48h_in,
            row.new_7d_in,
            row.base_in,
            row.season_in,
            row.trails_open,
            row.trails_total,
            row.lifts_open,
            row.lifts_hold,
            json.dumps(row.raw, sort_keys=True),
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


# --- one ingest ----------------------------------------------------------------------


def ingest_one(
    conn: sqlite3.Connection,
    client: httpx.Client,
    run_id: int | None,
    mountain: Mountain,
    now: datetime,
) -> ResortOutcome:
    """Fetch, normalize, and store one resort; never raises for a source failure."""
    spec_ref = mountain.report_adapter
    if spec_ref is None:
        return ResortOutcome(mountain.key, None, "disabled")
    outcome = ResortOutcome(mountain.key, spec_ref.preset, "failed")
    used = fetches_today(conn, mountain.key, now)
    if used >= DAILY_FETCHES:
        outcome.status = "skipped_budget"
        outcome.reason = f"{used} of {DAILY_FETCHES} fetches used today"
        return outcome
    try:
        outcome.fetch_id, outcome.fetched = fetch_resort(conn, client, run_id, mountain, now)
        row = normalize(outcome.fetched)
    except (adapter.AdapterError, ResortError, base.SourceError, ValueError) as exc:
        outcome.reason = f"{type(exc).__name__}: {exc}"
        return outcome
    with db.transaction(conn):
        observation_id = store(conn, outcome.fetch_id, row)
    outcome.status = "ok"
    outcome.reported_at = row.reported_at
    outcome.stale = row.stale
    outcome.observation_id = observation_id
    outcome.duplicate = observation_id is None
    return outcome


def renormalize(conn: sqlite3.Connection, fetch_id: int) -> int | None:
    """Re-derive the observation for one stored resort response (after a parser fix)."""
    row = normalize(base.load_fetch(conn, fetch_id))
    with db.transaction(conn):
        return store(conn, fetch_id, row)


# --- record --------------------------------------------------------------------------


def record(client: httpx.Client, mountains: Iterable[Mountain]) -> list[base.Fetched]:
    """Fetch every enabled resort live, without the database (`hazelst record resorts`)."""
    out: list[base.Fetched] = []
    for mountain in mountains:
        spec_ref = mountain.report_adapter
        if spec_ref is None:
            continue
        spec = adapter.preset(spec_ref.preset)
        params = dict(spec_ref.params)
        meta: dict[str, Any] = {"mountain": mountain.key, "preset": spec.name, "params": params}
        variables: dict[str, str] = {}
        if spec.prefetch is not None:
            config = base.get(
                client,
                CONFIG_SOURCE,
                adapter.format_url(spec.prefetch.url, params),
                meta={"mountain": mountain.key},
            )
            out.append(config)
            try:
                variables = config_vars(spec.prefetch, config)
            except ResortError:
                continue
        url, query = data_request(spec, params, variables)
        out.append(_redact(base.get(client, SOURCE, url, query, meta=meta), variables.values()))
    return out


def label(fetched: base.Fetched) -> str:
    kind = "config" if fetched.source == CONFIG_SOURCE else "report"
    return f"{fetched.meta.get('mountain', 'unknown')}-{kind}"
