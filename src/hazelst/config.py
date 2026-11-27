"""Effective configuration: environment variables plus the mountain roster.

Accepted `config_change` rows overlay the roster's numeric thresholds once the
database exists (technical-design.md §7). A row names a dotted path, for
example `mountain.loon.wind_hold_mph.watch`; `THRESHOLD_FIELDS` lists the
fields a path may name.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("hazelst.config")

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
    snocountry_id: str | None = Field(default=None, pattern=r"^\d+$")  # null: no SnoCountry rows
    report_adapter: ReportAdapter | None = None  # null disables the adapter
    wind_hold_mph: WindHold
    season_open: date | None = None
    favored_by: tuple[str, ...] = ()
    # Ground elevation of the center of the NOHRSC pixel containing (lat, lon);
    # `hazelst elevations nohrsc-pixels` looks it up once.
    nohrsc_pixel_elev_ft: int | None = Field(default=None, gt=0)

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
    scheduler: bool = False  # run the job schedule inside `hazelst serve` (on in production)
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
    config_changes: tuple[int, ...] = ()  # accepted config_change ids overlaid on the roster

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
    if "HAZELST_SCHEDULER" in env:
        values["scheduler"] = _parse_bool("HAZELST_SCHEDULER", env["HAZELST_SCHEDULER"])
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
    """The effective config: environment, roster, and accepted `config_change` rows."""
    settings = settings_from_env(env)
    cfg = Config(settings=settings, mountains=load_mountains(settings.mountains_path))
    return with_changes(cfg, _db_changes(settings.db_path))


# --- config_change overlay ------------------------------------------------------

# Roster fields a `config_change` path may name: `mountain.<key>.<field>`.
THRESHOLD_FIELDS = ("wind_hold_mph.watch", "wind_hold_mph.likely")


def thresholds(mountains: Iterable[Mountain]) -> dict[str, float]:
    """Every path a `config_change` may set, with its current value."""
    out: dict[str, float] = {}
    for m in mountains:
        for name in THRESHOLD_FIELDS:
            group, field = name.split(".")
            out[f"mountain.{m.key}.{name}"] = getattr(getattr(m, group), field)
    return out


def apply_change(mountains: tuple[Mountain, ...], path: str, new: object) -> tuple[Mountain, ...]:
    """The roster with one threshold set; ConfigError when the path or value is invalid."""
    parts = path.split(".")
    if len(parts) != 4 or parts[0] != "mountain" or ".".join(parts[2:]) not in THRESHOLD_FIELDS:
        raise ConfigError(
            f"{path!r} is not a threshold path (mountain.<key>.{'|'.join(THRESHOLD_FIELDS)})"
        )
    key, group, field = parts[1], parts[2], parts[3]
    if not any(m.key == key for m in mountains):
        raise ConfigError(f"{path!r}: unknown mountain {key!r}")
    if isinstance(new, bool) or not isinstance(new, int | float) or float(new) != int(new):
        raise ConfigError(f"{path!r}: {new!r} is not a whole number")
    out = []
    for m in mountains:
        if m.key == key:
            data = m.model_dump()
            data[group][field] = int(new)
            try:
                m = Mountain.model_validate(data)
            except ValueError as exc:
                raise ConfigError(f"{path!r} = {new!r}: {exc}") from exc
        out.append(m)
    return tuple(out)


def with_changes(cfg: Config, changes: Iterable[tuple[int, str, object]]) -> Config:
    """`cfg` overlaid by accepted changes `(id, path, new)`, applied in order.

    Each change sets an absolute value, so applying the same rows again is a
    no-op. A row that no longer fits the roster (a removed mountain) is
    skipped and logged rather than stopping every job.
    """
    mountains = cfg.mountains
    applied = []
    for change_id, path, new in changes:
        try:
            mountains = apply_change(mountains, path, new)
        except ConfigError as exc:
            log.warning("config_change %s skipped: %s", change_id, exc)
            continue
        applied.append(change_id)
    return cfg.model_copy(update={"mountains": mountains, "config_changes": tuple(applied)})


def json_value(raw: object) -> object:
    """A JSON column's value. Columns typed `JSON` have numeric affinity in SQLite, so a
    stored number comes back as a number and only text needs decoding."""
    return json.loads(raw) if isinstance(raw, str | bytes) else raw


def accepted_changes(conn: sqlite3.Connection) -> list[tuple[int, str, object]]:
    """Accepted `config_change` rows, in the order they were accepted."""
    try:
        rows = conn.execute(
            "SELECT id, path, new FROM config_change WHERE status = 'accepted'"
            " ORDER BY decided_at, id"
        ).fetchall()
    except sqlite3.OperationalError:  # not migrated yet
        return []
    return [(int(r[0]), str(r[1]), json_value(r[2])) for r in rows]


def effective(cfg: Config, conn: sqlite3.Connection) -> Config:
    """`cfg` with the accepted `config_change` rows in `conn` overlaid."""
    return with_changes(cfg, accepted_changes(conn))


def _db_changes(path: Path) -> list[tuple[int, str, object]]:
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return accepted_changes(conn)
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()
