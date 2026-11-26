"""`knowledge-propose`: the week's evidence -> `KnowledgeProposals`.

`gather` reads the evidence (read-only): `skill` cells and how each moved
since the previous `knowledge-propose` call, the week's MISS verifications
with their tags and the discussion each forecast was written with, the
current `knowledge` statements, and the thresholds a `config_change` may set.
`propose` makes the call. The `knowledge-review` job validates the output and
stores the proposed rows (`knowledge.py`); this module writes only
`agent_call`.

How a cell moved is measured against the previous call's input: each call
stores its `skill_changes` in `agent_call.input`, so that row is the previous
week's snapshot and no extra table is needed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from hazelst.agents.client import AgentClient, AgentResult
from hazelst.agents.prompts import system_prompt
from hazelst.agents.schemas import KnowledgeProposals
from hazelst.config import Config, thresholds
from hazelst.sources import discussions

ROLE = "knowledge-propose"
WEEK_DAYS = 7
MIN_N = 3  # a cell needs at least three verified snow days to be evidence
REPORT_TIME = dtime(7, 0)  # a miss is paired with the discussions the 7:00 report saw


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillStats(_Input):
    n: int
    bias_ratio: float | None
    mae_in: float | None
    hit_rate: float | None


class SkillCell(_Input):
    key: str  # evidence key: skill:<mountain>:<model>:<lead_bucket>:<tag or all>
    mountain: str
    model: str
    lead_bucket: str
    tag: str  # '' = every day
    n: int
    bias_ratio: float | None
    mae_in: float | None
    hit_rate: float | None
    previous: SkillStats | None  # the cell in the previous call's input; None = new


class Miss(_Input):
    key: str  # evidence key: verification:<date>:<edition>:<mountain>:<window>:<method>
    date: date
    edition: str
    mountain: str
    window: str
    forecast_in: float | None
    observed_in: float | None
    observed_source: str | None
    tags: list[str]
    discussion: str | None  # key into `discussions`


class Discussion(_Input):
    key: str  # discussion:<product_id>
    office: str
    issued_at: str
    text: str  # Key Messages + Discussion sections


class KnowledgeItem(_Input):
    id: int
    mountain: str | None
    tag: str | None
    statement: str
    status: str


class KnowledgeInput(_Input):
    """The user message: matches the role prompt's input description."""

    week_start: date
    week_end: date
    skill_changes: list[SkillCell]
    misses: list[Miss]
    discussions: dict[str, Discussion]
    knowledge: list[KnowledgeItem]
    config: dict[str, float]  # threshold path -> current value

    def evidence_keys(self) -> set[str]:
        keys = {c.key for c in self.skill_changes} | {m.key for m in self.misses}
        return keys | set(self.discussions)


def skill_key(mountain: str, model: str, bucket: str, tag: str) -> str:
    return f"skill:{mountain}:{model}:{bucket}:{tag or 'all'}"


def previous_snapshot(conn: sqlite3.Connection) -> dict[str, SkillStats]:
    """Skill cells from the newest `knowledge-propose` call's input, by key."""
    row = conn.execute(
        "SELECT input FROM agent_call WHERE role = ? ORDER BY id DESC LIMIT 1", (ROLE,)
    ).fetchone()
    if row is None or row[0] is None:
        return {}
    try:
        cells = json.loads(row[0]).get("skill_changes", [])
        return {
            c["key"]: SkillStats(
                n=c["n"], bias_ratio=c["bias_ratio"], mae_in=c["mae_in"], hit_rate=c["hit_rate"]
            )
            for c in cells
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def skill_cells(conn: sqlite3.Connection) -> list[SkillCell]:
    before = previous_snapshot(conn)
    rows = conn.execute(
        "SELECT mountain, model, lead_bucket, tag, n, bias_ratio, mae_in, hit_rate FROM skill"
        " WHERE n >= ? ORDER BY mountain, model, lead_bucket, tag",
        (MIN_N,),
    ).fetchall()
    out = []
    for r in rows:
        key = skill_key(r[0], r[1], r[2], r[3] or "")
        out.append(
            SkillCell(
                key=key,
                mountain=r[0],
                model=r[1],
                lead_bucket=r[2],
                tag=r[3] or "",
                n=r[4],
                bias_ratio=r[5],
                mae_in=r[6],
                hit_rate=r[7],
                previous=before.get(key),
            )
        )
    return out


def _discussion(conn: sqlite3.Connection, office: str, day: date, tz: str) -> Discussion | None:
    """The office's newest AFD issued by 7:00 AM local on `day`."""
    as_of = datetime.combine(day, REPORT_TIME, ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))
    row = conn.execute(
        "SELECT product_id, issued_at, text FROM discussion WHERE office = ? AND kind = 'AFD'"
        " AND issued_at <= ? ORDER BY issued_at DESC LIMIT 1",
        (office, as_of.isoformat(timespec="seconds")),
    ).fetchone()
    if row is None:
        return None
    return Discussion(
        key=f"discussion:{row[0]}",
        office=office,
        issued_at=row[1],
        text=discussions.summary(row[2] or ""),
    )


def week_misses(
    conn: sqlite3.Connection, cfg: Config, start: date, end: date
) -> tuple[list[Miss], dict[str, Discussion]]:
    """Raw-mean 1d MISSes scored for days `start`..`end`, with each day's discussion."""
    offices = {m.key: m.nws.office for m in cfg.mountains}
    rows = conn.execute(
        "SELECT for_date, edition, mountain, window, method, forecast_in, observed_in,"
        " observed_source, tags FROM verification WHERE result = 'MISS'"
        " AND method = 'raw_mean' AND window = '1d' AND for_date BETWEEN ? AND ?"
        " ORDER BY for_date, mountain, edition",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    misses: list[Miss] = []
    found: dict[str, Discussion] = {}
    cache: dict[tuple[str, date], Discussion | None] = {}
    for r in rows:
        day = date.fromisoformat(r[0])
        office = offices.get(r[2])
        disc = None
        if office is not None:
            if (office, day) not in cache:
                cache[(office, day)] = _discussion(conn, office, day, cfg.timezone)
            disc = cache[(office, day)]
        if disc is not None:
            found[disc.key] = disc
        misses.append(
            Miss(
                key=f"verification:{r[0]}:{r[1]}:{r[2]}:{r[3]}:{r[4]}",
                date=day,
                edition=r[1],
                mountain=r[2],
                window=r[3],
                forecast_in=r[5],
                observed_in=r[6],
                observed_source=r[7],
                tags=json.loads(r[8]) if r[8] else [],
                discussion=disc.key if disc else None,
            )
        )
    return misses, found


def current_knowledge(conn: sqlite3.Connection) -> list[KnowledgeItem]:
    rows = conn.execute(
        "SELECT id, mountain, tag, statement, status FROM knowledge"
        " WHERE status IN ('accepted', 'proposed') ORDER BY id"
    ).fetchall()
    return [
        KnowledgeItem(id=r[0], mountain=r[1], tag=r[2], statement=r[3], status=r[4]) for r in rows
    ]


def gather(conn: sqlite3.Connection, cfg: Config, for_date: date) -> KnowledgeInput:
    """The evidence for a run on `for_date`.

    Day X is scored at 9 PM on X + 1, so a run on D sees days up to D - 2;
    the week is D - 8 .. D - 2, and consecutive Monday runs cover every day.
    """
    end = for_date - timedelta(days=2)
    start = end - timedelta(days=WEEK_DAYS - 1)
    misses, found = week_misses(conn, cfg, start, end)
    return KnowledgeInput(
        week_start=start,
        week_end=end,
        skill_changes=skill_cells(conn),
        misses=misses,
        discussions=found,
        knowledge=current_knowledge(conn),
        config=thresholds(cfg.mountains),
    )


def propose(
    client: AgentClient, data: KnowledgeInput, *, run_id: int | None = None
) -> AgentResult[KnowledgeProposals]:
    return client.call(
        ROLE,
        system_prompt=system_prompt(ROLE),
        data=data,
        output_model=KnowledgeProposals,
        run_id=run_id,
    )
