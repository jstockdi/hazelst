"""Inline SVG charts with fixed viewBoxes (ported from prototype/render.py and mockup C)."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from markupsafe import Markup

from hazelst.models import MountainReport

EARLIER = "#cfe2ef"
RECENT = "#1a5c88"
MIST = "#5b7183"
INK = "#132534"
DEEP = "#0f3a5a"


def num(value: float | None) -> str:
    """0.0 -> "0", 2.50 -> "2.5"; None -> an en dash."""
    return "\u2013" if value is None else f"{value:g}"


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
