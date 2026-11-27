"""season-backfill: a past season's forecasts and ground truth, loaded into the live tables.

For report days `start`..`end` (local dates):

- `previous_runs`: Open-Meteo Previous Runs, GFS and GEM at summit, lead days
  1-5 (one request per lead day; lead day 1 adds the 850 hPa variables) ->
  `fetch`, `forecast_hourly` (`issued_at` NULL, `lead_h` = 24 x (N - 1)). A
  new fetch for a lead day replaces the rows of earlier fetches for the same
  hours (a re-run after the query changed).
- `lsr`: IEM Local Storm Reports for the days and the morning after ->
  `snow_report`.
- `pns`: IEM AFOS PNS listings per office per day, then each product's text
  -> `discussion` (`PNS`), `snow_report`.
- `nohrsc`: the NOHRSC 24 h grid ending 12Z on each day + 1 (the day's
  obs_date) -> `observation` (`nohrsc`).
- `snocountry`: one request per mountain for the resort's whole SnoCountry
  history (not limited to the range) -> `observation` (`snocountry`).
- `ghcn`: GHCN-Daily `SNOW` / `SNWD` for every station in `stations.json`,
  dates start..end + 1, one request per 50 stations -> `snow_report`.

`skill-learn` then learns from these days like verified live days: the
observation comes from `verify.observed` (no `verification` rows exist for a
backfilled day), and the forecasts from `forecast_hourly`.

Idempotent: a request whose response is already stored with HTTP 200 (same
source, URL, and query) is not sent again, PNS products are fetched once
(`discussion` ids), NOHRSC dates with stored rows are skipped, and a
SnoCountry resort already stored is not requested again (its rows upsert).
Rerunning a range therefore sends only the requests that failed before and
writes nothing twice. The CLI runs one job per month so the job lock is
released between months and scheduled jobs are not held up for the whole
season (one job in all when only whole-history steps are selected).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from hazelst import config, db
from hazelst.sources import (
    base,
    elevation,
    ghcn,
    iem,
    nohrsc,
    open_meteo,
    pns,
    previous_runs,
    snocountry,
    snow_report,
)


@dataclass
class Counts:
    requests: int = 0
    cached: int = 0
    errors: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)

    def add(self, table: str, n: int) -> None:
        self.rows[table] = self.rows.get(table, 0) + n

    def summary(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "cached": self.cached,
            "rows": self.rows,
            "errors": self.errors[:20],
            "error_count": len(self.errors),
        }


def chunks(start: date, end: date, days: int = 31) -> Iterator[tuple[date, date]]:
    """Calendar-month pieces of `start`..`end` (inclusive), each at most `days` long."""
    first = start
    while first <= end:
        next_month = (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = min(end, next_month - timedelta(days=1), first + timedelta(days=days - 1))
        yield first, last
        first = last + timedelta(days=1)


def _query_key(url: str, query: dict[str, Any]) -> str:
    return json.dumps([url, {k: str(v) for k, v in query.items()}], sort_keys=True)


def stored_requests(conn: sqlite3.Connection, source: str) -> set[str]:
    """Keys of the requests already stored with HTTP 200 for a source."""
    out = set()
    for url, params in conn.execute(
        "SELECT url, params FROM fetch WHERE source = ? AND http_status = 200", (source,)
    ):
        query = json.loads(params or "{}").get("query", {})
        out.add(_query_key(url, query))
    return out


def _local(day: date, tz: str) -> datetime:
    return datetime.combine(day, time(0), ZoneInfo(tz))


class Backfill:
    """One range's backfill inside a `season-backfill` run."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: httpx.Client,
        run_id: int,
        cfg: config.Config,
        log: Callable[..., None] = lambda **_: None,
    ) -> None:
        self.conn = conn
        self.client = client
        self.run_id = run_id
        self.cfg = cfg
        self.log = log
        self._stored: dict[str, set[str]] = {}

    def _seen(self, source: str, url: str, query: dict[str, Any]) -> bool:
        if source not in self._stored:
            self._stored[source] = stored_requests(self.conn, source)
        return _query_key(url, query) in self._stored[source]

    def _get(
        self,
        counts: Counts,
        source: str,
        url: str,
        query: dict[str, Any],
        fetch: Callable[[], base.Fetched],
    ) -> base.Fetched | None:
        """The response, or None when it is already stored or the request failed."""
        if self._seen(source, url, query):
            counts.cached += 1
            return None
        counts.requests += 1
        try:
            fetched = fetch()
        except base.SourceError as exc:
            counts.errors.append(str(exc))
            return None
        if fetched.http_status != 200:
            counts.errors.append(f"{source}: HTTP {fetched.http_status} {url} {query}")
            with db.transaction(self.conn):
                base.store_fetch(self.conn, self.run_id, fetched)
            return None
        return fetched

    def _remember(self, fetched: base.Fetched) -> None:
        self._stored.setdefault(fetched.source, set()).add(_query_key(fetched.url, fetched.params))

    # --- steps ---------------------------------------------------------------------

    def previous_runs(self, start: date, end: date) -> Counts:
        """Lead days 1-5 for report days `start`..`end` (UTC dates through end + 1)."""
        counts = Counts()
        mountains = self.cfg.mountains
        last = end + timedelta(days=1)
        for lead in previous_runs.LEAD_DAYS:
            query = previous_runs.params(mountains, lead, start, last)
            fetched = self._get(
                counts,
                previous_runs.SOURCE,
                previous_runs.URL,
                query,
                partial(previous_runs.fetch, self.client, mountains, lead, start, last),
            )
            if fetched is None:
                continue
            try:
                rows = previous_runs.normalize(fetched)
            except (open_meteo.NormalizeError, KeyError, ValueError) as exc:
                counts.errors.append(f"previous_runs lead {lead}: {exc}")
                continue
            with db.transaction(self.conn):
                fetch_id = base.store_fetch(self.conn, self.run_id, fetched)
                dropped = previous_runs.drop_superseded(self.conn, fetch_id, rows)
                counts.add("forecast_hourly", open_meteo.store(self.conn, fetch_id, rows))
                if dropped:
                    counts.add("superseded", dropped)
            self._remember(fetched)
        return counts

    def lsr(self, start: date, end: date) -> Counts:
        """Storm reports from midnight before `start` to midnight after the morning of end + 1."""
        counts = Counts()
        tz = self.cfg.timezone
        sts, ets = _local(start, tz), _local(end + timedelta(days=2), tz)
        query = iem.lsr_params(sts, ets)
        fetched = self._get(
            counts, iem.LSR_SOURCE, iem.LSR_URL, query, lambda: iem.fetch_lsr(self.client, sts, ets)
        )
        if fetched is None:
            return counts
        try:
            rows = iem.normalize_lsr(fetched, self.cfg.mountains)
        except iem.NormalizeError as exc:
            counts.errors.append(str(exc))
            return counts
        with db.transaction(self.conn):
            base.store_fetch(self.conn, self.run_id, fetched)
            counts.add("snow_report", snow_report.store(self.conn, rows, "lsr"))
        self._remember(fetched)
        self._fill_elevations(counts)
        return counts

    def _fill_elevations(self, counts: Counts) -> None:
        """Open-Meteo elevations for new `snow_report` rows; a failed lookup is counted."""
        result = elevation.fill(self.conn, self.client)
        counts.add("elev_ft", result.filled)
        if result.failed:
            counts.errors.append(f"elevation: {result.failed} rows without elev_ft")

    def pns(self, start: date, end: date) -> Counts:
        """PNS products listed for each office on `start`..`end` + 1."""
        counts = Counts()
        known = pns.known_ids(self.conn)
        day = start
        while day <= end + timedelta(days=1):
            for office in pns.OFFICES:
                query = {"pil": f"PNS{office}", "date": day.isoformat()}
                listing = self._get(
                    counts,
                    pns.ARCHIVE_LIST_SOURCE,
                    pns.ARCHIVE_LIST_URL,
                    query,
                    partial(pns.fetch_archive_list, self.client, office, day),
                )
                if listing is None:
                    continue
                try:
                    products = [p for p in pns.parse_list(listing) if p.product_id not in known]
                except pns.NormalizeError as exc:
                    counts.errors.append(str(exc))
                    continue
                texts = []
                failed = False
                for p in products:
                    counts.requests += 1
                    try:
                        f = pns.fetch_product(self.client, p)
                    except base.SourceError as exc:
                        counts.errors.append(str(exc))
                        failed = True
                        continue
                    if f.http_status != 200:
                        counts.errors.append(f"pns {p.product_id}: HTTP {f.http_status}")
                        failed = True
                        continue
                    texts.append(f)
                with db.transaction(self.conn):
                    if not failed:  # a listing is stored (and skipped next time) once complete
                        base.store_fetch(self.conn, self.run_id, listing)
                    for f in texts:
                        base.store_fetch(self.conn, self.run_id, f)
                        try:
                            product = pns.normalize(f, self.cfg.mountains)
                        except pns.NormalizeError as exc:
                            counts.errors.append(str(exc))
                            continue
                        counts.add("snow_report", pns.store(self.conn, product))
                        counts.add("discussion", 1)
                        known.add(product.product_id)
                if not failed:
                    self._remember(listing)
            day += timedelta(days=1)
        self._fill_elevations(counts)
        return counts

    def nohrsc(self, start: date, end: date) -> Counts:
        """The 24 h grid for each obs_date `start` + 1 .. `end` + 1."""
        counts = Counts()
        day = start + timedelta(days=1)
        while day <= end + timedelta(days=1):
            if nohrsc.stored_fetch_times(self.conn, day):
                counts.cached += 1
                day += timedelta(days=1)
                continue
            counts.requests += 1
            try:
                (grid,) = nohrsc.fetch(self.client, day, products=("24h",))
            except base.SourceError as exc:
                counts.errors.append(str(exc))
                day += timedelta(days=1)
                continue
            if grid.http_status != 200:
                counts.errors.append(f"nohrsc {day}: HTTP {grid.http_status}")
                day += timedelta(days=1)
                continue
            try:
                rows = nohrsc.normalize({"24h": grid}, self.cfg.mountains)
            except nohrsc.NormalizeError as exc:
                counts.errors.append(str(exc))
                day += timedelta(days=1)
                continue
            with db.transaction(self.conn):
                fetch_id = base.store_fetch(self.conn, self.run_id, grid)
                counts.add("observation", nohrsc.store(self.conn, rows, {"24h": fetch_id}))
            day += timedelta(days=1)
        return counts

    def snocountry(self, start: date, end: date) -> Counts:
        """Each mountain's full SnoCountry history, once (the range does not narrow it)."""
        counts = Counts()
        for m in self.cfg.mountains:
            if not m.snocountry_id:
                continue
            query = snocountry.params(m.snocountry_id)
            if self._seen(snocountry.SOURCE, snocountry.URL, query) or self._seen(
                snocountry.SOURCE, snocountry.FALLBACK_URL, query
            ):
                counts.cached += 1
                continue
            fetched = self._get(
                counts,
                snocountry.SOURCE,
                snocountry.URL,
                query,
                partial(snocountry.fetch_one, self.client, m),
            )
            if fetched is None:
                continue
            try:
                rows = snocountry.normalize(fetched, self.cfg.timezone)
            except snocountry.NormalizeError as exc:
                counts.errors.append(str(exc))
                continue
            with db.transaction(self.conn):
                fetch_id = base.store_fetch(self.conn, self.run_id, fetched)
                counts.add("observation", snocountry.store(self.conn, rows, fetch_id))
            counts.add("inferred_zero", sum(1 for r in rows if r.inferred_zero))
            self._remember(fetched)
        return counts

    def ghcn(self, start: date, end: date) -> Counts:
        """Station days `start`..`end` + 1 (the morning after each report day)."""
        counts = Counts()
        stations = ghcn.load_stations(ghcn.stations_path(self.cfg.settings))
        by_id = ghcn.nearest(stations)
        last = end + timedelta(days=1)
        for ids in ghcn.batches(by_id):
            query = ghcn.params(ids, start, last)
            fetched = self._get(
                counts,
                ghcn.SOURCE,
                ghcn.URL,
                query,
                partial(ghcn.fetch, self.client, ids, start, last),
            )
            if fetched is None:
                continue
            try:
                rows = ghcn.normalize(fetched, by_id, self.cfg.timezone)
            except (ghcn.NormalizeError, ValueError) as exc:
                counts.errors.append(f"ghcn {ids[0]}..: {exc}")
                continue
            with db.transaction(self.conn):
                base.store_fetch(self.conn, self.run_id, fetched)
                counts.add("snow_report", ghcn.store(self.conn, rows))
            self._remember(fetched)
        return counts


STEPS: tuple[str, ...] = ("previous_runs", "lsr", "pns", "nohrsc", "snocountry", "ghcn")
# Steps that load a whole history in one pass, whatever the range.
WHOLE_HISTORY_STEPS = frozenset({"snocountry"})


def run(bf: Backfill, start: date, end: date, steps: tuple[str, ...] = STEPS) -> dict[str, Any]:
    """Every step for one range; returns a summary per step."""
    out: dict[str, Any] = {"from": start.isoformat(), "to": end.isoformat()}
    for name in steps:
        started = datetime.now(UTC)
        counts: Counts = getattr(bf, name)(start, end)
        ms = round((datetime.now(UTC) - started).total_seconds() * 1000)
        out[name] = {"ms": ms, **counts.summary()}
        bf.log(run_id=bf.run_id, step=f"season-backfill/{name}", ms=ms, **counts.summary())
    return out
