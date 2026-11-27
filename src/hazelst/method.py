"""The method each mountain's page uses, chosen on held-out evidence (decisions row 28).

forecast-method.md, "Method choice":

- **Candidates.** `raw_mean` and every other method `report-build` stores in
  `forecast_window` that the held-out test can score (`resort_ratio`,
  `corrected`), plus the finer-model day-1 candidates (`ratio.FINE_METHODS`,
  J29) whether or not a report has stored them yet. A finer-model candidate
  whose model has fewer than 15 fit days at a mountain is left out there
  (`ratio.thin_methods`). `corrected` never becomes the headline
  (forecast-method.md, "Corrected value"): its row's `chosen` means the page
  shows it beneath the headline.
- **Held-out season.** Seasons run 1 Nov - 30 Apr. The test season is the
  newest season that ended before the decision date; the fit uses every day
  through the 30 Apr before it (`ratio.held_out`, the same days and values as
  `hazelst ratio backtest`). Every method is scored on the same lead-day-1
  snow days with a resort report.
- **Live days.** Once the current season has at least 20 live verified snow
  days at a mountain (1d `verification` rows of editions other than
  `backfill`, every candidate scored on the day, report or a forecast at
  least 0.5 inch), that mountain is decided on those days only. A day's error
  is the mean over its editions.
- **Rule.** A headline method is chosen when its MAE is at least 0.05 inch
  below `raw_mean`'s and it has the lowest MAE among the headline candidates;
  a tie for lowest keeps `raw_mean`. `corrected` is shown when its MAE is at
  least 0.05 inch below `raw_mean`'s.
- **Day 1.** When the winner is a finer-model candidate, it is chosen for day
  1 only, and the same rule over the global candidates alone picks the method
  for the windows and days 2-5; both rows are `chosen` (`Choice.day1`,
  `Choice.headline`).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from hazelst import corrected, learn, ratio, verify
from hazelst.config import TIMEZONE, Mountain

RAW_MEAN = verify.RAW_MEAN
MARGIN_IN = 0.05
LIVE_DAYS = 20
EPSILON = 1e-9
# Methods shown beside the headline, never as it.
BESIDE: frozenset[str] = frozenset({corrected.METHOD})
# What a chosen `method_gate` row does on the page (`hazelst method choose`, column `role`).
HEADLINE, BENEATH, DAY1 = "headline", "beneath", "day1"


def role(method: str) -> str:
    """The page role of `method` when its row is chosen: `day1` for a finer-model
    candidate (day 1 only), `beneath` for `corrected` (shown under the headline),
    else `headline` (the windows and days; days 2-5 when a `day1` row is chosen)."""
    if method in ratio.FINE_METHODS:
        return DAY1
    if method in BESIDE:
        return BENEATH
    return HEADLINE


@dataclass(frozen=True)
class GateRow:
    """One `method_gate` row, in column order (without `decided_at`)."""

    mountain: str
    method: str
    test_from: date | None
    test_to: date | None
    n: int
    mae_in: float | None
    raw_mean_mae_in: float | None
    chosen: bool

    @property
    def role(self) -> str:
        """`headline`, `beneath`, or `day1` when chosen (`role()`); "" otherwise."""
        return role(self.method) if self.chosen else ""


@dataclass(frozen=True)
class Choice:
    """The page's reading of `method_gate`: headline method per mountain, `corrected` set."""

    headline: dict[str, str]
    beside: dict[str, set[str]]
    day1: dict[str, str] = field(default_factory=dict)  # a finer-model candidate for day 1

    def uses(self, method: str) -> list[str]:
        """Mountains whose headline is `method`."""
        return sorted(m for m, chosen in self.headline.items() if chosen == method)

    def shows(self, method: str) -> list[str]:
        """Mountains that show `method` beneath the headline."""
        return sorted(m for m, methods in self.beside.items() if method in methods)


# --- seasons ----------------------------------------------------------------------------------


def season_span(start_year: int) -> tuple[date, date]:
    """1 Nov of `start_year` through 30 Apr of the next year."""
    return date(start_year, 11, 1), date(start_year + 1, 4, 30)


def held_out_season(as_of: date) -> tuple[date, date]:
    """The newest season that ended before `as_of`."""
    year = as_of.year - 1 if as_of > date(as_of.year, 4, 30) else as_of.year - 2
    return season_span(year)


def current_season(as_of: date) -> tuple[date, date]:
    """The season that contains `as_of`, or the one that ended last before it (May - Oct)."""
    return season_span(as_of.year if as_of.month >= 11 else as_of.year - 1)


# --- candidates and the rule -----------------------------------------------------------------


def stored_methods(conn: sqlite3.Connection) -> set[str]:
    """Methods `report-build` has stored in `forecast_window`, plus `raw_mean`."""
    found = {r[0] for r in conn.execute("SELECT DISTINCT method FROM forecast_window")}
    return found | {RAW_MEAN}


def candidates(conn: sqlite3.Connection) -> set[str]:
    """The stored methods and the finer-model day-1 candidates."""
    return stored_methods(conn) | set(ratio.FINE_METHODS)


def decide_day1(maes: Mapping[str, float]) -> tuple[str, set[str], str | None]:
    """(headline method, methods beside it, finer-model day-1 method or None).

    The rule runs over every candidate; when a finer-model candidate wins, it
    takes day 1 and the rule over the global candidates picks the headline.
    """
    headline, beside = decide(maes)
    if headline not in ratio.FINE_METHODS:
        return headline, beside, None
    global_headline, _ = decide({m: v for m, v in maes.items() if m not in ratio.FINE_METHODS})
    return global_headline, beside, headline


def decide(maes: Mapping[str, float]) -> tuple[str, set[str]]:
    """(headline method, methods shown beside it) from each candidate's MAE on the same days."""
    raw = maes.get(RAW_MEAN)
    if raw is None:
        return RAW_MEAN, set()

    def beats(method: str) -> bool:
        return raw - maes[method] >= MARGIN_IN - EPSILON

    beside = {m for m in maes if m in BESIDE and beats(m)}
    headline = {m: v for m, v in maes.items() if m != RAW_MEAN and m not in BESIDE}
    if not headline:
        return RAW_MEAN, beside
    best = min(headline.values())
    lowest = [m for m, v in headline.items() if abs(v - best) <= EPSILON]
    if len(lowest) != 1 or not beats(lowest[0]):
        return RAW_MEAN, beside
    return lowest[0], beside


def _rows(
    mountain: str,
    errors: Mapping[str, Sequence[float]],
    span: tuple[date, date] | None,
) -> list[GateRow]:
    """Gate rows for one mountain from each method's absolute errors on the same days."""
    maes = {m: sum(e) / len(e) for m, e in errors.items() if e}
    headline, beside, day1 = decide_day1(maes)
    raw = maes.get(RAW_MEAN)
    test_from, test_to = span if span else (None, None)
    out = []
    for method in sorted(maes, key=lambda m: (m != RAW_MEAN, m)):
        out.append(
            GateRow(
                mountain=mountain,
                method=method,
                test_from=test_from,
                test_to=test_to,
                n=len(errors[method]),
                mae_in=round(maes[method], 3),
                raw_mean_mae_in=None if raw is None else round(raw, 3),
                chosen=method in (headline, day1) or method in beside,
            )
        )
    if not out:
        out.append(GateRow(mountain, RAW_MEAN, test_from, test_to, 0, None, None, True))
    return out


# --- held-out season --------------------------------------------------------------------------


def held_out_errors(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    candidates: Iterable[str],
    as_of: date,
    tz: str = TIMEZONE,
) -> tuple[tuple[date, date], dict[str, dict[str, list[float]]]]:
    """(test span, mountain -> method -> absolute errors) on the held-out season."""
    test_from, test_to = held_out_season(as_of)
    fit_to = date(test_from.year, 4, 30)
    found = ratio.held_out(
        conn, mountains, fit_to=fit_to, test_from=test_from, test_to=test_to, tz=tz
    )
    wanted = set(candidates)
    out: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for d in found.days:
        thin = set(found.thin.get(d.mountain, ()))
        for name, value in d.forecasts.items():
            method = ratio.LIVE_NAMES.get(name, name)
            if method in wanted and method not in thin:
                out[d.mountain][method].append(abs(value - d.observed))
    return (test_from, test_to), out


# --- live days --------------------------------------------------------------------------------


def live_errors(
    conn: sqlite3.Connection,
    mountain: str,
    candidates: Iterable[str],
    span: tuple[date, date],
) -> tuple[list[date], dict[str, list[float]]]:
    """(days, method -> each day's absolute error) over the live verified snow days in `span`.

    A day counts when every candidate scored at the mountain this season has a
    1d row for one of its live editions; a day's error is the mean over those
    editions.
    """
    wanted = set(candidates)
    rows = conn.execute(
        "SELECT for_date, edition, method, forecast_in, observed_in FROM verification"
        " WHERE mountain = ? AND window = ? AND edition != ? AND result != 'NO_OBS'"
        " AND for_date >= ? AND for_date <= ?",
        (mountain, verify.WINDOW, verify.BACKFILL_EDITION, *(d.isoformat() for d in span)),
    ).fetchall()
    by_key: dict[tuple[str, str], dict[str, tuple[float, float]]] = defaultdict(dict)
    for for_date, edition, method, forecast_in, observed_in in rows:
        if method in wanted and forecast_in is not None and observed_in is not None:
            by_key[(for_date, edition)][method] = (forecast_in, observed_in)
    scored = {m for methods in by_key.values() for m in methods}
    if RAW_MEAN not in scored:
        return [], {}
    per_day: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (for_date, _edition), methods in by_key.items():
        if set(methods) != scored:
            continue
        values = [f for f, _ in methods.values()] + [methods[RAW_MEAN][1]]
        if max(values) < learn.SNOW_DAY_IN:
            continue
        for method, (forecast_in, observed_in) in methods.items():
            per_day[for_date][method].append(abs(forecast_in - observed_in))
    days = sorted(per_day)
    errors: dict[str, list[float]] = defaultdict(list)
    for day in days:
        for method, values in per_day[day].items():
            errors[method].append(sum(values) / len(values))
    return [date.fromisoformat(d) for d in days], errors


# --- choose and store -------------------------------------------------------------------------


def choose(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    as_of: date,
    tz: str = TIMEZONE,
) -> list[GateRow]:
    """Gate rows for every mountain as of `as_of` (module docstring)."""
    wanted = candidates(conn)
    span, held = held_out_errors(conn, mountains, wanted, as_of, tz)
    season_from, season_to = current_season(as_of)
    live_to = min(season_to, as_of - timedelta(days=1))
    ratios = ratio.Ratios.load(conn)
    out: list[GateRow] = []
    for m in mountains:
        live_wanted = wanted - ratio.thin_methods(ratios, m.key)
        days, live = live_errors(conn, m.key, live_wanted, (season_from, live_to))
        if len(days) >= LIVE_DAYS:
            out += _rows(m.key, live, (days[0], days[-1]))
        else:
            out += _rows(m.key, held.get(m.key, {}), span)
    return out


def store(conn: sqlite3.Connection, rows: Iterable[GateRow], decided_at: datetime) -> int:
    """Replace the whole `method_gate` table."""
    stamp = decided_at.astimezone(UTC).isoformat(timespec="seconds")
    params = [
        (
            r.mountain,
            r.method,
            None if r.test_from is None else r.test_from.isoformat(),
            None if r.test_to is None else r.test_to.isoformat(),
            r.n,
            r.mae_in,
            r.raw_mean_mae_in,
            int(r.chosen),
            stamp,
        )
        for r in rows
    ]
    conn.execute("DELETE FROM method_gate")
    conn.executemany(
        "INSERT INTO method_gate (mountain, method, test_from, test_to, n, mae_in,"
        " raw_mean_mae_in, chosen, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def load(conn: sqlite3.Connection) -> Choice:
    """The chosen methods; a mountain without a chosen headline row uses `raw_mean`."""
    headline: dict[str, str] = {}
    beside: dict[str, set[str]] = defaultdict(set)
    day1: dict[str, str] = {}
    for mountain, method in conn.execute(
        "SELECT mountain, method FROM method_gate WHERE chosen = 1"
    ):
        kind = role(method)
        if kind == BENEATH:
            beside[mountain].add(method)
        elif kind == DAY1:
            day1[mountain] = method
        else:
            headline[mountain] = method
    return Choice(headline=headline, beside=dict(beside), day1=day1)
