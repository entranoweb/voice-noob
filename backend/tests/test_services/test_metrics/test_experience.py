"""Tests for the voice-specific experience metrics.

These are the measurements a text harness structurally cannot make. All of them
must report unmeasurable on a text-only run rather than scoring perfectly —
otherwise every text run would look like flawless audio.
"""

from __future__ import annotations

from app.monitoring.call_trace import Speaker
from app.services.qa.metrics.base import MetricContext, TurnData
from app.services.qa.metrics.experience.transcription_accuracy import (
    DEFAULT_MAX_WER,
    TranscriptionAccuracy,
    normalise,
    word_error_rate,
)
from app.services.qa.metrics.experience.turn_taking import (
    InterruptionHandling,
    TimeToFirstAudio,
)


def audio_context(*turns: TurnData, **criteria: object) -> MetricContext:
    return MetricContext(
        run_id="r",
        turns=turns,
        has_audio=True,
        success_criteria=dict(criteria),
    )


class TestWordErrorRate:
    def test_identical_text_scores_zero(self) -> None:
        assert word_error_rate("book me for tuesday", "book me for tuesday") == 0.0

    def test_one_substitution_in_four_words(self) -> None:
        assert word_error_rate("book me for tuesday", "book me for tunaday") == 0.25

    def test_a_deletion_counts(self) -> None:
        assert word_error_rate("book me for tuesday", "book for tuesday") == 0.25

    def test_an_insertion_counts(self) -> None:
        assert word_error_rate("book tuesday", "book me tuesday") == 0.5

    def test_it_can_exceed_one(self) -> None:
        """A system hallucinating words onto a short utterance is worse than one
        that drops them, and clamping to 1.0 would hide the difference."""
        rate = word_error_rate("hello", "hello there how are you doing today friend")
        assert rate is not None
        assert rate > 1.0

    def test_an_empty_reference_is_not_a_perfect_score(self) -> None:
        """There is no denominator. Calling it zero would flatter every silence."""
        assert word_error_rate("", "anything at all") is None

    def test_casing_and_punctuation_are_the_transcriber_s_choice(self) -> None:
        assert word_error_rate("Book me, please.", "book me please") == 0.0

    def test_normalise_strips_punctuation_without_joining_words(self) -> None:
        assert normalise("well,then") == ["well", "then"]


class TestTranscriptionAccuracy:
    def test_a_text_only_run_is_not_measurable(self) -> None:
        """Intended and transcribed are equal by construction, so a perfect
        score here would mean nothing."""
        score = TranscriptionAccuracy().compute(
            MetricContext(
                run_id="r",
                turns=(
                    TurnData(
                        index=0,
                        speaker=Speaker.CALLER,
                        text_intended="hello",
                        text_transcribed="hello",
                    ),
                ),
            ),
        )
        assert score.value is None

    def test_clean_audio_passes(self) -> None:
        score = TranscriptionAccuracy().compute(
            audio_context(
                TurnData(
                    index=0,
                    speaker=Speaker.CALLER,
                    text_intended="book me for tuesday",
                    text_transcribed="book me for tuesday",
                ),
            ),
        )
        assert score.value == 0.0
        assert score.passed is True

    def test_the_flagship_mishearing_fails(self) -> None:
        """The agent then reasons perfectly about the wrong day, and every
        text-driven metric calls the run a pass."""
        score = TranscriptionAccuracy().compute(
            audio_context(
                TurnData(
                    index=0,
                    speaker=Speaker.CALLER,
                    text_intended="book me for tuesday at two",
                    text_transcribed="book me for tuna day at two",
                ),
            ),
        )
        assert score.passed is False
        assert score.value is not None
        assert score.value > DEFAULT_MAX_WER

    def test_the_worst_turn_is_reported_separately(self) -> None:
        """An average hides one catastrophic turn among many clean ones, and the
        catastrophic one was probably the phone number."""
        score = TranscriptionAccuracy().compute(
            audio_context(
                TurnData(
                    index=0,
                    speaker=Speaker.CALLER,
                    text_intended="hello there",
                    text_transcribed="hello there",
                ),
                TurnData(
                    index=1,
                    speaker=Speaker.CALLER,
                    text_intended="five five five one two three",
                    text_transcribed="wide dive dive fun who free",
                ),
            ),
        )
        assert score.detail["worst_turn_wer"] == 1.0
        assert score.detail["turns_compared"] == 2

    def test_a_scenario_can_set_its_own_limit(self) -> None:
        score = TranscriptionAccuracy().compute(
            audio_context(
                TurnData(
                    index=0,
                    speaker=Speaker.CALLER,
                    text_intended="book me for tuesday",
                    text_transcribed="book me for tunaday",
                ),
                max_wer=0.5,
            ),
        )
        assert score.passed is True

    def test_audio_with_no_transcription_pairs_is_not_measurable(self) -> None:
        score = TranscriptionAccuracy().compute(
            audio_context(TurnData(index=0, speaker=Speaker.CALLER)),
        )
        assert score.value is None


class TestTimeToFirstAudio:
    def test_a_text_only_run_is_not_measurable(self) -> None:
        score = TimeToFirstAudio().compute(
            MetricContext(
                run_id="r",
                turns=(TurnData(index=0, speaker=Speaker.AGENT, ttfb_ms=200),),
            ),
        )
        assert score.value is None

    def test_a_responsive_agent_passes(self) -> None:
        score = TimeToFirstAudio().compute(
            audio_context(
                TurnData(index=0, speaker=Speaker.AGENT, ttfb_ms=300),
                TurnData(index=1, speaker=Speaker.AGENT, ttfb_ms=450),
            ),
        )
        assert score.passed is True

    def test_a_long_silence_before_speaking_fails(self) -> None:
        """Distinct from response_speed: the whole turn may be quick once it
        starts. What the caller experiences is the silence before it."""
        score = TimeToFirstAudio().compute(
            audio_context(
                TurnData(index=0, speaker=Speaker.AGENT, ttfb_ms=3200),
            ),
        )
        assert score.passed is False

    def test_caller_turns_are_ignored(self) -> None:
        """How fast the caller starts talking is not the agent's latency."""
        score = TimeToFirstAudio().compute(
            audio_context(
                TurnData(index=0, speaker=Speaker.CALLER, ttfb_ms=9000),
                TurnData(index=1, speaker=Speaker.AGENT, ttfb_ms=300),
            ),
        )
        assert score.detail["turns_measured"] == 1
        assert score.passed is True

    def test_audio_without_timings_is_not_measurable(self) -> None:
        score = TimeToFirstAudio().compute(
            audio_context(TurnData(index=0, speaker=Speaker.AGENT)),
        )
        assert score.value is None


class TestInterruptionHandling:
    def test_a_text_only_run_is_not_measurable(self) -> None:
        score = InterruptionHandling().compute(
            MetricContext(
                run_id="r",
                turns=(TurnData(index=0, speaker=Speaker.AGENT),),
            ),
        )
        assert score.value is None

    def test_a_call_with_no_interruptions_scores_zero_and_passes(self) -> None:
        """A real result, not an absent one: nothing interrupted the agent, so
        nothing was mishandled."""
        score = InterruptionHandling().compute(
            audio_context(TurnData(index=0, speaker=Speaker.AGENT)),
        )
        assert score.value == 0.0
        assert score.passed is True
        assert score.detail["barge_ins"] == 0

    def test_an_agent_that_stops_when_interrupted_passes(self) -> None:
        score = InterruptionHandling().compute(
            audio_context(
                TurnData(index=0, speaker=Speaker.AGENT, barge_in=True, interrupted=True),
            ),
        )
        assert score.value == 0.0
        assert score.passed is True

    def test_an_agent_that_talks_through_a_barge_in_fails(self) -> None:
        """Both parties speaking and neither being understood."""
        score = InterruptionHandling().compute(
            audio_context(
                TurnData(index=0, speaker=Speaker.AGENT, barge_in=True, interrupted=False),
            ),
        )
        assert score.value == 1.0
        assert score.passed is False

    def test_the_rate_is_over_barge_ins_not_over_all_turns(self) -> None:
        """Being interrupted often is the caller's behaviour; handling it badly
        is the agent's. Dividing by every turn would confuse the two."""
        score = InterruptionHandling().compute(
            audio_context(
                TurnData(index=0, speaker=Speaker.AGENT, barge_in=True, interrupted=True),
                TurnData(index=1, speaker=Speaker.AGENT, barge_in=True, interrupted=False),
                TurnData(index=2, speaker=Speaker.AGENT),
                TurnData(index=3, speaker=Speaker.AGENT),
            ),
        )
        assert score.value == 0.5
        assert score.detail["barge_ins"] == 2
