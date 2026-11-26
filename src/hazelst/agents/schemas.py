"""Output schemas for the Claude roles (agent-runs.md, Claude roles).

Each model is both the JSON schema passed to the SDK as `output_format` and the
validator applied to `ResultMessage.structured_output`. `extra="forbid"` makes
the generated schema closed (`additionalProperties: false`).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["low", "medium", "high"]


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BestBet(_Schema):
    mountain: str = Field(description="Mountain key from the digest, e.g. 'sugarbush'")
    day: date = Field(description="Day from the digest's forecast days, YYYY-MM-DD")
    why: str = Field(min_length=1, description="One or two sentences grounded in the digest")


class ReportDraft(_Schema):
    """Output of `report-write` and `report-revise`."""

    headline: str = Field(min_length=1, description="One line, at most 14 words")
    best_bet: BestBet
    storm: str = Field(
        min_length=1,
        description="The storm section: what happens, when, how confident, which sources",
    )
    mountains: dict[str, str] = Field(
        description="One entry per mountain key in the digest: key -> 1-3 sentences"
    )
    cautions: list[str] = Field(
        description="One item per wind hold, mixing, freezing rain, or active NWS alert"
    )


class ConditionsExtract(_Schema):
    """Output of `conditions-extract`: one resort report page whose adapter failed."""

    new_24h_in: float | None = Field(ge=0, description="New snow in the last 24 h, inches")
    base_in: float | None = Field(ge=0, description="Base depth, inches")
    trails_open: int | None = Field(ge=0)
    trails_total: int | None = Field(ge=0)
    lifts_open: int | None = Field(ge=0)
    confidence: Confidence = Field(description="How clearly the page states these values")


class KnowledgeProposal(_Schema):
    mountain: str | None = Field(description="Mountain key, or null for a regional statement")
    tag: str | None = Field(description="Pattern tag the statement is about, or null")
    statement: str = Field(
        min_length=1,
        description="Plain-language statement, e.g. 'NW upslope: Sugarbush gets 1.6x model mean'",
    )
    evidence: list[str] = Field(
        min_length=1, description="Verification keys or run ids from the input that support it"
    )


class ConfigChangeProposal(_Schema):
    path: str = Field(
        min_length=1, description="Dotted config path, e.g. 'mountain.loon.wind_hold_mph.watch'"
    )
    new: float = Field(description="Proposed numeric value")
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class KnowledgeProposals(_Schema):
    """Output of `knowledge-propose`; every item waits for owner approval."""

    knowledge: list[KnowledgeProposal]
    config_changes: list[ConfigChangeProposal]


ROLE_OUTPUT: dict[str, type[BaseModel]] = {
    "report-write": ReportDraft,
    "report-revise": ReportDraft,
    "conditions-extract": ConditionsExtract,
    "knowledge-propose": KnowledgeProposals,
}


def output_format(model: type[BaseModel]) -> dict[str, Any]:
    """The SDK `output_format` for a role's output model."""
    return {"type": "json_schema", "schema": model.model_json_schema()}
