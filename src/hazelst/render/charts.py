"""Number formatting shared by the page templates."""

from __future__ import annotations


def num(value: float | None) -> str:
    """0.0 -> "0", 2.50 -> "2.5"; None -> an en dash."""
    return "\u2013" if value is None else f"{value:g}"
