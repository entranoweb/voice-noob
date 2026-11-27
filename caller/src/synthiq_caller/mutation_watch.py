"""Writes the scenario did not declare (audit F6).

A scenario that says ``mutates: none`` promises the call changes nothing on the
system under test. The batch only compared that promise with what the operator
allowed; it never looked at what the agent did. This reads the trace: a write
tool that succeeded in such a scenario is a fault of the run (a failed check,
never a pass), and the after-call cleanup hook runs for it.

Which tools write comes from the target profile's ``write_tools`` list, else from
the names in ``tool_outcomes`` (tools whose reply says whether they did the thing).
A name matches a tool by whole name or as a fragment of it, ignoring case (as
``tool_outcomes`` does): platforms prefix tool names, so ``book`` matches
``agent_v2_book_appointment``. Name tools precisely enough that a read tool is not caught.
A write tool whose outcome is unknown or errored is a *possible* write: the run is
never reported as a clean read-only call.
With neither, nothing is guessed: the run says the watch was not made.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from synthiq_caller.grading import DID, REFUSED, Check, Grade, tool_outcome
from synthiq_caller.scenario import Scenario
from synthiq_caller.traces.base import ToolCall, Trace

CLEANUP_NOT_ALLOWED = "cleanup not run: mutation not allowed, residue may remain"
CLEANUP_RUN = "cleanup requested: the after-call hook was run for this call"

NO_LIST = (
    "not checked: the target profile has no write_tools list and the scenario has no "
    "tool_outcomes, so no tool is known to write; undeclared writes could not be seen"
)


NO_LIST_SHORT = (
    "no write_tools list in the target profile, no tool_outcomes, or no trace read; "
    "a write in these calls could not be seen"
)


@dataclass
class WriteWatch:
    checked: bool
    tools: list[str] = field(default_factory=list)
    note: str = ""
    # Write tools called whose outcome is not known (the platform withheld the reply, or
    # the request failed on the way): they may have written. Never a clean result.
    possible: list[str] = field(default_factory=list)
    # What was done about residue, for a run with a stray write.
    cleanup: str = ""

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
    unsure: list[str] = []
    for call in trace.tool_calls:
        if not _is_write(call, names):
            continue
        outcome = tool_outcome(call, scenario.tool_outcomes)[0]
        if outcome == DID and call.name not in done:
            done.append(call.name)
        elif outcome != DID and outcome != REFUSED and call.name not in unsure:
            unsure.append(call.name)  # UNKNOWN or ERRORED: it may have written
    if unsure:
        return WriteWatch(
            checked=False,
            tools=done,
            possible=unsure,
            note=f"not fully checked: could not tell whether {unsure} wrote (outcome unknown)",
        )
    return WriteWatch(checked=True, tools=done)


def flag(grade: Grade, found: WriteWatch) -> None:
    """Record a stray write on the grade: a failed check, and never a pass."""
    for name in found.possible:
        grade.checks.append(
            Check(
                f"a write tool ({name}) may have written: its outcome is not known",
                False,
                label=f"possible undeclared write: {name}",
            )
        )
    if found.possible and not found.stray and grade.outcome == "passed":
        # Not a pass: nothing shows the call changed nothing. Not a failure either.
        grade.outcome, grade.fault = "ungraded", "unknown"
        grade.reason = (
            "a write tool was called and its outcome is not known, so the call may have "
            "changed the system under test; it cannot be passed as read-only"
        )
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
            + (f"; {found.cleanup}" if found.cleanup else "")
        )


def restored(payload: dict[str, Any]) -> WriteWatch:
    """The watch a stored run carries, for a regrade that has no profile to read it from."""
    stored = payload.get("undeclared_writes") or {}
    return WriteWatch(
        checked=bool(stored.get("checked")),
        tools=[str(t) for t in stored.get("tools") or []],
        note=str(stored.get("note", "")),
        possible=[str(t) for t in stored.get("possible") or []],
        cleanup=str(stored.get("cleanup", "")),
    )


def tally(payloads: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Counts over stored runs of ``mutates: none`` scenarios, for the report and scorecard."""
    counts = {"watched": 0, "stray": 0, "possible": 0, "not_checked": 0, "cleanup_not_run": 0}
    for payload in payloads:
        if payload.get("mutates") != "none":
            continue
        found = restored(payload)
        counts["watched"] += 1
        counts["stray"] += found.stray
        counts["possible"] += bool(found.possible)
        counts["not_checked"] += (not found.checked) and not found.possible
        counts["cleanup_not_run"] += found.cleanup == CLEANUP_NOT_ALLOWED
    return counts


def describe(counts: dict[str, int]) -> list[str]:
    """Report lines; empty when no read-only scenario was run."""
    if not counts.get("watched"):
        return []
    lines = [
        f"undeclared writes (read-only scenarios, {counts['watched']} call(s)): "
        f"{counts['stray']} with a successful write, {counts['possible']} with a possible "
        f"write (outcome unknown), {counts['not_checked']} not checked"
    ]
    if counts["not_checked"]:
        lines.append(f"  not checked: {NO_LIST_SHORT}")
    if counts["cleanup_not_run"]:
        lines.append(f"  {counts['cleanup_not_run']} call(s): {CLEANUP_NOT_ALLOWED}")
    return lines
