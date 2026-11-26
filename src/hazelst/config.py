"""Effective configuration: environment variables plus the mountain roster.

Accepted `config_change` rows overlay these values once the database exists
(see technical-design.md §7).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MOUNTAINS = PROJECT_ROOT / "config" / "mountains.json"
DEFAULT_USER_AGENT = "hazelst-forecast (github.com/jstockdi/hazelst)"
TIMEZONE = "America/New_York"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """Raised when the environment or roster is invalid."""


class NwsRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    office: str = Field(pattern=r"^[A-Z]{3}$")
    grid: str | None = None  # "BTV/108,19"; resolved from api.weather.gov/points and cached


class WindHold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    watch: int = Field(gt=0)
    likely: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> WindHold:
        if self.likely < self.watch:
            raise ValueError("wind_hold_mph.likely must be >= watch")
        return self


class ReportAdapter(BaseModel):
    """A resort report adapter: a preset name and its URL parameters (data-sources.md)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    params: dict[str, str | int] = Field(default_factory=dict)

    def __hash__(self) -> int:  # params is a dict; keep Mountain hashable
        return hash((self.preset, tuple(sorted(self.params.items()))))


class Mountain(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z_]*$")
    name: str
    state: str = Field(pattern=r"^(VT|NH|ME)$")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    summit_ft: int = Field(gt=0)
    base_ft: int = Field(gt=0)
    nws: NwsRef
    report_adapter: ReportAdapter | None = None  # null disables the adapter
    wind_hold_mph: WindHold
    season_open: date | None = None
    favored_by: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _summit_above_base(self) -> Mountain:
        if self.summit_ft <= self.base_ft:
            raise ValueError(f"{self.key}: summit_ft must be above base_ft")
        return self


Effort = Literal["low", "medium", "high", "xhigh", "max"]
AGENT_ROLES = ("report-write", "report-revise", "conditions-extract", "knowledge-propose")


class AgentRole(BaseModel):
    """Model and effort for one Claude role (agent-runs.md, Claude roles)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    effort: Effort


DEFAULT_AGENT_ROLES: dict[str, AgentRole] = {
    "report-write": AgentRole(model="claude-opus-5-5", effort="high"),
    "report-revise": AgentRole(model="claude-opus-5-5", effort="medium"),
    "conditions-extract": AgentRole(model="claude-opus-5-5", effort="low"),
    "knowledge-propose": AgentRole(model="claude-opus-5-5", effort="high"),
}


def _role_env_suffix(role: str) -> str:
    return role.upper().replace("-", "_")


class Settings(BaseModel):
    """Values read from the environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    db_path: Path = Path("hazelst.db")
    mountains_path: Path = DEFAULT_MOUNTAINS
    nws_user_agent: str = DEFAULT_USER_AGENT
    enable_agents: bool = False
    agent_daily_calls: int = Field(default=10, ge=0)
    agent_roles: dict[str, AgentRole] = Field(default_factory=lambda: dict(DEFAULT_AGENT_ROLES))
    claude_token: SecretStr | None = None

    @field_validator("agent_roles")
    @classmethod
    def _every_role(cls, value: dict[str, AgentRole]) -> dict[str, AgentRole]:
        if set(value) != set(AGENT_ROLES):
            raise ValueError(f"agent_roles must name exactly {list(AGENT_ROLES)}")
        return value

    @field_validator("nws_user_agent")
    @classmethod
    def _user_agent_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("NWS_USER_AGENT must not be blank")
        return value

    @model_validator(mode="after")
    def _token_when_agents_enabled(self) -> Settings:
        if self.enable_agents and self.claude_token is None:
            raise ValueError("HAZELST_ENABLE_AGENTS is true but CLAUDE_CODE_OAUTH_TOKEN is not set")
        return self

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to print or log: the token is reported as set or unset."""
        data = self.model_dump(mode="json", exclude={"claude_token"})
        data["claude_token"] = "set" if self.claude_token is not None else "unset"
        return data


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    settings: Settings
    mountains: tuple[Mountain, ...]
    timezone: str = TIMEZONE

    def mountain(self, key: str) -> Mountain:
        for mountain in self.mountains:
            if mountain.key == key:
                return mountain
        raise KeyError(key)


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    if "HAZELST_DB" in env:
        values["db_path"] = Path(env["HAZELST_DB"])
    if "HAZELST_MOUNTAINS" in env:
        values["mountains_path"] = Path(env["HAZELST_MOUNTAINS"])
    if "NWS_USER_AGENT" in env:
        values["nws_user_agent"] = env["NWS_USER_AGENT"]
    if "HAZELST_ENABLE_AGENTS" in env:
        values["enable_agents"] = _parse_bool("HAZELST_ENABLE_AGENTS", env["HAZELST_ENABLE_AGENTS"])
    if "HAZELST_AGENT_DAILY_CALLS" in env:
        values["agent_daily_calls"] = _parse_int(
            "HAZELST_AGENT_DAILY_CALLS", env["HAZELST_AGENT_DAILY_CALLS"]
        )
    roles = dict(DEFAULT_AGENT_ROLES)
    for role in AGENT_ROLES:
        suffix = _role_env_suffix(role)
        overrides = {
            field: env[f"HAZELST_AGENT_{field.upper()}_{suffix}"]
            for field in ("model", "effort")
            if f"HAZELST_AGENT_{field.upper()}_{suffix}" in env
        }
        if overrides:
            try:
                roles[role] = AgentRole.model_validate({**roles[role].model_dump(), **overrides})
            except ValueError as exc:
                raise ConfigError(f"{role}: {exc}") from exc
    values["agent_roles"] = roles
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        values["claude_token"] = SecretStr(token)
    try:
        return Settings(**values)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_mountains(path: Path) -> tuple[Mountain, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path} must be a non-empty JSON list")
    try:
        mountains = tuple(Mountain.model_validate(item) for item in raw)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    keys = [m.key for m in mountains]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ConfigError(f"{path}: duplicate mountain keys {duplicates}")
    return mountains


def load(env: Mapping[str, str] | None = None) -> Config:
    settings = settings_from_env(env)
    return Config(settings=settings, mountains=load_mountains(settings.mountains_path))
