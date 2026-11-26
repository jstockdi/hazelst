"""Inline SVG charts with fixed viewBoxes (ported from prototype/render.py and mockup C)."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from markupsafe import Markup

from hazelst.models import MountainReport

DAY_SHADES = ("#0f3a5a", "#1a5c88", "#2f86b4", "#7fb7d6", "#12806a")
EARLIER = "#cfe2ef"
RECENT = "#1a5c88"
GRID = "#eef4f8"
MIST = "#5b7183"
INK = "#132534"
DEEP = "#0f3a5a"


def num(value: float | None) -> str:
    """0.0 -> "0", 2.50 -> "2.5"; None -> an en dash."""
    return "\u2013" if value is None else f"{value:g}"


def forecast_chart(mountains: Sequence[MountainReport], days: Sequence[str]) -> Markup:
    """New snow per mountain, stacked by day; y-axis at least 6 inches."""
    width, height, left, bottom, top = 340, 210, 26, 66, 12
    bar = (width - left) / max(len(mountains), 1)
    peak = max([6.0, *(sum(d.snow_in for d in m.forecast.days) for m in mountains)])
    scale = (height - bottom - top) / peak
    step = 2 if peak <= 8 else 5 if peak <= 25 else 10
    out: list[str] = []
    for value in range(0, int(peak) + 1, step):
        y = height - bottom - value * scale
        out.append(
            f'<line x1="{left}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
            f'<text x="{left - 4}" y="{y + 3:.1f}" font-size="9" text-anchor="end"'
            f' fill="{MIST}">{value}″</text>'
        )
    for i, m in enumerate(mountains):
        x, y = left + i * bar + bar * 0.18, float(height - bottom)
        for d, day in enumerate(m.forecast.days):
            h = day.snow_in * scale
            y -= h
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar * 0.64:.1f}" height="{h:.1f}"'
                f' fill="{DAY_SHADES[d % len(DAY_SHADES)]}"/>'
            )
        total = m.forecast.windows["5d"].snow_in
        out.append(
            f'<text x="{x + bar * 0.32:.1f}" y="{y - 3:.1f}" font-size="9" text-anchor="middle"'
            f' font-weight="600" fill="{DEEP}">{num(total)}</text>'
            f'<text transform="translate({x + bar * 0.32 + 3:.1f},{height - bottom + 8})'
            f' rotate(-40)" font-size="9" text-anchor="end" fill="{INK}">{escape(m.name)}</text>'
        )
    for j, label in enumerate(days):
        out.append(
            f'<rect x="{left + j * 62}" y="{height - 12}" width="8" height="8"'
            f' fill="{DAY_SHADES[j % len(DAY_SHADES)]}"/>'
            f'<text x="{left + j * 62 + 11}" y="{height - 5}" font-size="9"'
            f' fill="{MIST}">{escape(label)}</text>'
        )
    return Markup(
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img"'
        f' aria-label="Forecast new snow by mountain, next five days">{"".join(out)}</svg>'
    )


def accumulation_chart(mountains: Sequence[MountainReport]) -> Markup:
    """Season total per mountain with the last 7 days highlighted; base depth at right."""
    rows = [m for m in mountains if m.accumulation is not None]
    width, left, right, row_h, top = 340, 84, 92, 26, 6

    def season(m: MountainReport) -> float:
        acc = m.accumulation
        assert acc is not None
        last7 = sum(v or 0 for v in acc.last7_in)
        return max(acc.season_in or 0, last7)

    peak = max([1.0, *(season(m) for m in rows)])
    scale = (width - left - right) / peak
    out: list[str] = []
    for i, m in enumerate(rows):
        acc = m.accumulation
        assert acc is not None
        total = season(m)
        last7 = sum(v or 0 for v in acc.last7_in)
        y = top + i * row_h
        if not m.is_open:
            base = "closed"
        elif acc.base_in is None:
            base = ""
        else:
            base = f"{num(acc.base_in)}″ base"
        out.append(
            f'<text x="{left - 8}" y="{y + 14}" font-size="11" text-anchor="end"'
            f' fill="{INK}">{escape(m.name)}</text>'
            f'<rect x="{left}" y="{y + 4}" width="{(total - last7) * scale:.1f}" height="14"'
            f' rx="2" fill="{EARLIER}"/>'
            f'<rect x="{left + (total - last7) * scale:.1f}" y="{y + 4}"'
            f' width="{last7 * scale:.1f}" height="14" rx="2" fill="{RECENT}"/>'
            f'<text x="{left + total * scale + 4:.1f}" y="{y + 15}" font-size="10"'
            f' font-weight="600" fill="{DEEP}">{num(round(total, 1))}″</text>'
            f'<text x="{width}" y="{y + 15}" font-size="10" text-anchor="end"'
            f' fill="{MIST}">{escape(base)}</text>'
        )
    legend_y = top + len(rows) * row_h + 10
    out.append(
        f'<rect x="{left}" y="{legend_y}" width="10" height="8" fill="{EARLIER}"/>'
        f'<text x="{left + 14}" y="{legend_y + 8}" font-size="9" fill="{MIST}">'
        "Earlier this season</text>"
        f'<rect x="{left + 110}" y="{legend_y}" width="10" height="8" fill="{RECENT}"/>'
        f'<text x="{left + 124}" y="{legend_y + 8}" font-size="9" fill="{MIST}">'
        "Last 7 days</text>"
    )
    return Markup(
        f'<svg viewBox="0 0 {width} {legend_y + 14}" width="100%" role="img"'
        f' aria-label="Observed season snowfall by mountain">{"".join(out)}</svg>'
    )
