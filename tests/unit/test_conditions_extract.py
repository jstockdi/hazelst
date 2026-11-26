from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from hazelst import config, db, jobs
from hazelst.agents import conditions_extract
from hazelst.agents.client import AgentClient
from hazelst.agents.schemas import ConditionsExtract
from hazelst.agents.stub import StubBackend
from hazelst.sources import base
from hazelst.sources.resorts import source as resorts
from tests.unit.test_resorts import Sites, fixture

BOYNE = ("loon", "sunday_river", "sugarloaf")
EXTRACTED = ConditionsExtract(
    new_24h_in=2.0,
    base_in=18.0,
    trails_open=40,
    trails_total=155,
    lifts_open=6,
    confidence="medium",
)


def env(tmp_path: Path, enabled: bool = True) -> dict[str, str]:
    return {
        "HAZELST_DB": str(tmp_path / "h.db"),
        "HAZELST_ENABLE_AGENTS": "true" if enabled else "false",
        "CLAUDE_CODE_OAUTH_TOKEN": "test-token",
    }


class Harness:
    def __init__(self, cfg: config.Config, conn: sqlite3.Connection, backend: StubBackend):
        self.cfg = cfg
        self.conn = conn
        self.backend = backend
        self.agents = AgentClient(conn, cfg.settings, backend)

    def run(self, sites: Sites) -> dict[str, Any]:
        run_id = db.start_run(self.conn, "ingest-obs", None)
        with sites.client() as client:
            ctx = jobs.JobContext(
                cfg=self.cfg,
                conn=self.conn,
                run_id=run_id,
                for_date=jobs.today(self.cfg),
                client=client,
            )
            return jobs.ingest_resorts(ctx, self.agents)

    def rows(self, source: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM observation WHERE source = ? ORDER BY mountain", (source,)
        ).fetchall()


def make_harness(
    tmp_path: Path, backend: StubBackend, *, enabled: bool = True
) -> Iterator[Harness]:
    cfg = config.load(env(tmp_path, enabled))
    conn = db.connect(cfg.settings.db_path)
    db.migrate(conn)
    db.sync_mountains(conn, cfg.mountains)
    try:
        yield Harness(cfg, conn, backend)
    finally:
        conn.close()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    yield from make_harness(tmp_path, StubBackend({"conditions-extract": [EXTRACTED]}))


def broken_page(_: httpx.Request) -> httpx.Response:
    """A changed page: report text present, but none of the preset's selectors match."""
    text = " ".join(["New snow 2 inches overnight, base 18 inches, 40 of 155 trails open."] * 10)
    return httpx.Response(
        200, text=f"<html><body><div class='new-layout'>{text}</div></body></html>"
    )


def broken_json(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="{" + "x" * 500)


# --- the Boyne guard -------------------------------------------------------------------


@pytest.mark.parametrize("key", BOYNE)
def test_boyne_resorts_never_reach_claude(harness: Harness, key: str) -> None:
    host = {"loon": "loonmtn", "sunday_river": "sundayriver", "sugarloaf": "sugarloaf"}[key]
    summary = harness.run(Sites({f"www.{host}.com": broken_json}))
    assert summary["resorts"][key]["status"] == "failed"
    assert "extract" not in summary["resorts"][key]
    assert harness.backend.requests == []
    assert harness.conn.execute("SELECT count(*) FROM agent_call").fetchone()[0] == 0


@pytest.mark.parametrize("key", BOYNE)
def test_boyne_guard_holds_by_key_and_by_preset(key: str) -> None:
    mountains = {m.key: m for m in config.load_mountains(config.DEFAULT_MOUNTAINS)}
    page = base.Fetched(
        source=resorts.SOURCE,
        url="https://example.test/",
        params={},
        http_status=200,
        fetched_at="2026-11-26T12:00:00+00:00",
        ms=1,
        body=b"<html><body>" + b"text " * 100 + b"</body></html>",
        meta={"mountain": key, "preset": "boyne_reportpal"},
    )
    with pytest.raises(conditions_extract.ExtractionNotAllowed):
        conditions_extract.build_input(mountains[key], page)
    # Guarded by key even if the roster were changed to another preset.
    renamed = mountains[key].model_copy(
        update={"report_adapter": config.ReportAdapter(preset="dor_html", params={"host": "x"})}
    )
    with pytest.raises(conditions_extract.ExtractionNotAllowed):
        conditions_extract.build_input(
            renamed, dataclasses.replace(page, meta={"mountain": key, "preset": "dor_html"})
        )
    # And by preset even under a non-Boyne key.
    other = mountains["killington"].model_copy(
        update={"report_adapter": config.ReportAdapter(preset="boyne_reportpal", params={})}
    )
    with pytest.raises(conditions_extract.ExtractionNotAllowed):
        conditions_extract.build_input(other, page)


# --- non-Boyne failures ------------------------------------------------------------------


def test_failed_page_is_extracted_and_stored_as_resort_llm(harness: Harness) -> None:
    summary = harness.run(Sites({"killington.com": broken_page}))
    killington = summary["resorts"]["killington"]
    assert killington["status"] == "failed"
    assert killington["extract"]["status"] == "ok"
    rows = harness.rows("resort_llm")
    assert [r["mountain"] for r in rows] == ["killington"]
    row = rows[0]
    assert (row["new_24h_in"], row["base_in"], row["trails_open"], row["lifts_open"]) == (
        2.0,
        18.0,
        40,
        6,
    )
    raw = json.loads(row["raw"])
    assert raw["agent_call_id"] == killington["extract"]["call_id"]
    assert raw["confidence"] == "medium"
    # The other seven adapters still produced their own rows; the LLM row adds, never replaces.
    assert len(harness.rows("resort")) == 7
    # Only the failed page was sent, as data, with scripts stripped.
    [request] = harness.backend.requests
    sent = json.loads(request.prompt)
    assert sent["mountain"] == "killington"
    assert "40 of 155 trails open" in sent["page_text"]


def test_extraction_runs_at_most_once_per_resort_per_day(harness: Harness) -> None:
    sites = Sites({"killington.com": broken_page})
    harness.run(sites)
    second = harness.run(sites)
    assert second["resorts"]["killington"]["extract"] == {
        "status": "skipped",
        "reason": "already called today",
    }
    assert len(harness.backend.requests) == 1


def test_block_page_is_not_sent(harness: Harness) -> None:
    blocked = fixture("stratton/report_blocked.html.gz")
    summary = harness.run(
        Sites({"resortId%5B%5D=1": lambda _: httpx.Response(200, content=blocked)})
    )
    extract = summary["resorts"]["stratton"]["extract"]
    assert extract["status"] == "skipped"
    assert "characters of text" in extract["reason"]
    assert harness.backend.requests == []


def test_http_error_is_not_sent(harness: Harness) -> None:
    summary = harness.run(Sites({"cranmore.com": lambda _: httpx.Response(403)}))
    assert summary["resorts"]["cranmore"]["extract"]["status"] == "skipped"
    assert harness.backend.requests == []


def test_agents_disabled_means_no_call(tmp_path: Path) -> None:
    backend = StubBackend({"conditions-extract": [EXTRACTED]})
    for h in make_harness(tmp_path, backend, enabled=False):
        summary = h.run(Sites({"killington.com": broken_page}))
        assert summary["resorts"]["killington"]["extract"]["status"] == "unavailable"
        assert backend.requests == []
        assert h.rows("resort_llm") == []


def test_daily_cap_respected(tmp_path: Path) -> None:
    backend = StubBackend({"conditions-extract": [EXTRACTED]})
    cfg_env = {**env(tmp_path), "HAZELST_AGENT_DAILY_CALLS": "0"}
    cfg = config.load(cfg_env)
    conn = db.connect(cfg.settings.db_path)
    db.migrate(conn)
    db.sync_mountains(conn, cfg.mountains)
    h = Harness(cfg, conn, backend)
    summary = h.run(Sites({"killington.com": broken_page}))
    assert "cap" in summary["resorts"]["killington"]["extract"]["reason"]
    assert backend.requests == []
    conn.close()


def test_all_null_extraction_stores_nothing(tmp_path: Path) -> None:
    empty = ConditionsExtract(
        new_24h_in=None,
        base_in=None,
        trails_open=None,
        trails_total=None,
        lifts_open=None,
        confidence="low",
    )
    for h in make_harness(tmp_path, StubBackend({"conditions-extract": [empty]})):
        summary = h.run(Sites({"killington.com": broken_page}))
        assert summary["resorts"]["killington"]["extract"]["status"] == "no_values"
        assert h.rows("resort_llm") == []


def test_invalid_output_is_a_failed_call(tmp_path: Path) -> None:
    bad = {"new_24h_in": -3, "confidence": "certain"}
    for h in make_harness(tmp_path, StubBackend({"conditions-extract": [bad]})):
        summary = h.run(Sites({"killington.com": broken_page}))
        extract = summary["resorts"]["killington"]["extract"]
        assert extract["status"] == "failed"
        status = h.conn.execute("SELECT status FROM agent_call").fetchone()[0]
        assert status == "invalid"
        assert h.rows("resort_llm") == []
