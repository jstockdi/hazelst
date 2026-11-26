"""Contract tests: every resort adapter on the pages saved in J00.

A failure here means a preset no longer matches its saved page. The live
counterpart is the `resort_adapter_failed` log line from `ingest-obs`.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from typing import Any

import pytest

from hazelst import config
from hazelst.sources.resorts import adapter
from tests.support import FIXTURES

RESORTS = FIXTURES / "resorts"


def body(path: str) -> bytes:
    return gzip.decompress((RESORTS / path).read_bytes())


def parse(preset: str, path: str) -> adapter.Report:
    return adapter.parse(adapter.preset(preset), body(path))


def mountain(key: str) -> config.Mountain:
    return next(m for m in config.load_mountains(config.DEFAULT_MOUNTAINS) if m.key == key)


EXPECTED: dict[str, tuple[str, str, datetime, dict[str, Any]]] = {
    "killington": (
        "dor_html",
        "killington/report.html.gz",
        datetime(2026, 11, 15, 15, 44, tzinfo=UTC),  # 10:44 AM EST
        {
            "new_24h_in": 0.0,
            "new_48h_in": 0.0,
            "new_7d_in": 0.0,
            "base_in": None,
            "season_in": 5.0,
            "trails_open": 3,
            "trails_total": 155,
            "lifts_open": 2,
            "lifts_total": 19,
            "lifts_hold": None,
        },
    ),
    "pico": (
        "dor_html",
        "pico/report.html.gz",
        datetime(2026, 11, 15, 15, 44, tzinfo=UTC),
        {
            "new_24h_in": 0.0,
            "new_48h_in": 0.0,
            "new_7d_in": 0.0,
            "base_in": None,
            "season_in": 5.0,
            "trails_open": 0,
            "trails_total": 58,
            "lifts_open": 0,
            "lifts_total": 7,
            "lifts_hold": None,
        },
    ),
    "sugarbush": (
        "mtnpowder",
        "sugarbush/snow_api.json.gz",
        datetime(2026, 11, 25, 18, 2, 17, tzinfo=UTC),  # 13:02:17-0500
        {
            "new_24h_in": 0.0,
            "new_48h_in": 0.0,
            "new_7d_in": None,  # "--"
            "base_in": 0.0,  # SnowBaseRangeIn "--", falls back to BaseArea.BaseIn
            "season_in": 7.0,
            "trails_open": 0,
            "trails_total": 111,
            "lifts_open": 0,
            "lifts_total": 16,
            "lifts_hold": None,
        },
    ),
    "stratton": (
        "mtnpowder",
        "stratton/snow_api.json.gz",
        datetime(2026, 11, 25, 21, 4, 24, tzinfo=UTC),
        {
            "new_24h_in": 0.0,
            "new_48h_in": 0.0,
            "new_7d_in": 0.0,
            "base_in": 0.0,
            "season_in": 6.0,
            "trails_open": 0,
            "trails_total": 99,
            "lifts_open": 0,
            "lifts_total": 14,
            "lifts_hold": None,
        },
    ),
    "loon": (
        "boyne_reportpal",
        "loon/reportpal.json.gz",
        datetime(2026, 11, 25, 16, 27, 41, tzinfo=UTC),
        {
            "new_24h_in": 0.0,
            "new_48h_in": None,  # snow48Hours is null
            "new_7d_in": 0.0,
            "base_in": 0.0,
            "season_in": 0.0,
            "trails_open": 0,  # resortwide counts alpine only (8 Nordic trails open)
            "trails_total": 73,
            "lifts_open": 0,
            "lifts_total": 13,
            "lifts_hold": 0,
        },
    ),
    "sunday_river": (
        "boyne_reportpal",
        "sunday_river/reportpal.json.gz",
        datetime(2026, 11, 26, 14, 13, 39, tzinfo=UTC),
        {
            "new_24h_in": 0.0,
            "new_48h_in": 0.0,
            "new_7d_in": 0.0,
            "base_in": 0.0,
            "season_in": 0.0,
            "trails_open": 3,
            "trails_total": 144,
            "lifts_open": 1,
            "lifts_total": 19,
            "lifts_hold": 0,
        },
    ),
    "sugarloaf": (
        "boyne_reportpal",
        "sugarloaf/reportpal.json.gz",
        datetime(2026, 11, 25, 19, 48, 53, tzinfo=UTC),
        {
            "new_24h_in": 0.0,
            "new_48h_in": 0.0,
            "new_7d_in": 0.0,
            "base_in": 0.0,
            "season_in": 0.0,
            "trails_open": 0,
            "trails_total": 176,
            "lifts_open": 0,
            "lifts_total": 15,
            "lifts_hold": 0,
        },
    ),
    "cranmore": (
        "cranmore_html",
        "cranmore/snow-report.html.gz",
        datetime(2026, 11, 24, 5, 0, tzinfo=UTC),  # date only: midnight EST
        {
            # Pre-opening: the snow datapoints are absent from the markup -> null, not zero.
            "new_24h_in": None,
            "new_48h_in": None,
            "new_7d_in": None,
            "base_in": None,
            "season_in": None,
            "trails_open": 0,
            "trails_total": 62,
            "lifts_open": 0,
            "lifts_total": 7,
            "lifts_hold": None,
        },
    ),
}


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_saved_page_parses(key: str) -> None:
    preset, path, reported_at, values = EXPECTED[key]
    spec = mountain(key).report_adapter
    assert spec is not None and spec.preset == preset
    report = parse(preset, path)
    assert report.reported_at == reported_at
    assert report.values == values


def test_every_roster_mountain_has_a_contract_case() -> None:
    keys = {m.key for m in config.load_mountains(config.DEFAULT_MOUNTAINS) if m.report_adapter}
    assert keys == set(EXPECTED)


def test_cranmore_in_season_archive() -> None:
    report = parse("cranmore_html", "cranmore/snow-report-2026-02-11-archived.html.gz")
    assert report.reported_at == datetime(2026, 2, 11, 5, 0, tzinfo=UTC)
    assert report.values == {
        "new_24h_in": 4.0,  # "Overnight"
        "new_48h_in": None,  # not published
        "new_7d_in": 5.0,
        "base_in": 40.0,  # "26-40\"": upper bound
        "season_in": 63.0,
        "trails_open": 55,  # "Night Trails" is a separate label and is ignored
        "trails_total": 62,
        "lifts_open": 4,
        "lifts_total": 7,
        "lifts_hold": None,
    }
    assert report.raw["base_in"] == '26-40"'
    assert report.raw["reported_at"] == "Feb 11, 2026"


@pytest.mark.parametrize("preset", sorted(adapter.presets()))
def test_stratton_block_page_is_a_failure_not_zeros(preset: str) -> None:
    with pytest.raises(adapter.AdapterError):
        parse(preset, "stratton/report_blocked.html.gz")


def test_dor_scope_ignores_the_header_menu_copy() -> None:
    # The header menu repeats the snow grid and status bars before main#page.
    html = body("killington/report.html.gz")
    main = html.index(b"<main")
    assert html.index(b'dor-snow-val">5"') < main
    assert html.index(b'aria-label="Open Trails"') < main
    # Change only the header copies; the scoped parse must not see them.
    altered = html.replace(b'dor-snow-val">5"', b'dor-snow-val">99"', 1).replace(
        b'<div class="dor-bar-count">3/155</div>', b'<div class="dor-bar-count">99/155</div>', 1
    )
    assert altered.index(b'99"') < main
    assert altered.index(b"99/155") < main
    report = adapter.parse(adapter.preset("dor_html"), altered)
    assert report.values["season_in"] == 5.0
    assert report.values["trails_open"] == 3


def test_dor_status_bars_by_aria_label_not_position() -> None:
    # Pico orders its bars differently (Uphill before Terrain Parks); counts still match labels.
    report = parse("dor_html", "pico/report.html.gz")
    assert report.raw["trails"] == "0/58"
    assert report.raw["lifts"] == "0/7"


def test_dor_uses_the_snow_report_date_not_the_narrative_date() -> None:
    report = parse("dor_html", "killington/report.html.gz")
    # The narrative report in section.condition-report is dated November 26, 8:32 AM.
    assert report.raw["reported_at"] == "November 15, 2026 10:44 AM"


def test_mtnpowder_uses_the_snow_report_time_not_feed_refresh() -> None:
    report = parse("mtnpowder", "sugarbush/snow_api.json.gz")
    assert report.raw["reported_at"] == "2026-11-25T13:02:17-0500"


def test_html_page_through_a_json_preset_fails() -> None:
    with pytest.raises(adapter.AdapterError, match="not JSON"):
        parse("boyne_reportpal", "loon/mountain-report.html.gz")
