"""forecast-verify: score each edition's 1d forecast against the resort's own report.

A report date X has a 1d window from 6 AM X to 6 AM X + 1 (local). Each
mountain is scored only against the snow its resort reported for that day
(decisions row 24, data-model.md "How the data compounds"):

1. `resort`: the earliest non-stale resort report on X + 1 with a 24 h value
   (then `resort_llm`, which never overrides an adapter value);
2. `snocountry`: the resort-reported value SnoCountry holds for obs_date X + 1
   (an inferred zero counts: the resort reported a base depth that day);
3. none: `NO_OBS`.

Every other source's value for the day is recorded beside the score as
`context` (`verification.context`, the accumulation's days, the writer's
digest), never as the observation:

- `nohrsc`: the 24 h grid ending 12Z on X + 1, at the mountain's pixel;
- `pns` / `lsr` / `ghcn`: each point report within 20 mi of the mountain in
  `snow_report`: 24 h reports ending 4-10 AM on X + 1 (PNS `SNOW_24`, and GHCN
  co-op and CoCoRaHS days, which end at 7 AM), and reports of unknown span
  (LSRs, storm totals) ending in the window or by 10 AM on X + 1.

`observation.obs_date` D means "the 24 h ending the morning of D", so day X
reads obs_date X + 1. HIT is within 2 inches or 30% of the observed value.

A day with no stored report (a season backfill) is scored as edition
`backfill`: each model's lead-day-1 summit forecast from `forecast_hourly`,
their mean, and `resort_ratio` (ratio.py, fitted leaving the day's season out).

The same day-by-day choice fills the accumulation section: the resort-reported
snow on each of the 7 days before the report date ("no report" without one),
the resort's season total and base depth, and the context beside each day.
`summary_cells` reads the scores per mountain, and the forecast against the
context reports by elevation band.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import astuple, dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from hazelst.config import TIMEZONE, Mountain
from hazelst.models import Accumulation, AccumulationDay, ContextReport, VerificationSummary
from hazelst.sources.open_meteo import is_fine
from hazelst.sources.snow_report import distance_mi

Result = Literal["HIT", "MISS", "NO_OBS"]
CONTEXT_MI = 20.0  # point reports within this distance are recorded as context
HIT_ABS_IN = 2.0
HIT_REL = 0.30
WINDOW = "1d"
RAW_MEAN = "raw_mean"
BACKFILL_EDITION = "backfill"  # a day with no report: scored from forecast_hourly
SEASON_LOOKBACK_DAYS = 3  # season and base come from a report at most this old
DAILY_REPORT_HOURS = (4, 10)  # a 24 h point report ending in this local-hour range
GHCN_PRODUCT = "GHCN"  # snow_report.product_id of GHCN-Daily station rows
RESORT_SOURCES = ("resort", "resort_llm", "snocountry")  # the only sources scored
NOHRSC_PLACE = "NOHRSC 24 h grid"


@dataclass(frozen=True)
class Observed:
    """The resort-reported snow for one mountain-day and where it came from."""

    snow_in: float
    source: str  # resort | resort_llm | snocountry
    detail: str  # observation id


@dataclass(frozen=True)
class VerificationRow:
    """One `verification` row, in column order."""

    for_date: str
    edition: str
    mountain: str
    window: str
    method: str
    forecast_in: float
    observed_in: float | None
    observed_source: str | None
    error_in: float | None
    ratio: float | None
    result: Result
    tags: str  # JSON list
    observed_elev_ft: int | None = None  # NULL since J26: resort measurement sites are unknown
    context: str = "[]"  # JSON list of ContextReport


# --- scoring ---------------------------------------------------------------------


def score(forecast_in: float, observed: float | None) -> tuple[float | None, float | None, Result]:
    """(error, ratio, result): HIT within 2 inches or 30% of the observation."""
    if observed is None:
        return None, None, "NO_OBS"
    error = round(forecast_in - observed, 2)
    ratio = round(forecast_in / observed, 3) if observed > 0 else None
    hit = abs(error) <= HIT_ABS_IN or (observed > 0 and abs(error) / observed <= HIT_REL)
    return error, ratio, "HIT" if hit else "MISS"


# --- observations ----------------------------------------------------------------


def _local(day: date, hour: int, tz: str) -> datetime:
    return datetime.combine(day, time(hour), ZoneInfo(tz))


def _resort(conn: sqlite3.Connection, mountain: str, obs_date: date) -> Observed | None:
    for source in ("resort", "resort_llm"):
        row = conn.execute(
            "SELECT id, new_24h_in FROM observation WHERE mountain = ? AND source = ?"
            " AND obs_date = ? AND new_24h_in IS NOT NULL"
            " AND coalesce(json_extract(raw, '$.stale'), 0) = 0"
            " ORDER BY reported_at, id LIMIT 1",
            (mountain, source, obs_date.isoformat()),
        ).fetchone()
        if row is not None:
            return Observed(float(row[1]), source, f"observation:{row[0]}")
    return None


def _snocountry(conn: sqlite3.Connection, mountain: str, obs_date: date) -> Observed | None:
    row = conn.execute(
        "SELECT id, new_24h_in FROM observation WHERE mountain = ? AND source = 'snocountry'"
        " AND obs_date = ? AND new_24h_in IS NOT NULL ORDER BY id DESC LIMIT 1",
        (mountain, obs_date.isoformat()),
    ).fetchone()
    return None if row is None else Observed(float(row[1]), "snocountry", f"observation:{row[0]}")


def observed(
    conn: sqlite3.Connection, mountain: str, day: date, tz: str = TIMEZONE
) -> Observed | None:
    """The snow the resort reported for 6 AM `day` to 6 AM the next day, or None.

    Only resort-reported values count (decisions row 24): the live report,
    else SnoCountry's copy for the same date. Resort values are keyed by
    obs_date, so `tz` is unused; callers pass it alongside `context`.
    """
    obs_date = day + timedelta(days=1)
    return _resort(conn, mountain, obs_date) or _snocountry(conn, mountain, obs_date)


# --- context: every other source for the day --------------------------------------


@dataclass(frozen=True)
class PointReport:
    """A `snow_report` row inside one day's window (any distance)."""

    id: str
    product_id: str
    place: str
    lat: float
    lon: float
    elev_ft: int | None
    ending_at: str
    snow_in: float
    hours: int | None


def report_source(product_id: str) -> str:
    if product_id == GHCN_PRODUCT:
        return "ghcn"
    return "pns" if "-PNS" in product_id else "lsr"


def day_reports(conn: sqlite3.Connection, day: date, tz: str = TIMEZONE) -> list[PointReport]:
    """Point reports for day X: 24 h reports ending 4-10 AM on X + 1, and reports of
    unknown span ending after 6 AM X and by 10 AM X + 1 (other spans are skipped)."""
    start = _local(day, 6, tz)
    next_day = day + timedelta(days=1)
    first, last = (_local(next_day, h, tz) for h in DAILY_REPORT_HOURS)
    lo = (start - timedelta(days=1)).astimezone(ZoneInfo("UTC")).date().isoformat()
    hi = (last + timedelta(days=1)).astimezone(ZoneInfo("UTC")).date().isoformat()
    out: list[PointReport] = []
    for r in conn.execute(
        "SELECT id, product_id, place, lat, lon, elev_ft, ending_at, snow_in, hours"
        " FROM snow_report WHERE ending_at >= ? AND ending_at < ? AND snow_in IS NOT NULL"
        " AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY id",
        (lo, hi),
    ):
        ending = datetime.fromisoformat(r[6])
        daily = r[8] == 24 and first <= ending <= last
        unknown_span = r[8] is None and start < ending <= last
        if daily or unknown_span:
            out.append(
                PointReport(
                    id=str(r[0]),
                    product_id=str(r[1] or ""),
                    place=str(r[2] or ""),
                    lat=float(r[3]),
                    lon=float(r[4]),
                    elev_ft=None if r[5] is None else int(r[5]),
                    ending_at=str(r[6]),
                    snow_in=float(r[7]),
                    hours=None if r[8] is None else int(r[8]),
                )
            )
    return out


def _nohrsc(conn: sqlite3.Connection, mountain: Mountain, obs_date: date) -> ContextReport | None:
    row = conn.execute(
        "SELECT new_24h_in FROM observation WHERE mountain = ? AND source = 'nohrsc'"
        " AND obs_date = ? AND new_24h_in IS NOT NULL ORDER BY id DESC LIMIT 1",
        (mountain.key, obs_date.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return ContextReport(
        source="nohrsc",
        place=NOHRSC_PLACE,
        snow_in=float(row[0]),
        elev_ft=mountain.nohrsc_pixel_elev_ft,
    )


def context(
    conn: sqlite3.Connection,
    mountain: Mountain,
    day: date,
    tz: str = TIMEZONE,
    reports: Sequence[PointReport] | None = None,
) -> list[ContextReport]:
    """Every non-resort value for the mountain-day: NOHRSC at the pixel, then each PNS /
    LSR / GHCN report within 20 mi, nearest first. `reports` defaults to `day_reports`."""
    out: list[ContextReport] = []
    grid = _nohrsc(conn, mountain, day + timedelta(days=1))
    if grid is not None:
        out.append(grid)
    near: list[tuple[float, PointReport]] = []
    for r in day_reports(conn, day, tz) if reports is None else reports:
        miles = round(distance_mi(r.lat, r.lon, mountain.lat, mountain.lon), 1)
        if miles <= CONTEXT_MI:
            near.append((miles, r))
    near.sort(key=lambda x: (x[0], x[1].place, x[1].id))
    out.extend(
        ContextReport(
            source=report_source(r.product_id),
            place=r.place,
            snow_in=r.snow_in,
            elev_ft=r.elev_ft,
            distance_mi=miles,
            hours=r.hours,
            ending_at=r.ending_at,
        )
        for miles, r in near
    )
    return out


def context_json(reports: Sequence[ContextReport]) -> str:
    return json.dumps([r.model_dump() for r in reports])


# --- forecasts per edition -------------------------------------------------------


@dataclass(frozen=True)
class EditionForecast:
    edition: str
    run_id: int
    mountain: str
    methods: dict[str, float]  # raw_mean (and corrected), plus each model
    tags: list[str]


def edition_forecasts(conn: sqlite3.Connection, for_date: date) -> list[EditionForecast]:
    """The 1d forecasts of each edition's newest report version for `for_date`."""
    runs = conn.execute(
        "SELECT json_extract(r.payload, '$.edition') AS edition, r.run_id FROM report r"
        " JOIN (SELECT json_extract(payload, '$.edition') AS edition, max(version) AS version"
        "       FROM report WHERE for_date = ? AND status IN ('published', 'numbers_only')"
        "       GROUP BY 1) latest"
        " ON latest.version = r.version AND r.for_date = ?"
        " WHERE latest.edition IS NOT NULL ORDER BY 1",
        (for_date.isoformat(), for_date.isoformat()),
    ).fetchall()
    out: list[EditionForecast] = []
    for edition, run_id in runs:
        tags = {
            r[0]: json.loads(r[1] or "[]")
            for r in conn.execute(
                "SELECT mountain, tags FROM forecast_day WHERE run_id = ? AND day = ?",
                (run_id, for_date.isoformat()),
            )
        }
        by_mountain: dict[str, dict[str, float]] = {}
        for mountain, method, snow_in, by_model in conn.execute(
            "SELECT mountain, method, snow_in, by_model FROM forecast_window"
            " WHERE run_id = ? AND window = ? ORDER BY mountain, method",
            (run_id, WINDOW),
        ):
            methods = by_mountain.setdefault(mountain, {})
            methods[method] = float(snow_in)
            if method == RAW_MEAN:
                for model, value in json.loads(by_model or "{}").items():
                    if value is not None:
                        methods[model] = float(value)
        out.extend(
            EditionForecast(edition, run_id, m, methods, tags.get(m, []))
            for m, methods in sorted(by_mountain.items())
        )
    return out


def backfill_forecasts(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    tz: str = TIMEZONE,
    extra: Mapping[tuple[str, date], Mapping[str, float]] | None = None,
) -> dict[date, list[EditionForecast]]:
    """Edition `backfill` for days `start`..`end`: each model's lead-day-1 summit forecast
    from `forecast_hourly` (a season backfill's Previous Runs and Historical Forecast, or
    any stored run), and the global models' mean as `raw_mean`. Used only for days with
    no stored report edition.
    `extra` adds other methods per (mountain, day), such as `resort_ratio`."""
    from hazelst.learn import model_forecasts  # learn imports this module

    by_day: dict[date, dict[str, dict[str, float]]] = {}
    for (mountain, model, lead_day, day), snow_in in model_forecasts(conn, tz, through=end).items():
        if lead_day == 1 and day >= start:
            by_day.setdefault(day, {}).setdefault(mountain, {})[model] = round(snow_in, 2)
    out: dict[date, list[EditionForecast]] = {}
    for day, mountains in sorted(by_day.items()):
        out[day] = []
        for mountain, models in sorted(mountains.items()):
            # The model mean is the global models'; the finer models are scored by name.
            global_values = [v for m, v in models.items() if not is_fine(m)]
            mean = (
                {RAW_MEAN: round(sum(global_values) / len(global_values), 2)}
                if global_values
                else {}
            )
            out[day].append(
                EditionForecast(
                    BACKFILL_EDITION,
                    0,
                    mountain,
                    {**models, **mean, **(extra or {}).get((mountain, day), {})},
                    [],
                )
            )
    return out


def verify_date(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    for_date: date,
    tz: str = TIMEZONE,
    forecasts: Sequence[EditionForecast] | None = None,
) -> list[VerificationRow]:
    """Score every edition, mountain, and method for `for_date`'s 1d window.

    `forecasts` defaults to the stored report editions (`edition_forecasts`).
    """
    by_key = {m.key: m for m in mountains}
    seen: dict[str, tuple[Observed | None, str]] = {}
    reports: list[PointReport] | None = None
    rows: list[VerificationRow] = []
    if forecasts is None:
        forecasts = edition_forecasts(conn, for_date)
    for f in forecasts:
        if f.mountain not in by_key:
            continue
        if f.mountain not in seen:
            if reports is None:
                reports = day_reports(conn, for_date, tz)
            found = context(conn, by_key[f.mountain], for_date, tz, reports)
            seen[f.mountain] = (observed(conn, f.mountain, for_date, tz), context_json(found))
        obs, ctx = seen[f.mountain]
        for method, forecast_in in sorted(f.methods.items()):
            error, ratio, result = score(forecast_in, obs.snow_in if obs else None)
            rows.append(
                VerificationRow(
                    for_date=for_date.isoformat(),
                    edition=f.edition,
                    mountain=f.mountain,
                    window=WINDOW,
                    method=method,
                    forecast_in=forecast_in,
                    observed_in=obs.snow_in if obs else None,
                    observed_source=obs.source if obs else None,
                    error_in=error,
                    ratio=ratio,
                    result=result,
                    tags=json.dumps(f.tags),
                    observed_elev_ft=None,
                    context=ctx,
                )
            )
    return rows


def store(conn: sqlite3.Connection, rows: Iterable[VerificationRow]) -> int:
    params = [astuple(r) for r in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO verification (for_date, edition, mountain, window, method,"
        " forecast_in, observed_in, observed_source, error_in, ratio, result, tags,"
        " observed_elev_ft, context) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def summarize(rows: Iterable[VerificationRow]) -> dict[str, dict[str, int]]:
    """Edition -> result counts for the model mean (one per mountain)."""
    counts: dict[str, Counter[str]] = {}
    for r in rows:
        if r.method == RAW_MEAN:
            counts.setdefault(r.edition, Counter())[r.result] += 1
    return {e: dict(c) for e, c in sorted(counts.items())}


def latest_summary(conn: sqlite3.Connection, before: date) -> VerificationSummary | None:
    """Result counts (model mean) for the newest verified date before `before`."""
    row = conn.execute(
        "SELECT max(for_date) FROM verification WHERE for_date < ? AND method = ? AND edition != ?",
        (before.isoformat(), RAW_MEAN, BACKFILL_EDITION),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return summary_for(conn, date.fromisoformat(row[0]))


def summary_for(conn: sqlite3.Connection, for_date: date) -> VerificationSummary | None:
    counts: dict[str, dict[str, int]] = {}
    for edition, result, n in conn.execute(
        "SELECT edition, result, count(*) FROM verification WHERE for_date = ? AND method = ?"
        " AND edition != ? GROUP BY 1, 2 ORDER BY 1, 2",
        (for_date.isoformat(), RAW_MEAN, BACKFILL_EDITION),
    ):
        counts.setdefault(edition, {})[result] = n
    if not counts:
        return None
    return VerificationSummary(for_date=for_date, editions=counts)


# --- summary by elevation --------------------------------------------------------

BANDS: tuple[str, ...] = ("< 1,500 ft", "1,500-2,500 ft", "> 2,500 ft", "unknown")
ALL_MOUNTAINS = "all"
RESORT_GROUP = "resort"  # the score: the forecast against the resort's report


def elevation_band(elev_ft: int | None) -> str:
    """The band of a report's elevation; 1,500 and 2,500 ft fall in the middle band."""
    if elev_ft is None:
        return BANDS[3]
    if elev_ft < 1500:
        return BANDS[0]
    if elev_ft <= 2500:
        return BANDS[1]
    return BANDS[2]


@dataclass(frozen=True)
class SummaryCell:
    """Scores for one mountain (or `all`) and one group.

    The group is `all` (by mountain), or, by elevation, `resort` (the score)
    or an elevation band of the context reports (the forecast against the
    highest non-resort report in that band; context, not a score).
    """

    mountain: str
    group: str
    n: int
    bias_ratio: float | None  # sum of forecasts / sum of observations
    mae_in: float
    hit_rate: float


def _cell(mountain: str, group: str, rows: Sequence[tuple[float, float, str]]) -> SummaryCell:
    forecast = sum(r[0] for r in rows)
    observed_total = sum(r[1] for r in rows)
    return SummaryCell(
        mountain=mountain,
        group=group,
        n=len(rows),
        bias_ratio=round(forecast / observed_total, 2) if observed_total > 0 else None,
        mae_in=round(sum(abs(r[0] - r[1]) for r in rows) / len(rows), 2),
        hit_rate=round(sum(1 for r in rows if r[2] == "HIT") / len(rows), 2),
    )


def _band_peaks(context_text: str | None) -> dict[str, float]:
    """Elevation band -> the highest context report in it (NOHRSC at its pixel elevation)."""
    peaks: dict[str, float] = {}
    for item in json.loads(context_text or "[]"):
        band = elevation_band(item.get("elev_ft"))
        peaks[band] = max(peaks.get(band, 0.0), float(item["snow_in"]))
    return peaks


def summary_cells(
    conn: sqlite3.Connection,
    *,
    by: Literal["mountain", "elevation"] = "elevation",
    start: date | None = None,
    end: date | None = None,
    edition: str | None = None,
    method: str = RAW_MEAN,
    snow_days_only: bool = True,
) -> list[SummaryCell]:
    """n, bias ratio, MAE, and hit rate per mountain (and all mountains) per group.

    One row per mountain-day: `edition` if given, else the earliest edition
    scored that day. The `resort` / `all` group reads the scored rows (not
    NO_OBS). By elevation, each band compares the same forecast with the
    highest context report in that band (`verification.context`), so the
    bands show how valley and high-site reports differ from the resort's
    number; they never feed the score. Snow days only by default (the
    observation or the forecast is at least 0.5 inch), as in `skill`.
    """
    from hazelst.learn import SNOW_DAY_IN  # learn imports this module

    sql = (
        "SELECT for_date, edition, mountain, forecast_in, observed_in, result, context"
        " FROM verification WHERE window = ? AND method = ?"
    )
    params: list[object] = [WINDOW, method]
    if start is not None:
        sql, params = sql + " AND for_date >= ?", [*params, start.isoformat()]
    if end is not None:
        sql, params = sql + " AND for_date <= ?", [*params, end.isoformat()]
    if edition is not None:
        sql, params = sql + " AND edition = ?", [*params, edition]
    seen: set[tuple[str, str]] = set()
    groups: dict[tuple[str, str], list[tuple[float, float, str]]] = {}

    def add(mountain: str, group: str, forecast_in: float, value: float) -> None:
        if snow_days_only and max(forecast_in, value) < SNOW_DAY_IN:
            return
        row = (forecast_in, value, score(forecast_in, value)[2])
        groups.setdefault((mountain, group), []).append(row)
        groups.setdefault((ALL_MOUNTAINS, group), []).append(row)

    for for_date, _edition, mountain, forecast_in, observed_in, _result, ctx in conn.execute(
        sql + " ORDER BY for_date, mountain, edition", params
    ):
        if (for_date, mountain) in seen:
            continue
        seen.add((for_date, mountain))
        if observed_in is not None:
            add(
                mountain,
                RESORT_GROUP if by == "elevation" else ALL_MOUNTAINS,
                float(forecast_in),
                float(observed_in),
            )
        if by == "elevation":
            for band, value in _band_peaks(ctx).items():
                add(mountain, band, float(forecast_in), value)
    order = {g: i for i, g in enumerate((RESORT_GROUP, *BANDS, ALL_MOUNTAINS))}
    keys = sorted(groups, key=lambda k: (k[0] == ALL_MOUNTAINS, k[0], order[k[1]]))
    return [_cell(m, g, groups[(m, g)]) for m, g in keys]


# --- accumulation ----------------------------------------------------------------


def _season(
    conn: sqlite3.Connection, mountain: str, for_date: date
) -> tuple[str, float | None, float | None] | None:
    """(source, season_in, base_in) from the newest non-stale resort report (no fallback)."""
    oldest = (for_date - timedelta(days=SEASON_LOOKBACK_DAYS)).isoformat()
    row = conn.execute(
        "SELECT season_in, base_in FROM observation WHERE mountain = ? AND source = 'resort'"
        " AND obs_date BETWEEN ? AND ? AND coalesce(json_extract(raw, '$.stale'), 0) = 0"
        " AND (season_in IS NOT NULL OR base_in IS NOT NULL)"
        " ORDER BY reported_at DESC, id DESC LIMIT 1",
        (mountain, oldest, for_date.isoformat()),
    ).fetchone()
    return None if row is None else ("resort", row[0], row[1])


def accumulation(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    for_date: date,
    tz: str = TIMEZONE,
) -> dict[str, Accumulation]:
    """Per mountain: the resort-reported snow on each of the 7 days before `for_date`
    (None = no report), the resort's season total and base, and each day's context.

    A mountain with neither a resort value nor any context is left out (the
    page shows its empty state when no mountain has resort data).
    """
    days = [for_date - timedelta(days=n) for n in range(7, 0, -1)]
    reports = {d: day_reports(conn, d, tz) for d in days}
    out: dict[str, Accumulation] = {}
    for m in mountains:
        found = [observed(conn, m.key, d, tz) for d in days]
        beside = [context(conn, m, d, tz, reports[d]) for d in days]
        season = _season(conn, m.key, for_date)
        sources = sorted({o.source for o in found if o is not None})
        if season is None and not sources and not any(beside):
            continue
        if season:
            source = season[0]
        elif sources:
            source = max(sources, key=lambda s: sum(1 for o in found if o and o.source == s))
        else:
            source = "none"
        out[m.key] = Accumulation(
            source=source,
            last7_in=[round(o.snow_in, 1) if o else None for o in found],
            season_in=season[1] if season else None,
            base_in=season[2] if season else None,
            sources=sorted({*sources, *([season[0]] if season else [])}),
            days=[
                AccumulationDay(
                    date=d,
                    snow_in=round(o.snow_in, 1) if o else None,
                    source=o.source if o else None,
                    context=c,
                )
                for d, o, c in zip(days, found, beside, strict=True)
            ],
        )
    return out
