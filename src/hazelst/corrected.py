"""The `corrected` forecast method.

data-model.md, "How the data compounds", step 4:

- **Correction.** A `skill` cell (mountain x model x lead bucket, tag '' =
  every day) qualifies once it has n >= 10 verified snow days and a bias ratio.
  Each window is rebuilt from its segments (`1d` = day 1, `2-3d` = days 2-3,
  `4-5d` = days 4-5, taken as differences of the per-model window totals).
  In each segment, every model in use with a qualifying cell is divided by
  its bias ratio (clamped to 0.25-4) and the models are blended with weights
  proportional to 1 / MAE. A window gets a `corrected` row only when every
  segment it spans has at least one qualifying model; models without a cell
  are left out of the blend.
- **Where it shows.** `report-build` stores `corrected` rows whenever they
  exist, and `forecast-verify` scores them beside `raw_mean`. The page shows
  the corrected value beneath the headline only for a mountain where
  `method_gate` chose it (`method.py`, decisions row 28).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from hazelst.derive import confidence
from hazelst.learn import ALL_TAGS
from hazelst.models import Forecast, MountainForecast, WindowKey

METHOD = "corrected"
MIN_N = 10
SCALE_LIMITS = (0.25, 4.0)
MIN_MAE_IN = 0.1  # floor for the 1 / MAE weight
SEGMENTS: dict[WindowKey, tuple[str, ...]] = {
    "1d": ("1d",),
    "3d": ("1d", "2-3d"),
    "5d": ("1d", "2-3d", "4-5d"),
}
# Segment -> (window that ends it, window before it).
SEGMENT_SPAN: dict[str, tuple[WindowKey, WindowKey | None]] = {
    "1d": ("1d", None),
    "2-3d": ("3d", "1d"),
    "4-5d": ("5d", "3d"),
}


@dataclass(frozen=True)
class Cell:
    """A qualifying `skill` cell."""

    n: int
    bias_ratio: float
    mae_in: float


Cells = Mapping[tuple[str, str, str], Cell]  # (mountain, model, lead bucket) -> cell


@dataclass(frozen=True)
class CorrectedWindow:
    snow_in: float
    lo_in: float
    hi_in: float
    by_model: dict[str, float | None]  # each model's corrected window total, where it has cells


# --- skill cells -----------------------------------------------------------------------


def load_cells(conn: sqlite3.Connection, min_n: int = MIN_N) -> dict[tuple[str, str, str], Cell]:
    """Qualifying all-day cells: n >= `min_n` and a positive bias ratio."""
    return {
        (mountain, model, bucket): Cell(int(n), float(bias), float(mae))
        for mountain, model, bucket, n, bias, mae in conn.execute(
            "SELECT mountain, model, lead_bucket, n, bias_ratio, mae_in FROM skill"
            " WHERE tag = ? AND n >= ? AND bias_ratio > 0 AND mae_in IS NOT NULL",
            (ALL_TAGS, min_n),
        )
    }


# --- correction -----------------------------------------------------------------------


def _scale(bias_ratio: float) -> float:
    lo, hi = SCALE_LIMITS
    return min(max(bias_ratio, lo), hi)


def segment_values(mf: MountainForecast, model: str) -> dict[str, float | None]:
    """A model's snow in each segment, from its window totals."""
    out: dict[str, float | None] = {}
    for segment, (window, before) in SEGMENT_SPAN.items():
        total = mf.windows[window].by_model.get(model) if window in mf.windows else None
        prior = mf.windows[before].by_model.get(model) if before and before in mf.windows else 0.0
        out[segment] = None if total is None or prior is None else max(total - prior, 0.0)
    return out


def correct_mountain(
    mf: MountainForecast, models: Sequence[str], cells: Cells
) -> dict[WindowKey, CorrectedWindow]:
    """Corrected windows for one mountain; a window missing a segment's cells is left out."""
    values = {m: segment_values(mf, m) for m in models}
    corrected: dict[str, dict[str, float]] = {}  # segment -> model -> corrected value
    blended: dict[str, float] = {}
    for segment in SEGMENT_SPAN:
        per_model: dict[str, float] = {}
        weights: dict[str, float] = {}
        for model in models:
            c = cells.get((mf.key, model, segment))
            value = values[model][segment]
            if c is None or value is None:
                continue
            per_model[model] = value / _scale(c.bias_ratio)
            weights[model] = 1 / max(c.mae_in, MIN_MAE_IN)
        if per_model:
            corrected[segment] = per_model
            blended[segment] = sum(per_model[m] * weights[m] for m in per_model) / sum(
                weights.values()
            )
    out: dict[WindowKey, CorrectedWindow] = {}
    for window, segments in SEGMENTS.items():
        if window not in mf.windows or any(s not in blended for s in segments):
            continue
        by_model: dict[str, float | None] = {}
        for model in models:
            parts = [corrected[s].get(model) for s in segments]
            by_model[model] = (
                None if any(p is None for p in parts) else round(sum(p or 0.0 for p in parts), 1)
            )
        totals = [v for v in by_model.values() if v is not None]
        raw = mf.windows[window]
        lo, hi = (min(totals), max(totals)) if totals else raw.range
        out[window] = CorrectedWindow(
            snow_in=round(sum(blended[s] for s in segments), 1),
            lo_in=round(lo, 1),
            hi_in=round(hi, 1),
            by_model=by_model,
        )
    return out


def correct(forecast: Forecast, cells: Cells) -> dict[str, dict[WindowKey, CorrectedWindow]]:
    """Mountain -> corrected windows, using the models the mean used (stale ones left out)."""
    out: dict[str, dict[WindowKey, CorrectedWindow]] = {}
    for mf in forecast.mountains:
        windows = correct_mountain(mf, forecast.used_models, cells)
        if windows:
            out[mf.key] = windows
    return out


def store(
    conn: sqlite3.Connection,
    run_id: int,
    forecast: Forecast,
    corrected: Mapping[str, Mapping[WindowKey, CorrectedWindow]],
) -> int:
    """`forecast_window` rows with method `corrected`; NWS and base values copy the raw row."""
    params = []
    for mf in forecast.mountains:
        for window, c in corrected.get(mf.key, {}).items():
            raw = mf.windows[window]
            params.append(
                (
                    run_id,
                    mf.key,
                    window,
                    METHOD,
                    c.snow_in,
                    c.lo_in,
                    c.hi_in,
                    confidence(c.lo_in, c.hi_in),
                    json.dumps(c.by_model),
                    raw.base_snow_in,
                    raw.nws_in,
                    raw.nws_ice_in,
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO forecast_window (run_id, mountain, window, method, snow_in,"
        " lo_in, hi_in, confidence, by_model, base_snow_in, nws_in, nws_ice_in)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def apply(
    forecast: Forecast,
    corrected: Mapping[str, Mapping[WindowKey, CorrectedWindow]],
    passed: Iterable[str],
) -> Forecast:
    """The forecast with `corrected_in` set on the windows of mountains that passed the gate."""
    keep = set(passed)
    mountains = []
    for mf in forecast.mountains:
        windows = corrected.get(mf.key, {}) if mf.key in keep else {}
        if not windows:
            mountains.append(mf)
            continue
        updated = {
            key: w.model_copy(update={"corrected_in": windows[key].snow_in})
            if key in windows
            else w
            for key, w in mf.windows.items()
        }
        mountains.append(mf.model_copy(update={"windows": updated}))
    return forecast.model_copy(update={"mountains": mountains})
