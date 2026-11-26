"""A backend that never calls Claude: canned responses per role, for CI and tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from hazelst.agents.client import AgentRequest, AgentResponse

Reply = BaseModel | Mapping[str, Any] | AgentResponse | Exception
Responder = Reply | Callable[[AgentRequest], Reply]


@dataclass
class StubBackend:
    """Answers each role from `responses`.

    A value can be a Pydantic model or dict (returned as `structured_output`
    with a `success` result), a full `AgentResponse` (to simulate error
    subtypes), an exception (raised), or a callable taking the request.
    A list of values is consumed one per call.
    """

    responses: dict[str, Responder | list[Responder]] = field(default_factory=dict)
    sdk_version: str = "stub"
    requests: list[AgentRequest] = field(default_factory=list)

    def run(self, request: AgentRequest) -> AgentResponse:
        self.requests.append(request)
        if request.role not in self.responses:
            raise KeyError(f"no stub response for role {request.role!r}")
        entry = self.responses[request.role]
        if isinstance(entry, list):
            if not entry:
                raise IndexError(f"stub responses for {request.role!r} are used up")
            entry = entry.pop(0)
        reply = entry(request) if callable(entry) and not isinstance(entry, BaseModel) else entry
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, AgentResponse):
            return reply
        output = reply.model_dump(mode="json") if isinstance(reply, BaseModel) else dict(reply)
        return AgentResponse(
            subtype="success",
            is_error=False,
            structured_output=output,
            usage={"input_tokens": 0, "output_tokens": 0},
            total_cost_usd=0.0,
            num_turns=2,
        )
