"""Pieces the Retell and Vapi trace readers share.

A platform can hold back part of a call: a result that never arrived, arguments
removed by its redaction setting, a transcript it was told not to keep. The
readers never fill such a gap with a guess. They mark it, so a grader or a
person can tell "the tool failed" from "the platform did not say".
"""

from __future__ import annotations

import json
from typing import Any

from synthiq_caller.traces.base import ToolCall, Trace

# Stands in for a tool result the platform did not return.
UNAVAILABLE = "unavailable"
# The single key of an ``arguments`` object the platform did not return in a readable form.
UNAVAILABLE_KEY = "_unavailable"


class TraceUnavailableError(RuntimeError):
    """The platform has the call but kept no tool-call record of it."""


def as_object(value: Any) -> Any:
    """A JSON string decoded; anything else, or a string that is not JSON, as it is."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def arguments_of(raw: Any) -> dict[str, Any]:
    """The arguments of a tool call as an object, or a marked gap when they are not one."""
    if raw is None or raw == "":
        return {UNAVAILABLE_KEY: "the platform returned no arguments"}
    value = as_object(raw)
    if isinstance(value, dict):
        return value
    return {UNAVAILABLE_KEY: "the arguments were not a JSON object"}


def inferred_ok(value: Any) -> bool:
    """Whether a result reads as a success when the platform gives no flag of its own.

    This is a reading of the tool's own reply, not something the platform states.
    """
    if isinstance(value, dict):
        if value.get("success") is False:
            return False
        if value.get("status") == "error":
            return False
        if value.get("error"):
            return False
    return True


def error_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("code") or value.get("error") or value.get("message") or "")
    return str(value) if isinstance(value, str) else ""


def missing_result(name: str, arguments: dict[str, Any], t: float | None, step: str) -> ToolCall:
    """A tool call whose result the platform did not return.

    ``ok`` is false because an unknown outcome must never count as a success;
    ``error`` says the outcome is unknown, not that the tool refused.
    """
    return ToolCall(
        name=name,
        arguments=arguments,
        result=UNAVAILABLE,
        ok=False,
        step_id=step,
        t=t,
        error=f"{UNAVAILABLE}: the platform returned no result for this call",
    )


def is_unavailable(call: ToolCall) -> bool:
    """True when any part of this call was held back by the platform."""
    return call.result == UNAVAILABLE or UNAVAILABLE_KEY in call.arguments


def unavailable_calls(trace: Trace) -> list[ToolCall]:
    """The calls a grader should treat as undetermined, not as passed or failed."""
    return [call for call in trace.tool_calls if is_unavailable(call)]
