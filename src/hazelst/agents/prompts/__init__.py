"""Role prompts and the stable system prompt, versioned by SHA in `agent_call.prompt_sha`.

The system prompt for a report role is the role prompt, then `SKILL.md`
(regional knowledge, `.claude/skills/hazel-st/SKILL.md`), then the accepted
`knowledge` statements, as one string (agent-runs.md, Call conventions).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from hazelst.config import AGENT_ROLES, PROJECT_ROOT

SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "hazel-st" / "SKILL.md"
# The wheel carries SKILL.md here (pyproject force-include); a source checkout reads SKILL_PATH.
SKILL_RESOURCE = "SKILL.md"
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)

# Roles whose system prompt carries the regional knowledge.
REPORT_ROLES = ("report-write", "report-revise")


def role_prompt(role: str) -> str:
    """The prompt text for a role (`prompts/<role>.md`)."""
    if role not in AGENT_ROLES:
        raise ValueError(f"unknown role {role!r}")
    return resources.files(__name__).joinpath(f"{role}.md").read_text("utf-8").strip()


def skill_text(path: Path | None = None) -> str:
    """SKILL.md without its front matter."""
    if path is not None:
        text = path.read_text("utf-8")
    else:
        packaged = resources.files(__name__).joinpath(SKILL_RESOURCE)
        text = packaged.read_text("utf-8") if packaged.is_file() else SKILL_PATH.read_text("utf-8")
    return _FRONT_MATTER.sub("", text, count=1).strip()


def knowledge_block(statements: Sequence[str]) -> str:
    lines = "\n".join(f"- {s}" for s in statements) if statements else "- (none yet)"
    intro = "Verified by past forecasts and accepted by the owner:"
    return f"# Accepted local knowledge\n\n{intro}\n\n{lines}"


def system_prompt(role: str, knowledge: Sequence[str] = ()) -> str:
    """The stable system prompt for a role."""
    if role not in REPORT_ROLES:
        return role_prompt(role)
    parts = [role_prompt(role)]
    if role == "report-revise":
        parts.append(role_prompt("report-write"))
    parts += [skill_text(), knowledge_block(knowledge)]
    return "\n\n".join(parts)
