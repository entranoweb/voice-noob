"""Tool-call metrics: what was called, and was it called correctly.

Two separate questions, deliberately kept apart:

* **tool_call_validity** — of the calls the agent made, how many were well
  formed? A malformed call is a model failure and is fixed by prompting or a
  better schema.
* **expected_tools_invoked** — did the calls the scenario required actually
  happen? This is the accuracy question, and it is the one the framework
  previously asked a language model to answer by reading a transcript that
  structurally could not contain a tool call.

Both are exact assertions over recorded invocations. Never ask a judge whether a
function ran; the runtime knows.
"""

from __future__ import annotations

from typing import Any

from app.monitoring.call_trace import ToolOutcome
from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    MetricScore,
)
from app.services.qa.metrics.registry import register

# Outcomes that indicate the model produced a bad call, as opposed to a
# downstream failure. A tool that returned "reservation not found" was called
# correctly; one rejected for a missing argument was not.
MALFORMED_OUTCOMES = frozenset({ToolOutcome.INVALID_ARGS})


@register
class ToolCallValidity(BaseMetric):
    """Fraction of tool calls that were well formed."""

    name = "tool_call_validity"
    version = "v1"
    category = MetricCategory.DIAGNOSTIC
    kind = MetricKind.DETERMINISTIC

    def compute(self, context: MetricContext) -> MetricScore:
        calls = context.tool_calls
        if not calls:
            # No calls is not a validity failure. A scenario that required calls
            # fails on expected_tools_invoked instead, which is the right place
            # for it — conflating the two makes a clean no-tools conversation
            # look broken.
            return self.not_measurable("no tool calls were made")

        malformed = [c for c in calls if c.outcome in MALFORMED_OUTCOMES]
        valid = len(calls) - len(malformed)

        return self.score(
            valid / len(calls),
            passed=not malformed,
            total_calls=len(calls),
            malformed_calls=[{"name": c.name, "error": c.error} for c in malformed],
        )


@register
class ExpectedToolsInvoked(BaseMetric):
    """Did every tool the scenario requires actually get called successfully?

    A required tool that was called but errored does not count as invoked: from
    the caller's point of view the appointment was not booked either way.
    """

    name = "expected_tools_invoked"
    version = "v1"
    category = MetricCategory.ACCURACY
    kind = MetricKind.DETERMINISTIC

    def compute(self, context: MetricContext) -> MetricScore:
        required = _required_tool_names(context)
        if not required:
            return self.not_measurable("scenario requires no specific tools")

        succeeded = {c.name for c in context.tool_calls if c.outcome == ToolOutcome.OK}
        attempted = {c.name for c in context.tool_calls}

        invoked = required & succeeded
        missing = sorted(required - succeeded)

        return self.score(
            len(invoked) / len(required),
            passed=not missing,
            required=sorted(required),
            invoked=sorted(invoked),
            missing=missing,
            # Separating these two makes the failure actionable: a tool that was
            # attempted and failed is a different bug from one never reached.
            attempted_but_failed=sorted((required & attempted) - succeeded),
        )


def _required_tool_names(context: MetricContext) -> set[str]:
    """Tool names the scenario requires, from either place they can be declared.

    ``expected_tool_calls`` carries structured entries; ``success_criteria`` may
    carry a plain ``must_invoke_tools`` list. Both existed in the scenario
    library and neither was ever read, so both are honoured here.
    """
    names: set[str] = set()

    for entry in context.expected_tool_calls:
        name = entry.get("tool") or entry.get("name")
        if isinstance(name, str) and name:
            names.add(name)

    must_invoke: Any = context.success_criteria.get("must_invoke_tools")
    if isinstance(must_invoke, list):
        names.update(str(n) for n in must_invoke if n)

    return names
