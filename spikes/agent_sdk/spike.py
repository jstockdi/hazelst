"""One Claude Agent SDK call with structured output and no tools.

Reads CLAUDE_CODE_OAUTH_TOKEN from the environment. Prints the validated
output, the result metadata, and the usage; never prints the token.
"""

import asyncio
import json
import os
import sys
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    query,
)
from pydantic import BaseModel, Field, ValidationError

MODEL = os.environ.get("SPIKE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("SPIKE_EFFORT", "low")


class MountainLine(BaseModel):
    mountain: str = Field(description="Mountain name exactly as given")
    text: str = Field(description="One sentence on the outlook")


class SpikeDraft(BaseModel):
    headline: str = Field(description="One line, at most 12 words")
    best_bet: str = Field(description="Mountain name from the input")
    mountains: list[MountainLine]


SYSTEM = (
    "You write one-line ski outlooks from the data you are given. "
    "Use only the numbers in the input."
)
PROMPT = json.dumps(
    {
        "date": "2026-11-26",
        "windows": [
            {"mountain": "Killington", "snow_1d_in": 3.1, "precip_type": "snow"},
            {"mountain": "Sugarbush", "snow_1d_in": 4.0, "precip_type": "snow"},
            {"mountain": "Loon", "snow_1d_in": 0.4, "precip_type": "mix"},
        ],
    }
)


def options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        effort=EFFORT,  # type: ignore[arg-type]
        system_prompt=SYSTEM,
        output_format={"type": "json_schema", "schema": SpikeDraft.model_json_schema()},
        tools=[],
        setting_sources=[],
        skills=[],
        strict_mcp_config=True,
        permission_mode="dontAsk",
        verbatim_prompts=True,
        extra_args={"no-session-persistence": None},
        max_turns=int(os.environ.get("SPIKE_MAX_TURNS", "5")),
        env={
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_AGENT_SDK_CLIENT_APP": "hazelst-spike/0.1",
        },
        stderr=lambda line: print(f"[cli] {line}", file=sys.stderr),
    )


async def main() -> int:
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("CLAUDE_CODE_OAUTH_TOKEN is not set", file=sys.stderr)
        return 2
    start = time.monotonic()
    result: ResultMessage | None = None
    async for message in query(prompt=PROMPT, options=options()):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            data = message.data
            print("init:", json.dumps({k: data.get(k) for k in ("model", "tools", "apiKeySource", "mcp_servers", "skills")}))
        elif isinstance(message, (AssistantMessage, UserMessage)) and isinstance(message.content, list):
            for block in message.content:
                detail = getattr(block, "input", None) or getattr(block, "content", None) or ""
                print(f"{type(message).__name__}.{type(block).__name__}: {str(detail)[:160]}")
        elif isinstance(message, ResultMessage):
            result = message
    elapsed = time.monotonic() - start
    if result is None:
        print("no result message", file=sys.stderr)
        return 1
    print(
        "result:",
        json.dumps(
            {
                "subtype": result.subtype,
                "is_error": result.is_error,
                "stop_reason": result.stop_reason,
                "num_turns": result.num_turns,
                "duration_ms": result.duration_ms,
                "total_cost_usd": result.total_cost_usd,
                "usage": result.usage,
                "model_usage": result.model_usage,
                "elapsed_s": round(elapsed, 1),
            },
            default=str,
        ),
    )
    try:
        draft = SpikeDraft.model_validate(result.structured_output)
    except ValidationError as exc:
        print("schema validation failed:", exc, file=sys.stderr)
        print("raw:", result.structured_output, result.result, file=sys.stderr)
        return 1
    print("valid:", draft.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
