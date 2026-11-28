"""The `resort_ratio` forecast method: each model's snow water times a ratio fitted per resort.

forecast-method.md, "Snow amount"; data-model.md, "How the data compounds", step 4;
decisions row 26.

- **Snow water.** Open-Meteo's `snowfall` is snow water converted at a fixed
  0.7 cm per mm (7:1), so each hour's snow water is `snowfall_in / 7`. It
  keeps each model's own rain / snow split; `precip_in` is not used.
- **Cells.** A ratio is fitted per mountain, per model (and `raw_mean`, the
  models' mean), per summit 2 m temperature band (`<=15`, `15-25`, `25-32`,
  `>32` degrees F, upper bounds inclusive; each hour by its model's own
  temperature), and per `nw_upslope` side of the day (`yes` / `no`). Band
  `''` pools every band and upslope `''` both sides. The `all` mountain pools
  every mountain and serves only as the fallback for a thin band.
- **Fit.** Ratio of sums over lead-day-1 snow days (the resort reported or the
  model forecast at least 0.5 inch): the resort-reported 6 AM -> 6 AM snow is
  shared among a day's bands in proportion to the forecast snow water in each,
  and a cell's ratio is its share of resort snow over its snow water, clamped
  to 4-25. A day with no forecast snow water says nothing about the ratio and
  is left out. A day counts toward a band's `n` when that band holds at least
  0.01 inch of its snow water.
- **Resolve.** With the split, a mountain whose model has at least 15 snow
  days on each upslope side uses its (band, side) cell, else its band cell;
  without the split (or a thin side) the mountain's band cell; a band with
  fewer than 15 snow days uses the all-mountain value for that band, then the
  mountain's all-band value, then the all-mountain all-band value. A model
  with no cells of its own (ECMWF has no Previous Runs snowfall) uses the
  `raw_mean` cells.
- **Forecast.** Each model's hourly snow water times its resolved ratio is
  summed into days and windows like `raw_mean`: the headline is the mean of
  the models in use (stale models left out), the range the min-max of the
  per-model totals.
- **Where it shows.** The page uses `resort_ratio` for a mountain's headline,
  range, chart, and table only where `method_gate` chose it (`method.py`).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from hazelst import corrected, learn, tags, verify
from hazelst.config import TIMEZONE, Mountain
from hazelst.derive import DIRECTION_MODEL, WINDOWS, _Series, confidence, nws_disagrees
from hazelst.models import Forecast, ForecastDay, ForecastWindow, MountainForecast, WindowKey
from hazelst.sources import open_meteo
from hazelst.sources.open_meteo import HourlyRow

METHOD = "resort_ratio"
MEAN_MODEL = "raw_mean"  # snow_ratio.model of the fit of the models' mean
ALL_MOUNTAINS = "all"
SNOW_PER_WATER = 7.0  # Open-Meteo snowfall: 0.7 cm per mm of water
FLAT_RATIO = 10.0  # the backtest's flat 10:1 baseline
BANDS: tuple[str, ...] = ("<=15", "15-25", "25-32", ">32")
ALL_BANDS = ""
UPSLOPE_YES, UPSLOPE_NO, BOTH_SIDES = "yes", "no", ""
MIN_DAYS = 15
RATIO_LIMITS = (4.0, 25.0)
BAND_DAY_MIN_WATER_IN = 0.01  # a day counts toward a band's n with this much snow water in it
SEASON_START_MONTH = 7  # seasons run 1 Jul - 30 Jun (leave-one-season-out scoring)


def band(temp_f: float | None) -> str:
    """The summit temperature band of an hour; `''` when the temperature is missing."""
    if temp_f is None:
        return ALL_BANDS
    if temp_f <= 15:
        return BANDS[0]
    if temp_f <= 25:
        return BANDS[1]
    if temp_f <= 32:
        return BANDS[2]
    return BANDS[3]


def season(day: date) -> int:
    """The year a season starts in (1 Jul - 30 Jun)."""
    return day.year if day.month >= SEASON_START_MONTH else day.year - 1


def clamp(value: float) -> float:
    lo, hi = RATIO_LIMITS
    return min(max(value, lo), hi)


# --- lead-day-1 forecasts ---------------------------------------------------------------


@dataclass(frozen=True)
class DayWater:
    """One model's (or the mean's) lead-day-1 forecast for one mountain-day."""

    snow_in: float  # Open-Meteo snowfall (7:1)
    water: Mapping[str, float]  # temperature band -> snow water inches ('' = no temperature)

    @property
    def total_water(self) -> float:
        return sum(self.water.values())


@dataclass(frozen=True)
class LeadDay:
    """Every model's lead-day-1 forecast for one mountain-day, and its upslope side."""

    mountain: str
    day: date
    upslope: bool
    models: Mapping[str, DayWater]  # model id -> forecast; MEAN_MODEL = the models' mean

    def raw_models(self) -> dict[str, DayWater]:
        """The global models (the finer models compete for day 1 separately)."""
        return {
            k: v for k, v in self.models.items() if k != MEAN_MODEL and not open_meteo.is_fine(k)
        }

    def fine_models(self) -> dict[str, DayWater]:
        """The finer models (J29), in `open_meteo.FINE_MODELS` order."""
        return {m: self.models[m] for m in open_meteo.FINE_MODELS if m in self.models}


@dataclass
class _Acc:
    snow: float = 0.0
    water: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    n: int = 0


def _day_water(hours: Iterable[tuple[float, float | None]]) -> DayWater:
    """(snowfall_in, temp_f) per hour -> the day's snow and its snow water by band."""
    water: dict[str, float] = defaultdict(float)
    snow = 0.0
    for snowfall, temp in hours:
        snow += snowfall
        water[band(temp)] += snowfall / SNOW_PER_WATER
    return DayWater(round(snow, 3), {b: round(w, 4) for b, w in water.items() if w > 0})


def lead_days(
    conn: sqlite3.Connection,
    tz: str = TIMEZONE,
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[tuple[str, date], LeadDay]:
    """(mountain, day) -> each model's lead-day-1 summit forecast, as `learn.model_forecasts`.

    A fetch's model series counts for a day when it covers the whole day
    (>= 23 h, no hour before its run) and the day's first hour is under 24 h
    from the run. The models' mean is taken per hour over the models of that
    fetch. `nw_upslope` is the tag's rule over the fetch's hours (850 hPa means
    of the models, GFS direction). Several fetches of a day are averaged; the
    day is upslope when at least half of them tag it. The mean and the tag
    read the global models only; a fetch of the finer models alone adds their
    forecasts and no upslope vote. Only the configured models count
    (`open_meteo.KNOWN_MODELS`).
    """
    local_day = learn._local_day_fn(tz)
    known, known_params = open_meteo.known_models_sql()
    sql = (
        "SELECT fetch_id, mountain, model, valid_at, lead_h, snowfall_in, temp_f,"
        " t850_f, rh850, ws850_mph, wd850 FROM forecast_hourly"
        " WHERE elevation = 'summit' AND lead_h IS NOT NULL AND lead_h >= 0 AND lead_h < 48"
        f" AND snowfall_in IS NOT NULL AND {known}"
    )
    params: list[object] = list(known_params)
    if start is not None:
        sql += " AND valid_at >= ?"
        params.append((start - timedelta(days=1)).isoformat())
    if end is not None:
        sql += " AND valid_at < ?"
        params.append((end + timedelta(days=3)).isoformat())
    per_day: dict[tuple[str, date], list[tuple[dict[str, DayWater], bool | None]]] = defaultdict(
        list
    )
    # One fetch at a time (rows come in fetch order): (mountain, day) -> model -> hour -> values.
    groups: dict[tuple[str, str], dict[str, dict[str, tuple[Any, ...]]]] = {}
    current: int | None = None
    for fetch_id, mountain, model, valid_at, lead_h, *values in conn.execute(
        sql + " ORDER BY fetch_id", params
    ):
        if fetch_id != current:
            _fetch_days(groups, per_day, start, end)
            groups, current = {}, fetch_id
        by_model = groups.setdefault((mountain, local_day(valid_at)), {})
        by_model.setdefault(model, {})[valid_at] = (lead_h, *values)
    _fetch_days(groups, per_day, start, end)
    out: dict[tuple[str, date], LeadDay] = {}
    for (mountain, day), fetches in per_day.items():
        votes = [up for _, up in fetches if up is not None]
        out[(mountain, day)] = LeadDay(
            mountain=mountain,
            day=day,
            upslope=bool(votes) and 2 * sum(1 for up in votes if up) >= len(votes),
            models=_average([m for m, _ in fetches]),
        )
    return out


def _fetch_days(
    groups: Mapping[tuple[str, str], Mapping[str, Mapping[str, tuple[Any, ...]]]],
    per_day: dict[tuple[str, date], list[tuple[dict[str, DayWater], bool | None]]],
    start: date | None,
    end: date | None,
) -> None:
    """Add one fetch's lead-day-1 days to `per_day`: each model's water, and the global
    models' mean and upslope tag (None for a fetch of the finer models alone)."""
    for (mountain, day_text), by_model in groups.items():
        day = date.fromisoformat(day_text)
        if (start is not None and day < start) or (end is not None and day > end):
            continue
        kept = {
            model: hours
            for model, hours in by_model.items()
            if len(hours) >= learn.MIN_HOURS and min(h[0] for h in hours.values()) < 24
        }
        if not kept:
            continue
        models = {
            model: _day_water((h[1], h[2]) for h in hours.values()) for model, hours in kept.items()
        }
        globals_ = {m: hours for m, hours in kept.items() if not open_meteo.is_fine(m)}
        if not globals_:
            per_day[(mountain, day)].append((models, None))
            continue
        mean_hours: list[tuple[float, float | None]] = []
        tag_hours: list[tags.Hour] = []
        for t in sorted({t for hours in globals_.values() for t in hours}):
            at = [hours[t] for hours in globals_.values() if t in hours]
            temps = [a[2] for a in at if a[2] is not None]
            mean_hours.append(
                (sum(a[1] for a in at) / len(at), sum(temps) / len(temps) if temps else None)
            )
            gfs = globals_.get(DIRECTION_MODEL, {}).get(t)
            tag_hours.append(
                tags.Hour(
                    t850_f=_mean(a[3] for a in at),
                    rh850=_mean(a[4] for a in at),
                    ws850_mph=_mean(a[5] for a in at),
                    wd850=None if gfs is None else gfs[6],
                )
            )
        models[MEAN_MODEL] = _day_water(mean_hours)
        per_day[(mountain, day)].append((models, tags.is_upslope(tag_hours)))


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _average(fetches: Sequence[Mapping[str, DayWater]]) -> dict[str, DayWater]:
    """Each model's forecast averaged over the fetches that have it."""
    by_model: dict[str, list[DayWater]] = defaultdict(list)
    for models in fetches:
        for model, dw in models.items():
            by_model[model].append(dw)
    out: dict[str, DayWater] = {}
    for model, items in by_model.items():
        water: dict[str, float] = defaultdict(float)
        for dw in items:
            for b, w in dw.water.items():
                water[b] += w / len(items)
        out[model] = DayWater(
            round(sum(dw.snow_in for dw in items) / len(items), 3),
            {b: round(w, 4) for b, w in water.items()},
        )
    return out


def observed_days(
    conn: sqlite3.Connection, keys: Iterable[tuple[str, date]], tz: str = TIMEZONE
) -> dict[tuple[str, date], float]:
    """(mountain, day) -> the resort-reported snow (`verify.observed`); days without one omitted."""
    out: dict[tuple[str, date], float] = {}
    for mountain, day in keys:
        found = verify.observed(conn, mountain, day, tz)
        if found is not None:
            out[(mountain, day)] = found.snow_in
    return out


# --- fit ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RatioRow:
    """One `snow_ratio` cell, in column order."""

    mountain: str
    model: str
    temp_band: str
    upslope: str
    ratio: float
    n: int
    fitted_through: str


def fit(pairs: Iterable[tuple[LeadDay, float]], fitted_through: date) -> list[RatioRow]:
    """`snow_ratio` cells from (lead day, resort-reported snow) pairs (module docstring)."""
    acc: dict[tuple[str, str, str, str], _Acc] = defaultdict(_Acc)
    for day, observed in pairs:
        side = UPSLOPE_YES if day.upslope else UPSLOPE_NO
        for model, dw in day.models.items():
            total = dw.total_water
            if total <= 0 or max(observed, dw.snow_in) < learn.SNOW_DAY_IN:
                continue
            for mountain in (day.mountain, ALL_MOUNTAINS):
                for upslope in (side, BOTH_SIDES):
                    cell = acc[(mountain, model, ALL_BANDS, upslope)]
                    cell.snow += observed
                    cell.water[ALL_BANDS] += total
                    cell.n += 1
                    for b, w in dw.water.items():
                        if b == ALL_BANDS:
                            continue
                        cell = acc[(mountain, model, b, upslope)]
                        cell.snow += observed * w / total
                        cell.water[b] += w
                        cell.n += 1 if w >= BAND_DAY_MIN_WATER_IN else 0
    through = fitted_through.isoformat()
    out = []
    for (mountain, model, b, upslope), cell in sorted(acc.items()):
        water = cell.water[b]
        if water <= 0:
            continue
        out.append(
            RatioRow(
                mountain, model, b, upslope, round(clamp(cell.snow / water), 2), cell.n, through
            )
        )
    return out


def fit_pairs(
    days: Mapping[tuple[str, date], LeadDay],
    observed: Mapping[tuple[str, date], float],
    keep: Callable[[date], bool] = lambda _: True,
) -> list[tuple[LeadDay, float]]:
    return [
        (ld, observed[key]) for key, ld in sorted(days.items()) if key in observed and keep(ld.day)
    ]


def refit(
    conn: sqlite3.Connection, mountains: Sequence[Mountain], for_date: date, tz: str = TIMEZONE
) -> list[RatioRow]:
    """Cells from every lead-day-1 day up to `for_date` - 2 (skill-learn's rule)."""
    through = for_date - timedelta(days=2)
    keys = {m.key for m in mountains}
    days = {k: v for k, v in lead_days(conn, tz, end=through).items() if k[0] in keys}
    return fit(fit_pairs(days, observed_days(conn, days, tz)), through)


def store(conn: sqlite3.Connection, rows: Iterable[RatioRow]) -> int:
    """Replace the whole `snow_ratio` table (refitted from scratch every night)."""
    params = [
        (r.mountain, r.model, r.temp_band, r.upslope, r.ratio, r.n, r.fitted_through) for r in rows
    ]
    conn.execute("DELETE FROM snow_ratio")
    conn.executemany(
        "INSERT INTO snow_ratio (mountain, model, temp_band, upslope, ratio, n, fitted_through)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


# --- resolve -----------------------------------------------------------------------------


class Ratios:
    """Fitted cells and the fallback rules that pick one for a model-hour."""

    def __init__(self, rows: Iterable[RatioRow]) -> None:
        self.cells: dict[tuple[str, str, str, str], RatioRow] = {
            (r.mountain, r.model, r.temp_band, r.upslope): r for r in rows
        }
        self.models = {r.model for r in self.cells.values()}

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Ratios:
        return cls(
            RatioRow(*r)
            for r in conn.execute(
                "SELECT mountain, model, temp_band, upslope, ratio, n, fitted_through"
                " FROM snow_ratio"
            )
        )

    def __bool__(self) -> bool:
        return bool(self.cells)

    def model_key(self, model: str) -> str:
        return model if model in self.models else MEAN_MODEL

    def n(self, mountain: str, model: str, b: str, upslope: str) -> int:
        cell = self.cells.get((mountain, model, b, upslope))
        return 0 if cell is None else cell.n

    def splits(self, mountain: str, model: str) -> bool:
        """At least 15 snow days on each upslope side for this mountain and model."""
        key = self.model_key(model)
        return all(
            self.n(mountain, key, ALL_BANDS, s) >= MIN_DAYS for s in (UPSLOPE_YES, UPSLOPE_NO)
        )

    def chain(
        self, mountain: str, model: str, b: str, side: str | None
    ) -> list[tuple[str, str, str, str]]:
        """The cells tried, in order, for one model-hour; `side` None = no upslope split."""
        key = self.model_key(model)
        out: list[tuple[str, str, str, str]] = []
        if side is not None and self.splits(mountain, model):
            out.append((mountain, key, b, side))
        out += [
            (mountain, key, b, BOTH_SIDES),
            (ALL_MOUNTAINS, key, b, BOTH_SIDES),
            (mountain, key, ALL_BANDS, BOTH_SIDES),
            (ALL_MOUNTAINS, key, ALL_BANDS, BOTH_SIDES),
        ]
        return out

    def resolve(self, mountain: str, model: str, b: str, side: str | None) -> RatioRow | None:
        chain = self.chain(mountain, model, b, side)
        for cell_key in chain:
            cell = self.cells.get(cell_key)
            if cell is not None and cell.n >= MIN_DAYS:
                return cell
        return self.cells.get(chain[-1])

    def ratio(self, mountain: str, model: str, b: str, side: str | None) -> float | None:
        cell = self.resolve(mountain, model, b, side)
        return None if cell is None else cell.ratio

    def day_snow(self, day: LeadDay, dw: DayWater, model: str, split: bool) -> float | None:
        """One model's resort-ratio snow for a lead day: its snow water by band x ratio."""
        side = (UPSLOPE_YES if day.upslope else UPSLOPE_NO) if split else None
        total = 0.0
        for b, w in dw.water.items():
            r = self.ratio(day.mountain, model, b, side)
            if r is None:
                return None
            total += w * r
        return total

    def mean_snow(self, day: LeadDay, split: bool = True) -> float | None:
        """The mean over the models of their resort-ratio snow for one lead day."""
        values = [self.day_snow(day, dw, m, split) for m, dw in day.raw_models().items()]
        present = [v for v in values if v is not None]
        return sum(present) / len(present) if present else None


# --- finer models: day-1 candidates ------------------------------------------------------

# Short names for the finer models' methods (`<short>_ratio`).
FINE_SHORT: dict[str, str] = {
    "ecmwf_ifs": "ifs9",
    "ncep_hrrr_conus": "hrrr",
    "gem_hrdps_continental": "hrdps",
    "ncep_nbm_conus": "nbm",
    "ecmwf_aifs025_single": "aifs",
}
FINE_RATIO = "fine_ratio"  # the mean of the finer models, each times its own ratio
NBM_MODEL = "ncep_nbm_conus"
NBM_RAW = "nbm_raw"  # NBM's native snowfall, as is


def fine_method(model: str) -> str:
    """The day-1 method of one finer model times its ratio, e.g. `hrrr_ratio`."""
    return f"{FINE_SHORT[model]}_ratio"


FINE_METHODS: tuple[str, ...] = (
    *(fine_method(m) for m in open_meteo.FINE_MODELS),
    FINE_RATIO,
    NBM_RAW,
)
assert set(FINE_SHORT) == set(open_meteo.FINE_MODELS)


def fit_days(ratios: Ratios, mountain: str, model: str) -> int:
    """Snow days in a model's own all-band fit at a mountain (0 without one)."""
    return ratios.n(mountain, model, ALL_BANDS, BOTH_SIDES)


def thin_models(ratios: Ratios, mountain: str) -> list[str]:
    """Finer models with fewer than 15 fit days of their own at the mountain."""
    return [m for m in open_meteo.FINE_MODELS if fit_days(ratios, mountain, m) < MIN_DAYS]


def thin_methods(ratios: Ratios, mountain: str) -> set[str]:
    """Day-1 methods left out of the method choice at a mountain: each thin model's
    `<short>_ratio` (and `nbm_raw` with NBM's), and `fine_ratio` unless at least two
    finer models have enough fit days (a mean of one model repeats that model)."""
    thin = thin_models(ratios, mountain)
    out = {fine_method(m) for m in thin}
    if NBM_MODEL in thin:
        out.add(NBM_RAW)
    if len(open_meteo.FINE_MODELS) - len(thin) < 2:
        out.add(FINE_RATIO)
    return out


def fine_values(ld: LeadDay, ratios: Ratios) -> dict[str, float]:
    """Each finer-model candidate's day-1 snow for one lead day (methods without data absent).

    `<short>_ratio` is the model's snow water by band times its own resolved
    ratio (with the upslope split, as `resort_ratio`); `fine_ratio` the mean of
    those over the models with at least 15 fit days at the mountain (all of
    them when none has); `nbm_raw` NBM's native snowfall.
    """
    out: dict[str, float] = {}
    nbm = ld.models.get(NBM_MODEL)
    if nbm is not None:
        out[NBM_RAW] = nbm.snow_in
    if not ratios:
        return out
    thin = set(thin_models(ratios, ld.mountain))
    scaled: dict[str, float] = {}
    for model, dw in ld.fine_models().items():
        value = ratios.day_snow(ld, dw, model, split=True)
        if value is not None:
            scaled[model] = value
            out[fine_method(model)] = value
    members = [v for m, v in scaled.items() if m not in thin] or list(scaled.values())
    if members:
        out[FINE_RATIO] = sum(members) / len(members)
    return out


# --- live forecast -----------------------------------------------------------------------


@dataclass(frozen=True)
class RatioWindow:
    snow_in: float
    lo_in: float
    hi_in: float
    by_model: dict[str, float | None]


@dataclass(frozen=True)
class RatioForecast:
    """One mountain's `resort_ratio` windows and days (6 AM -> 6 AM, as derive's)."""

    windows: dict[WindowKey, RatioWindow]
    days: list[float]


def compute_mountain(
    mf: MountainForecast,
    rows: Iterable[HourlyRow],
    ratios: Ratios,
    *,
    anchor: datetime,
    models: Sequence[str],
) -> RatioForecast | None:
    start = anchor.astimezone(UTC)
    hours = [start + timedelta(hours=i) for i in range(WINDOWS["5d"])]
    s = _Series(rows, hours)
    sides = [
        UPSLOPE_YES if "nw_upslope" in d.tags else UPSLOPE_NO for d in mf.days for _ in range(24)
    ]
    scaled: dict[str, list[float | None]] = {}
    for model in (m for m in open_meteo.MODELS if m in s.models()):
        snow = s.get(model, "summit", "snowfall_in")
        temp = s.get(model, "summit", "temp_f")
        series: list[float | None] = []
        for i, value in enumerate(snow):
            if value is None:
                series.append(None)
                continue
            r = ratios.ratio(mf.key, model, band(temp[i]), sides[i] if i < len(sides) else None)
            if r is None:
                return None
            series.append(value / SNOW_PER_WATER * r)
        scaled[model] = series
    used = [m for m in models if m in scaled]
    if not used:
        return None
    mean = [_mean(values) for values in zip(*(scaled[m] for m in used), strict=True)]
    windows: dict[WindowKey, RatioWindow] = {}
    for key, length in WINDOWS.items():
        per_model: dict[str, float | None] = {
            m: sum(x for x in v[:length] if x is not None)
            if any(x is not None for x in v[:length])
            else None
            for m, v in scaled.items()
        }
        totals = [v for m, v in per_model.items() if m in used and v is not None]
        lo, hi = (min(totals), max(totals)) if totals else (0.0, 0.0)
        windows[key] = RatioWindow(
            snow_in=round(sum(x for x in mean[:length] if x is not None), 1),
            lo_in=round(lo, 1),
            hi_in=round(hi, 1),
            by_model={m: None if v is None else round(v, 1) for m, v in per_model.items()},
        )
    days = [
        round(sum(x for x in mean[24 * d : 24 * (d + 1)] if x is not None), 1)
        for d in range(len(mf.days))
    ]
    return RatioForecast(windows=windows, days=days)


def compute(
    forecast: Forecast, hourly: Iterable[HourlyRow], ratios: Ratios
) -> dict[str, RatioForecast]:
    """Mountain -> `resort_ratio` forecast, from the rows derive read; empty without ratios."""
    if not ratios:
        return {}
    by_mountain: dict[str, list[HourlyRow]] = defaultdict(list)
    for row in hourly:
        by_mountain[row.mountain].append(row)
    out: dict[str, RatioForecast] = {}
    for mf in forecast.mountains:
        found = compute_mountain(
            mf, by_mountain[mf.key], ratios, anchor=forecast.anchor, models=forecast.used_models
        )
        if found is not None:
            out[mf.key] = found
    return out


def store_rows(
    conn: sqlite3.Connection,
    run_id: int,
    forecast: Forecast,
    computed: Mapping[str, RatioForecast],
) -> int:
    """`forecast_window` rows (method `resort_ratio`) and `forecast_day.resort_ratio_in`.

    Call after `derive.store`, which writes the day rows. NWS and base values
    copy the `raw_mean` row.
    """
    params = []
    for mf in forecast.mountains:
        rf = computed.get(mf.key)
        if rf is None:
            continue
        for window, w in rf.windows.items():
            raw = mf.windows[window]
            params.append(
                (
                    run_id,
                    mf.key,
                    window,
                    METHOD,
                    w.snow_in,
                    w.lo_in,
                    w.hi_in,
                    confidence(w.lo_in, w.hi_in),
                    json.dumps(w.by_model),
                    raw.base_snow_in,
                    raw.nws_in,
                    raw.nws_ice_in,
                )
            )
        conn.executemany(
            "UPDATE forecast_day SET resort_ratio_in = ? WHERE run_id = ? AND mountain = ?"
            " AND day = ?",
            [
                (v, run_id, mf.key, d.date.isoformat())
                for d, v in zip(mf.days, rf.days, strict=False)
            ],
        )
    conn.executemany(
        "INSERT OR REPLACE INTO forecast_window (run_id, mountain, window, method, snow_in,"
        " lo_in, hi_in, confidence, by_model, base_snow_in, nws_in, nws_ice_in)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def apply(
    forecast: Forecast, computed: Mapping[str, RatioForecast], passed: Iterable[str]
) -> Forecast:
    """The forecast with `resort_ratio` as the headline, range, days, and table where passed.

    The model mean stays on each window as `raw_mean_in`; precip type, tags,
    and the other day values are unchanged.
    """
    keep = set(passed)
    mountains = []
    for mf in forecast.mountains:
        rf = computed.get(mf.key) if mf.key in keep else None
        if rf is None:
            mountains.append(mf)
            continue
        windows: dict[WindowKey, ForecastWindow] = {}
        for key, w in mf.windows.items():
            r = rf.windows.get(key)
            if r is None:
                windows[key] = w
                continue
            windows[key] = w.model_copy(
                update={
                    "snow_in": r.snow_in,
                    "range": (r.lo_in, r.hi_in),
                    "confidence": confidence(r.lo_in, r.hi_in),
                    "by_model": r.by_model,
                    "nws_disagrees": nws_disagrees(r.snow_in, w.nws_in),
                    "raw_mean_in": w.snow_in,
                }
            )
        days: list[ForecastDay] = [
            d.model_copy(update={"snow_in": rf.days[i]}) if i < len(rf.days) else d
            for i, d in enumerate(mf.days)
        ]
        mountains.append(mf.model_copy(update={"windows": windows, "days": days, "method": METHOD}))
    return forecast.model_copy(update={"mountains": mountains})


# --- backfill edition --------------------------------------------------------------------


def backfill_values(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    start: date,
    end: date,
    tz: str = TIMEZONE,
) -> dict[tuple[str, date], dict[str, float]]:
    """(mountain, day) -> {"resort_ratio": snow, each finer-model candidate: snow} for the
    `backfill` edition, `start`..`end`.

    Leave one season out: a day's ratios are fitted on every other season's
    lead-day-1 days, so a backfilled score never reads its own day.
    """
    keys = {m.key for m in mountains}
    days = {k: v for k, v in lead_days(conn, tz).items() if k[0] in keys}
    observed = observed_days(conn, days, tz)
    wanted = {k: v for k, v in days.items() if start <= k[1] <= end}
    out: dict[tuple[str, date], dict[str, float]] = {}
    for s in sorted({season(k[1]) for k in wanted}):
        pairs = fit_pairs(days, observed, lambda d, s=s: season(d) != s)  # type: ignore[misc]
        ratios = Ratios(fit(pairs, max((ld.day for ld, _ in pairs), default=start)))
        for key, ld in wanted.items():
            if season(ld.day) != s:
                continue
            value = ratios.mean_snow(ld) if ratios else None
            values = {k: round(v, 2) for k, v in fine_values(ld, ratios).items()}
            if value is not None:
                values[METHOD] = round(value, 2)
            if values:
                out[key] = values
    return out


# --- backtest ----------------------------------------------------------------------------

GLOBAL_BACKTEST_METHODS: tuple[str, ...] = (
    "raw_mean",
    "flat_10",
    "ratio_temp",
    "ratio_upslope",
    "corrected",
)
BACKTEST_METHODS: tuple[str, ...] = (*GLOBAL_BACKTEST_METHODS, *FINE_METHODS)


@dataclass(frozen=True)
class BacktestCell:
    mountain: str
    method: str
    n: int
    bias_ratio: float | None
    mae_in: float
    hit_rate: float


@dataclass(frozen=True)
class Backtest:
    fit_to: date
    test_from: date
    test_to: date
    fit_days: int
    cells: list[BacktestCell]
    corrected_fallback_days: int  # test days without corrected cells (raw mean used)
    # Finer-model candidate -> test days without its value (raw mean used).
    fine_fallback_days: dict[str, int] = field(default_factory=dict)
    # (mountain, finer model) -> snow days in its own all-band fit through `fit_to`.
    fine_fit_days: dict[tuple[str, str], int] = field(default_factory=dict)
    # Mountain -> candidates left out of the method choice (thin fit, n < 15).
    thin: dict[str, list[str]] = field(default_factory=dict)


def _corrected_cells(
    conn: sqlite3.Connection, mountains: Sequence[Mountain], fit_to: date, tz: str
) -> dict[tuple[str, str], corrected.Cell]:
    """(mountain, model) -> the 1d skill cell learned from days through `fit_to`."""
    found = learn.samples(conn, mountains, fit_to + timedelta(days=2), tz)
    rows = learn.build(s for s in found if s.lead_day == 1)
    return {
        (r.mountain, r.model): corrected.Cell(r.n, r.bias_ratio, r.mae_in)
        for r in rows
        if r.tag == learn.ALL_TAGS
        and r.lead_bucket == "1d"
        and r.n >= corrected.MIN_N
        and r.bias_ratio is not None
        and r.bias_ratio > 0
    }


def _corrected_value(
    mountain: str, models: Mapping[str, DayWater], cells: Mapping[tuple[str, str], corrected.Cell]
) -> float | None:
    """corrected.py's 1d blend: each model / its clamped bias ratio, weighted by 1 / MAE."""
    values: dict[str, float] = {}
    weights: dict[str, float] = {}
    for model, dw in models.items():
        c = cells.get((mountain, model))
        if c is None:
            continue
        values[model] = dw.snow_in / corrected._scale(c.bias_ratio)
        weights[model] = 1 / max(c.mae_in, corrected.MIN_MAE_IN)
    if not values:
        return None
    return sum(values[m] * weights[m] for m in values) / sum(weights.values())


# Backtest method -> the `forecast_window` method it reproduces (others keep their name).
LIVE_NAMES: dict[str, str] = {"ratio_upslope": METHOD}


@dataclass(frozen=True)
class HeldOutDay:
    """One held-out mountain-day: the resort report and each backtest method's forecast."""

    mountain: str
    day: date
    observed: float
    forecasts: dict[str, float]  # backtest method -> 1d forecast, rounded to 0.01


@dataclass(frozen=True)
class HeldOut:
    fit_days: int
    days: list[HeldOutDay]
    corrected_fallback_days: int
    # Finer-model candidate -> test days without its value (raw mean used).
    fine_fallback_days: dict[str, int] = field(default_factory=dict)
    # (mountain, finer model) -> snow days in its own all-band fit through `fit_to`.
    fine_fit_days: dict[tuple[str, str], int] = field(default_factory=dict)
    # Mountain -> candidates left out of the method choice (thin fit, n < 15).
    thin: dict[str, list[str]] = field(default_factory=dict)


def held_out(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    *,
    fit_to: date,
    test_from: date,
    test_to: date,
    tz: str = TIMEZONE,
) -> HeldOut:
    """Fit on lead-day-1 days through `fit_to` only; each method's forecast for the test days.

    A test day is a mountain-day `test_from`..`test_to` with a resort report
    where the report or any method's forecast is at least 0.5 inch, so every
    method is scored on the same days. `corrected` uses skill cells learned
    through `fit_to`; a day without cells takes the raw mean.
    """
    keys = {m.key for m in mountains}
    days = {k: v for k, v in lead_days(conn, tz, end=test_to).items() if k[0] in keys}
    observed = observed_days(conn, days, tz)
    pairs = fit_pairs(days, observed, lambda d: d <= fit_to)
    ratios = Ratios(fit(pairs, fit_to))
    cells = _corrected_cells(conn, mountains, fit_to, tz)
    out: list[HeldOutDay] = []
    fallback = 0
    fine_fallback: dict[str, int] = dict.fromkeys(FINE_METHODS, 0)
    for key, ld in sorted(days.items()):
        if not test_from <= ld.day <= test_to or key not in observed:
            continue
        raw = ld.raw_models()
        if not raw:
            continue
        mean = sum(dw.snow_in for dw in raw.values()) / len(raw)
        temp = ratios.mean_snow(ld, split=False)
        upslope = ratios.mean_snow(ld, split=True)
        found = _corrected_value(ld.mountain, raw, cells)
        forecasts = {
            "raw_mean": mean,
            "flat_10": mean * FLAT_RATIO / SNOW_PER_WATER,
            "ratio_temp": mean if temp is None else temp,
            "ratio_upslope": mean if upslope is None else upslope,
            "corrected": mean if found is None else found,
        }
        fine = fine_values(ld, ratios)
        forecasts.update({m: fine.get(m, mean) for m in FINE_METHODS})
        obs = observed[key]
        if max(obs, *forecasts.values()) < learn.SNOW_DAY_IN:
            continue
        fallback += 1 if found is None else 0
        for m in FINE_METHODS:
            if m not in fine:
                fine_fallback[m] += 1
        out.append(
            HeldOutDay(ld.mountain, ld.day, obs, {m: round(v, 2) for m, v in forecasts.items()})
        )
    names = sorted(keys)
    return HeldOut(
        fit_days=len({(ld.mountain, ld.day) for ld, _ in pairs}),
        days=out,
        corrected_fallback_days=fallback,
        fine_fallback_days=fine_fallback,
        fine_fit_days={
            (m, model): fit_days(ratios, m, model)
            for m in names
            for model in open_meteo.FINE_MODELS
        },
        thin={m: sorted(thin_methods(ratios, m)) for m in names},
    )


def backtest(
    conn: sqlite3.Connection,
    mountains: Sequence[Mountain],
    *,
    fit_to: date,
    test_from: date,
    test_to: date,
    tz: str = TIMEZONE,
) -> Backtest:
    """`held_out`, scored per mountain and for all: n, bias ratio, MAE, hit rate."""
    found = held_out(conn, mountains, fit_to=fit_to, test_from=test_from, test_to=test_to, tz=tz)
    keys = sorted({m.key for m in mountains})
    out: list[BacktestCell] = []
    for mountain in [*keys, ALL_MOUNTAINS]:
        for method in BACKTEST_METHODS:
            group = [
                (d.forecasts[method], d.observed)
                for d in found.days
                if mountain in (ALL_MOUNTAINS, d.mountain) and method in d.forecasts
            ]
            if not group:
                continue
            forecast_total = sum(f for f, _ in group)
            observed_total = sum(o for _, o in group)
            hits = sum(1 for f, o in group if verify.score(f, o)[2] == "HIT")
            out.append(
                BacktestCell(
                    mountain=mountain,
                    method=method,
                    n=len(group),
                    bias_ratio=round(forecast_total / observed_total, 2)
                    if observed_total > 0
                    else None,
                    mae_in=round(sum(abs(f - o) for f, o in group) / len(group), 2),
                    hit_rate=round(hits / len(group), 2),
                )
            )
    return Backtest(
        fit_to=fit_to,
        test_from=test_from,
        test_to=test_to,
        fit_days=found.fit_days,
        cells=out,
        corrected_fallback_days=found.corrected_fallback_days,
        fine_fallback_days=found.fine_fallback_days,
        fine_fit_days=found.fine_fit_days,
        thin=found.thin,
    )
