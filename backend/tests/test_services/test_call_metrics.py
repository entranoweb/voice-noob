"""Tests for computing metrics from a call that really happened.

The asymmetry these assert is the honest one: a real call can be measured for
latency and interruptions and cannot be measured for word error rate, because a
human caller arrives with no script to compare against.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.call_record import CallRecord, CallStatus
from app.monitoring.audio_turns import AudioTurnRecorder
from app.services.qa.call_metrics import context_for_call, metrics_for_call


def _record(**kwargs: object) -> CallRecord:
    """A call record built in memory. Nothing here touches the database."""
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "provider": "telnyx",
        "provider_call_id": "v3:abc",
        "direction": "inbound",
        "status": CallStatus.COMPLETED.value,
        "from_number": "+15551230000",
        "to_number": "+15559997777",
        "duration_seconds": 30,
    }
    defaults.update(kwargs)
    return CallRecord(**defaults)


def _recorded_call() -> list[dict[str, object]]:
    recorder = AudioTurnRecorder()
    recorder.agent_audio_delta(byte_count=8000, at=0.0)
    recorder.agent_turn_ended(at=1.0)
    recorder.caller_speech_started(at=1.2)
    recorder.caller_speech_stopped(at=2.0)
    recorder.caller_transcript("book me for Tuesday", at=2.1)
    recorder.agent_audio_delta(byte_count=8000, at=2.4)
    recorder.caller_speech_started(at=2.6)
    recorder.agent_turn_ended(interrupted=True, at=2.7)
    return recorder.conversation()


class TestARealCall:
    def test_latency_and_interruptions_are_measurable(self) -> None:
        scores = metrics_for_call(_record(turns=_recorded_call())).by_name()

        ttfb = scores["time_to_first_audio"]
        assert ttfb.measured
        assert ttfb.value == pytest.approx(400.0)

        interruptions = scores["interruption_handling"]
        assert interruptions.measured
        assert interruptions.detail["barge_ins"] == 1
        assert interruptions.detail["talked_over"] == 0

    def test_word_error_rate_is_not(self) -> None:
        scores = metrics_for_call(_record(turns=_recorded_call())).by_name()

        wer = scores["transcription_accuracy"]
        assert wer.value is None
        assert "intended" in str(wer.detail)

    def test_a_scenario_metric_reports_no_expectation_rather_than_failure(self) -> None:
        """A real call declares no expected end state, so task completion has
        nothing to check. That is unmeasurable, not failed."""
        scores = metrics_for_call(_record(turns=_recorded_call())).by_name()

        assert scores["task_completion"].value is None
        assert scores["state_restored"].value is None


class TestACallWithNoAudio:
    def test_the_audio_metrics_stay_unmeasurable(self) -> None:
        """No turns stored means the bridge recorded no audio. The three audio
        metrics must say so rather than score a zero."""
        scores = metrics_for_call(_record(turns=None)).by_name()

        for name in ("transcription_accuracy", "time_to_first_audio", "interruption_handling"):
            assert scores[name].value is None, f"{name} invented a number for a silent call"

    def test_the_context_reports_no_audio(self) -> None:
        assert context_for_call(_record(turns=None)).has_audio is False
        assert context_for_call(_record(turns=_recorded_call())).has_audio is True


class TestTermination:
    def test_a_completed_call_reads_as_a_caller_hangup(self) -> None:
        context = context_for_call(_record(status=CallStatus.COMPLETED.value))
        assert context.termination_reason.value == "caller_hangup"

    def test_a_call_still_in_progress_claims_nothing(self) -> None:
        context = context_for_call(_record(status=CallStatus.RINGING.value))
        assert context.termination_reason.value == "unknown"

    def test_a_zero_duration_is_not_reported_as_a_measured_zero(self) -> None:
        assert context_for_call(_record(duration_seconds=0)).duration_ms is None
