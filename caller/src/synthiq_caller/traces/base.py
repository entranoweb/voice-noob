"""What a call's trace looks like once an engine's own format is stripped away."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: Any
    ok: bool
    # The step of the agent's flow that made the call, when the engine has steps.
    step_id: str = ""
    # Seconds into the call, when known.
    t: float | None = None
    error: str = ""
    # Named values the engine recorded the tool as having set (found or not
    # found, for instance). Some engines keep a tool's answer here and store
    # only a bare acknowledgement as ``result``.
    outputs: dict[str, Any] = field(default_factory=dict)
    # How the tool was reached, when the engine says: method and address only,
    # never headers.
    request: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    call_ref: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The steps of the agent's flow, in the order they were entered.
    step_path: list[str] = field(default_factory=list)
    termination: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> Trace:
        return Trace(
            call_ref=data.get("call_ref", ""),
            tool_calls=[ToolCall(**c) for c in data.get("tool_calls", [])],
            step_path=list(data.get("step_path", [])),
            termination=data.get("termination", ""),
        )


class TraceReader(Protocol):
    async def read(self, call_ref: str) -> Trace:
        """The trace of one finished call. Raises when it cannot be read."""
        ...
