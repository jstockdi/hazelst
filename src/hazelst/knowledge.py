"""`knowledge` and `config_change` rows: validate proposals, store them, record decisions.

`knowledge-propose` output never takes effect on its own. The `knowledge-review`
job stores each valid item with `status = 'proposed'`; the owner accepts or
rejects it from the CLI (`hazelst knowledge accept <id>`, `hazelst config
accept <id>`). Accepted statements go into the report system prompt
(`narrative.accepted_knowledge`); accepted config changes overlay the roster
(`config.effective`).

A proposal is dropped, not stored, when it names an unknown mountain, repeats
a current statement or pending change, cites no evidence key from the input,
or (for a config change) names a path that is not a numeric threshold, keeps
the current value, or would make the roster invalid.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from hazelst import config
from hazelst.agents.knowledge_propose import KnowledgeInput
from hazelst.agents.schemas import ConfigChangeProposal, KnowledgeProposal, KnowledgeProposals

PROPOSED_BY = "knowledge-propose"
OWNER = "owner"
Table = Literal["knowledge", "config_change"]
Decision = Literal["accepted", "rejected"]


class DecisionError(ValueError):
    """The row cannot take this decision (unknown id, already decided, invalid change)."""


@dataclass
class Validated:
    knowledge: list[KnowledgeProposal] = field(default_factory=list)
    config_changes: list[tuple[ConfigChangeProposal, float]] = field(default_factory=list)  # old
    dropped: list[dict[str, Any]] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def validate(
    proposals: KnowledgeProposals,
    data: KnowledgeInput,
    cfg: config.Config,
    conn: sqlite3.Connection,
) -> Validated:
    out = Validated()
    keys = data.evidence_keys()
    mountains = {m.key for m in cfg.mountains}
    seen = {_norm(r[0]) for r in conn.execute("SELECT statement FROM knowledge")}
    for item in proposals.knowledge:
        reason = None
        if item.mountain is not None and item.mountain not in mountains:
            reason = f"unknown mountain {item.mountain!r}"
        elif _norm(item.statement) in seen:
            reason = "repeats a current statement"
        elif not keys.intersection(item.evidence):
            reason = "no evidence key from the input"
        if reason:
            out.dropped.append({"knowledge": item.statement, "reason": reason})
            continue
        seen.add(_norm(item.statement))
        out.knowledge.append(item)

    current = config.thresholds(cfg.mountains)
    pending = {
        (r[0], config.json_value(r[1]))
        for r in conn.execute("SELECT path, new FROM config_change WHERE status = 'proposed'")
    }
    for change in proposals.config_changes:
        reason = None
        if change.path not in current:
            reason = "not a threshold path"
        elif change.new == current[change.path]:
            reason = "keeps the current value"
        elif (change.path, change.new) in pending:
            reason = "repeats a pending change"
        elif not keys.intersection(change.evidence):
            reason = "no evidence key from the input"
        else:
            try:
                config.apply_change(cfg.mountains, change.path, change.new)
            except config.ConfigError as exc:
                reason = str(exc)
        if reason:
            out.dropped.append({"config_change": change.path, "new": change.new, "reason": reason})
            continue
        pending.add((change.path, change.new))
        out.config_changes.append((change, current[change.path]))
    return out


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def store(
    conn: sqlite3.Connection,
    valid: Validated,
    *,
    call_id: int,
    run_id: int | None,
    now: str,
) -> dict[str, list[int]]:
    """Insert the proposed rows; returns their ids. The caller holds the transaction."""
    ids: dict[str, list[int]] = {"knowledge": [], "config_change": []}
    for item in valid.knowledge:
        evidence = {"keys": item.evidence, "agent_call": call_id, "run_id": run_id}
        cur = conn.execute(
            "INSERT INTO knowledge (mountain, tag, statement, evidence, status, proposed_by,"
            " created_at) VALUES (?, ?, ?, ?, 'proposed', ?, ?)",
            (item.mountain, item.tag, item.statement, json.dumps(evidence), PROPOSED_BY, now),
        )
        assert cur.lastrowid is not None
        ids["knowledge"].append(cur.lastrowid)
    for change, old in valid.config_changes:
        evidence = {"keys": change.evidence, "agent_call": call_id, "run_id": run_id}
        cur = conn.execute(
            "INSERT INTO config_change (path, old, new, reason, evidence, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'proposed', ?)",
            (
                change.path,
                json.dumps(_number(old)),
                json.dumps(_number(change.new)),
                change.reason,
                json.dumps(evidence),
                now,
            ),
        )
        assert cur.lastrowid is not None
        ids["config_change"].append(cur.lastrowid)
    return ids


def rows(conn: sqlite3.Connection, table: Table, status: str | None = None) -> list[dict[str, Any]]:
    """Rows of `table`, newest first; `status` filters (None = every row)."""
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    out = []
    for r in conn.execute(f"SELECT * FROM {table} {where} ORDER BY id DESC", params):
        row = dict(r)
        for col in ("evidence", "old", "new"):
            if row.get(col) is not None:
                row[col] = config.json_value(row[col])
        out.append(row)
    return out


def decide(
    conn: sqlite3.Connection,
    cfg: config.Config,
    table: Table,
    row_id: int,
    decision: Decision,
    *,
    now: str,
) -> dict[str, Any]:
    """Accept or reject one proposed row as the owner; returns the updated row.

    Accepting a config change first checks it still applies to the effective
    config, so an accepted row can never make the roster invalid.
    """
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise DecisionError(f"{table} {row_id} does not exist")
    if row["status"] != "proposed":
        raise DecisionError(f"{table} {row_id} is {row['status']}, not proposed")
    if table == "config_change" and decision == "accepted":
        try:
            config.apply_change(
                config.effective(cfg, conn).mountains, row["path"], config.json_value(row["new"])
            )
        except config.ConfigError as exc:
            raise DecisionError(f"config_change {row_id}: {exc}") from exc
    conn.execute(
        f"UPDATE {table} SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
        (decision, OWNER, now, row_id),
    )
    return next(r for r in rows(conn, table) if r["id"] == row_id)
