"""HTML pages from the report payload and narrative (report-page.md, mockup C). Pure."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from hazelst.config import TIMEZONE
from hazelst.models import (
    BestBet,
    MountainReport,
    Narrative,
    PrecipType,
    ReportPayload,
    VerificationSummary,
)
from hazelst.render import charts

MODEL_NAMES = {"gfs_seamless": "GFS", "ecmwf_ifs025": "ECMWF", "gem_seamless": "GEM"}
OFFICE_NAMES = {"BTV": "Burlington", "ALY": "Albany", "GYX": "Gray"}
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


def run_label(model: str, issued: datetime | None) -> str:
    name = MODEL_NAMES.get(model, model)
    return name if issued is None else f"{name} {issued.astimezone(ZoneInfo('UTC')):%HZ}"


def sorted_by_5d(mountains: Sequence[MountainReport]) -> list[MountainReport]:
    return sorted(mountains, key=lambda m: -m.forecast.windows["5d"].snow_in)


ACCUMULATION_SOURCE_NAMES = {
    "resort": "resort reports",
    "resort_llm": "resort report pages read by Claude",
    "snocountry": "SnoCountry (resort-reported history)",
}
CONTEXT_SOURCE_NAMES = {
    "pns": "NWS snowfall reports",
    "lsr": "NWS local storm reports",
    "ghcn": "GHCN-Daily stations",
    "nohrsc": "NOHRSC snowfall analysis",
}
RESORT_LABELS = {"resort": "resort", "resort_llm": "resort page", "snocountry": "SnoCountry"}
NO_REPORT = "no report"
RESULT_WORDS = (("HIT", "HIT"), ("MISS", "MISS"), ("NO_OBS", NO_REPORT))


def accumulation_sources(payload: ReportPayload) -> list[str]:
    """Names of the resort-reported sources behind the accumulation section."""
    used = {s for m in payload.mountains if m.accumulation for s in m.accumulation.sources}
    return [name for key, name in ACCUMULATION_SOURCE_NAMES.items() if key in used]


def context_sources(payload: ReportPayload) -> list[str]:
    """Names of the non-resort sources shown beside the accumulation."""
    used = {
        c.source
        for m in payload.mountains
        if m.accumulation
        for d in m.accumulation.days
        for c in d.context
    }
    return [name for key, name in CONTEXT_SOURCE_NAMES.items() if key in used]


def verification_counts(summary: VerificationSummary) -> str:
    """ "7:00 AM 5 HIT · 2 MISS; 8:15 AM 6 HIT · 1 MISS" (model mean, next 24 h)."""
    parts = []
    for edition, counts in summary.editions.items():
        tally = " · ".join(f"{counts[key]} {word}" for key, word in RESULT_WORDS if counts.get(key))
        parts.append(f"{EDITIONS.get(edition, edition)} {tally}")
    return "; ".join(parts)


def verification_line(payload: ReportPayload) -> str:
    summary = payload.verification
    if summary is None:
        return "Verification: none yet."
    day = f"{summary.for_date:%a} {summary.for_date.day} {summary.for_date:%b}"
    return (
        f"Verification, {day} (next 24 h, model mean, within 2″ or 30% is a HIT):"
        f" {verification_counts(summary)}."
    )


# --- snow timeline ---------------------------------------------------------------------

TIMELINE_PAST_DAYS = 3  # resort-reported days left of the now rule
TIMELINE_DAYS = 4  # forecast days right of it, from 6 AM on the report date
# Palette D, one scale for reported and forecast cells: (upper bound in inches,
# background, text color). Bins take the amount as shown.
SNOW_BINS: tuple[tuple[float, str, str], ...] = (
    (ACCUMULATION_MIN_IN, "#ffffff", "#8a9cab"),
    (1, "#f3f8c9", "#132534"),
    (3, "#c7e9b4", "#132534"),
    (6, "#7fcdbb", "#132534"),
    (12, "#2c7fb8", "#ffffff"),
    (math.inf, "#253494", "#ffffff"),
)
# Precip-type words on forecast cells; on the two dark bins the word takes the text color.
WET_COLORS: dict[PrecipType, str] = {"rain": "#12806a", "ice": "#7c3aed", "mix": "#a21caf"}
WHITE_TEXT = "#ffffff"


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


def snow_bin(value: float) -> tuple[str, str]:
    """(background, text color) for an amount, binned on the amount as shown."""
    shown = shown_snow(value)
    for top, background, color in SNOW_BINS:
        if shown < top:
            return background, color
    raise AssertionError("the last bin is unbounded")


@dataclass(frozen=True)
class TimelineCell:
    kind: str  # "past" | "fut" | "nr" (no resort report)
    text: str  # "3″", ".6″", "0", "·", an en dash (no report), or "" for a type word only
    word: str  # "rain" | "ice" | "mix" | ""
    background: str
    color: str
    word_color: str
    title: str


@dataclass(frozen=True)
class TimelineRow:
    name: str
    past: list[TimelineCell]
    days: list[TimelineCell]


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
        return TimelineCell("nr", "\u2013", "", "", "", "", f"{label} · no resort report")
    via = RESORT_LABELS.get(source or "", source or "")
    title = f"{label} · resort reported {charts.num(snow_in)}″" + (f" ({via})" if via else "")
    background, color = snow_bin(snow_in)
    text = snow_text(snow_in) if shown_snow(snow_in) > 0 else "0"
    return TimelineCell("past", text, "", background, color, color, title)


def _forecast_cell(m: MountainReport, index: int) -> TimelineCell:
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
    background, color = snow_bin(day.snow_in)
    word = day.precip_type if wet else ""
    word_color = color if color == WHITE_TEXT else WET_COLORS.get(day.precip_type, color)
    if shown_snow(day.snow_in) < ACCUMULATION_MIN_IN:
        if wet:
            return TimelineCell("fut", "", word, background, color, word_color, title)
        return TimelineCell("fut", "·", "", background, color, color, title)
    return TimelineCell("fut", snow_text(day.snow_in), word, background, color, word_color, title)


def _past(m: MountainReport, days: Sequence[date]) -> list[tuple[float | None, str | None]]:
    by_date = {d.date: d for d in m.accumulation.days} if m.accumulation else {}
    return [(by_date[d].snow_in, by_date[d].source) if d in by_date else (None, None) for d in days]


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
            name=m.name,
            past=[
                _past_cell(d, v, s)
                for d, (v, s) in zip(past_dates, _past(m, past_dates), strict=True)
            ],
            days=[_forecast_cell(m, i) for i in range(min(TIMELINE_DAYS, len(m.forecast.days)))],
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
    rows = sorted_by_5d(payload.mountains)
    context: dict[str, Any] = {
        "p": payload,
        "narrative": narrative,
        "headline": narrative.headline,
        "body": md_to_html(narrative.body_md),
        "edition": edition_label(payload),
        "rows": rows,
        "timeline": snow_timeline(payload),
        "accumulation_sources": accumulation_sources(payload),
        "context_sources": context_sources(payload),
        "verification": verification_line(payload),
        "runs": [(run_label(r.model, r.issued_at), r.stale) for r in payload.models],
        "offices": [f"{OFFICE_NAMES.get(o, o)}" for o in payload.discussions],
        "method_labels": METHOD_LABELS,
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
