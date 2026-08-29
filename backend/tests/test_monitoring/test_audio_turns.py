"""Tests for reconstructing turns from a live audio bridge.

Most of these are assertions about what the recorder refuses to claim. That is
the point of the module: the three audio metrics are only worth anything if the
absence of a measurement stays visibly absent.
"""

from __future__ import annotations

import pytest

from app.monitoring.audio_turns import AudioTurnRecorder
from app.monitoring.call_trace import Speaker
from app.services.qa.metrics.context import build_context
from app.services.qa.metrics.runner import evaluate


class TestTimeToFirstAudio:
    def test_measures_the_gap_between_caller_silence_and_agent_audio(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.caller_speech_started(at=0.0)
        recorder.caller_speech_stopped(at=1.0)
        recorder.agent_audio_delta(byte_count=160, at=1.4)

        agent_turn = recorder.conversation()[1]
        assert agent_turn["ttfb_ms"] == pytest.approx(400.0)

    def test_an_opening_greeting_records_no_latency(self) -> None:
        """Nobody waited for it, so there is no wait to report."""
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.2)

        assert "ttfb_ms" not in recorder.conversation()[0]

    def test_the_clock_is_not_reused_across_turns(self) -> None:
        """A second agent turn with no caller turn before it has no latency."""
        recorder = AudioTurnRecorder()
        recorder.caller_speech_started(at=0.0)
        recorder.caller_speech_stopped(at=1.0)
        recorder.agent_audio_delta(byte_count=160, at=1.2)
        recorder.agent_turn_ended(at=1.5)
        recorder.agent_audio_delta(byte_count=160, at=2.0)

        turns = recorder.conversation()
        assert turns[1]["ttfb_ms"] == pytest.approx(200.0)
        assert "ttfb_ms" not in turns[2]

    def test_response_ms_reports_the_same_gap(self) -> None:
        """One observation point, so the two names carry one number."""
        recorder = AudioTurnRecorder()
        recorder.caller_speech_stopped(at=1.0)
        recorder.agent_audio_delta(byte_count=160, at=1.3)

        turn = recorder.conversation()[-1]
        assert turn["response_ms"] == turn["ttfb_ms"]
        assert turn["ttfb_ms"] == pytest.approx(300.0)


class TestBargeIn:
    def test_speaking_over_the_agent_marks_the_agents_turn(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.caller_speech_started(at=0.1)

        assert recorder.conversation()[0]["barge_in"] is True

    def test_speaking_after_the_agent_finished_is_not_a_barge_in(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.agent_turn_ended(at=0.5)
        recorder.caller_speech_started(at=1.0)

        assert recorder.conversation()[0]["barge_in"] is False

    def test_an_interrupted_turn_is_flagged_after_the_caller_turn_opens(self) -> None:
        """The cancellation arrives after the caller's turn has already started.

        Looking only at the trailing turn would drop the flag on exactly the
        turns that were interrupted, and every barge-in would then read as the
        agent talking over the caller.
        """
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.caller_speech_started(at=0.1)
        recorder.agent_turn_ended(interrupted=True, at=0.2)

        agent_turn = recorder.conversation()[0]
        assert agent_turn["barge_in"] is True
        assert agent_turn["interrupted"] is True

    def test_the_metric_sees_a_handled_interruption(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.caller_speech_started(at=0.1)
        recorder.agent_turn_ended(interrupted=True, at=0.2)

        scores = evaluate(
            build_context(
                run_id="r",
                conversation=recorder.conversation(),
                has_audio=recorder.has_audio,
            ),
        ).by_name()

        interruptions = scores["interruption_handling"]
        assert interruptions.value == 0.0
        assert interruptions.detail["talked_over"] == 0

    def test_the_metric_sees_an_agent_that_talked_through_it(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.caller_speech_started(at=0.1)
        recorder.agent_turn_ended(interrupted=False, at=0.9)

        scores = evaluate(
            build_context(
                run_id="r",
                conversation=recorder.conversation(),
                has_audio=recorder.has_audio,
            ),
        ).by_name()

        interruptions = scores["interruption_handling"]
        assert interruptions.value == 1.0
        assert interruptions.passed is False


class TestTranscriptionGroundTruth:
    def test_a_real_caller_leaves_word_error_rate_unmeasurable(self) -> None:
        """No script, no reference, no WER. Never a flawless zero."""
        recorder = AudioTurnRecorder()
        recorder.caller_speech_started(at=0.0)
        recorder.caller_speech_stopped(at=1.0)
        recorder.caller_transcript("book me for tuna day", at=1.1)

        turn = recorder.conversation()[0]
        assert turn["text_transcribed"] == "book me for tuna day"
        assert turn["text_intended"] is None

        scores = evaluate(
            build_context(
                run_id="r",
                conversation=recorder.conversation(),
                has_audio=recorder.has_audio,
            ),
        ).by_name()
        wer = scores["transcription_accuracy"]
        assert wer.value is None
        assert not wer.measured

    def test_a_scripted_caller_makes_word_error_rate_measurable(self) -> None:
        recorder = AudioTurnRecorder(intended_caller_script=["book me for Tuesday"])
        recorder.caller_speech_started(at=0.0)
        recorder.caller_speech_stopped(at=1.0)
        recorder.caller_transcript("book me for tuna day", at=1.1)

        scores = evaluate(
            build_context(
                run_id="r",
                conversation=recorder.conversation(),
                has_audio=recorder.has_audio,
            ),
        ).by_name()
        wer = scores["transcription_accuracy"]
        assert wer.measured
        # "book me for Tuesday" heard as "book me for tuna day": one
        # substitution and one insertion over four reference words.
        assert wer.value == 0.5

    def test_the_agents_own_audio_is_never_transcribed_back(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.agent_transcript_delta("Sure, ", at=0.0)
        recorder.agent_transcript_delta("Tuesday works.", at=0.1)

        turn = recorder.conversation()[0]
        assert turn["text_intended"] == "Sure, Tuesday works."
        assert turn["text_transcribed"] is None


class TestNoAudio:
    def test_a_recorder_that_saw_nothing_reports_no_audio(self) -> None:
        recorder = AudioTurnRecorder()
        assert recorder.has_audio is False
        assert recorder.conversation() == []

    def test_the_audio_metrics_stay_unmeasurable_rather_than_zero(self) -> None:
        recorder = AudioTurnRecorder()
        scores = evaluate(
            build_context(
                run_id="r",
                conversation=recorder.conversation(),
                has_audio=recorder.has_audio,
            ),
        ).by_name()

        for name in ("transcription_accuracy", "time_to_first_audio", "interruption_handling"):
            assert scores[name].value is None, f"{name} invented a number with no audio"


class TestAudioDuration:
    def test_duration_follows_the_bytes_actually_sent(self) -> None:
        recorder = AudioTurnRecorder()
        # 8000 bytes of µ-law at 8kHz is one second.
        recorder.agent_audio_delta(byte_count=8000, at=0.0)

        assert recorder.conversation()[0]["audio_duration_ms"] == 1000.0


class TestSpeakerAttribution:
    def test_turns_alternate_as_the_events_arrive(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.agent_turn_ended(at=0.5)
        recorder.caller_speech_started(at=1.0)
        recorder.caller_speech_stopped(at=2.0)
        recorder.agent_audio_delta(byte_count=160, at=2.2)

        speakers = [turn["speaker"] for turn in recorder.conversation()]
        assert speakers == [Speaker.AGENT.value, Speaker.CALLER.value, Speaker.AGENT.value]
