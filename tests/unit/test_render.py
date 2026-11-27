import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from functools import cache
from pathlib import Path

import pytest

from hazelst.models import Accumulation, AccumulationDay, MountainReport, ReportPayload
from hazelst.render import charts, page
from tests.pages import recorded_payload, sample_narrative, sample_payload

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
UPDATE = os.environ.get("HAZELST_UPDATE_GOLDEN") == "1"


@cache
def sample() -> ReportPayload:
    return sample_payload()


@cache
def recorded() -> ReportPayload:
    return recorded_payload()


def check_golden(name: str, html: str) -> None:
    path = GOLDEN / name
    if UPDATE:
        path.write_text(html)
    assert html == path.read_text(), f"{name} changed; rerun with HAZELST_UPDATE_GOLDEN=1"


def test_golden_sample_run_page() -> None:
    """2026-11-25 with the sample run's narrative: the mockup C layout."""
    check_golden("2026-11-25.html", page.render_report(sample(), sample_narrative()))


def test_golden_numbers_only_page() -> None:
    """2026-11-26 7:00 AM edition without a narrative: notice, cautions from alerts."""
    check_golden("2026-11-26-numbers-only.html", page.render_report(recorded()))


def test_sections_in_order() -> None:
    html = page.render_report(sample(), sample_narrative())
    marks = [
        "Best bet",
        "<h2>The storm</h2>",
        "<h2>Mountain by mountain</h2>",
        "<h2>Cautions</h2>",
        "Snow · reported and forecast",
        'class="tl"',
        "Forecast · what falls next",
        'id="fct"',
        "Accumulation · snow that has fallen",
        'class="foot"',
    ]
    positions = [html.index(m) for m in marks]
    assert positions == sorted(positions)


def test_no_third_party_requests() -> None:
    html = page.render_report(sample(), sample_narrative())
    assert "http://" not in html and "https://" not in html
    assert "fonts.googleapis" not in html


def test_narrative_markdown_is_escaped() -> None:
    out = page.md_to_html("## Cautions\n\n**Wind.** <script>alert(1)</script> & gusts")
    assert out == (
        "<h2>Cautions</h2>\n<p><b>Wind.</b> &lt;script&gt;alert(1)&lt;/script&gt; &amp; gusts</p>"
    )


def test_parse_narrative() -> None:
    n = sample_narrative()
    assert n.headline.startswith("Ski the cold, quiet Thanksgiving morning")
    assert n.best_bet is not None and n.best_bet.mountain == "Sunday River"
    assert n.body_md.lstrip().startswith("## The storm")


def test_forecast_table_sorted_by_5d_and_hides_zero_ranges() -> None:
    p = recorded()
    html = page.render_report(p)
    table = html[html.index('id="fct"') :]
    order = [m.name for m in page.sorted_by_5d(p.mountains)]
    assert [table.index(f'class="name">{n}') for n in order] == sorted(
        table.index(f'class="name">{n}') for n in order
    )
    zero = [m for m in p.mountains if m.forecast.windows["1d"].range == (0.0, 0.0)]
    assert zero, "fixture day should have a zero-width range"
    assert "<small>0\N{EN DASH}0</small>" not in table


def test_edition_label_in_footer_not_band() -> None:
    p = recorded()
    assert page.edition_label(p) == "Thu 26 Nov 2026 · 7:00 AM"
    later = p.model_copy(
        update={"edition": "1440", "issued": datetime(2026, 11, 26, 22, 43, tzinfo=UTC)}
    )
    assert page.edition_label(later) == "Thu 26 Nov 2026 · 2:40 PM"  # no issue time
    assert page.edition_label(p.model_copy(update={"edition": None})) == "Thu 26 Nov 2026 · 7:00 AM"
    html = page.render_report(later)
    assert 'class="when"' not in html and "numbers only" not in html
    assert '<p>Thu 26 Nov 2026 · 2:40 PM</p>\n    <p><a href="/archive">Archive</a>' in html


def with_accumulation(p: ReportPayload, last7: list[float | None]) -> ReportPayload:
    mountains = [
        m.model_copy(
            update={
                "accumulation": Accumulation(
                    source="resort", last7_in=last7, season_in=12, base_in=18
                )
            }
        )
        for m in p.mountains
    ]
    return p.model_copy(update={"mountains": mountains})


def test_accumulation_empty_states() -> None:
    p = recorded()
    assert page.accumulation_state(p)[0] == "empty"
    assert "Thu 19 \N{EN DASH} Wed 25" in page.accumulation_state(p)[1]
    quiet = with_accumulation(p, [0, 0.2, 0, 0, 0, 0.1, 0])
    state, text = page.accumulation_state(quiet)
    assert state == "empty" and text.startswith("No mountain reported 0.5″")
    snowy = with_accumulation(p, [0, 2, 3, 1, 0, 0, 2])
    assert page.accumulation_state(snowy)[0] == "chart"
    html = page.render_report(snowy)
    assert "Observed season snowfall" in html and "18″ base" in html


def test_closed_mountain_listed() -> None:
    p = recorded()
    closed = p.model_copy(
        update={
            "mountains": [m.model_copy(update={"is_open": m.key != "pico"}) for m in p.mountains]
        }
    )
    html = page.render_report(closed)
    assert "Pico<small>model mean</small><small>closed</small>" in html
    assert '<div role="rowheader" class="tl-n">Pico<small>closed</small></div>' in html


@pytest.mark.parametrize(
    ("value", "text"), [(0.0, "0"), (2.5, "2.5"), (12.0, "12"), (None, "\N{EN DASH}")]
)
def test_num(value: float | None, text: str) -> None:
    assert charts.num(value) == text


# --- snow timeline ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.0, "0\u2033"),
        (0.04, "0\u2033"),
        (0.6, ".6\u2033"),
        (0.94, ".9\u2033"),
        (0.97, "1\u2033"),  # tenths first: 1.0, so whole inches
        (1.49, "1\u2033"),
        (1.5, "2\u2033"),
        (2.5, "3\u2033"),  # half up, not to even
        (12.4, "12\u2033"),
    ],
)
def test_snow_text_rounding(value: float, text: str) -> None:
    assert page.snow_text(value) == text


@pytest.mark.parametrize(
    ("value", "background", "color"),
    [
        (0.0, "#ffffff", "#8a9cab"),
        (0.44, "#ffffff", "#8a9cab"),
        (0.45, "#f3f8c9", "#132534"),  # shows .5″
        (0.9, "#f3f8c9", "#132534"),
        (0.97, "#c7e9b4", "#132534"),  # shows 1″
        (2.4, "#c7e9b4", "#132534"),
        (2.6, "#7fcdbb", "#132534"),  # shows 3″
        (5.6, "#2c7fb8", "#ffffff"),  # shows 6″
        (11.4, "#2c7fb8", "#ffffff"),
        (11.6, "#253494", "#ffffff"),  # shows 12″
        (40.0, "#253494", "#ffffff"),
    ],
)
def test_snow_bin_palette_d(value: float, background: str, color: str) -> None:
    assert page.snow_bin(value) == (background, color)


def with_days(
    m: MountainReport, snow: Sequence[float], types: list[str] | None = None
) -> MountainReport:
    days = [
        d.model_copy(
            update={"snow_in": v, "precip_type": (types or [d.precip_type] * len(snow))[i]}
        )
        for i, (d, v) in enumerate(zip(m.forecast.days, snow, strict=False))
    ]
    days += m.forecast.days[len(snow) :]
    return m.model_copy(update={"forecast": m.forecast.model_copy(update={"days": days})})


def with_past(m: MountainReport, day: date_t, values: list[float | None]) -> MountainReport:
    days = [
        AccumulationDay(date=day - timedelta(days=len(values) - i), snow_in=v, source="resort")
        for i, v in enumerate(values)
    ]
    acc = Accumulation(source="resort", last7_in=[None] * 4 + values, days=days)
    return m.model_copy(update={"accumulation": acc})


def test_timeline_sorted_by_4_day_forecast_ties_in_roster_order() -> None:
    p = recorded()
    snow = {"killington": [0, 0, 0, 0, 9], "loon": [1, 1, 1, 1], "pico": [2, 0, 0, 2]}
    mountains = [with_days(m, snow.get(m.key, [0, 0, 0, 0])) for m in p.mountains]
    t = page.snow_timeline(p.model_copy(update={"mountains": mountains}))
    roster = [m.name for m in p.mountains]
    # Loon and Pico tie at 4″; the fifth day does not count; zeros keep roster order.
    first, second = (
        ("Pico", "Loon") if roster.index("Pico") < roster.index("Loon") else ("Loon", "Pico")
    )
    rest = [n for n in roster if n not in ("Pico", "Loon")]
    assert [r.name for r in t.rows] == [first, second, *rest]
    assert len(t.days) == 4 and t.days[0] == ("Thu", "26")
    assert t.past_days == [("Mon", "23"), ("Tue", "24"), ("Wed", "25")]


def test_timeline_past_always_shown_with_no_report_and_zero() -> None:
    p = recorded()
    mountains = [
        with_past(m, p.date, [None, 0.0, 0.3]) if m.key == "loon" else m for m in p.mountains
    ]
    t = page.snow_timeline(p.model_copy(update={"mountains": mountains}))
    loon = next(r for r in t.rows if r.name == "Loon")
    assert [(c.kind, c.text) for c in loon.past] == [
        ("nr", "\u2013"),
        ("past", "0"),
        ("past", ".3\u2033"),
    ]
    assert loon.past[2].title == "Wed 25 · resort reported 0.3\u2033 (resort)"
    # A mountain without any accumulation is all "no report", and no row collapses.
    assert all(len(r.past) == 3 for r in t.rows)
    other = next(r for r in t.rows if r.name == "Killington")
    assert {c.kind for c in other.past} == {"nr"}
    html = page.render_report(p.model_copy(update={"mountains": mountains}))
    assert 'class="c nr"' in html and "No resort reported snow" not in html


def test_timeline_forecast_cells() -> None:
    p = recorded()
    m = with_days(p.mountains[0], [0.2, 0.1, 2.0, 7.0], ["rain", "dry", "mix", "ice"])
    days = page.snow_timeline(p.model_copy(update={"mountains": [m]})).rows[0].days
    assert [(c.text, c.word) for c in days] == [
        ("", "rain"),
        ("\u00b7", ""),
        ("2\u2033", "mix"),
        ("7\u2033", "ice"),
    ]
    assert all(c.kind == "fut" for c in days)
    assert days[0].word_color == page.WET_COLORS["rain"]
    assert days[2].word_color == page.WET_COLORS["mix"]
    assert days[3].word_color == "#ffffff"  # dark bin: the word takes the text color
    title = days[0].title
    assert title.startswith(f"{m.forecast.days[0].label} · 0.2\u2033 snow · rain · ")
    assert "\u2033 liquid" in title
    if m.forecast.days[0].hi_f is not None:
        assert f"high {m.forecast.days[0].hi_f}\u00b0F" in title


def test_timeline_day1_method_in_title() -> None:
    p = recorded()
    m = p.mountains[0]
    m = m.model_copy(
        update={"forecast": m.forecast.model_copy(update={"day1_method": "hrrr_ratio"})}
    )
    days = page.snow_timeline(p.model_copy(update={"mountains": [m]})).rows[0].days
    assert days[0].title.endswith("day 1: HRRR ratio")
    assert "day 1" not in days[1].title


def test_now_rule_is_plain_with_vertical_header() -> None:
    html = page.render_report(recorded())
    assert '<div role="columnheader" class="tl-nowh"><span>Now</span></div>' in html
    rule = html[html.index('<div class="tl-now"') :]
    assert rule.startswith('<div class="tl-now" style="grid-row:2 / span 8"></div>')


def test_timeline_markup_fits_390px() -> None:
    """Fixed widths are only the 70 px names and the 3 px rule; day columns shrink to fit."""
    css = (Path(page.__file__).parent / "static" / "page.css").read_text()
    assert (
        ".tl{display:grid;grid-template-columns:70px repeat(3,minmax(0,1fr)) 3px"
        " repeat(4,minmax(0,1fr));gap:3px;"
    ) in css
    assert "min-width:0;min-height:36px;" in css
    p = recorded()
    html = page.render_report(p)
    grid = html[
        html.index('<div class="tl"') : html.index("</figure>", html.index('<div class="tl"'))
    ]
    assert "width" not in grid and "nowrap" not in grid
    cells = grid.count('role="cell"')
    assert cells == 7 * len(p.mountains) and grid.count(" title=") == cells
    assert f'style="grid-row:2 / span {len(p.mountains)}"' in grid
    # Removed: the stacked chart, the type grid, and the by-day details.
    assert 'class="types"' not in html and "stacked by day" not in html
    assert "<h2>By day</h2>" not in html and "<details" not in html


def test_section_labels_lead_straight_into_grid_and_table() -> None:
    html = page.render_report(recorded())
    timeline = html[
        html.index("Snow · reported and forecast</div>") : html.index('<div class="tl"')
    ]
    table = html[html.index("Forecast · what falls next</div>") : html.index('<table id="fct">')]
    for part in (timeline, table):
        assert "<h2>" not in part and 'class="lead"' not in part
