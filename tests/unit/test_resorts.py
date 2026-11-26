from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from hazelst import config, db, jobs
from hazelst.sources import base
from hazelst.sources.resorts import adapter
from hazelst.sources.resorts import source as resorts
from tests.support import FIXTURES

RESORTS = FIXTURES / "resorts"
USER_AGENT = "hazelst-forecast (github.com/jstockdi/hazelst)"


def fixture(path: str) -> bytes:
    return gzip.decompress((RESORTS / path).read_bytes())


def feed_token(key: str) -> str:
    return str(json.loads(fixture(f"{key}/mtnfeed_config.json.gz"))["bearerToken"])


# --- value parsers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4", 4.0),
        ('4"', 4.0),
        ("1.5", 1.5),
        (" 12 in", 12.0),
        (3, 3.0),
        (0, 0.0),
        ("--", None),
        ("", None),
        (None, None),
        ("  ", None),
    ],
)
def test_inches(raw: Any, expected: float | None) -> None:
    assert adapter.parse_inches(raw) == expected


@pytest.mark.parametrize("raw", ["Trace", "4-6", True, "abc"])
def test_inches_rejects_text(raw: Any) -> None:
    with pytest.raises(adapter.AdapterError):
        adapter.parse_inches(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [('26-40"', 40.0), ("18 - 36", 36.0), ("12\u201324", 24.0), ("30", 30.0), ("--", None)],
)
def test_range_max(raw: str, expected: float | None) -> None:
    assert adapter.parse_range_max(raw) == expected


def test_ratio_and_int() -> None:
    assert adapter.parse_ratio("55/62") == (55, 62)
    assert adapter.parse_ratio(" 0 / 7 ") == (0, 7)
    assert adapter.parse_ratio(None) is None
    with pytest.raises(adapter.AdapterError):
        adapter.parse_ratio("55 of 62")
    assert adapter.parse_int(7) == 7
    assert adapter.parse_int("12") == 12
    assert adapter.parse_int("--") is None
    with pytest.raises(adapter.AdapterError):
        adapter.parse_int("1.5")


@pytest.mark.parametrize(
    "raw",
    ["2026-11-25T21:04:24Z", "2026-11-25T16:04:24-05:00", "2026-11-25T16:04:24-0500"],
)
def test_datetime_iso_forms(raw: str) -> None:
    assert adapter.parse_datetime(raw) == datetime(2026, 11, 25, 21, 4, 24, tzinfo=UTC)


def test_datetime_local_format_with_regex_and_dst() -> None:
    parsed = adapter.parse_datetime(
        "Updated July 4, 2026 9:05 AM",
        regex="Updated (.+)",
        format="%B %d, %Y %I:%M %p",
        tz="America/New_York",
    )
    assert parsed == datetime(2026, 7, 4, 13, 5, tzinfo=UTC)  # EDT
    with pytest.raises(adapter.AdapterError, match="date pattern"):
        adapter.parse_datetime("No date", regex="Updated (.+)")
    with pytest.raises(adapter.AdapterError, match="no tz"):
        adapter.parse_datetime("2026-11-25T16:04:24")


def test_resolve_path() -> None:
    doc = {"a": {"b": [{"c": 1}, {"c": None}]}}
    assert adapter.resolve_path(doc, "a.b[0].c") == 1
    assert adapter.resolve_path(doc, "a.b[1].c") is None
    assert adapter.resolve_path(doc, "a.b[5].c") is None
    assert adapter.resolve_path(doc, "a.x.c") is None


def test_embedded_json_kind() -> None:
    spec = adapter._preset(
        "inline",
        {
            "kind": "embedded_json",
            "url": "https://example.test/",
            "script": "script#__NEXT_DATA__",
            "fields": {
                "new_24h_in": "props.snow.d1",
                "reported_at": {"path": "props.at", "parse": "datetime"},
            },
        },
    )
    page = (
        b'<html><body><script id="__NEXT_DATA__" type="application/json">'
        b'{"props": {"snow": {"d1": "3"}, "at": "2026-12-01T12:00:00Z"}}</script></body></html>'
    )
    report = adapter.parse(spec, page)
    assert report.values["new_24h_in"] == 3.0
    with pytest.raises(adapter.AdapterError, match="not found"):
        adapter.parse(spec, b"<html></html>")


def test_presets_validate() -> None:
    assert set(adapter.presets()) == {"boyne_reportpal", "mtnpowder", "dor_html", "cranmore_html"}
    with pytest.raises(ValueError, match="unknown resort preset"):
        adapter.preset("nope")
    with pytest.raises(ValueError, match="unknown parser"):
        adapter._preset("x", {"kind": "json", "url": "u", "fields": {"new_24h_in": {"parse": "x"}}})
    with pytest.raises(ValueError, match="unknown fields"):
        adapter._preset("x", {"kind": "json", "url": "u", "fields": {"depth": "a"}})


def test_roster_adapters_resolve() -> None:
    for mountain in config.load_mountains(config.DEFAULT_MOUNTAINS):
        spec_ref = mountain.report_adapter
        assert spec_ref is not None
        spec = adapter.preset(spec_ref.preset)
        variables = dict.fromkeys(spec.prefetch.vars, "T") if spec.prefetch else {}
        url, _ = resorts.data_request(spec, spec_ref.params, variables)
        assert url.startswith("https://")


def test_mountains_stay_hashable() -> None:
    mountains = config.load_mountains(config.DEFAULT_MOUNTAINS)
    assert len(set(mountains)) == len(mountains)


def test_report_adapter_config_shape() -> None:
    raw = json.loads(config.DEFAULT_MOUNTAINS.read_text())
    raw[0]["report_adapter"] = None
    assert config.Mountain.model_validate(raw[0]).report_adapter is None
    raw[0]["report_adapter"] = "killington"
    with pytest.raises(ValueError):
        config.Mountain.model_validate(raw[0])


def test_extraction_never_for_boyne() -> None:
    allowed = {
        m.key
        for m in config.load_mountains(config.DEFAULT_MOUNTAINS)
        if resorts.extraction_allowed(m)
    }
    assert allowed == {"killington", "pico", "sugarbush", "stratton", "cranmore"}


def test_day_bounds_follow_local_midnight() -> None:
    assert resorts.day_bounds_utc(datetime(2026, 11, 26, 4, 30, tzinfo=UTC)) == (
        "2026-11-25T05:00:00+00:00",
        "2026-11-26T05:00:00+00:00",
    )
    assert resorts.day_bounds_utc(datetime(2026, 7, 4, 12, tzinfo=UTC)) == (
        "2026-07-04T04:00:00+00:00",
        "2026-07-05T04:00:00+00:00",
    )


# --- normalize -----------------------------------------------------------------------


def fetched(key: str, preset: str, path: str, fetched_at: str, status: int = 200) -> base.Fetched:
    return base.Fetched(
        source=resorts.SOURCE,
        url="https://example.test/",
        params={},
        http_status=status,
        fetched_at=fetched_at,
        ms=1,
        body=fixture(path),
        meta={"mountain": key, "preset": preset, "params": {}},
    )


def test_normalize_sets_obs_date_and_raw() -> None:
    row = resorts.normalize(
        fetched("loon", "boyne_reportpal", "loon/reportpal.json.gz", "2026-11-26T14:22:51+00:00")
    )
    assert row.source == "resort"
    assert row.reported_at == "2026-11-25T16:27:41+00:00"
    assert row.obs_date == "2026-11-25"  # local date of reported_at
    assert row.lifts_hold == 0
    assert row.raw["lifts_total"] == 13
    assert row.raw["stale"] is False


def test_normalize_obs_date_is_local_not_utc() -> None:
    # 2026-11-25T03:00Z is the evening of Nov 24 in New York.
    page = json.loads(fixture("loon/reportpal.json.gz"))
    page["updated"] = "2026-11-25T03:00:00Z"
    f = fetched("loon", "boyne_reportpal", "loon/reportpal.json.gz", "2026-11-25T11:00:00+00:00")
    row = resorts.normalize(base.Fetched(**{**f.__dict__, "body": json.dumps(page).encode()}))
    assert row.obs_date == "2026-11-24"


def test_normalize_flags_stale_reports() -> None:
    row = resorts.normalize(
        fetched("killington", "dor_html", "killington/report.html.gz", "2026-11-26T14:17:27+00:00")
    )
    assert row.reported_at == "2026-11-15T15:44:00+00:00"
    assert row.stale is True


def test_normalize_rejects_errors_and_block_pages() -> None:
    with pytest.raises(adapter.AdapterError, match="HTTP 403"):
        resorts.normalize(
            fetched(
                "loon", "boyne_reportpal", "loon/reportpal.json.gz", "2026-11-26T14:00:00Z", 403
            )
        )
    with pytest.raises(adapter.AdapterError):
        resorts.normalize(
            fetched("stratton", "mtnpowder", "stratton/report_blocked.html.gz", "2026-11-26T14:00Z")
        )


# --- ingest against a replayed site set -------------------------------------------------

PAGES: dict[tuple[str, str], str] = {
    ("killington.com", "/mountain-report"): "killington/report.html.gz",
    ("picomountain.com", "/mountain-report"): "pico/report.html.gz",
    ("v4.mtnfeed.com", "/resorts/sugarbush.json"): "sugarbush/mtnfeed_config.json.gz",
    ("v4.mtnfeed.com", "/resorts/stratton.json"): "stratton/mtnfeed_config.json.gz",
    ("www.loonmtn.com", "/api/reportpal"): "loon/reportpal.json.gz",
    ("www.sundayriver.com", "/api/reportpal"): "sunday_river/reportpal.json.gz",
    ("www.sugarloaf.com", "/api/reportpal"): "sugarloaf/reportpal.json.gz",
    ("cranmore.com", "/snow-report"): "cranmore/snow-report.html.gz",
}
FEEDS = {"70": "sugarbush", "1": "stratton"}


class Sites:
    """Serves the J00 pages; records every request."""

    def __init__(
        self, override: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None
    ):
        self.requests: list[httpx.Request] = []
        self.override = override or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        for name, respond in self.override.items():
            if name in str(request.url):
                return respond(request)
        if (host, path) == ("mtnpowder.com", "/feed/v3.json"):
            key = FEEDS.get(request.url.params.get("resortId[]", ""))
            if key is None or request.url.params.get("bearer_token") != feed_token(key):
                return httpx.Response(401, text="bad token")
            return httpx.Response(200, content=fixture(f"{key}/snow_api.json.gz"))
        page = PAGES.get((host, path))
        if page is None:
            return httpx.Response(404, text="no fixture")
        return httpx.Response(200, content=fixture(page))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), headers={"User-Agent": USER_AGENT})


def run_obs(cfg: config.Config, sites: Sites) -> dict[str, Any]:
    with sites.client() as client:
        return jobs.run_job(cfg, "ingest-obs", client=client)


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    return config.load({"HAZELST_DB": str(tmp_path / "h.db")})


def observations(cfg: config.Config) -> list[sqlite3.Row]:
    conn = db.connect(cfg.settings.db_path)
    try:
        return conn.execute("SELECT * FROM observation ORDER BY mountain").fetchall()
    finally:
        conn.close()


def test_ingest_obs_fills_observation_for_all_eight(cfg: config.Config) -> None:
    sites = Sites()
    summary = run_obs(cfg, sites)
    step = summary["resorts"]
    assert step["status"] == "ok"
    assert step["ok"] == 8 and step["failed"] == 0
    rows = observations(cfg)
    assert [r["mountain"] for r in rows] == sorted(m.key for m in cfg.mountains)
    assert {r["source"] for r in rows} == {"resort"}
    by_key = {r["mountain"]: r for r in rows}
    assert by_key["sugarbush"]["season_in"] == 7.0
    assert by_key["cranmore"]["new_24h_in"] is None
    assert json.loads(by_key["killington"]["raw"])["stale"] is True
    assert {r.headers["User-Agent"] for r in sites.requests} == {USER_AGENT}


def test_feed_token_is_not_stored_in_fetch_params(cfg: config.Config) -> None:
    run_obs(cfg, Sites())
    conn = db.connect(cfg.settings.db_path)
    params = [r[0] for r in conn.execute("SELECT params FROM fetch WHERE source = 'resort'")]
    conn.close()
    for token in (feed_token("sugarbush"), feed_token("stratton")):
        assert all(token not in p for p in params)
    assert any(resorts.REDACTED in p for p in params)


def test_rerun_is_idempotent_and_config_is_cached(cfg: config.Config) -> None:
    sites = Sites()
    run_obs(cfg, sites)
    second = run_obs(cfg, sites)
    assert all(s.get("duplicate") for s in second["resorts"]["resorts"].values())
    assert len(observations(cfg)) == 8
    config_hits = [r for r in sites.requests if r.url.host == "v4.mtnfeed.com"]
    assert len(config_hits) == 2  # once per resort per day, not per run


def test_budget_three_fetches_per_resort_per_day(cfg: config.Config) -> None:
    sites = Sites()
    for _ in range(3):
        run_obs(cfg, sites)
    fourth = run_obs(cfg, sites)["resorts"]
    assert fourth["skipped_budget"] == 8
    assert fourth["resorts"]["loon"]["status"] == "skipped_budget"
    data_hits = [r for r in sites.requests if r.url.host == "www.loonmtn.com"]
    assert len(data_hits) == 3


def test_disabled_adapter_is_reported(cfg: config.Config) -> None:
    mountains = tuple(
        m.model_copy(update={"report_adapter": None}) if m.key == "stratton" else m
        for m in cfg.mountains
    )
    summary = run_obs(cfg.model_copy(update={"mountains": mountains}), Sites())
    assert summary["resorts"]["resorts"]["stratton"] == {"status": "disabled", "preset": None}
    assert summary["resorts"]["disabled"] == 1


def test_blocked_feed_fails_and_logs(
    cfg: config.Config, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = fixture("stratton/report_blocked.html.gz")
    sites = Sites({"resortId%5B%5D=1": lambda _: httpx.Response(200, content=blocked)})
    summary = run_obs(cfg, sites)["resorts"]
    stratton = summary["resorts"]["stratton"]
    assert stratton["status"] == "failed"
    assert "not JSON" in stratton["reason"]
    assert summary["ok"] == 7
    assert "stratton" not in {r["mountain"] for r in observations(cfg)}
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    failures = [e for e in events if e.get("event") == "resort_adapter_failed"]
    assert [(e["mountain"], e["preset"]) for e in failures] == [("stratton", "mtnpowder")]


def test_renormalize_from_stored_fetch(cfg: config.Config) -> None:
    run_obs(cfg, Sites())
    conn = db.connect(cfg.settings.db_path)
    row = conn.execute("SELECT fetch_id FROM observation WHERE mountain = 'pico'").fetchone()
    fetch_id = row[0]
    conn.execute("DELETE FROM observation WHERE mountain = 'pico'")
    assert resorts.renormalize(conn, fetch_id) is not None
    assert resorts.renormalize(conn, fetch_id) is None  # already stored
    conn.close()


def test_record_returns_config_and_reports() -> None:
    sites = Sites()
    with sites.client() as client:
        recorded = resorts.record(client, config.load_mountains(config.DEFAULT_MOUNTAINS))
    labels = sorted(resorts.label(f) for f in recorded)
    assert len(labels) == 10
    assert "stratton-config" in labels and "stratton-report" in labels
    assert all(f.http_status == 200 for f in recorded)
