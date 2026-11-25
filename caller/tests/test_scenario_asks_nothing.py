"""A scenario that states no expectations never passes. A guard, not a fix.

It holds on main today; this pins it so a later change cannot turn "nothing was
asked" into "passed". Every scenario here has a clean call, a complete trace and
a normal ending, the conditions under which a pass is most tempting.
"""

import pytest

from synthiq_caller.director import RunRecord
from synthiq_caller.grading import ENDED_NORMALLY, grade
from synthiq_caller.scenario import parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace

NOTHING_ASKED = {
    "name": "nothing asked",
    "mutates": "none",
    "persona": {"facts": {"name": "Sam Example"}},
    "steps": [{"when": "name", "clips": ["name_01"]}],
    "hangup_after": "goodbye",
}


def _record() -> RunRecord:
    return RunRecord(scenario="nothing asked", call_ref="c1", ended=ENDED_NORMALLY)


def _busy_trace() -> Trace:
    call = ToolCall(name="lookup", arguments={"a": "b"}, result={"ok": 1}, ok=True)
    return Trace(call_ref="c1", final=True, tool_calls=[call], step_path=["R0", "END"])


@pytest.mark.parametrize(
    "trace",
    [None, Trace(call_ref="c1", final=True), _busy_trace()],
    ids=["no trace", "empty trace", "successful tool call"],
)
def test_a_scenario_with_no_expectations_is_ungraded_never_passed(trace: Trace | None) -> None:
    scenario = parse_scenario(NOTHING_ASKED)
    graded = grade(scenario, _record(), trace)
    assert graded.outcome == "ungraded"
    assert graded.outcome != "passed"
    if trace is not None:
        assert graded.reason == "the scenario asks nothing of the call"


def test_empty_expect_sections_are_the_same_as_none() -> None:
    scenario = parse_scenario({**NOTHING_ASKED, "expect": {"tools": [], "fields": []}})
    graded = grade(scenario, _record(), Trace(call_ref="c1", final=True))
    assert graded.outcome == "ungraded"
