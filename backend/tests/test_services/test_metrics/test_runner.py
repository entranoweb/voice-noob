"""Tests for the metric runner and registry.

The central behaviour under test: a broken run is reported as ERROR, never as a
failing agent. That distinction is the reason this layer exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.monitoring.call_trace import Speaker, TerminationReason, ToolOutcome
from app.services.qa.metrics import registry
from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    ToolCallData,
    TurnData,
)
from app.services.qa.metrics.registry import (
    CATEGORY_ORDER,
    DuplicateMetricError,
    UnknownMetricError,
    all_metrics,
)
from app.services.qa.metrics.runner import MetricRunner, RunOutcome, evaluate


def working_context(**overrides: Any) -> MetricContext:
    """A run that ends validly, books the appointment, and is quick about it."""
    base: dict[str, Any] = {
        "run_id": "run-1",
        "turns": (
            TurnData(index=0, speaker=Speaker.CALLER, text_intended="book me in"),
            TurnData(index=1, speaker=Speaker.AGENT, text_intended="done", response_ms=200),
        ),
        "tool_calls": (ToolCallData(name="book_appointment", outcome=ToolOutcome.OK),),
        "expected_tool_calls": ({"tool": "book_appointment"},),
        "termination_reason": TerminationReason.AGENT_ENDED,
        "expected_db_state": {"appointments": [{"status": "scheduled"}]},
        "final_db_state": {"appointments": [{"id": 1, "status": "scheduled"}]},
    }
    base.update(overrides)
    return MetricContext(**base)


class TestRegistry:
    def test_the_registered_set_is_exactly_what_we_expect(self) -> None:
        """An exact set, not a subset: a metric that silently stops registering
        drops out of every result with nothing to notice it by."""
        assert set(registry.registered_names()) == {
            "conversation_has_turns",
            "conversation_valid_end",
            "expected_tools_invoked",
            "interruption_handling",
            "response_speed",
            "state_restored",
            "task_completion",
            "time_to_first_audio",
            "tool_call_validity",
            "transcription_accuracy",
        }

    def test_validation_metrics_come_first(self) -> None:
        """Ordering is the design: gates before anything they gate."""
        categories = [m.category for m in all_metrics()]
        assert categories == sorted(categories, key=CATEGORY_ORDER.index)

    def test_lookup_by_name(self) -> None:
        assert registry.get("task_completion").name == "task_completion"

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(UnknownMetricError, match="no_such_metric"):
            registry.get("no_such_metric")

    def test_duplicate_registration_raises(self) -> None:
        """A silently shadowed metric would vanish from every result set."""

        class Duplicate(BaseMetric):
            name = "task_completion"

        with pytest.raises(DuplicateMetricError, match="task_completion"):
            registry.register(Duplicate)

    def test_filtering_by_category(self) -> None:
        names = {m.name for m in all_metrics([MetricCategory.ACCURACY])}
        assert names == {"task_completion", "expected_tools_invoked"}

    def test_all_phase_one_metrics_are_deterministic(self) -> None:
        """Phase 1 ships nothing that needs a calibration set to be trusted."""
        assert all(m.kind is MetricKind.DETERMINISTIC for m in all_metrics())


class TestOutcomes:
    def test_a_good_run_passes(self) -> None:
        results = evaluate(working_context())
        assert results.outcome is RunOutcome.PASSED
        assert results.trustworthy is True

    def test_a_run_that_did_not_book_fails(self) -> None:
        results = evaluate(
            working_context(
                tool_calls=(),
                final_db_state={"appointments": []},
            ),
        )
        assert results.outcome is RunOutcome.FAILED
        assert results.trustworthy is True  # a real result, just a bad one

    def test_a_pipeline_error_is_an_error_not_a_failure(self) -> None:
        """The behaviour this whole layer exists for.

        Previously this run was written as score 50 / passed=False, which sits
        below the pass threshold and raised a failure alert about an agent that
        had done nothing wrong.
        """
        results = evaluate(
            working_context(termination_reason=TerminationReason.PIPELINE_ERROR),
        )
        assert results.outcome is RunOutcome.ERROR
        assert results.trustworthy is False
        assert any("conversation_valid_end" in r for r in results.invalid_reasons)

    def test_an_empty_conversation_is_an_error(self) -> None:
        results = evaluate(working_context(turns=()))
        assert results.outcome is RunOutcome.ERROR

    def test_error_still_returns_every_score_for_debugging(self) -> None:
        """Marked untrustworthy, but not discarded — they explain the break."""
        results = evaluate(
            working_context(termination_reason=TerminationReason.PIPELINE_ERROR),
        )
        assert len(results.scores) == len(registry.registered_names())

    def test_a_run_with_nothing_measurable_is_an_error_not_a_pass(self) -> None:
        """Silently passing an unmeasured run is how a suite becomes decorative."""
        results = evaluate(
            MetricContext(
                run_id="run-1",
                turns=(
                    TurnData(index=0, speaker=Speaker.CALLER),
                    TurnData(index=1, speaker=Speaker.AGENT),
                ),
                termination_reason=TerminationReason.AGENT_ENDED,
            ),
        )
        assert results.outcome is RunOutcome.ERROR
        assert "no accuracy metric was measurable" in results.invalid_reasons[0]

    def test_a_diagnostic_failure_alone_does_not_fail_the_run(self) -> None:
        """Slow but correct is a real distinction; only accuracy decides."""
        results = evaluate(
            working_context(
                turns=(
                    TurnData(index=0, speaker=Speaker.CALLER),
                    TurnData(index=1, speaker=Speaker.AGENT, response_ms=9000),
                ),
            ),
        )
        assert results.outcome is RunOutcome.PASSED
        assert results.by_name()["response_speed"].passed is False


class TestResults:
    def test_scores_are_addressable_by_name(self) -> None:
        results = evaluate(working_context())
        assert results.by_name()["task_completion"].value == 1.0

    def test_scores_are_addressable_by_category(self) -> None:
        results = evaluate(working_context())
        accuracy = {s.metric for s in results.by_category(MetricCategory.ACCURACY)}
        assert accuracy == {"task_completion", "expected_tools_invoked"}

    def test_accuracy_score_averages_measured_metrics(self) -> None:
        results = evaluate(
            working_context(
                tool_calls=(),  # expected_tools_invoked -> 0.0
                # task_completion still 1.0
            ),
        )
        assert results.accuracy_score() == 0.5

    def test_accuracy_score_is_none_when_nothing_was_measured(self) -> None:
        """Not 0.0 — an unmeasured run is not a failed one."""
        results = MetricRunner().run(
            MetricContext(run_id="r", termination_reason=TerminationReason.AGENT_ENDED),
        )
        assert results.accuracy_score() is None

    def test_every_score_carries_its_version(self) -> None:
        """Scores from different rubric versions are not comparable, so the
        version has to travel with the number."""
        results = evaluate(working_context())
        assert all(s.version for s in results.scores)
