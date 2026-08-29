"""Tests for the validation gates and response-speed metric."""

from __future__ import annotations

from typing import Any

from app.monitoring.call_trace import Speaker, TerminationReason
from app.services.qa.metrics.base import MetricCategory, MetricContext, TurnData
from app.services.qa.metrics.diagnostic.response_speed import ResponseSpeed, percentile
from app.services.qa.metrics.validation.conversation_valid_end import (
    ConversationHasTurns,
    ConversationValidEnd,
)


def context(**kwargs: Any) -> MetricContext:
    return MetricContext(run_id="run-1", **kwargs)


def turn(index: int, speaker: Speaker, **kwargs: Any) -> TurnData:
    return TurnData(index=index, speaker=speaker, **kwargs)


def exchange(response_ms: list[float] | None = None) -> tuple[TurnData, ...]:
    """A caller turn followed by an agent turn, optionally with latencies."""
    turns: list[TurnData] = []
    for i, ms in enumerate(response_ms or [None]):  # type: ignore[arg-type]
        turns.append(turn(i * 2, Speaker.CALLER, text_intended="hello"))
        turns.append(turn(i * 2 + 1, Speaker.AGENT, text_intended="hi", response_ms=ms))
    return tuple(turns)


class TestConversationValidEnd:
    metric = ConversationValidEnd()

    def test_is_a_validation_gate(self) -> None:
        assert self.metric.category is MetricCategory.VALIDATION

    def test_a_caller_hangup_is_a_real_outcome(self) -> None:
        """The agent owns a caller hanging up. It is scoreable, possibly badly."""
        score = self.metric.compute(
            context(termination_reason=TerminationReason.CALLER_HANGUP),
        )
        assert score.passed is True
        assert score.detail["harness_failure"] is False

    def test_agent_ending_the_call_is_valid(self) -> None:
        score = self.metric.compute(context(termination_reason=TerminationReason.AGENT_ENDED))
        assert score.passed is True

    def test_a_transfer_is_valid(self) -> None:
        score = self.metric.compute(context(termination_reason=TerminationReason.TRANSFERRED))
        assert score.passed is True

    def test_a_pipeline_error_invalidates_the_run(self) -> None:
        """The stack broke. Scoring the agent here manufactures a failure."""
        score = self.metric.compute(
            context(termination_reason=TerminationReason.PIPELINE_ERROR),
        )
        assert score.passed is False
        assert score.detail["harness_failure"] is True

    def test_an_unknown_ending_invalidates_the_run(self) -> None:
        score = self.metric.compute(context(termination_reason=TerminationReason.UNKNOWN))
        assert score.passed is False
        assert score.detail["harness_failure"] is True


class TestConversationHasTurns:
    metric = ConversationHasTurns()

    def test_a_normal_exchange_passes(self) -> None:
        assert self.metric.compute(context(turns=exchange())).passed is True

    def test_an_empty_conversation_fails_validation(self) -> None:
        score = self.metric.compute(context())
        assert score.passed is False
        assert score.detail == {"agent_turns": 0, "caller_turns": 0}

    def test_a_caller_who_got_no_reply_fails_validation(self) -> None:
        """No agent turn means no behaviour to grade."""
        score = self.metric.compute(context(turns=(turn(0, Speaker.CALLER),)))
        assert score.passed is False
        assert score.detail["agent_turns"] == 0


class TestPercentile:
    def test_median_of_odd_length(self) -> None:
        assert percentile([3.0, 1.0, 2.0], 50) == 2.0

    def test_p95_picks_the_tail(self) -> None:
        values = [float(n) for n in range(1, 101)]
        assert percentile(values, 95) == 95.0

    def test_empty_is_zero(self) -> None:
        assert percentile([], 95) == 0.0

    def test_single_value(self) -> None:
        assert percentile([42.0], 95) == 42.0


class TestResponseSpeed:
    metric = ResponseSpeed()

    def test_reports_percentiles_not_just_a_mean(self) -> None:
        score = self.metric.compute(context(turns=exchange([100, 120, 110, 3000])))
        assert score.detail["max_ms"] == 3000.0
        assert score.detail["p50_ms"] < score.detail["p95_ms"]

    def test_one_bad_stall_fails_even_when_the_rest_are_fast(self) -> None:
        """A mean would hide this; a caller would not."""
        score = self.metric.compute(context(turns=exchange([90, 95, 100, 5000])))
        assert score.passed is False

    def test_a_consistently_fast_call_passes(self) -> None:
        score = self.metric.compute(context(turns=exchange([200, 250, 180, 220])))
        assert score.passed is True
        assert score.unit == "ms"

    def test_scenario_can_tighten_the_limit(self) -> None:
        """A scenario declaring a 5s budget should not fail at 3s."""
        turns = exchange([3000, 3000])
        assert self.metric.compute(context(turns=turns)).passed is False
        tightened = self.metric.compute(
            context(turns=turns, success_criteria={"response_time_limit_seconds": 5}),
        )
        assert tightened.passed is True
        assert tightened.detail["limit_ms"] == 5000.0

    def test_caller_turn_latencies_are_ignored(self) -> None:
        """Only the gap before the agent speaks is the caller's experience."""
        turns = (
            turn(0, Speaker.CALLER, response_ms=9999),
            turn(1, Speaker.AGENT, response_ms=100),
        )
        score = self.metric.compute(context(turns=turns))
        assert score.detail["turns_measured"] == 1
        assert score.detail["max_ms"] == 100.0

    def test_unmeasurable_without_timings(self) -> None:
        """A text-only run has no latency to report, and must not report zero."""
        score = self.metric.compute(context(turns=exchange()))
        assert score.value is None
        assert score.passed is None
