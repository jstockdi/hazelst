"""HTML pages from the report payload and narrative (report-page.md, mockup C). Pure."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from hazelst.config import TIMEZONE
from hazelst.models import (
    AccumulationDay,
    BestBet,
    ContextReport,
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
# Precip type at the summit -> (label, background, text color).
TYPE_STYLE: dict[PrecipType, tuple[str, str, str]] = {
    "snow": ("Snow", "#1a5c88", "#fff"),
    "ice": ("Ice", "#7c3aed", "#fff"),
    "rain": ("Rain", "#12806a", "#fff"),
    "mix": ("Mix", "#c4b5fd", "#132534"),
    "dry": ("—", "#eef4f8", "#5b7183"),
}
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


def issued_label(value: datetime, edition: str | None = None) -> str:
    """ "Thu 26 Nov 2026 · 7:00 AM edition" (plus the issue time when it differs)."""
    t = local(value)
    day = f"{t:%a} {t.day} {t:%b %Y}"
    label = EDITIONS.get(edition or "")
    if label is None:
        return f"{day} · {clock(value)}"
    if label == clock(value):
        return f"{day} · {label} edition"
    return f"{day} · {label} edition, issued {clock(value)}"


def run_label(model: str, issued: datetime | None) -> str:
    name = MODEL_NAMES.get(model, model)
    return name if issued is None else f"{name} {issued.astimezone(ZoneInfo('UTC')):%HZ}"


def sorted_by_5d(mountains: Sequence[MountainReport]) -> list[MountainReport]:
    return sorted(mountains, key=lambda m: -m.forecast.windows["5d"].snow_in)


def numbers_only_headline(payload: ReportPayload) -> str:
    """A factual headline for a page without a written report."""
    top = sorted_by_5d(payload.mountains)
    if not top or top[0].forecast.windows["5d"].snow_in < ACCUMULATION_MIN_IN:
        return "No significant snow in the next 5 days."
    best = top[0]
    total = best.forecast.windows["5d"].snow_in
    return f"Most snow in the next 5 days: {best.name}, {charts.num(total)}″ at the summit."


@dataclass(frozen=True)
class TypeCell:
    label: str
    background: str
    color: str
    liquid: str
    wind: bool


def type_cells(m: MountainReport) -> list[TypeCell]:
    cells = []
    for day in m.forecast.days:
        label, background, color = TYPE_STYLE[day.precip_type]
        cells.append(
            TypeCell(
                label=label,
                background=background,
                color=color,
                liquid="" if day.precip_type == "dry" else f"{day.precip_in:.2f}″",
                wind=any(t.startswith("wind_hold") for t in day.tags),
            )
        )
    return cells


def _resort_rows(payload: ReportPayload) -> list[MountainReport]:
    """Mountains whose resort reported anything (only resort numbers make the chart)."""
    return [m for m in payload.mountains if m.accumulation and m.accumulation.has_resort_data]


def accumulation_state(payload: ReportPayload) -> tuple[str, str]:
    """("chart" | "empty", empty-state sentence) for the accumulation section."""
    span = f"{payload.past_days[0]} \u2013 {payload.past_days[-1]}" if payload.past_days else ""
    with_data = _resort_rows(payload)
    if not with_data:
        return "empty", f"No resort snow reports for the last 7 days ({span})."
    recent = [sum(v or 0 for v in m.accumulation.last7_in) for m in with_data if m.accumulation]
    if max(recent) < ACCUMULATION_MIN_IN:
        return (
            "empty",
            f"No mountain reported {ACCUMULATION_MIN_IN:g}″ or more of new snow"
            f" in the last 7 days ({span}).",
        )
    return "chart", ""


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
CONTEXT_LABELS = {"nohrsc": "NOHRSC", "pns": "PNS", "lsr": "LSR", "ghcn": "GHCN"}
RESORT_LABELS = {"resort": "resort", "resort_llm": "resort page", "snocountry": "SnoCountry"}
CONTEXT_SHOWN = 3  # point reports shown per day on the page (all are in the JSON)
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


def context_label(c: ContextReport) -> str:
    """ "GHCN Stowe 2.1 SSE 3″ (720 ft, 4.2 mi)"; NOHRSC: "NOHRSC 2.1″ (pixel 2,150 ft)"."""
    name = CONTEXT_LABELS.get(c.source, c.source.upper())
    amount = f"{charts.num(c.snow_in)}\u2033"
    if c.source == "nohrsc":
        where = f" (pixel {c.elev_ft:,} ft)" if c.elev_ft is not None else ""
        return f"{name} {amount}{where}"
    detail = [f"{c.elev_ft:,} ft" if c.elev_ft is not None else "elevation unknown"]
    if c.distance_mi is not None:
        detail.append(f"{charts.num(c.distance_mi)} mi")
    return f"{name} {c.place} {amount} ({', '.join(detail)})"


def context_shown(day: AccumulationDay) -> tuple[list[str], int]:
    """Labels for the page: NOHRSC, then the highest point reports; and how many are left out."""
    grid = [c for c in day.context if c.source == "nohrsc"]
    points = sorted(
        (c for c in day.context if c.source != "nohrsc"),
        key=lambda c: (c.elev_ft is None, -(c.elev_ft or 0), c.distance_mi or 0),
    )
    shown = [*grid, *points[:CONTEXT_SHOWN]]
    return [context_label(c) for c in shown], max(len(points) - CONTEXT_SHOWN, 0)


@dataclass(frozen=True)
class AccumulationDayRow:
    label: str  # "Sat 7"
    resort: str  # "3″ resort" | "no report"
    context: list[str]
    more: int


@dataclass(frozen=True)
class AccumulationByDay:
    name: str
    summary: str  # "3″ resort-reported in 7 days · 6 days no report"
    days: list[AccumulationDayRow]


def accumulation_by_day(payload: ReportPayload) -> list[AccumulationByDay]:
    """Per mountain, each day's resort number ("no report" without one) and the context."""
    out: list[AccumulationByDay] = []
    for m in payload.mountains:
        acc = m.accumulation
        if acc is None or not acc.days:
            continue
        rows = []
        for d in acc.days:
            if d.snow_in is None:
                resort = NO_REPORT
            else:
                resort = (
                    f"{charts.num(d.snow_in)}\u2033 {RESORT_LABELS.get(d.source or '', d.source)}"
                )
            labels, more = context_shown(d)
            rows.append(AccumulationDayRow(f"{d.date:%a} {d.date.day}", resort, labels, more))
        reported = [d.snow_in for d in acc.days if d.snow_in is not None]
        missing = len(acc.days) - len(reported)
        parts = [
            f"{charts.num(round(sum(reported), 1))}\u2033 resort-reported"
            if reported
            else NO_REPORT
        ]
        if reported and missing:
            parts.append(f"{missing} {'day' if missing == 1 else 'days'} no report")
        out.append(AccumulationByDay(m.name, " · ".join(parts), rows))
    return out


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


def cautions(payload: ReportPayload) -> list[str]:
    """Numbers-only cautions: active alerts and lift-hold wind days, straight from the data."""
    lines: list[str] = []
    by_alert: dict[str, list[str]] = {}
    for m in payload.mountains:
        for event in m.alerts:
            by_alert.setdefault(event, []).append(m.name)
    for event, names in sorted(by_alert.items()):
        lines.append(f"{event}: {', '.join(names)}.")
    for m in payload.mountains:
        days = [d.label for d in m.forecast.days if "wind_hold_likely" in d.tags]
        if days:
            lines.append(f"Lift holds likely on wind at {m.name}: {', '.join(days)}.")
    return lines


# --- pages -----------------------------------------------------------------------------


def render_report(payload: ReportPayload, narrative: Narrative | None = None) -> str:
    rows = sorted_by_5d(payload.mountains)
    state, empty_text = accumulation_state(payload)
    accumulation_rows = sorted(
        _resort_rows(payload),
        key=lambda m: -((m.accumulation.season_in or 0) if m.accumulation else 0),
    )
    context: dict[str, Any] = {
        "p": payload,
        "narrative": narrative,
        "headline": narrative.headline if narrative else numbers_only_headline(payload),
        "body": md_to_html(narrative.body_md) if narrative else Markup(""),
        "issued": issued_label(payload.issued, payload.edition),
        "rows": rows,
        "types": [(m, type_cells(m)) for m in rows],
        "forecast_chart": charts.forecast_chart(rows, payload.days),
        "accumulation_state": state,
        "accumulation_empty": empty_text,
        "accumulation_chart": charts.accumulation_chart(accumulation_rows)
        if state == "chart"
        else Markup(""),
        "cautions": cautions(payload),
        "accumulation_sources": accumulation_sources(payload),
        "context_sources": context_sources(payload),
        "accumulation_days": accumulation_by_day(payload),
        "verification": verification_line(payload),
        "runs": [(run_label(r.model, r.issued_at), r.stale) for r in payload.models],
        "offices": [f"{OFFICE_NAMES.get(o, o)}" for o in payload.discussions],
        "used_names": [MODEL_NAMES.get(m, m) for m in payload.used_models],
    }
    return _env.get_template("report.html").render(**context)


@dataclass(frozen=True)
class ArchiveEntry:
    date: date
    headline: str
    status: str
    verification: str | None = None


def render_archive(entries: Sequence[ArchiveEntry]) -> str:
    return _env.get_template("archive.html").render(entries=entries)


def render_not_found(message: str) -> str:
    return _env.get_template("missing.html").render(message=message)
