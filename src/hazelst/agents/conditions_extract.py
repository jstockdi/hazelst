"""`conditions-extract`: read a resort page whose code adapter failed.

Runs only for resorts whose content may go to Claude: never for the Boyne
resorts (Loon, Sunday River, Sugarloaf), which publish no `ai-input` content
signal (data-sources.md, technical-design.md §17 row 8). The guard is checked
here, before any call, by preset and by mountain key.

The result is stored by the caller as an `observation` with
`source = 'resort_llm'`; it never replaces an adapter row.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from hazelst.agents.client import AgentClient, AgentResult, day_bounds_utc
from hazelst.agents.prompts import role_prompt
from hazelst.agents.schemas import ConditionsExtract
from hazelst.config import Mountain
from hazelst.sources.base import Fetched
from hazelst.sources.resorts import adapter
from hazelst.sources.resorts.source import extraction_allowed

ROLE = "conditions-extract"
MAX_TEXT = 20_000  # characters of page text sent
MIN_TEXT = 200  # shorter text (a bot challenge, an empty shell) is not worth a call


class ExtractionNotAllowed(RuntimeError):
    """The resort's content must not be sent to Claude, or the page has no text."""


class ExtractInput(BaseModel):
    """The user message: matches the role prompt's input description."""

    model_config = ConfigDict(extra="forbid")

    mountain: str
    fetched_at: str
    page_text: str


def called_today(conn: sqlite3.Connection, mountain: str, now: datetime) -> bool:
    """Whether `conditions-extract` already ran for `mountain` today (at most once a day)."""
    start, end = day_bounds_utc(now)
    row = conn.execute(
        "SELECT 1 FROM agent_call WHERE role = ? AND created_at >= ? AND created_at < ?"
        " AND json_extract(input, '$.mountain') = ? LIMIT 1",
        (ROLE, start, end, mountain),
    ).fetchone()
    return row is not None


def build_input(mountain: Mountain, fetched: Fetched) -> ExtractInput:
    """The extraction input for one failed page; raises `ExtractionNotAllowed` when barred."""
    if not extraction_allowed(mountain) or fetched.meta.get("preset") == "boyne_reportpal":
        raise ExtractionNotAllowed(f"{mountain.key}: content is code-only (no ai-input signal)")
    if fetched.http_status != 200:
        raise ExtractionNotAllowed(f"{mountain.key}: HTTP {fetched.http_status}, no page")
    text = adapter.page_text(fetched.body, limit=MAX_TEXT)
    if len(text) < MIN_TEXT:
        raise ExtractionNotAllowed(f"{mountain.key}: page has {len(text)} characters of text")
    return ExtractInput(mountain=mountain.key, fetched_at=fetched.fetched_at, page_text=text)


def extract(
    client: AgentClient,
    mountain: Mountain,
    fetched: Fetched,
    *,
    run_id: int | None = None,
) -> AgentResult[ConditionsExtract]:
    """Call `conditions-extract` on one failed page.

    Raises `ExtractionNotAllowed` before any call for a barred resort, and the
    client's `AgentUnavailable` / `AgentCallFailed` otherwise.
    """
    data = build_input(mountain, fetched)
    return client.call(
        ROLE,
        system_prompt=role_prompt(ROLE),
        data=data,
        output_model=ConditionsExtract,
        run_id=run_id,
    )
