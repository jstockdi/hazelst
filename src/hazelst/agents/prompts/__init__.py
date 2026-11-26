"""Role prompts, one Markdown file per role, versioned by SHA in `agent_call.prompt_sha`."""

from __future__ import annotations

from importlib import resources

from hazelst.config import AGENT_ROLES


def role_prompt(role: str) -> str:
    """The prompt text for a role (`prompts/<role>.md`)."""
    if role not in AGENT_ROLES:
        raise ValueError(f"unknown role {role!r}")
    return resources.files(__name__).joinpath(f"{role}.md").read_text("utf-8").strip()
