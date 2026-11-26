"""knowledge-review, owner decisions, the config_change overlay, and replay, with a stub Claude."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hazelst import cli, config, db, jobs
from hazelst.agents.client import AgentClient, AgentRequest
from hazelst.agents.knowledge_propose import KnowledgeInput
from hazelst.agents.schemas import ConfigChangeProposal, KnowledgeProposal, KnowledgeProposals
from hazelst.agents.stub import Responder, StubBackend
from tests.support import replay_client
from tests.unit.test_report_build import DAY, NOON, passing_draft, recorded

MONDAY = date(2026, 11, 30)  # reviews 2026-11-22 .. 2026-11-28
AFD = (
    "Area Forecast Discussion\n.KEY MESSAGES...\nNorthwest flow upslope snow tonight.\n"
    "\n.DISCUSSION...\nUpslope showers favor the western slopes.\n"
    "\n.AVIATION...\nVFR.\n"
)
UPSLOPE = "NW upslope: Sugarbush gets 1.6x model mean (n=4)"


def env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "HAZELST_DB": str(tmp_path / "h.db"),
        "HAZELST_ENABLE_AGENTS": "true",
        "CLAUDE_CODE_OAUTH_TOKEN": "test-token",
        **extra,
    }


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    for key, value in env(tmp_path).items():
        monkeypatch.setenv(key, value)
    c = config.load()
    conn = db.connect(c.settings.db_path)
    db.migrate(conn)
    conn.close()
    return c


def connect(c: config.Config) -> sqlite3.Connection:
    return db.connect(c.settings.db_path)


def seed_week(c: config.Config) -> None:
    """Skill cells, the week's verifications, and the BTV discussion of each miss day."""
    conn = connect(c)
    conn.executemany(
        "INSERT INTO skill VALUES (?, ?, '1d', ?, ?, ?, ?, ?, '2026-11-30T07:00:00+00:00')",
        [
            ("sugarbush", "gfs_seamless", "", 9, 0.62, 2.1, 0.33),
            ("sugarbush", "gfs_seamless", "nw_upslope", 4, 0.55, 3.0, 0.25),
            ("loon", "gfs_seamless", "", 2, 1.1, 0.5, 1.0),  # n < 3: not evidence
        ],
    )
    verification = [
        ("2026-11-24", "0700", "sugarbush", 2.0, 6.0, "MISS", ["nw_upslope"]),
        ("2026-11-25", "0700", "sugarbush", 1.0, 4.5, "MISS", ["nw_upslope"]),
        ("2026-11-25", "0700", "loon", 1.0, 1.5, "HIT", []),
        ("2026-11-20", "0700", "sugarbush", 1.0, 5.0, "MISS", []),  # before the week
    ]
    conn.executemany(
        "INSERT INTO verification VALUES (?, ?, ?, '1d', 'raw_mean', ?, ?, 'resort', 0, 0, ?, ?)",
        [(d, e, m, f, o, r, json.dumps(t)) for d, e, m, f, o, r, t in verification],
    )
    conn.executemany(
        "INSERT INTO discussion VALUES (?, 'BTV', 'AFD', ?, ?)",
        [
            ("AFD-BTV-1", "2026-11-24T08:30:00+00:00", AFD),
            ("AFD-BTV-2", "2026-11-25T08:30:00+00:00", AFD),
            ("AFD-BTV-LATE", "2026-11-25T20:00:00+00:00", "after the report"),
        ],
    )
    conn.close()


KEY_MISS = "verification:2026-11-24:0700:sugarbush:1d:raw_mean"
KEY_SKILL = "skill:sugarbush:gfs_seamless:1d:nw_upslope"


def proposals() -> KnowledgeProposals:
    return KnowledgeProposals(
        knowledge=[
            KnowledgeProposal(
                mountain="sugarbush", tag="nw_upslope", statement=UPSLOPE, evidence=[KEY_SKILL]
            ),
            KnowledgeProposal(
                mountain="stowe", tag=None, statement="Off roster.", evidence=[KEY_MISS]
            ),
            KnowledgeProposal(mountain=None, tag=None, statement="Unsupported.", evidence=["x"]),
        ],
        config_changes=[
            ConfigChangeProposal(
                path="mountain.sugarbush.wind_hold_mph.watch",
                new=35,
                reason="Holds started below 40 mph",
                evidence=[KEY_MISS],
            ),
            ConfigChangeProposal(
                path="mountain.sugarbush.summit_ft", new=4000, reason="x", evidence=[KEY_MISS]
            ),
            ConfigChangeProposal(
                path="mountain.loon.wind_hold_mph.watch", new=35, reason="x", evidence=[KEY_MISS]
            ),
            ConfigChangeProposal(
                path="mountain.loon.wind_hold_mph.watch", new=60, reason="x", evidence=[KEY_MISS]
            ),
        ],
    )


def use_stub(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, Responder | list[Responder]]
) -> StubBackend:
    backend = StubBackend(responses)
    monkeypatch.setattr(
        jobs,
        "make_agents",
        lambda ctx: AgentClient(ctx.conn, ctx.cfg.settings, backend, clock=jobs.clock),
    )
    monkeypatch.setattr(
        cli,
        "AgentClient",
        lambda conn, settings: AgentClient(conn, settings, backend, clock=jobs.clock),
    )
    return backend


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    capsys.readouterr()  # drop earlier job log lines
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except json.JSONDecodeError:
        return code, out


# --- knowledge-review ---------------------------------------------------------------


def test_review_stores_valid_proposals_as_proposed(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_week(cfg)
    backend = use_stub(monkeypatch, {"knowledge-propose": proposals()})
    summary = jobs.run_job(cfg, "knowledge-review", MONDAY)

    assert summary["status"] == "proposed"
    assert summary["week"] == ["2026-11-22", "2026-11-28"]
    assert summary["proposed"] == {"knowledge": [1], "config_change": [1]}
    reasons = {d.get("knowledge") or d["config_change"]: d["reason"] for d in summary["dropped"]}
    assert reasons["Off roster."] == "unknown mountain 'stowe'"
    assert reasons["Unsupported."] == "no evidence key from the input"
    assert reasons["mountain.sugarbush.summit_ft"] == "not a threshold path"
    assert "keeps the current value" in [d["reason"] for d in summary["dropped"]]
    assert any("likely must be >= watch" in d["reason"] for d in summary["dropped"])

    conn = connect(cfg)
    k = dict(conn.execute("SELECT * FROM knowledge").fetchone())
    assert (k["mountain"], k["tag"], k["statement"]) == ("sugarbush", "nw_upslope", UPSLOPE)
    assert (k["status"], k["proposed_by"], k["decided_by"]) == (
        "proposed",
        "knowledge-propose",
        None,
    )
    assert json.loads(k["evidence"]) == {
        "keys": [KEY_SKILL],
        "agent_call": summary["agent_call"],
        "run_id": summary["run_id"],
    }
    change = dict(conn.execute("SELECT * FROM config_change").fetchone())
    assert (change["path"], config.json_value(change["old"]), config.json_value(change["new"])) == (
        "mountain.sugarbush.wind_hold_mph.watch",
        40,
        35,
    )
    assert change["status"] == "proposed" and change["decided_at"] is None
    role, run_id, status = conn.execute("SELECT role, run_id, status FROM agent_call").fetchone()
    assert (role, run_id, status) == ("knowledge-propose", summary["run_id"], "ok")

    sent = KnowledgeInput.model_validate_json(backend.requests[0].prompt)
    assert [c.key for c in sent.skill_changes] == [
        "skill:sugarbush:gfs_seamless:1d:all",
        KEY_SKILL,
    ]
    assert all(c.previous is None for c in sent.skill_changes)
    assert [m.key for m in sent.misses] == [
        KEY_MISS,
        "verification:2026-11-25:0700:sugarbush:1d:raw_mean",
    ]
    assert sent.misses[0].tags == ["nw_upslope"]
    assert sent.misses[1].discussion == "discussion:AFD-BTV-2"
    text = sent.discussions["discussion:AFD-BTV-2"].text
    assert "Upslope showers" in text and "VFR" not in text
    assert sent.config["mountain.loon.wind_hold_mph.watch"] == 35
    assert backend.requests[0].system_prompt.startswith("# Role: knowledge-propose")


def test_review_reports_how_skill_moved_since_the_last_call(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_week(cfg)
    empty = KnowledgeProposals(knowledge=[], config_changes=[])
    backend = use_stub(monkeypatch, {"knowledge-propose": [empty, empty]})
    jobs.run_job(cfg, "knowledge-review", MONDAY)
    conn = connect(cfg)
    conn.execute(
        "UPDATE skill SET n = 12, bias_ratio = 0.7 WHERE mountain = 'sugarbush' AND tag = ''"
    )
    conn.close()
    summary = jobs.run_job(cfg, "knowledge-review", date(2026, 12, 7))
    assert summary["proposed"] == {"knowledge": [], "config_change": []}
    sent = KnowledgeInput.model_validate_json(backend.requests[1].prompt)
    assert sent.skill_changes[0].key == "skill:sugarbush:gfs_seamless:1d:all"
    cell = sent.skill_changes[0]
    assert (cell.n, cell.bias_ratio) == (12, 0.7)
    assert cell.previous is not None
    assert (cell.previous.n, cell.previous.bias_ratio) == (9, 0.62)


def test_review_without_evidence_makes_no_call(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = use_stub(monkeypatch, {})
    summary = jobs.run_job(cfg, "knowledge-review", MONDAY)
    assert summary["status"].startswith("skipped (no skill cells")
    assert backend.requests == []


def test_review_with_agents_disabled_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = config.load({"HAZELST_DB": str(tmp_path / "h.db")})
    conn = connect(c)
    db.migrate(conn)
    conn.close()
    seed_week(c)
    summary = jobs.run_job(c, "knowledge-review", MONDAY)
    assert summary["status"] == "skipped (agents are disabled (HAZELST_ENABLE_AGENTS))"


def test_failed_call_fails_the_run(cfg: config.Config, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_week(cfg)
    use_stub(monkeypatch, {"knowledge-propose": RuntimeError("boom")})
    with pytest.raises(jobs.JobError, match="knowledge-propose call 1 error"):
        jobs.run_job(cfg, "knowledge-review", MONDAY)


# --- owner decisions ------------------------------------------------------------------


def test_cli_lists_accepts_and_rejects(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_week(cfg)
    extra = KnowledgeProposal(mountain=None, tag=None, statement="Second.", evidence=[KEY_MISS])
    both = proposals().model_copy(update={"knowledge": [*proposals().knowledge, extra]})
    use_stub(monkeypatch, {"knowledge-propose": both})
    jobs.run_job(cfg, "knowledge-review", MONDAY)

    code, listed = run_cli(capsys, "knowledge", "list")
    assert code == 0
    assert [r["id"] for r in listed["knowledge"]] == [2, 1]
    assert [r["path"] for r in listed["config_change"]] == [
        "mountain.sugarbush.wind_hold_mph.watch"
    ]

    code, row = run_cli(capsys, "knowledge", "accept", "1")
    assert code == 0
    assert (row["status"], row["decided_by"]) == ("accepted", "owner")
    assert row["decided_at"]
    code, row = run_cli(capsys, "knowledge", "reject", "2")
    assert (code, row["status"], row["decided_by"]) == (0, "rejected", "owner")

    assert cli.main(["knowledge", "accept", "2"]) == 1
    assert "knowledge 2 is rejected, not proposed" in capsys.readouterr().err
    assert cli.main(["knowledge", "accept", "99"]) == 1
    assert "does not exist" in capsys.readouterr().err

    _, listed = run_cli(capsys, "knowledge", "list", "--status", "accepted")
    assert [r["statement"] for r in listed["knowledge"]] == [UPSLOPE]


def test_accepted_knowledge_reaches_the_report_write_prompt(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_week(cfg)
    use_stub(monkeypatch, {"knowledge-propose": proposals()})
    jobs.run_job(cfg, "knowledge-review", MONDAY)
    monkeypatch.setattr(jobs, "clock", lambda: NOON)
    jobs.run_job(cfg, "ingest-forecast", DAY, client=replay_client(recorded()))

    backend = use_stub(monkeypatch, {"report-write": [passing_draft, passing_draft]})
    jobs.run_job(cfg, "report-build", DAY)
    assert UPSLOPE not in backend.requests[0].system_prompt  # proposed, not accepted

    assert cli.main(["knowledge", "accept", "1"]) == 0
    jobs.run_job(cfg, "report-build", DAY, options={"force": True})
    prompt = backend.requests[1].system_prompt
    assert (
        "# Accepted local knowledge\n\nVerified by past forecasts and accepted by the owner:"
        f"\n\n- {UPSLOPE}" in prompt
    )


# --- config_change overlay ------------------------------------------------------------


def test_accepted_config_change_changes_the_effective_config(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_week(cfg)
    use_stub(monkeypatch, {"knowledge-propose": proposals()})
    jobs.run_job(cfg, "knowledge-review", MONDAY)
    path = "mountain.sugarbush.wind_hold_mph.watch"
    assert config.thresholds(config.load().mountains)[path] == 40  # proposed only

    code, row = run_cli(capsys, "config", "accept", "1")
    assert (code, row["status"], row["decided_by"], row["new"]) == (0, "accepted", "owner", 35)

    loaded = config.load()
    assert loaded.mountain("sugarbush").wind_hold_mph.watch == 35
    assert loaded.config_changes == (1,)
    code, printed = run_cli(capsys, "config", "--full")
    sugarbush = next(m for m in printed["mountains"] if m["key"] == "sugarbush")
    assert (code, sugarbush["wind_hold_mph"]["watch"], printed["config_changes"]) == (0, 35, [1])

    # A scheduler holds the cfg it started with; each run re-reads accepted rows.
    seen: list[config.Config] = []
    monkeypatch.setitem(jobs.JOBS, "backup", lambda ctx: seen.append(ctx.cfg))
    jobs.run_job(cfg, "backup", MONDAY)
    assert seen[0].mountain("sugarbush").wind_hold_mph.watch == 35
    assert '"step": "config", "config_changes": [1]' in capsys.readouterr().out


def test_config_reject_and_invalid_accept(
    cfg: config.Config, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = connect(cfg)
    conn.executemany(
        "INSERT INTO config_change (path, old, new, reason, status)"
        " VALUES (?, ?, ?, 'r', 'proposed')",
        [
            ("mountain.loon.wind_hold_mph.watch", "35", "45"),
            ("mountain.loon.wind_hold_mph.watch", "35", "60"),
        ],
    )
    conn.close()
    code, row = run_cli(capsys, "config", "reject", "1")
    assert (code, row["status"], row["decided_by"]) == (0, "rejected", "owner")
    assert cli.main(["config", "accept", "2"]) == 1
    assert "likely must be >= watch" in capsys.readouterr().err
    assert config.load().mountain("loon").wind_hold_mph.watch == 35


def test_apply_change_validates_path_and_value() -> None:
    roster = config.load({}).mountains
    changed = config.apply_change(roster, "mountain.loon.wind_hold_mph.likely", 52.0)
    assert next(m for m in changed if m.key == "loon").wind_hold_mph.likely == 52
    for path, new, message in [
        ("mountain.loon.summit_ft", 1, "not a threshold path"),
        ("mountain.stowe.wind_hold_mph.watch", 30, "unknown mountain"),
        ("mountain.loon.wind_hold_mph.watch", 30.5, "not a whole number"),
        ("mountain.loon.wind_hold_mph.watch", 0, "greater than 0"),
    ]:
        with pytest.raises(config.ConfigError, match=message):
            config.apply_change(roster, path, new)


def test_overlay_skips_a_row_that_no_longer_fits() -> None:
    base = config.load({})
    out = config.with_changes(
        base,
        [
            (1, "mountain.loon.wind_hold_mph.watch", 30),
            (2, "mountain.gone.wind_hold_mph.watch", 30),
            (3, "mountain.loon.wind_hold_mph.watch", 32),
        ],
    )
    assert out.mountain("loon").wind_hold_mph.watch == 32
    assert out.config_changes == (1, 3)
    assert config.with_changes(
        out, [(3, "mountain.loon.wind_hold_mph.watch", 32)]
    ) == out.model_copy(update={"config_changes": (3,)})


# --- replay -----------------------------------------------------------------------------


def test_replay_regenerates_with_accepted_knowledge(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(jobs, "clock", lambda: NOON)
    jobs.run_job(cfg, "ingest-forecast", DAY, client=replay_client(recorded()))

    def replayed(request: AgentRequest) -> Any:
        draft = passing_draft(request)
        return draft.model_copy(update={"headline": "Replayed headline"})

    backend = use_stub(monkeypatch, {"report-write": [passing_draft, replayed]})
    jobs.run_job(cfg, "report-build", DAY)
    conn = connect(cfg)
    conn.execute("INSERT INTO knowledge (statement, status) VALUES (?, 'accepted')", (UPSLOPE,))
    capsys.readouterr()

    code = cli.main(["replay", DAY.isoformat()])
    out = capsys.readouterr().out
    assert code == 0
    summary = json.loads(out[: out.index("\n}\n") + 2])
    assert summary["accepted_knowledge"] == 1
    assert summary["stored"]["version"] == 1
    assert summary["stored"]["headline"] == "Forecast numbers for the next five days"
    assert summary["replay"]["status"] == "published"
    assert summary["replay"]["headline"] == "Replayed headline"
    assert "(narratives are identical)" in out  # same body, new headline
    assert UPSLOPE in backend.requests[1].system_prompt
    assert conn.execute("SELECT count(*) FROM report").fetchone()[0] == 1  # nothing published
    run_id, status = conn.execute(
        "SELECT run_id, status FROM agent_call ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (run_id, status) == (None, "ok")


def test_replay_without_a_report(cfg: config.Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["replay", "2026-11-01"]) == 1
    assert "no stored report for 2026-11-01" in capsys.readouterr().err
