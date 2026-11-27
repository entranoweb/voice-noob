"""Writes the scenario did not declare (audit F6).

A scenario that says ``mutates: none`` promises the call changes nothing on the
system under test. The batch only compared that promise with what the operator
allowed; it never looked at what the agent did. This reads the trace: a write
tool that succeeded in such a scenario is a fault of the run (a failed check,
never a pass), and the after-call cleanup hook runs for it.

Which tools write comes from the target profile's ``write_tools`` list, else from
the names in ``tool_outcomes`` (tools whose reply says whether they did the thing).
With neither, nothing is guessed: the run says the watch was not made.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from synthiq_caller.grading import DID, Check, Grade, tool_outcome
from synthiq_caller.scenario import Scenario
from synthiq_caller.traces.base import ToolCall, Trace

NO_LIST = (
    "not checked: the target profile has no write_tools list and the scenario has no "
    "tool_outcomes, so no tool is known to write; undeclared writes could not be seen"
)


@dataclass
class WriteWatch:
    checked: bool
    tools: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def stray(self) -> bool:
        return bool(self.tools)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _is_write(call: ToolCall, names: Sequence[str]) -> bool:
    lowered = call.name.casefold()
    return any(name.casefold() == lowered or name.casefold() in lowered for name in names if name)


def watch_writes(
    scenario: Scenario, trace: Trace | None, write_tools: Sequence[str] | None
) -> WriteWatch:
    """What a ``mutates: none`` scenario's call wrote. Other levels declare writes."""
    if scenario.mutates != "none":
        return WriteWatch(checked=False, note="the scenario declares that it may change state")
    names = list(write_tools or []) or list(scenario.tool_outcomes)
    if not names:
        return WriteWatch(checked=False, note=NO_LIST)
    if trace is None:
        return WriteWatch(checked=False, note="not checked: no trace was read for this call")
    done: list[str] = []
    for call in trace.tool_calls:
        if not _is_write(call, names):
            continue
        if tool_outcome(call, scenario.tool_outcomes)[0] == DID and call.name not in done:
            done.append(call.name)
    return WriteWatch(checked=True, tools=done)


def flag(grade: Grade, found: WriteWatch) -> None:
    """Record a stray write on the grade: a failed check, and never a pass."""
    if not found.stray:
        return
    for name in found.tools:
        grade.checks.append(
            Check(
                f"a successful write ({name}) in a scenario that declares mutates: none",
                False,
                label=f"undeclared write: {name}",
            )
        )
    if grade.outcome == "passed":
        grade.outcome, grade.fault = "failed", "unknown"
        grade.reason = (
            "the trace shows a write the scenario did not declare; the declaration or the "
            "agent is wrong, and the system under test may need cleaning up"
        )


def restored(payload: dict[str, Any]) -> WriteWatch:
    """The watch a stored run carries, for a regrade that has no profile to read it from."""
    stored = payload.get("undeclared_writes") or {}
    return WriteWatch(
        checked=bool(stored.get("checked")),
        tools=[str(t) for t in stored.get("tools") or []],
        note=str(stored.get("note", "")),
    )
