"""The one place Claude is called: SDK options, model and effort, logging, daily call cap.

Call conventions follow agent-runs.md ("Call conventions") as verified by the
J01 spike: one `query()` per call through the Claude Agent SDK, structured
output from the role's Pydantic model, no tools, no filesystem settings, a
fresh writable `HOME`, and the `CLAUDE_CODE_OAUTH_TOKEN` token.

`AgentClient.call` checks `HAZELST_ENABLE_AGENTS` and the daily call cap, runs
the call through a `Backend` (the SDK, or `StubBackend` in tests), validates
the output, and writes one `agent_call` row per attempted call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, SecretStr, ValidationError

from hazelst import __version__
from hazelst.agents.schemas import output_format
from hazelst.config import AGENT_ROLES, TIMEZONE, Effort, Settings

log = logging.getLogger("hazelst.agents")

CALL_TIMEOUT_S = 600.0
MAX_TURNS = 5
STDERR_TAIL = 20

CallStatus = Literal["ok", "error", "invalid"]


class AgentError(Exception):
    """Base class for agent failures; callers fall back to `numbers_only`."""


class AgentUnavailable(AgentError):
    """The call was not attempted (agents disabled, cap reached, bad environment).

    No `agent_call` row is written, so the attempt does not count toward the cap.
    """


class AgentCallFailed(AgentError):
    """The call was attempted and logged, but produced no valid output."""

    def __init__(self, message: str, *, call_id: int, status: CallStatus) -> None:
        super().__init__(message)
        self.call_id = call_id
        self.status = status


@dataclass(frozen=True)
class AgentRequest:
    role: str
    model: str
    effort: Effort
    system_prompt: str
    prompt: str
    output_schema: dict[str, Any]
    timeout_s: float = CALL_TIMEOUT_S


@dataclass(frozen=True)
class AgentResponse:
    """The parts of the SDK `ResultMessage` the app uses."""

    subtype: str
    is_error: bool
    structured_output: Any
    result_text: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    total_cost_usd: float | None = None
    num_turns: int | None = None
    errors: tuple[str, ...] = ()


class Backend(Protocol):
    sdk_version: str

    def run(self, request: AgentRequest) -> AgentResponse: ...


@dataclass(frozen=True)
class AgentResult[T: BaseModel]:
    call_id: int
    output: T


# --- SDK backend ----------------------------------------------------------------


class SdkBackend:
    """Runs a call through `claude_agent_sdk.query` with the J01 options."""

    def __init__(self, token: SecretStr | None) -> None:
        if token is None:
            raise AgentUnavailable("CLAUDE_CODE_OAUTH_TOKEN is not set")
        if os.environ.get("ANTHROPIC_API_KEY"):
            # The CLI prefers an API key over the subscription token.
            raise AgentUnavailable("ANTHROPIC_API_KEY must not be set; the token is used instead")
        import claude_agent_sdk

        self._token = token
        self.sdk_version: str = claude_agent_sdk.__version__

    def options(self, request: AgentRequest, home: str, stderr: Callable[[str], None]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            model=request.model,
            effort=request.effort,
            system_prompt=request.system_prompt,
            output_format={"type": "json_schema", "schema": request.output_schema},
            tools=[],
            setting_sources=[],
            skills=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            verbatim_prompts=True,
            max_turns=MAX_TURNS,
            extra_args={"no-session-persistence": None},
            cwd=home,
            env={
                "HOME": home,
                "CLAUDE_CODE_OAUTH_TOKEN": self._token.get_secret_value(),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_AGENT_SDK_CLIENT_APP": f"hazelst/{__version__}",
            },
            stderr=stderr,
        )

    async def _query(self, request: AgentRequest, home: str, stderr: list[str]) -> AgentResponse:
        from claude_agent_sdk import ResultMessage, query

        def keep(line: str) -> None:
            stderr.append(line)
            del stderr[:-STDERR_TAIL]

        result: ResultMessage | None = None
        try:
            async for message in query(
                prompt=request.prompt, options=self.options(request, home, keep)
            ):
                if isinstance(message, ResultMessage):
                    result = message
        except Exception:
            # query() raises after an error result; the result carries the detail.
            if result is None:
                raise
        if result is None:
            raise RuntimeError("the SDK returned no result message")
        return AgentResponse(
            subtype=result.subtype,
            is_error=result.is_error,
            structured_output=result.structured_output,
            result_text=result.result,
            usage=result.usage or {},
            total_cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
            errors=tuple(result.errors or ()),
        )

    def run(self, request: AgentRequest) -> AgentResponse:
        # A fresh HOME per call under the tmp dir (the /tmp tmpfs in the container):
        # writable, empty of user settings and credentials, and gone afterwards.
        home = tempfile.mkdtemp(prefix="hazelst-claude-")
        stderr: list[str] = []
        try:
            return asyncio.run(
                asyncio.wait_for(self._query(request, home, stderr), timeout=request.timeout_s)
            )
        except TimeoutError:
            raise TimeoutError(f"no result after {request.timeout_s:.0f} s") from None
        except Exception as exc:
            tail = " | ".join(stderr[-5:])
            detail = f"{type(exc).__name__}: {exc}" + (f" [cli: {tail}]" if tail else "")
            raise RuntimeError(detail) from exc
        finally:
            shutil.rmtree(home, ignore_errors=True)


# --- client ---------------------------------------------------------------------


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_json(data: BaseModel | Mapping[str, Any]) -> str:
    """The user message: the role input as JSON (also what `input_sha` hashes)."""
    if isinstance(data, BaseModel):
        return data.model_dump_json()
    return json.dumps(data, ensure_ascii=False, default=str)


def day_bounds_utc(now: datetime, tz: str = TIMEZONE) -> tuple[str, str]:
    """UTC ISO bounds of the local (America/New_York) day containing `now`."""
    zone = ZoneInfo(tz)
    local_day = now.astimezone(zone).date()
    start = datetime.combine(local_day, dtime(0), tzinfo=zone)
    end = datetime.combine(local_day + timedelta(days=1), dtime(0), tzinfo=zone)
    return (
        start.astimezone(UTC).isoformat(timespec="seconds"),
        end.astimezone(UTC).isoformat(timespec="seconds"),
    )


class AgentClient:
    """Calls Claude roles with the cap, the enable flag, validation, and `agent_call` logging."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        backend: Backend | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._backend = backend
        self._clock = clock

    def calls_today(self) -> int:
        start, end = day_bounds_utc(self._clock())
        row = self._conn.execute(
            "SELECT count(*) FROM agent_call WHERE created_at >= ? AND created_at < ?",
            (start, end),
        ).fetchone()
        return int(row[0])

    def _scrub(self, text: str) -> str:
        """Remove the token from text that is about to be stored or logged."""
        token = self._settings.claude_token
        if token is not None and token.get_secret_value():
            text = text.replace(token.get_secret_value(), "[token]")
        return text

    def _ensure_backend(self) -> Backend:
        if self._backend is None:
            self._backend = SdkBackend(self._settings.claude_token)
        return self._backend

    def call[T: BaseModel](
        self,
        role: str,
        *,
        system_prompt: str,
        data: BaseModel | Mapping[str, Any],
        output_model: type[T],
        run_id: int | None = None,
    ) -> AgentResult[T]:
        """Run one role call and return its validated output.

        Raises `AgentUnavailable` when the call is not attempted and
        `AgentCallFailed` when it was attempted (and logged) but failed.
        """
        if role not in AGENT_ROLES:
            raise ValueError(f"unknown role {role!r}")
        if not self._settings.enable_agents:
            raise AgentUnavailable("agents are disabled (HAZELST_ENABLE_AGENTS)")
        cap = self._settings.agent_daily_calls
        used = self.calls_today()
        if used >= cap:
            raise AgentUnavailable(f"daily call cap reached ({used}/{cap})")
        backend = self._ensure_backend()

        role_cfg = self._settings.agent_roles[role]
        prompt = input_json(data)
        request = AgentRequest(
            role=role,
            model=role_cfg.model,
            effort=role_cfg.effort,
            system_prompt=system_prompt,
            prompt=prompt,
            output_schema=output_format(output_model)["schema"],
        )

        created_at = self._clock().astimezone(UTC).isoformat(timespec="seconds")
        started = time.monotonic()
        response: AgentResponse | None = None
        status: CallStatus
        output: str | None
        errors: list[str] | None = None
        parsed: T | None = None
        try:
            response = backend.run(request)
        except Exception as exc:
            status, output, subtype = "error", self._scrub(f"{type(exc).__name__}: {exc}"), None
        else:
            subtype = response.subtype
            if response.is_error or response.subtype != "success":
                status = "error"
                detail = list(response.errors) or [response.result_text or ""]
                output = json.dumps({"errors": detail}, ensure_ascii=False)
            else:
                try:
                    parsed = output_model.model_validate(response.structured_output)
                except ValidationError as exc:
                    status = "invalid"
                    output = json.dumps(response.structured_output, ensure_ascii=False, default=str)
                    errors = [
                        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                    ]
                else:
                    status = "ok"
                    output = parsed.model_dump_json()
        ms = int((time.monotonic() - started) * 1000)

        usage = response.usage if response is not None else {}
        call_id = self._log(
            run_id=run_id,
            role=role,
            model=role_cfg.model,
            effort=role_cfg.effort,
            sdk_version=backend.sdk_version,
            system_prompt=system_prompt,
            prompt=prompt,
            output=output,
            usage=usage,
            cost=response.total_cost_usd if response is not None else None,
            ms=ms,
            created_at=created_at,
            status=status,
            subtype=subtype,
            errors=errors,
        )
        if parsed is None:
            raise AgentCallFailed(
                f"{role} call {call_id} {status}: {subtype or output}",
                call_id=call_id,
                status=status,
            )
        return AgentResult(call_id=call_id, output=parsed)

    def _log(
        self,
        *,
        run_id: int | None,
        role: str,
        model: str,
        effort: str,
        sdk_version: str,
        system_prompt: str,
        prompt: str,
        output: str | None,
        usage: Mapping[str, Any],
        cost: float | None,
        ms: int,
        created_at: str,
        status: CallStatus,
        subtype: str | None,
        errors: list[str] | None,
    ) -> int:
        tokens = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_write_tokens": usage.get("cache_creation_input_tokens"),
        }
        cur = self._conn.execute(
            """
            INSERT INTO agent_call (run_id, role, model, effort, sdk_version, prompt_sha,
                                    input_sha, input, output, input_tokens, output_tokens,
                                    cache_read_tokens, cache_write_tokens, cost_usd_est, ms,
                                    created_at, status, result_subtype, check_errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                role,
                model,
                effort,
                sdk_version,
                sha256(system_prompt),
                sha256(prompt),
                prompt,
                output,
                tokens["input_tokens"],
                tokens["output_tokens"],
                tokens["cache_read_tokens"],
                tokens["cache_write_tokens"],
                cost,
                ms,
                created_at,
                status,
                subtype,
                json.dumps(errors) if errors is not None else None,
            ),
        )
        assert cur.lastrowid is not None
        log.info(
            json.dumps(
                {
                    "event": "agent_call",
                    "id": cur.lastrowid,
                    "run_id": run_id,
                    "role": role,
                    "model": model,
                    "effort": effort,
                    "status": status,
                    "subtype": subtype,
                    **tokens,
                    "cost_usd_est": cost,
                    "ms": ms,
                }
            )
        )
        return cur.lastrowid

    def record_check_errors(self, call_id: int, errors: list[str]) -> None:
        """Store the `report-check` errors against the call that produced the draft."""
        self._conn.execute(
            "UPDATE agent_call SET check_errors = ? WHERE id = ?", (json.dumps(errors), call_id)
        )
