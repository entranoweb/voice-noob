"""Validation gate: did this run finish in a way worth scoring?

The validation layer answers a question that comes before every other one — *is
this run trustworthy?* A conversation cut off by a pipeline error did not
produce a real outcome, and scoring the agent on it manufactures a failure that
belongs to the harness.

This is the layer whose absence caused the framework's worst behaviour: a JSON
parse failure was written to the database as ``score: 50, passed: False``, below
the pass threshold, so every harness malfunction created a failure alert about
an agent that may have performed perfectly.
"""

from __future__ import annotations

from app.monitoring.call_trace import TerminationReason
from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    MetricScore,
)
from app.services.qa.metrics.registry import register

# Ways a conversation can end that still leave a scoreable outcome. A caller
# hanging up mid-sentence is a real result — possibly a bad one, and the agent
# owns it. A pipeline error is not.
VALID_ENDINGS = frozenset(
    {
        TerminationReason.CALLER_HANGUP,
        TerminationReason.AGENT_ENDED,
        TerminationReason.TRANSFERRED,
        TerminationReason.SILENCE_TIMEOUT,
        TerminationReason.MAX_DURATION,
    },
)

# Ends that invalidate the run: the harness or the stack broke, not the agent.
HARNESS_FAILURES = frozenset(
    {
        TerminationReason.PIPELINE_ERROR,
        TerminationReason.UNKNOWN,
    },
)


@register
class ConversationValidEnd(BaseMetric):
    """Whether the run terminated in a state that can be scored."""

    name = "conversation_valid_end"
    version = "v1"
    category = MetricCategory.VALIDATION
    kind = MetricKind.DETERMINISTIC

    def compute(self, context: MetricContext) -> MetricScore:
        reason = context.termination_reason
        valid = reason in VALID_ENDINGS

        return self.score(
            1.0 if valid else 0.0,
            passed=valid,
            termination_reason=str(reason),
            harness_failure=reason in HARNESS_FAILURES,
        )


@register
class ConversationHasTurns(BaseMetric):
    """Whether the conversation contains anything to score.

    A run with no agent turn produced no behaviour. Grading it says nothing about
    the agent and everything about the harness, so it fails validation and stops
    the other metrics from being read as agent results.
    """

    name = "conversation_has_turns"
    version = "v1"
    category = MetricCategory.VALIDATION
    kind = MetricKind.DETERMINISTIC

    def compute(self, context: MetricContext) -> MetricScore:
        agent_turns = len(context.agent_turns())
        caller_turns = len(context.caller_turns())
        valid = agent_turns > 0 and caller_turns > 0

        return self.score(
            1.0 if valid else 0.0,
            passed=valid,
            agent_turns=agent_turns,
            caller_turns=caller_turns,
        )
