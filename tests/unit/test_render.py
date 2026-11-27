import os
from functools import cache
from pathlib import Path

import pytest

from hazelst.models import Accumulation, ReportPayload
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
        "Forecast · what falls next",
        'class="types"',
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


def test_type_grid_colors_and_wind_mark() -> None:
    p = recorded()
    html = page.render_report(p)
    for t, (label, bg, _) in page.TYPE_STYLE.items():
        if any(d.precip_type == t for m in p.mountains for d in m.forecast.days):
            assert f"background:{bg}" in html, label
    windy = any(
        t.startswith("wind_hold") for m in p.mountains for d in m.forecast.days for t in d.tags
    )
    assert ("💨</i>" in html) == windy


def test_issued_label() -> None:
    p = recorded()
    assert page.issued_label(p.issued, "0700") == "Thu 26 Nov 2026 · 7:00 AM edition"
    assert (
        page.issued_label(p.issued, "0815") == "Thu 26 Nov 2026 · 8:15 AM edition, issued 7:00 AM"
    )
    assert page.issued_label(p.issued) == "Thu 26 Nov 2026 · 7:00 AM"


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
    assert "Pico <em>closed</em>" in html


@pytest.mark.parametrize(
    ("value", "text"), [(0.0, "0"), (2.5, "2.5"), (12.0, "12"), (None, "\N{EN DASH}")]
)
def test_num(value: float | None, text: str) -> None:
    assert charts.num(value) == text


def test_forecast_chart_axis_at_least_6_inches() -> None:
    svg = charts.forecast_chart(recorded().mountains, recorded().days)
    assert ">6″</text>" in svg
    assert svg.startswith('<svg viewBox="0 0 340 210"')
