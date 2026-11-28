import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from functools import cache
from pathlib import Path

import pytest

from hazelst.models import Accumulation, AccumulationDay, MountainReport, ReportPayload
from hazelst.render import charts, page
from hazelst.sources import open_meteo
from tests.pages import (
    recorded_narrative,
    recorded_payload,
    sample_narrative,
    sample_payload,
)

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
    """2026-11-25 with the sample run's narrative (the J35 layout)."""
    check_golden("2026-11-25.html", page.render_report(sample(), sample_narrative()))


def test_golden_recorded_page() -> None:
    """2026-11-26 7:00 AM edition from recorded data, with a short narrative."""
    check_golden("2026-11-26.html", page.render_report(recorded(), recorded_narrative()))


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


def test_timeline_day1_method_in_title() -> None:
    p = recorded()
    m = p.mountains[0]
    m = m.model_copy(
        update={"forecast": m.forecast.model_copy(update={"day1_method": "hrrr_ratio"})}
    )
    days = page.snow_timeline(p.model_copy(update={"mountains": [m]})).rows[0].days
    assert days[0].title.endswith("day 1: HRRR ratio")
    assert "day 1" not in days[1].title


def with_models(m: MountainReport, by_day: Sequence[dict[str, float]]) -> MountainReport:
    days = [
        d.model_copy(update={"by_model": by_day[i] if i < len(by_day) else {}})
        for i, d in enumerate(m.forecast.days)
    ]
    return m.model_copy(update={"forecast": m.forecast.model_copy(update={"days": days})})


def with_method(m: MountainReport, **update: object) -> MountainReport:
    return m.model_copy(update={"forecast": m.forecast.model_copy(update=update)})


GLOBAL = ["gfs_global", "ecmwf_ifs025", "gem_global"]


def test_timeline_past_cells_blank_zero_and_amounts() -> None:
    p = recorded()
    mountains = [
        with_past(m, p.date, [None, 0.0, 0.3]) if m.key == "loon" else m for m in p.mountains
    ]
    t = page.snow_timeline(p.model_copy(update={"mountains": mountains}))
    loon = next(r for r in t.rows if r.name == "Loon")
    # No report is blank; a reported zero, and anything under 0.5″, is a faint "0".
    assert [(c.text, c.background) for c in loon.past] == [("", ""), ("0", ""), ("0", "")]
    assert loon.past[2].title == "Wed 25 · resort reported 0.3″ (resort)"
    assert all(len(r.past) == 3 for r in t.rows)
    filled = page.snow_timeline(
        p.model_copy(
            update={
                "mountains": [
                    with_past(m, p.date, [0.6, 3.4, 12.0]) if m.key == "loon" else m
                    for m in p.mountains
                ]
            }
        )
    )
    loon = next(r for r in filled.rows if r.name == "Loon")
    assert [(c.text, c.background) for c in loon.past] == [
        (".6″", "#f3f8c9"),
        ("3″", "#7fcdbb"),
        ("12″", "#253494"),
    ]


def test_timeline_forecast_cells() -> None:
    p = recorded()
    m = with_days(p.mountains[0], [0.2, 0.1, 2.0, 7.0], ["rain", "dry", "mix", "ice"])
    m = with_models(m, [{}] * 4)
    days = page.snow_timeline(p.model_copy(update={"mountains": [m]})).rows[0].days
    assert [(c.text, c.sub, c.word) for c in days] == [
        ("", "", "rain"),
        ("0", "", ""),
        ("2″", "mix", ""),
        ("7″", "ice", ""),
    ]
    assert days[0].color == page.WET_COLORS["rain"] and not days[0].background
    assert days[3].background == "#2c7fb8" and days[3].color == "#ffffff"
    title = days[0].title
    assert title.startswith(f"{m.forecast.days[0].label} · 0.2″ snow · rain · ")


@pytest.mark.parametrize(
    ("snow", "values", "expected"),
    [
        (5.0, {"gfs_global": 8.0, "ecmwf_ifs025": 2.0, "gem_global": 6.0}, (2.0, 8.0)),
        (4.0, {"gfs_global": 3.0, "ecmwf_ifs025": 4.0, "gem_global": 4.4}, None),  # < 1.5″
        (1.0, {"gfs_global": 0.2, "ecmwf_ifs025": 1.7}, (0.2, 1.7)),  # exactly 1.5″
        (6.0, {"gfs_global": 4.5, "ecmwf_ifs025": 7.0}, None),  # 2.5″ < half of 6″
        (6.0, {"gfs_global": 3.0, "ecmwf_ifs025": 6.0}, (3.0, 6.0)),  # 3″ = half of 6″
        (5.0, {"gfs_global": 5.0}, None),  # one model is no spread
        (5.0, {"gfs_global": 5.0, "ncep_hrrr_conus": 12.0}, None),  # finer models don't count
    ],
)
def test_spread_rule(
    snow: float, values: dict[str, float], expected: tuple[float, float] | None
) -> None:
    day = (
        recorded()
        .mountains[0]
        .forecast.days[0]
        .model_copy(update={"snow_in": snow, "by_model": values})
    )
    assert page.model_spread(day, GLOBAL) == expected


def test_spread_shows_under_the_amount_and_wins_over_the_type_word() -> None:
    p = recorded()
    m = with_days(p.mountains[0], [1.3, 5.2, 4.0, 4.0], ["snow", "snow", "mix", "mix"])
    m = with_models(
        m,
        [
            {"gfs_global": 3.2, "ecmwf_ifs025": 0.0, "gem_global": 0.8},
            {"gfs_global": 7.6, "ecmwf_ifs025": 1.6, "gem_global": 6.3},
            {"gfs_global": 3.0, "ecmwf_ifs025": 8.0},
            {"gfs_global": 4.0, "ecmwf_ifs025": 4.5},
        ],
    )
    page_ = p.model_copy(update={"mountains": [m]})
    days = page.snow_timeline(page_).rows[0].days
    assert [(c.text, c.sub) for c in days] == [
        ("1″", "0\u20133"),
        ("5″", "2\u20138"),
        ("4″", "3\u20138"),
        ("4″", "mix"),
    ]
    html = page.render_report(page_, recorded_narrative())
    assert "5″<small " in html and ">2\u20138</small>" in html
    assert "2\u20138″" not in html  # no inch marks on the range


def test_drawer_rows_order_and_nws() -> None:
    p = recorded()
    m = with_models(
        p.mountains[0],
        [
            {
                "gfs_global": 3.2,
                "ecmwf_ifs025": 0.0,
                "gem_global": 0.8,
                "ncep_hrrr_conus": 3.0,
                "ecmwf_ifs": 0.3,
            },
            {"gfs_global": 7.6, "ecmwf_ifs025": 1.6, "gem_global": 6.3, "ecmwf_ifs": 2.0},
            {"gfs_global": 0.0, "ecmwf_ifs025": 0.0, "gem_global": 0.0},
            {"gfs_global": 0.0, "ecmwf_ifs025": 0.0, "gem_global": 0.0},
            {"ncep_nbm_conus": 9.0},  # day 5 is not shown, so NBM has no row
        ],
    )
    windows = dict(m.forecast.windows)
    windows["1d"] = windows["1d"].model_copy(update={"nws_in": 0.4})
    windows["3d"] = windows["3d"].model_copy(update={"nws_in": 2.6})
    m = with_method(m, windows=windows)
    row = page.snow_timeline(p.model_copy(update={"mountains": [m]})).rows[0]
    d = row.drawer
    assert [r.name for r in d.rows] == ["Hazel St", "GFS", "ECMWF", "GEM", "ECMWF 9 km", "HRRR"]
    assert d.rows[0].ours and not any(r.ours for r in d.rows[1:])
    assert d.rows[0].cells == [page.cell_text(x.snow_in) for x in m.forecast.days[:4]]
    assert d.rows[1].cells == ["3″", "8″", "0", "0"]
    assert d.rows[5].cells == ["3″", "·", "·", "·"]
    assert (d.nws_1d, d.nws_3d) == ("0", "3″ to Sat")


DRAWER_ORDER = [
    "Hazel St", "GFS", "ECMWF", "GEM", "ECMWF 9 km", "HRRR", "HRDPS", "NBM", "AIFS", "NWS"
]  # fmt: skip
EVERY_MODEL = {k: 1.0 for k in open_meteo.KNOWN_MODELS}


def drawer_labels(html: str, key: str) -> list[str]:
    body = html.split(f'<div class="drawer" id="d-{key}" hidden>')[1].split("\n      </div>")[0]
    return re.findall(r'<span class="dn">([^<]*)</span>', body)


def test_every_model_key_has_a_display_name() -> None:
    assert set(page.MODEL_NAMES) == set(open_meteo.KNOWN_MODELS)
    assert set(page.LEGACY_MODELS.values()) <= set(page.MODEL_NAMES)
    for name in page.MODEL_NAMES.values():
        assert "_" not in name and name == name.strip()


def test_drawer_labels_are_display_names_in_order() -> None:
    """Every model, given in reverse order: the drawer rows are display names, once each,
    in the fixed order Hazel St, GFS, ECMWF, GEM, ECMWF 9 km, HRRR, HRDPS, NBM, AIFS, NWS."""
    p = recorded()
    shuffled = dict(reversed(list(EVERY_MODEL.items())))
    p = p.model_copy(update={"mountains": [with_models(m, [shuffled]) for m in p.mountains]})
    html = page.render_report(p, recorded_narrative())
    names = set(page.MODEL_NAMES.values()) | {"Hazel St", "NWS"}
    for m in p.mountains:
        labels = drawer_labels(html, m.key)
        assert labels == DRAWER_ORDER
        assert all("_" not in x and x in names for x in labels)


def test_drawer_maps_pre_j34_seamless_ids_to_gfs_and_gem() -> None:
    """The live 2026-11-27 drawer printed `gem_seamless` / `gfs_seamless` after the finer
    models; payloads stored before J34 carry those ids as their GFS and GEM series."""
    m = with_models(
        recorded().mountains[0],
        [{"gem_seamless": 0.9, "gfs_seamless": 3.0, "ecmwf_ifs025": 0.0, "ncep_hrrr_conus": 3.0}],
    )
    rows = page.drawer(m, ["Fri 27", "Sat 28", "Sun 29"]).rows
    assert [(r.name, r.cells[0]) for r in rows] == [
        ("Hazel St", rows[0].cells[0]), ("GFS", "3″"), ("ECMWF", "0"), ("GEM", ".9″"),
        ("HRRR", "3″"),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "by_model",
    [{"gfs_global": 1.0, "icon_seamless": 2.0}, {"gfs_global": 1.0, "gfs_seamless": 1.0}],
)
def test_unmapped_or_doubled_model_fails_the_render(by_model: dict[str, float]) -> None:
    p = recorded()
    p = p.model_copy(update={"mountains": [with_models(p.mountains[0], [by_model])]})
    with pytest.raises(page.UnknownModelError):
        page.render_report(p, recorded_narrative())


def test_no_method_line_in_any_drawer() -> None:
    p = recorded()
    p = p.model_copy(update={"mountains": [with_models(m, [EVERY_MODEL]) for m in p.mountains]})
    html = page.render_report(p, recorded_narrative())
    assert 'class="meth"' not in html and "Hazel St uses the" not in html
    css = (Path(page.__file__).parent / "static" / "page.css").read_text()
    assert ".meth" not in css


def test_drawer_markup_is_aligned_with_the_grid() -> None:
    """Each drawer row is a subgrid of the main grid: name over columns 1-4, the now
    rule, then one cell per forecast day (NWS: 1 + 2 + 1); no repeated date header."""
    css = (Path(page.__file__).parent / "static" / "page.css").read_text()
    assert (
        ".grid{display:grid;grid-template-columns:minmax(max-content,1.4fr) repeat(3,1fr) 10px"
        " repeat(4,1fr);"
    ) in css
    assert ".drawer{grid-column:1/-1;display:grid;grid-template-columns:subgrid;" in css
    assert ".dr{grid-column:1/-1;display:grid;grid-template-columns:subgrid;" in css
    assert ".dr .dn{grid-column:1/5;" in css and ".dr .span2{grid-column:span 2;" in css
    p = recorded()
    html = page.render_report(p, recorded_narrative())
    drawers = re.findall(
        r'<div class="drawer" id="d-(\w+)" hidden>\n(.*?)\n      </div>', html, re.S
    )
    assert len(drawers) == len(p.mountains)
    for _, body in drawers:
        assert 'class="hd' not in body
        rows = re.findall(r'<div class="dr[^"]*">(.*?)</div>', body)
        assert rows[0].startswith('<span class="dn">Hazel St</span><span class="nowl"></span>')
        for r in rows[:-1]:
            assert r.count("<b>") + r.count("<span>") == 4 and r.count('class="nowl"') == 1
        assert rows[-1] == (
            '<span class="dn">NWS</span><span class="nowl"></span><span>'
            + rows[-1].split("<span>")[1].split("</span>")[0]
            + '</span><span class="span2">'
            + rows[-1].split('class="span2">')[1].split("</span>")[0]
            + "</span><span>·</span>"
        )


def test_mountain_button_opens_its_drawer() -> None:
    p = recorded()
    html = page.render_report(p, recorded_narrative())
    for m in p.mountains:
        assert (
            f'<button class="nm" type="button" aria-expanded="false" aria-controls="d-{m.key}">'
            f'{m.name}<i class="chev" aria-hidden="true">\u203a</i></button>'
        ) in html
        assert f'<div class="drawer" id="d-{m.key}" hidden>' in html
    assert 'b.setAttribute("aria-expanded"' in html and "mouseover" not in html


def test_page_order_and_no_forecast_table() -> None:
    html = page.render_report(sample(), sample_narrative())
    marks = [
        '<div class="brand">Hazel St</div><h1>',
        '<div class="grid">',
        "Best bet",
        "<h2>The storm</h2>",
        "<h2>Mountain by mountain</h2>",
        "<h2>Cautions</h2>",
        "<footer>",
    ]
    positions = [html.index(m) for m in marks]
    assert positions == sorted(positions)
    assert 'id="fct"' not in html and "<table" not in html
    assert "Resort ratio: each model" not in html and "Model mean: Open-Meteo" not in html
    assert 'class="band"' not in html and 'class="part"' not in html


def test_no_best_bet_says_so_in_the_same_card() -> None:
    # J37: a report with no day worth chasing has no best bet; the card says so.
    narrative = recorded_narrative().model_copy(update={"best_bet": None})
    html = page.render_report(recorded(), narrative)
    assert '<div class="call"><small>Best bet</small><b>No best bet today</b></div>' in html
    assert html.index("No best bet today") < html.index("<h2>The storm</h2>")


def test_footer_is_one_line() -> None:
    p = recorded().model_copy(
        update={"edition": "1440", "issued": datetime(2026, 11, 26, 22, 43, tzinfo=UTC)}
    )
    assert page.edition_label(p) == "Thu 26 Nov 2026 · 2:40 PM"
    html = page.render_report(p, recorded_narrative())
    assert (
        '<footer>Thu 26 Nov 2026 · 2:40 PM · <a href="/archive">Archive</a> ·'
        ' <a href="/api/report/2026-11-26.json">JSON</a></footer>'
    ) in html
    assert html.count("<footer>") == 1 and "Verification" not in html


def test_old_payload_without_model_values_renders() -> None:
    """Payloads stored before J35 have no `by_model` per day: no ranges, a two-row drawer."""
    p = recorded()
    p = p.model_copy(update={"mountains": [with_models(m, []) for m in p.mountains]})
    t = page.snow_timeline(p)
    assert all(c.sub in ("", "rain", "ice", "mix") for r in t.rows for c in r.days)
    assert all([x.name for x in r.drawer.rows] == ["Hazel St"] for r in t.rows)
    html = page.render_report(p, recorded_narrative())
    assert all(drawer_labels(html, m.key) == ["Hazel St", "NWS"] for m in p.mountains)
