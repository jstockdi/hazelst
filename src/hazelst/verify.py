"""forecast-verify: score each edition's 1d forecast against what fell; accumulation.

A report date X has a 1d window from 6 AM X to 6 AM X + 1 (local). Its
observed snow is the best available observation for that day, in priority
order (data-model.md, "How the data compounds"):

1. `resort`: the earliest non-stale resort report on X + 1 with a 24 h value
   (then `resort_llm`, which never overrides an adapter value);
2. `snocountry`: the resort-reported value SnoCountry holds for obs_date X + 1
   (an inferred zero counts: the resort reported a base depth that day);
3. `pns` / `lsr` / `ghcn`: point reports within 5 mi of the mountain in
   `snow_report`: 24 h reports ending 4-10 AM on X + 1 (PNS `SNOW_24`, and
   GHCN co-op and CoCoRaHS days, which end at 7 AM); if there are none,
   reports of unknown span (LSRs, storm totals) ending in the window or by
   10 AM on X + 1. The highest report counts, because spotters sit below the
   summits; on a tie the GHCN copy is named (a quality-checked record);
4. `nohrsc`: the 24 h grid ending 12Z on X + 1;
5. none: `NO_OBS`.

`observation.obs_date` D means "the 24 h ending the morning of D", so day X
reads obs_date X + 1. HIT is within 2 inches or 30% of the observed value.

Each row also records the observation's elevation (`observed_elev_ft`): the
report's for a point report, the NOHRSC pixel center's for `nohrsc`, NULL for
resort-reported values (measurement site unknown). `summary_cells` reads
the scores per mountain by elevation band.

A day with no stored report (a season backfill) is scored as edition
`backfill`: each model's lead-day-1 summit forecast from `forecast_hourly`.

The same day-by-day choice fills the accumulation section: snow observed on
each of the 7 days before the report date, the season total, and base depth.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import astuple, dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from hazelst.config import TIMEZONE, Mountain
from hazelst.models import Accumulation, VerificationSummary

Result = Literal["HIT", "MISS", "NO_OBS"]
NEAR_MI = 5.0
HIT_ABS_IN = 2.0
HIT_REL = 0.30
WINDOW = "1d"
RAW_MEAN = "raw_mean"
BACKFILL_EDITION = "backfill"  # a day with no report: scored from forecast_hourly
SEASON_LOOKBACK_DAYS = 3  # season and base come from a report at most this old
DAILY_REPORT_HOURS = (4, 10)  # a 24 h point report ending in this local-hour range
GHCN_PRODUCT = "GHCN"  # snow_report.product_id of GHCN-Daily station rows


@dataclass(frozen=True)
class Observed:
    """The observed snow for one mountain-day and where it came from."""

    snow_in: float
    source: str  # resort | resort_llm | snocountry | pns | lsr | ghcn | nohrsc
    detail: str  # observation id, or snow_report ids
    elev_ft: int | None = None  # the point report's elevation (snow_report.elev_ft)


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
    observed_elev_ft: int | None = None


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


def _report_source(product_id: str) -> str:
    if product_id == GHCN_PRODUCT:
        return "ghcn"
    return "pns" if "-PNS" in product_id else "lsr"


def _point_reports(conn: sqlite3.Connection, mountain: str, day: date, tz: str) -> Observed | None:
    """The highest qualifying PNS / LSR / GHCN report within 5 mi (see the module docstring)."""
    rows = conn.execute(
        "SELECT id, product_id, ending_at, snow_in, hours, elev_ft FROM snow_report"
        " WHERE nearest_mountain = ? AND distance_mi <= ? AND snow_in IS NOT NULL",
        (mountain, NEAR_MI),
    ).fetchall()
    start = _local(day, 6, tz)
    next_day = day + timedelta(days=1)
    first, last = (_local(next_day, h, tz) for h in DAILY_REPORT_HOURS)
    daily, other = [], []
    for r in rows:
        ending = datetime.fromisoformat(r[2])
        if r[4] == 24 and first <= ending <= last:
            daily.append(r)
        elif r[4] is None and start < ending <= last:
            other.append(r)
    chosen = daily or other
    if not chosen:
        return None
    best = max(chosen, key=lambda r: (r[3], r[1] == GHCN_PRODUCT))
    source = _report_source(str(best[1]))
    elev = None if best[5] is None else int(best[5])
    return Observed(float(best[3]), source, f"snow_report:{best[0]}", elev)


def _nohrsc(conn: sqlite3.Connection, mountain: str, obs_date: date) -> Observed | None:
    row = conn.execute(
        "SELECT id, new_24h_in FROM observation WHERE mountain = ? AND source = 'nohrsc'"
        " AND obs_date = ? AND new_24h_in IS NOT NULL ORDER BY id DESC LIMIT 1",
        (mountain, obs_date.isoformat()),
    ).fetchone()
    return None if row is None else Observed(float(row[1]), "nohrsc", f"observation:{row[0]}")


def observed(
    conn: sqlite3.Connection, mountain: str, day: date, tz: str = TIMEZONE
) -> Observed | None:
    """Snow observed at a mountain from 6 AM `day` to 6 AM the next day, best source first."""
    obs_date = day + timedelta(days=1)
    return (
        _resort(conn, mountain, obs_date)
        or _snocountry(conn, mountain, obs_date)
        or _point_reports(conn, mountain, day, tz)
        or _nohrsc(conn, mountain, obs_date)
    )


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
    conn: sqlite3.Connection, start: date, end: date, tz: str = TIMEZONE
) -> dict[date, list[EditionForecast]]:
    """Edition `backfill` for days `start`..`end`: each model's lead-day-1 summit forecast
    from `forecast_hourly` (a season backfill's Previous Runs, or any stored run), and
    their mean as `raw_mean`. Used only for days with no stored report edition."""
    from hazelst.learn import model_forecasts  # learn imports this module

    by_day: dict[date, dict[str, dict[str, float]]] = {}
    for (mountain, model, lead_day, day), snow_in in model_forecasts(conn, tz, through=end).items():
        if lead_day == 1 and day >= start:
            by_day.setdefault(day, {}).setdefault(mountain, {})[model] = round(snow_in, 2)
    out: dict[date, list[EditionForecast]] = {}
    for day, mountains in sorted(by_day.items()):
        out[day] = [
            EditionForecast(
                BACKFILL_EDITION,
                0,
                mountain,
                {**models, RAW_MEAN: round(sum(models.values()) / len(models), 2)},
                [],
            )
            for mountain, models in sorted(mountains.items())
        ]
    return out


POINT_SOURCES = frozenset({"pns", "lsr", "ghcn"})


def observed_elev_ft(obs: Observed | None, mountain: Mountain) -> int | None:
    """The observation's elevation: the report's for a point report, the NOHRSC pixel
    center's for `nohrsc`, unknown (None) for resort-reported values."""
    if obs is None:
        return None
    if obs.source in POINT_SOURCES:
        return obs.elev_ft
    if obs.source == "nohrsc":
        return mountain.nohrsc_pixel_elev_ft
    return None


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
    seen: dict[str, Observed | None] = {}
    rows: list[VerificationRow] = []
    if forecasts is None:
        forecasts = edition_forecasts(conn, for_date)
    for f in forecasts:
        if f.mountain not in by_key:
            continue
        if f.mountain not in seen:
            seen[f.mountain] = observed(conn, f.mountain, for_date, tz)
        obs = seen[f.mountain]
        elev = observed_elev_ft(obs, by_key[f.mountain])
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
                    observed_elev_ft=elev,
                )
            )
    return rows


def store(conn: sqlite3.Connection, rows: Iterable[VerificationRow]) -> int:
    params = [astuple(r) for r in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO verification (for_date, edition, mountain, window, method,"
        " forecast_in, observed_in, observed_source, error_in, ratio, result, tags,"
        " observed_elev_ft) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def elevation_band(elev_ft: int | None) -> str:
    """The band of an observation's elevation; 1,500 and 2,500 ft fall in the middle band."""
    if elev_ft is None:
        return BANDS[3]
    if elev_ft < 1500:
        return BANDS[0]
    if elev_ft <= 2500:
        return BANDS[1]
    return BANDS[2]


@dataclass(frozen=True)
class SummaryCell:
    """Scores for one mountain (or `all`) and one group (an elevation band, or `all`)."""

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

    Scored rows only (not NO_OBS). One row per mountain-day: `edition` if given,
    else the earliest edition scored that day. Snow days only by default (the
    observation or the forecast is at least 0.5 inch), as in `skill`.
    """
    from hazelst.learn import SNOW_DAY_IN  # learn imports this module

    sql = (
        "SELECT for_date, edition, mountain, forecast_in, observed_in, result, observed_elev_ft"
        " FROM verification WHERE window = ? AND method = ? AND result != 'NO_OBS'"
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
    for for_date, _edition, mountain, forecast_in, observed_in, result, elev in conn.execute(
        sql + " ORDER BY for_date, mountain, edition", params
    ):
        if (for_date, mountain) in seen:
            continue
        seen.add((for_date, mountain))
        if snow_days_only and max(forecast_in, observed_in) < SNOW_DAY_IN:
            continue
        group = elevation_band(elev) if by == "elevation" else ALL_MOUNTAINS
        row = (float(forecast_in), float(observed_in), str(result))
        groups.setdefault((mountain, group), []).append(row)
        groups.setdefault((ALL_MOUNTAINS, group), []).append(row)
    order = {g: i for i, g in enumerate((*BANDS, ALL_MOUNTAINS))}
    keys = sorted(groups, key=lambda k: (k[0] == ALL_MOUNTAINS, k[0], order[k[1]]))
    return [_cell(m, g, groups[(m, g)]) for m, g in keys]


# --- accumulation ----------------------------------------------------------------


def _season(
    conn: sqlite3.Connection, mountain: str, for_date: date
) -> tuple[str, float | None, float | None] | None:
    """(source, season_in, base_in) from the newest non-stale resort report, else NOHRSC."""
    oldest = (for_date - timedelta(days=SEASON_LOOKBACK_DAYS)).isoformat()
    row = conn.execute(
        "SELECT season_in, base_in FROM observation WHERE mountain = ? AND source = 'resort'"
        " AND obs_date BETWEEN ? AND ? AND coalesce(json_extract(raw, '$.stale'), 0) = 0"
        " AND (season_in IS NOT NULL OR base_in IS NOT NULL)"
        " ORDER BY reported_at DESC, id DESC LIMIT 1",
        (mountain, oldest, for_date.isoformat()),
    ).fetchone()
    if row is not None:
        return "resort", row[0], row[1]
    row = conn.execute(
        "SELECT season_in FROM observation WHERE mountain = ? AND source = 'nohrsc'"
        " AND obs_date BETWEEN ? AND ? AND season_in IS NOT NULL"
        " ORDER BY obs_date DESC, id DESC LIMIT 1",
        (mountain, oldest, for_date.isoformat()),
    ).fetchone()
    if row is not None:
        return "nohrsc", row[0], None
    return None


def accumulation(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    for_date: date,
    tz: str = TIMEZONE,
) -> dict[str, Accumulation]:
    """Per mountain: observed snow on each of the 7 days before `for_date`, season, base.

    A mountain with no observation at all is left out (the page shows its
    empty state when no mountain has data).
    """
    out: dict[str, Accumulation] = {}
    for m in mountains:
        days = [for_date - timedelta(days=n) for n in range(7, 0, -1)]
        found = [observed(conn, m.key, d, tz) for d in days]
        season = _season(conn, m.key, for_date)
        sources = sorted({o.source for o in found if o is not None})
        if season is None and not sources:
            continue
        source = (
            season[0]
            if season
            else max(sources, key=lambda s: sum(1 for o in found if o and o.source == s))
        )
        out[m.key] = Accumulation(
            source=source,
            last7_in=[round(o.snow_in, 1) if o else None for o in found],
            season_in=season[1] if season else None,
            base_in=season[2] if season else None,
            sources=sorted({*sources, *([season[0]] if season else [])}),
        )
    return out
