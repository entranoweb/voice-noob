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
    # A call that happened rather than a scenario that was run: measured, valid,
    # and with nothing to pass or fail against. A real caller declares no
    # expected end state, so grading one against an accuracy metric it was never
    # given would report every genuine call as an error.
    OBSERVED = "observed"


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

    def _measure(self, context: MetricContext) -> tuple[list[MetricScore], MetricResults | None]:
        """Every score, plus an ERROR result if a validation gate failed.

        Shared by both verdicts so they cannot drift apart: a gate that stops a
        scenario has to stop an observed call too, and the two answering
        differently would be a bug nobody would look for.
        """
        scores = [metric.compute(context) for metric in all_metrics(self.categories)]
        invalid = _failed_gates(scores)
        if invalid:
            return scores, MetricResults(
                outcome=RunOutcome.ERROR,
                scores=tuple(scores),
                invalid_reasons=invalid,
            )
        return scores, None

    def run(self, context: MetricContext) -> MetricResults:
        """Compute every applicable metric and derive the run's outcome."""
        scores, invalid_result = self._measure(context)
        if invalid_result is not None:
            return invalid_result

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

    def observe(self, context: MetricContext) -> MetricResults:
        """Compute every metric for a call that happened, with nothing to grade.

        Same metrics and the same validation gates as ``run``, without the
        accuracy verdict. That gate exists because a *scenario* with no
        measurable accuracy metric means the harness misbehaved — but a real call
        carries no scenario, so ``task_completion`` is unmeasurable by
        construction and the gate would mark every genuine call an error. Doing
        so would discard exactly the latency and interruption numbers this path
        exists to produce, under the flag that means "do not trust these".
        """
        scores, invalid_result = self._measure(context)
        if invalid_result is not None:
            return invalid_result

        return MetricResults(outcome=RunOutcome.OBSERVED, scores=tuple(scores))


def _failed_gates(scores: list[MetricScore]) -> tuple[str, ...]:
    """Validation metrics that measured and failed.

    A gate that could not be measured does not invalidate a run — only one that
    looked and found the run broken.
    """
    invalid = [
        s
        for s in scores
        if s.category == MetricCategory.VALIDATION and s.passed is False and s.measured
    ]
    return tuple(
        f"{s.metric}: {s.detail.get('termination_reason') or 'gate failed'}" for s in invalid
    )


def evaluate(context: MetricContext) -> MetricResults:
    """Convenience wrapper: run every registered metric over one context."""
    return MetricRunner().run(context)


def evaluate_observed(context: MetricContext) -> MetricResults:
    """Every registered metric over one real call, with no accuracy verdict."""
    return MetricRunner().observe(context)
