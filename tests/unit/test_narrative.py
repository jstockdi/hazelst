from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from hazelst import config, db
from hazelst.agents.client import AgentClient, AgentResponse
from hazelst.agents.prompts import SKILL_PATH, role_prompt, skill_text, system_prompt
from hazelst.agents.report_revise import ReviseInput
from hazelst.agents.schemas import ReportDraft
from hazelst.agents.stub import StubBackend
from hazelst.digest import ReportDigest
from hazelst.narrative import (
    accepted_knowledge,
    build_narrative,
    page_narrative,
    recheck_narrative,
)
from hazelst.render.page import md_to_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "report" / "2026-11-25"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def load_draft(name: str) -> ReportDraft:
    return ReportDraft.model_validate_json((FIXTURES / name).read_text())


DIGEST = ReportDigest.model_validate_json((FIXTURES / "digest.json").read_text())
GOOD = load_draft("draft-checked.json")
FIRST = load_draft("draft-first.json")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(tmp_path / "n.db")
    db.migrate(c)
    yield c
    c.close()


def client(conn: sqlite3.Connection, backend: StubBackend, **settings: Any) -> AgentClient:
    values: dict[str, Any] = {"enable_agents": True, "claude_token": SecretStr("t")} | settings
    return AgentClient(conn, config.Settings(**values), backend, clock=lambda: NOW)


def check_errors(conn: sqlite3.Connection) -> list[tuple[str, list[str] | None]]:
    rows = conn.execute("SELECT role, check_errors FROM agent_call ORDER BY id").fetchall()
    return [(r[0], json.loads(r[1]) if r[1] is not None else None) for r in rows]


# --- prompts ---------------------------------------------------------------------


def test_skill_text_drops_front_matter() -> None:
    assert SKILL_PATH.read_text().startswith("---\nname: hazel-st\n")
    text = skill_text()
    assert text.startswith("# Hazel St Forecast: regional knowledge and rules")
    assert "NW-flow upslope" in text and "Freezing rain" in text


@pytest.mark.parametrize(
    "role", ["report-write", "report-revise", "conditions-extract", "knowledge-propose"]
)
def test_every_role_has_a_prompt(role: str) -> None:
    assert role_prompt(role).startswith(f"# Role: {role}")


def test_report_system_prompt_is_role_skill_knowledge() -> None:
    prompt = system_prompt("report-write", ["NW upslope: Sugarbush gets 1.6x model mean (n=9)"])
    write, skill = role_prompt("report-write"), skill_text()
    assert prompt.index(write) < prompt.index(skill) < prompt.index("Sugarbush gets 1.6x")
    revise = system_prompt("report-revise")
    assert revise.startswith(role_prompt("report-revise"))
    assert write in revise and skill in revise and "(none yet)" in revise
    assert system_prompt("conditions-extract") == role_prompt("conditions-extract")


def test_accepted_knowledge_only(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO knowledge (statement, status) VALUES (?, ?)",
        [("a", "accepted"), ("b", "proposed"), ("c", "accepted"), ("d", "retired")],
    )
    assert accepted_knowledge(conn) == ["a", "c"]


# --- write -> check -> revise ----------------------------------------------------------


def test_clean_draft_publishes_after_one_call(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": GOOD})
    result = build_narrative(client(conn, backend), DIGEST, ["k1"], run_id=None)
    assert result.status == "published"
    assert result.draft == GOOD
    assert result.call_ids == [1]
    assert check_errors(conn) == [("report-write", [])]
    request = backend.requests[0]
    assert ReportDigest.model_validate_json(request.prompt) == DIGEST
    assert request.system_prompt == system_prompt("report-write", ["k1"])


def test_sample_run_first_draft_is_revised(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": FIRST, "report-revise": GOOD})
    result = build_narrative(client(conn, backend), DIGEST)
    assert result.status == "published"
    assert result.draft == GOOD
    assert result.call_ids == [1, 2]
    (write_row, revise_row) = check_errors(conn)
    assert write_row[0] == "report-write" and write_row[1] is not None
    assert [e.split(":")[0] for e in write_row[1]] == ["precip_type"] * 3
    assert revise_row == ("report-revise", [])
    sent = ReviseInput.model_validate_json(backend.requests[1].prompt)
    assert sent.draft == FIRST and sent.errors == write_row[1] and sent.digest == DIGEST


def test_second_failed_check_is_numbers_only(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": FIRST, "report-revise": FIRST})
    result = build_narrative(client(conn, backend), DIGEST)
    assert result.status == "numbers_only"
    assert result.reason == "report-check failed twice"
    assert len(result.errors) == 3
    assert result.page is None


def test_disabled_agents_are_numbers_only(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": GOOD})
    result = build_narrative(client(conn, backend, enable_agents=False, claude_token=None), DIGEST)
    assert result.status == "numbers_only"
    assert result.reason is not None and "disabled" in result.reason
    assert backend.requests == [] and check_errors(conn) == []


def test_cap_reached_is_numbers_only(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": GOOD})
    result = build_narrative(client(conn, backend, agent_daily_calls=0), DIGEST)
    assert result.status == "numbers_only"
    assert result.reason is not None and "cap" in result.reason


def test_failed_write_call_is_numbers_only(conn: sqlite3.Connection) -> None:
    error = AgentResponse("error_max_turns", True, None)
    result = build_narrative(client(conn, StubBackend({"report-write": error})), DIGEST)
    assert result.status == "numbers_only"
    assert result.reason is not None and result.reason.startswith("report-write:")


def test_failed_revise_call_is_numbers_only(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": FIRST, "report-revise": RuntimeError("down")})
    result = build_narrative(client(conn, backend), DIGEST)
    assert result.status == "numbers_only"
    assert result.reason is not None and result.reason.startswith("report-revise:")
    assert len(result.errors) == 3


def test_refresh_recheck_without_calls_when_still_valid(conn: sqlite3.Connection) -> None:
    backend = StubBackend()
    result = recheck_narrative(client(conn, backend), DIGEST, GOOD)
    assert result.status == "published" and result.call_ids == []


def test_refresh_revises_when_numbers_changed(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-revise": GOOD})
    result = recheck_narrative(client(conn, backend), DIGEST, FIRST)
    assert result.status == "published" and result.call_ids == [1]


# --- markdown --------------------------------------------------------------------------


def test_page_narrative_matches_the_page_renderer() -> None:
    page = page_narrative(GOOD, DIGEST)
    assert page.headline == GOOD.headline
    assert page.best_bet is not None
    assert (page.best_bet.mountain, page.best_bet.day) == ("Sunday River", "Thu 26")
    md = page.body_md
    order = ["## The storm", "## Mountain by mountain", "## Cautions"]
    assert [md.index(h) for h in order] == sorted(md.index(h) for h in order)
    names = [f"**{m.name}.**" for m in DIGEST.mountains]
    assert [md.index(n) for n in names] == sorted(md.index(n) for n in names)
    html = md_to_html(md)
    assert "<h2>The storm</h2>" in html and "<b>Killington.</b>" in html
    assert html.count("<p>") == 1 + len(DIGEST.mountains) + len(GOOD.cautions)
