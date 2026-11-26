import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from hazelst import cli, config, db
from hazelst.agents import client as agent_client
from hazelst.agents.client import (
    AgentCallFailed,
    AgentClient,
    AgentRequest,
    AgentResponse,
    AgentUnavailable,
    SdkBackend,
    day_bounds_utc,
)
from hazelst.agents.prompts import role_prompt
from hazelst.agents.schemas import (
    ROLE_OUTPUT,
    BestBet,
    ConditionsExtract,
    ConfigChangeProposal,
    KnowledgeProposal,
    KnowledgeProposals,
    ReportDraft,
    output_format,
)
from hazelst.agents.stub import StubBackend

NOON_ET = datetime(2026, 11, 26, 17, 0, tzinfo=UTC)

DRAFT = ReportDraft(
    headline="Ice Thursday night, rain Friday; nothing to ski for",
    best_bet=BestBet(mountain="sugarloaf", day=date(2026, 11, 26), why="Driest of the eight."),
    storm="A coastal low brings freezing rain Thursday night (NWS Burlington).",
    mountains={"killington": "Ice Thursday night.", "sugarloaf": "Mostly dry."},
    cautions=["Freezing rain Thursday night at Killington."],
)
EXTRACT = ConditionsExtract(
    new_24h_in=3, base_in=18, trails_open=42, trails_total=155, lifts_open=9, confidence="high"
)
PROPOSALS = KnowledgeProposals(
    knowledge=[
        KnowledgeProposal(
            mountain="sugarbush",
            tag="nw_upslope",
            statement="NW upslope: Sugarbush gets 1.6x model mean (n=9)",
            evidence=["2026-12-03/0700/sugarbush/1d/raw_mean"],
        )
    ],
    config_changes=[
        ConfigChangeProposal(
            path="mountain.loon.wind_hold_mph.watch",
            new=38,
            reason="Holds started below 35 mph four times",
            evidence=["run:41"],
        )
    ],
)
OUTPUTS = {
    "report-write": DRAFT,
    "report-revise": DRAFT,
    "conditions-extract": EXTRACT,
    "knowledge-propose": PROPOSALS,
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    yield c
    c.close()


def settings(**overrides: Any) -> config.Settings:
    values: dict[str, Any] = {"enable_agents": True, "claude_token": SecretStr("tok-secret")}
    values.update(overrides)
    return config.Settings(**values)


def make_client(conn: sqlite3.Connection, backend: StubBackend, **overrides: Any) -> AgentClient:
    return AgentClient(conn, settings(**overrides), backend, clock=lambda: NOON_ET)


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM agent_call ORDER BY id").fetchall()


# --- schemas ------------------------------------------------------------------


@pytest.mark.parametrize("role", list(OUTPUTS))
def test_schema_round_trip_through_stub(conn: sqlite3.Connection, role: str) -> None:
    model = ROLE_OUTPUT[role]
    backend = StubBackend({role: OUTPUTS[role]})
    result = make_client(conn, backend).call(
        role, system_prompt="p", data={"x": 1}, output_model=model
    )
    assert result.output == OUTPUTS[role]
    (row,) = rows(conn)
    assert row["status"] == "ok"
    assert model.model_validate_json(row["output"]) == OUTPUTS[role]
    assert backend.requests[0].output_schema == model.model_json_schema()


@pytest.mark.parametrize("model", [ReportDraft, ConditionsExtract, KnowledgeProposals])
def test_schemas_are_closed(model: type[BaseModel]) -> None:
    fmt = output_format(model)
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False


def test_report_draft_rejects_extra_fields() -> None:
    data = DRAFT.model_dump(mode="json") | {"title": "extra"}
    with pytest.raises(ValueError):
        ReportDraft.model_validate(data)


# --- client -------------------------------------------------------------------


def test_call_logs_every_field(conn: sqlite3.Connection) -> None:
    backend = StubBackend(
        {
            "conditions-extract": AgentResponse(
                subtype="success",
                is_error=False,
                structured_output=EXTRACT.model_dump(),
                usage={
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 300,
                },
                total_cost_usd=0.012,
            )
        }
    )
    run_id = db.start_run(conn, "ingest-obs", date(2026, 11, 26))
    make_client(conn, backend).call(
        "conditions-extract",
        system_prompt="the prompt",
        data={"mountain": "killington", "page_text": "3 in"},
        output_model=ConditionsExtract,
        run_id=run_id,
    )
    (row,) = rows(conn)
    request = backend.requests[0]
    assert request.model == "claude-opus-5-5"
    assert request.effort == "low"
    assert request.system_prompt == "the prompt"
    assert json.loads(request.prompt) == {"mountain": "killington", "page_text": "3 in"}
    assert dict(row) | {"id": 0} == {
        "id": 0,
        "run_id": run_id,
        "role": "conditions-extract",
        "model": "claude-opus-5-5",
        "effort": "low",
        "sdk_version": "stub",
        "prompt_sha": agent_client.sha256("the prompt"),
        "input_sha": agent_client.sha256(request.prompt),
        "input": request.prompt,
        "output": EXTRACT.model_dump_json(),
        "input_tokens": 120,
        "output_tokens": 40,
        "cache_read_tokens": 900,
        "cache_write_tokens": 300,
        "cost_usd_est": 0.012,
        "ms": row["ms"],
        "created_at": "2026-11-26T17:00:00+00:00",
        "status": "ok",
        "result_subtype": "success",
        "check_errors": None,
    }


def test_role_model_and_effort_come_from_config(conn: sqlite3.Connection) -> None:
    env = {
        "HAZELST_ENABLE_AGENTS": "true",
        "CLAUDE_CODE_OAUTH_TOKEN": "t",
        "HAZELST_AGENT_MODEL_CONDITIONS_EXTRACT": "claude-haiku-5-5",
        "HAZELST_AGENT_EFFORT_REPORT_WRITE": "max",
    }
    s = config.settings_from_env(env)
    assert s.agent_roles["conditions-extract"].model == "claude-haiku-5-5"
    assert s.agent_roles["report-write"].effort == "max"
    assert s.agent_roles["report-revise"].effort == "medium"
    backend = StubBackend({"conditions-extract": EXTRACT, "report-write": DRAFT})
    client = AgentClient(conn, s, backend, clock=lambda: NOON_ET)
    client.call("conditions-extract", system_prompt="p", data={}, output_model=ConditionsExtract)
    client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    assert [(r.model, r.effort) for r in backend.requests] == [
        ("claude-haiku-5-5", "low"),
        ("claude-opus-5-5", "max"),
    ]


def test_bad_effort_is_a_config_error() -> None:
    with pytest.raises(config.ConfigError):
        config.settings_from_env({"HAZELST_AGENT_EFFORT_REPORT_WRITE": "extreme"})


def test_disabled_agents_skip_without_a_row(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": DRAFT})
    client = make_client(conn, backend, enable_agents=False, claude_token=None)
    with pytest.raises(AgentUnavailable, match="disabled"):
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    assert rows(conn) == []
    assert backend.requests == []


def test_daily_cap_counts_today_new_york(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": [DRAFT, DRAFT, DRAFT]})
    # A call at 11:30 PM ET yesterday (04:30 UTC today) does not count toward today.
    conn.execute(
        "INSERT INTO agent_call (role, created_at, status) VALUES ('report-write', ?, 'ok')",
        ("2026-11-26T04:30:00+00:00",),
    )
    client = make_client(conn, backend, agent_daily_calls=2)
    assert client.calls_today() == 0
    for _ in range(2):
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    assert client.calls_today() == 2
    with pytest.raises(AgentUnavailable, match="cap"):
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    assert len(backend.requests) == 2
    assert len(rows(conn)) == 3


def test_failed_calls_count_toward_cap(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-write": [RuntimeError("boom"), DRAFT]})
    client = make_client(conn, backend, agent_daily_calls=1)
    with pytest.raises(AgentCallFailed):
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    with pytest.raises(AgentUnavailable):
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)


def test_day_bounds_follow_dst() -> None:
    assert day_bounds_utc(datetime(2026, 11, 26, 17, tzinfo=UTC)) == (
        "2026-11-26T05:00:00+00:00",
        "2026-11-27T05:00:00+00:00",
    )
    assert day_bounds_utc(datetime(2026, 7, 1, 17, tzinfo=UTC)) == (
        "2026-07-01T04:00:00+00:00",
        "2026-07-02T04:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("response", "subtype"),
    [
        (AgentResponse("error_max_turns", True, None), "error_max_turns"),
        (AgentResponse("success", True, None, result_text="API Error: 429"), "success"),
        (
            AgentResponse("error_max_structured_output_retries", False, None),
            "error_max_structured_output_retries",
        ),
    ],
)
def test_error_results_fail_and_log(
    conn: sqlite3.Connection, response: AgentResponse, subtype: str
) -> None:
    client = make_client(conn, StubBackend({"report-write": response}))
    with pytest.raises(AgentCallFailed) as exc:
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    assert exc.value.status == "error"
    (row,) = rows(conn)
    assert (row["status"], row["result_subtype"]) == ("error", subtype)


def test_invalid_output_is_logged_with_errors(conn: sqlite3.Connection) -> None:
    bad = DRAFT.model_dump(mode="json")
    del bad["storm"]
    client = make_client(conn, StubBackend({"report-write": bad}))
    with pytest.raises(AgentCallFailed) as exc:
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    assert exc.value.status == "invalid"
    (row,) = rows(conn)
    assert row["status"] == "invalid"
    assert json.loads(row["output"]) == bad
    assert any(e.startswith("storm:") for e in json.loads(row["check_errors"]))


def test_backend_exception_is_logged_without_token(conn: sqlite3.Connection) -> None:
    client = make_client(conn, StubBackend({"report-write": RuntimeError("bad tok-secret")}))
    with pytest.raises(AgentCallFailed):
        client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    (row,) = rows(conn)
    assert row["status"] == "error"
    assert row["result_subtype"] is None
    assert "tok-secret" not in row["output"]


def test_record_check_errors(conn: sqlite3.Connection) -> None:
    client = make_client(conn, StubBackend({"report-write": DRAFT}))
    result = client.call("report-write", system_prompt="p", data={}, output_model=ReportDraft)
    client.record_check_errors(result.call_id, ["killington: says rain, data says ice"])
    (row,) = rows(conn)
    assert json.loads(row["check_errors"]) == ["killington: says rain, data says ice"]


def test_pydantic_input_is_sent_as_json(conn: sqlite3.Connection) -> None:
    backend = StubBackend({"report-revise": DRAFT})
    make_client(conn, backend).call(
        "report-revise", system_prompt="p", data=DRAFT, output_model=ReportDraft
    )
    assert ReportDraft.model_validate_json(backend.requests[0].prompt) == DRAFT


def test_stub_callable_sees_request(conn: sqlite3.Connection) -> None:
    def reply(request: AgentRequest) -> ConditionsExtract:
        assert request.role == "conditions-extract"
        return EXTRACT

    result = make_client(conn, StubBackend({"conditions-extract": reply})).call(
        "conditions-extract", system_prompt="p", data={}, output_model=ConditionsExtract
    )
    assert result.output == EXTRACT


def test_unknown_role_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        make_client(conn, StubBackend()).call(
            "report-dream", system_prompt="p", data={}, output_model=ReportDraft
        )


# --- SDK backend (options only; no live calls in CI) -------------------------------


def test_sdk_backend_options_match_call_conventions() -> None:
    backend = SdkBackend(SecretStr("tok-secret"))
    request = AgentRequest(
        role="report-write",
        model="claude-opus-5-5",
        effort="high",
        system_prompt="sys",
        prompt="{}",
        output_schema=ReportDraft.model_json_schema(),
    )
    opts = backend.options(request, "/tmp/h", lambda _: None)
    assert opts.model == "claude-opus-5-5"
    assert opts.effort == "high"
    assert opts.system_prompt == "sys"
    assert opts.output_format == {"type": "json_schema", "schema": ReportDraft.model_json_schema()}
    assert opts.tools == []
    assert opts.setting_sources == []
    assert opts.skills == []
    assert opts.strict_mcp_config is True
    assert opts.permission_mode == "dontAsk"
    assert opts.verbatim_prompts is True
    assert opts.max_turns == 5
    assert opts.extra_args == {"no-session-persistence": None}
    assert opts.cwd == "/tmp/h"
    assert opts.env["HOME"] == "/tmp/h"
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-secret"
    assert opts.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert opts.env["DISABLE_AUTOUPDATER"] == "1"
    assert opts.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert opts.env["CLAUDE_AGENT_SDK_CLIENT_APP"].startswith("hazelst/")
    assert opts.fallback_model is None


def test_sdk_backend_refuses_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    with pytest.raises(AgentUnavailable, match="ANTHROPIC_API_KEY"):
        SdkBackend(SecretStr("t"))


def test_sdk_backend_needs_token() -> None:
    with pytest.raises(AgentUnavailable, match="CLAUDE_CODE_OAUTH_TOKEN"):
        SdkBackend(None)


# --- prompts and CLI --------------------------------------------------------------


def test_conditions_extract_prompt_loads() -> None:
    assert role_prompt("conditions-extract").startswith("# Role: conditions-extract")


def test_cli_smoke_skips_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HAZELST_DB", str(tmp_path / "c.db"))
    monkeypatch.delenv("HAZELST_ENABLE_AGENTS", raising=False)
    assert cli.main(["agents", "smoke"]) == 1
    assert "disabled" in capsys.readouterr().err
    assert cli.main(["agents", "calls"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["calls"] == [] and out["daily_cap"] == 10
