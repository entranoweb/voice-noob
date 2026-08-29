"""Tests for tool-call metrics."""

from __future__ import annotations

from typing import Any

from app.monitoring.call_trace import ToolOutcome
from app.services.qa.metrics.base import MetricCategory, MetricContext, ToolCallData
from app.services.qa.metrics.diagnostic.tool_call_validity import (
    ExpectedToolsInvoked,
    ToolCallValidity,
)


def call(name: str, outcome: ToolOutcome = ToolOutcome.OK, **kwargs: Any) -> ToolCallData:
    return ToolCallData(name=name, outcome=outcome, **kwargs)


def context(**kwargs: Any) -> MetricContext:
    return MetricContext(run_id="run-1", **kwargs)


class TestToolCallValidity:
    metric = ToolCallValidity()

    def test_all_well_formed_scores_one(self) -> None:
        score = self.metric.compute(
            context(tool_calls=(call("book_appointment"), call("check_availability"))),
        )
        assert score.value == 1.0
        assert score.passed is True

    def test_malformed_arguments_lower_the_score(self) -> None:
        score = self.metric.compute(
            context(
                tool_calls=(
                    call("book_appointment", ToolOutcome.INVALID_ARGS, error="missing date"),
                    call("check_availability"),
                ),
            ),
        )
        assert score.value == 0.5
        assert score.passed is False
        assert score.detail["malformed_calls"] == [
            {"name": "book_appointment", "error": "missing date"},
        ]

    def test_a_downstream_error_is_not_a_validity_failure(self) -> None:
        """'Reservation not found' means the call was well formed and the answer
        was no. Counting it as malformed would blame the model for a data issue.
        """
        score = self.metric.compute(
            context(tool_calls=(call("get_reservation", ToolOutcome.ERROR, error="not found"),)),
        )
        assert score.value == 1.0
        assert score.passed is True

    def test_a_timeout_is_not_a_validity_failure(self) -> None:
        score = self.metric.compute(
            context(tool_calls=(call("book_appointment", ToolOutcome.TIMEOUT),)),
        )
        assert score.passed is True

    def test_no_calls_is_unmeasurable_not_zero(self) -> None:
        """A conversation needing no tools must not look broken."""
        score = self.metric.compute(context())
        assert score.value is None
        assert score.passed is None

    def test_is_diagnostic_not_accuracy(self) -> None:
        """Malformed calls explain a failure; they do not define one."""
        assert self.metric.category is MetricCategory.DIAGNOSTIC


class TestExpectedToolsInvoked:
    metric = ExpectedToolsInvoked()

    def test_is_an_accuracy_metric(self) -> None:
        """This is the one that decides whether the agent did its job."""
        assert self.metric.category is MetricCategory.ACCURACY

    def test_passes_when_the_required_tool_ran(self) -> None:
        score = self.metric.compute(
            context(
                expected_tool_calls=({"tool": "book_appointment"},),
                tool_calls=(call("book_appointment"),),
            ),
        )
        assert score.value == 1.0
        assert score.passed is True

    def test_fails_when_the_required_tool_never_ran(self) -> None:
        """The flagship regression: booking silently stops happening."""
        score = self.metric.compute(
            context(
                expected_tool_calls=({"tool": "book_appointment"},),
                tool_calls=(call("check_availability"),),
            ),
        )
        assert score.value == 0.0
        assert score.passed is False
        assert score.detail["missing"] == ["book_appointment"]
        assert score.detail["attempted_but_failed"] == []

    def test_a_required_tool_that_errored_does_not_count_as_invoked(self) -> None:
        """From the caller's side the appointment was not booked either way."""
        score = self.metric.compute(
            context(
                expected_tool_calls=({"tool": "book_appointment"},),
                tool_calls=(call("book_appointment", ToolOutcome.ERROR, error="upstream 500"),),
            ),
        )
        assert score.passed is False
        assert score.detail["attempted_but_failed"] == ["book_appointment"]
        assert score.detail["missing"] == ["book_appointment"]

    def test_reads_must_invoke_tools_from_success_criteria(self) -> None:
        """The scenario library declares requirements in two places; both count."""
        score = self.metric.compute(
            context(
                success_criteria={"must_invoke_tools": ["book_appointment"]},
                tool_calls=(call("book_appointment"),),
            ),
        )
        assert score.passed is True
        assert score.detail["required"] == ["book_appointment"]

    def test_reads_the_name_key_as_well_as_tool(self) -> None:
        score = self.metric.compute(
            context(
                expected_tool_calls=({"name": "send_sms"},),
                tool_calls=(call("send_sms"),),
            ),
        )
        assert score.passed is True

    def test_merges_requirements_from_both_sources(self) -> None:
        score = self.metric.compute(
            context(
                expected_tool_calls=({"tool": "book_appointment"},),
                success_criteria={"must_invoke_tools": ["send_sms"]},
                tool_calls=(call("book_appointment"),),
            ),
        )
        assert score.detail["required"] == ["book_appointment", "send_sms"]
        assert score.detail["missing"] == ["send_sms"]
        assert score.value == 0.5

    def test_partial_credit_reflects_how_many_ran(self) -> None:
        score = self.metric.compute(
            context(
                expected_tool_calls=({"tool": "a"}, {"tool": "b"}, {"tool": "c"}, {"tool": "d"}),
                tool_calls=(call("a"), call("b"), call("c")),
            ),
        )
        assert score.value == 0.75
        assert score.passed is False

    def test_unmeasurable_when_the_scenario_requires_nothing(self) -> None:
        score = self.metric.compute(context(tool_calls=(call("anything"),)))
        assert score.value is None
        assert score.passed is None

    def test_extra_tools_beyond_the_requirement_are_allowed(self) -> None:
        score = self.metric.compute(
            context(
                expected_tool_calls=({"tool": "book_appointment"},),
                tool_calls=(call("book_appointment"), call("send_sms")),
            ),
        )
        assert score.passed is True
