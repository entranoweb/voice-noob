"""Compute a run's metrics, honouring the layer ordering.

The ordering is the point. Validation runs first, and if a validation gate fails
the run is reported as ``ERROR`` — not as a failing agent. Everything else is
still computed and returned, because the numbers are useful for debugging the
harness, but they are explicitly marked as not reflecting agent quality.

This is the fix for the framework's most damaging behaviour: parse failures and
broken runs were written to the database as a score of 50 with ``passed=False``,
which sits below the pass threshold and therefore raised a failure alert about
an agent that may have done nothing wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.services.qa.metrics.base import MetricCategory, MetricContext, MetricScore
from app.services.qa.metrics.registry import all_metrics


class RunOutcome(StrEnum):
    """What a run's results actually mean.

    ``ERROR`` exists so that "we could not measure this" stops being reported as
    "the agent failed". They are different facts and only one of them is the
    agent's problem.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class MetricResults:
    """Every score for one run, plus the verdict derived from them."""

    outcome: RunOutcome
    scores: tuple[MetricScore, ...] = ()
    invalid_reasons: tuple[str, ...] = ()

    def by_name(self) -> dict[str, MetricScore]:
        """Scores keyed by metric name."""
        return {s.metric: s for s in self.scores}

    def by_category(self, category: MetricCategory) -> tuple[MetricScore, ...]:
        """Scores in one category, in registry order."""
        return tuple(s for s in self.scores if s.category == category)

    @property
    def trustworthy(self) -> bool:
        """Whether the validation layer passed, i.e. whether to believe the rest."""
        return self.outcome is not RunOutcome.ERROR

    def accuracy_score(self) -> float | None:
        """Mean of the measured accuracy metrics, or None if none were measurable.

        Returns None rather than 0.0 when nothing could be measured: an
        unmeasured run is not a failed one.
        """
        measured = [
            s.value for s in self.by_category(MetricCategory.ACCURACY) if s.value is not None
        ]
        if not measured:
            return None
        return sum(measured) / len(measured)


@dataclass
class MetricRunner:
    """Runs registered metrics over a context."""

    categories: tuple[MetricCategory, ...] | None = None
    _scores: list[MetricScore] = field(default_factory=list, init=False)

    def run(self, context: MetricContext) -> MetricResults:
        """Compute every applicable metric and derive the run's outcome."""
        scores = [metric.compute(context) for metric in all_metrics(self.categories)]

        # Validation gates. A gate that could not be measured does not invalidate
        # the run — only one that measured and failed.
        invalid = [
            s
            for s in scores
            if s.category == MetricCategory.VALIDATION and s.passed is False and s.measured
        ]
        if invalid:
            return MetricResults(
                outcome=RunOutcome.ERROR,
                scores=tuple(scores),
                invalid_reasons=tuple(
                    f"{s.metric}: {s.detail.get('termination_reason') or 'gate failed'}"
                    for s in invalid
                ),
            )

        # Accuracy decides pass or fail. An accuracy metric that could not be
        # measured is skipped rather than counted against the agent; if none were
        # measurable the run has no verdict to give and is reported as an error,
        # because silently passing an unmeasured run is how a suite becomes
        # decorative.
        accuracy = [s for s in scores if s.category == MetricCategory.ACCURACY]
        measured = [s for s in accuracy if s.measured]

        if not measured:
            return MetricResults(
                outcome=RunOutcome.ERROR,
                scores=tuple(scores),
                invalid_reasons=("no accuracy metric was measurable for this run",),
            )

        passed = all(s.passed for s in measured if s.passed is not None)
        return MetricResults(
            outcome=RunOutcome.PASSED if passed else RunOutcome.FAILED,
            scores=tuple(scores),
        )


def evaluate(context: MetricContext) -> MetricResults:
    """Convenience wrapper: run every registered metric over one context."""
    return MetricRunner().run(context)
