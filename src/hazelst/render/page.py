"""HTML pages from the report payload and narrative (report-page.md, J35). Pure."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from hazelst.config import TIMEZONE
from hazelst.models import (
    BestBet,
    ForecastDay,
    MountainReport,
    Narrative,
    PrecipType,
    ReportPayload,
    VerificationSummary,
)
from hazelst.render import charts

# Drawer rows in this order (global, then finer); a model missing here shows last,
# under its Open-Meteo key.
MODEL_NAMES = {
    "gfs_global": "GFS",
    "ecmwf_ifs025": "ECMWF",
    "gem_global": "GEM",
    "ecmwf_ifs": "ECMWF 9 km",
    "ncep_hrrr_conus": "HRRR",
    "gem_hrdps_continental": "HRDPS",
    "ncep_nbm_conus": "NBM",
    "ecmwf_aifs025_single": "AIFS",
}
EDITIONS = {"0700": "7:00 AM", "0815": "8:15 AM", "1440": "2:40 PM"}
ACCUMULATION_MIN_IN = 0.5

_env = Environment(
    loader=PackageLoader("hazelst.render", "templates"),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_env.filters["num"] = charts.num


# --- narrative markdown --------------------------------------------------------------


METHOD_LABELS: dict[str, str] = {
    "raw_mean": "model mean",
    "resort_ratio": "resort ratio",
    "ifs9_ratio": "ECMWF 9 km ratio",
    "hrrr_ratio": "HRRR ratio",
    "hrdps_ratio": "HRDPS ratio",
    "nbm_ratio": "NBM ratio",
    "aifs_ratio": "AIFS ratio",
    "fine_ratio": "fine-model ratio",
    "nbm_raw": "NBM snowfall",
}


def md_to_html(md: str) -> Markup:
    """The narrative subset: `## ` headings, paragraphs, and `**bold**`; everything escaped."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", md.strip()):
        if not block.strip():
            continue
        text = html.escape(" ".join(block.split()), quote=False)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        if text.startswith("## "):
            out.append(f"<h2>{text[3:]}</h2>")
        else:
            out.append(f"<p>{text}</p>")
    return Markup("\n".join(out))


def parse_narrative(text: str) -> Narrative:
    """Front matter (`headline`, `best_bet`) and body, as in narrative.md "Output structure"."""
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not match:
        raise ValueError("narrative has no front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    fields = dict(re.findall(r'(\w+): "([^"]*)"', meta.get("best_bet", "")))
    best = BestBet(**fields) if {"mountain", "day", "why"} <= fields.keys() else None
    return Narrative(headline=meta["headline"], best_bet=best, body_md=match.group(2))


# --- view helpers ----------------------------------------------------------------------


def local(value: datetime) -> datetime:
    return value.astimezone(ZoneInfo(TIMEZONE))


def clock(value: datetime) -> str:
    t = local(value)
    return f"{t.hour % 12 or 12}:{t:%M} {t:%p}"


def edition_label(payload: ReportPayload) -> str:
    """ "Thu 26 Nov 2026 · 7:00 AM": the report date and its edition (the issue time
    when the payload has no edition)."""
    d = payload.date
    edition = EDITIONS.get(payload.edition or "") or clock(payload.issued)
    return f"{d:%a} {d.day} {d:%b %Y} · {edition}"


RESORT_LABELS = {"resort": "resort", "resort_llm": "resort page", "snocountry": "SnoCountry"}
NO_REPORT = "no report"
RESULT_WORDS = (("HIT", "HIT"), ("MISS", "MISS"), ("NO_OBS", NO_REPORT))


def verification_counts(summary: VerificationSummary) -> str:
    """ "7:00 AM 5 HIT · 2 MISS; 8:15 AM 6 HIT · 1 MISS" (model mean, next 24 h)."""
    parts = []
    for edition, counts in summary.editions.items():
        tally = " · ".join(f"{counts[key]} {word}" for key, word in RESULT_WORDS if counts.get(key))
        parts.append(f"{EDITIONS.get(edition, edition)} {tally}")
    return "; ".join(parts)


# --- snow timeline ---------------------------------------------------------------------

TIMELINE_PAST_DAYS = 3  # resort-reported days left of the now rule
TIMELINE_DAYS = 4  # forecast days right of it, from 6 AM on the report date
# Palette D, one scale for reported and forecast cells: (upper bound in inches,
# background, text color). Bins take the amount as shown; below 0.5″ a cell is
# not filled.
SNOW_BINS: tuple[tuple[float, str, str], ...] = (
    (ACCUMULATION_MIN_IN, "#ffffff", "#8a9cab"),
    (1, "#f3f8c9", "#132534"),
    (3, "#c7e9b4", "#132534"),
    (6, "#7fcdbb", "#132534"),
    (12, "#2c7fb8", "#ffffff"),
    (math.inf, "#253494", "#ffffff"),
)
# Precip-type words on unfilled forecast cells; on a filled cell the word takes its text color.
WET_COLORS: dict[PrecipType, str] = {"rain": "#0f6b57", "ice": "#6d28d9", "mix": "#6d28d9"}
# The global models disagree on a day when max - min >= max(1.5″, half the day's amount).
SPREAD_MIN_IN = 1.5
SPREAD_SHARE = 0.5


def _half_up(value: float, places: int = 0) -> float:
    scale: float = 10.0**places
    return math.floor(value * scale + 0.5 + 1e-9) / scale


def shown_snow(value: float) -> float:
    """The amount as the timeline shows it: tenths below 1″, whole inches from 1″ up.

    Tenths are taken first, so 0.97 shows as 1″ (not ".9″" or "1.0″")."""
    tenths = _half_up(value, 1)
    return _half_up(value) if tenths >= 1 else tenths


def snow_text(value: float) -> str:
    """3.4 -> "3″", 0.97 -> "1″", 0.6 -> ".6″", 0 -> "0″"."""
    shown = shown_snow(value)
    if shown >= 1:
        return f"{shown:.0f}″"
    return f".{round(shown * 10)}″" if shown > 0 else "0″"


def cell_text(value: float | None) -> str:
    """A drawer value: "·" for no value, "0" below 0.5″, else the amount as shown."""
    if value is None:
        return "·"
    return snow_text(value) if filled(value) else "0"


def filled(value: float) -> bool:
    return shown_snow(value) >= ACCUMULATION_MIN_IN


def snow_bin(value: float) -> tuple[str, str]:
    """(background, text color) for an amount, binned on the amount as shown."""
    shown = shown_snow(value)
    for top, background, color in SNOW_BINS:
        if shown < top:
            return background, color
    raise AssertionError("the last bin is unbounded")


def model_spread(day: ForecastDay, models: Iterable[str]) -> tuple[float, float] | None:
    """(min, max) of `models`' snow for the day when they disagree, else None.

    Disagree: at least two values and max - min >= max(1.5″, half the day's amount)."""
    values = [day.by_model[m] for m in models if m in day.by_model]
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    return (lo, hi) if hi - lo >= max(SPREAD_MIN_IN, SPREAD_SHARE * day.snow_in) else None


@dataclass(frozen=True)
class TimelineCell:
    kind: str  # "past" | "fut"
    text: str  # amount ("3″", ".6″"), "0" (faint), or "" (no report, or a type word only)
    sub: str  # under a filled amount: the model range ("2-8", en dash) or the type word; else ""
    word: str  # an unfilled forecast cell's type word: "rain" | "ice" | "mix" | ""
    background: str  # "" when not filled
    color: str
    title: str


@dataclass(frozen=True)
class DrawerRow:
    name: str
    cells: list[str]  # one per forecast day
    ours: bool = False


@dataclass(frozen=True)
class Drawer:
    rows: list[DrawerRow]  # Hazel St, then each model with a value on a shown day
    nws_1d: str
    nws_3d: str  # "3″ to Sun", or "·"
    method: str  # "Hazel St uses the model mean for Killington."


@dataclass(frozen=True)
class TimelineRow:
    key: str
    name: str
    past: list[TimelineCell]
    days: list[TimelineCell]
    drawer: Drawer


@dataclass(frozen=True)
class Timeline:
    past_days: list[tuple[str, str]]  # ("Wed", "25")
    days: list[tuple[str, str]]
    rows: list[TimelineRow]


def _day_parts(day: date) -> tuple[str, str]:
    return f"{day:%a}", str(day.day)


def _past_cell(day: date, snow_in: float | None, source: str | None) -> TimelineCell:
    label = f"{day:%a} {day.day}"
    if snow_in is None:
        return TimelineCell("past", "", "", "", "", "", f"{label} · no resort report")
    via = RESORT_LABELS.get(source or "", source or "")
    title = f"{label} · resort reported {charts.num(snow_in)}″" + (f" ({via})" if via else "")
    if not filled(snow_in):
        return TimelineCell("past", "0", "", "", "", "", title)
    background, color = snow_bin(snow_in)
    return TimelineCell("past", snow_text(snow_in), "", "", background, color, title)


def _forecast_cell(m: MountainReport, index: int, models: Sequence[str]) -> TimelineCell:
    day = m.forecast.days[index]
    wet = day.precip_type in WET_COLORS
    parts = [day.label, f"{charts.num(round(day.snow_in, 1))}″ snow"]
    if wet:
        parts.append(day.precip_type)
    parts.append(f"{day.precip_in:.2f}″ liquid")
    if day.hi_f is not None:
        parts.append(f"high {day.hi_f}°F")
    if day.gust_mph is not None:
        parts.append(f"gusts {day.gust_mph} mph")
    if "wind_hold_likely" in day.tags:
        parts.append("lift holds likely on wind")
    if index == 0 and m.forecast.day1_method:
        parts.append(f"day 1: {METHOD_LABELS.get(m.forecast.day1_method, m.forecast.day1_method)}")
    title = " · ".join(parts)
    if not filled(day.snow_in):
        if wet:
            return TimelineCell(
                "fut", "", "", day.precip_type, "", WET_COLORS[day.precip_type], title
            )
        return TimelineCell("fut", "0", "", "", "", "", title)
    background, color = snow_bin(day.snow_in)
    spread = model_spread(day, models)
    if spread is not None:
        sub = f"{_half_up(spread[0]):.0f}\u2013{_half_up(spread[1]):.0f}"
    else:
        sub = day.precip_type if wet else ""
    return TimelineCell("fut", snow_text(day.snow_in), sub, "", background, color, title)


def _past(m: MountainReport, days: Sequence[date]) -> list[tuple[float | None, str | None]]:
    by_date = {d.date: d for d in m.accumulation.days} if m.accumulation else {}
    return [(by_date[d].snow_in, by_date[d].source) if d in by_date else (None, None) for d in days]


def method_line(m: MountainReport) -> str:
    """ "Hazel St uses the model mean for Killington." (and day 1's finer-model method)."""
    f = m.forecast
    line = f"Hazel St uses the {METHOD_LABELS.get(f.method, f.method)} for {m.name}"
    if f.day1_method:
        line += f", the {METHOD_LABELS.get(f.day1_method, f.day1_method)} for day 1"
    return line + ("." if m.is_open else ". Closed for the season so far.")


def drawer(m: MountainReport, day_labels: Sequence[str]) -> Drawer:
    """The model drawer: Hazel St, each model with a value on a shown day, NWS."""
    days = m.forecast.days[:TIMELINE_DAYS]
    order = list(MODEL_NAMES)
    found = {k for d in days for k in d.by_model}
    models = sorted(found, key=lambda k: (order.index(k) if k in order else len(order), k))
    rows = [DrawerRow("Hazel St", [cell_text(d.snow_in) for d in days], ours=True)]
    rows += [
        DrawerRow(MODEL_NAMES.get(k, k), [cell_text(d.by_model.get(k)) for d in days])
        for k in models
    ]
    nws_1d = m.forecast.windows["1d"].nws_in if "1d" in m.forecast.windows else None
    nws_3d = m.forecast.windows["3d"].nws_in if "3d" in m.forecast.windows else None
    through = day_labels[2].split(" ")[0] if len(day_labels) > 2 else ""
    return Drawer(
        rows=rows,
        nws_1d=cell_text(nws_1d),
        nws_3d="·" if nws_3d is None else f"{cell_text(nws_3d)} to {through}",
        method=method_line(m),
    )


def snow_timeline(payload: ReportPayload) -> Timeline:
    """One row per mountain: the last 3 resort-reported days, then 4 forecast days.

    Rows sort by forecast snow over the 4 days; ties keep roster order."""
    past_dates = [payload.date - timedelta(days=n) for n in range(TIMELINE_PAST_DAYS, 0, -1)]
    ordered = sorted(
        payload.mountains,
        key=lambda m: -sum(d.snow_in for d in m.forecast.days[:TIMELINE_DAYS]),
    )
    rows = [
        TimelineRow(
            key=m.key,
            name=m.name,
            past=[
                _past_cell(d, v, s)
                for d, (v, s) in zip(past_dates, _past(m, past_dates), strict=True)
            ],
            days=[
                _forecast_cell(m, i, payload.used_models)
                for i in range(min(TIMELINE_DAYS, len(m.forecast.days)))
            ],
            drawer=drawer(m, payload.days),
        )
        for m in ordered
    ]
    return Timeline(
        past_days=[_day_parts(d) for d in past_dates],
        days=[(day.split(" ")[0], day.split(" ")[-1]) for day in payload.days[:TIMELINE_DAYS]],
        rows=rows,
    )


# --- pages -----------------------------------------------------------------------------


def render_report(payload: ReportPayload, narrative: Narrative) -> str:
    """The report page: every page carries a checked narrative (J33)."""
    context: dict[str, Any] = {
        "p": payload,
        "narrative": narrative,
        "headline": narrative.headline,
        "body": md_to_html(narrative.body_md),
        "edition": edition_label(payload),
        "timeline": snow_timeline(payload),
    }
    return _env.get_template("report.html").render(**context)


@dataclass(frozen=True)
class ArchiveEntry:
    date: date
    headline: str
    verification: str | None = None


def render_archive(entries: Sequence[ArchiveEntry]) -> str:
    return _env.get_template("archive.html").render(entries=entries)


def render_not_found(message: str) -> str:
    return _env.get_template("missing.html").render(message=message)
