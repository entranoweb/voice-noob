"""Tests for building a MetricContext from recorded run data."""

from __future__ import annotations

from app.monitoring.call_trace import Speaker, TerminationReason, ToolOutcome
from app.services.qa.metrics.context import (
    build_context,
    tool_calls_from_records,
    turns_from_conversation,
)


class TestTurnConversion:
    def test_maps_the_runner_speaker_vocabulary(self) -> None:
        """The runner says 'user'; the trace schema says 'caller'."""
        turns = turns_from_conversation(
            [
                {"speaker": "user", "message": "hello"},
                {"speaker": "agent", "message": "hi"},
            ],
        )
        assert [t.speaker for t in turns] == [Speaker.CALLER, Speaker.AGENT]

    def test_accepts_the_openai_role_vocabulary(self) -> None:
        turns = turns_from_conversation(
            [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "hey"}],
        )
        assert [t.speaker for t in turns] == [Speaker.AGENT, Speaker.CALLER]

    def test_drops_turns_with_an_unresolvable_speaker(self) -> None:
        """Guessing would attribute a turn to the wrong side and corrupt every
        per-speaker metric."""
        turns = turns_from_conversation(
            [{"speaker": "narrator", "message": "?"}, {"speaker": "agent", "message": "hi"}],
        )
        assert len(turns) == 1
        assert turns[0].speaker is Speaker.AGENT

    def test_ignores_malformed_entries(self) -> None:
        """Conversations come out of a JSON column and can hold anything."""
        turns = turns_from_conversation(["not a dict", None, {"speaker": "agent", "message": "hi"}])
        assert len(turns) == 1

    def test_text_only_runs_report_intended_and_transcribed_alike(self) -> None:
        turns = turns_from_conversation([{"speaker": "agent", "message": "hello"}])
        assert turns[0].text_intended == "hello"
        assert turns[0].text_transcribed == "hello"

    def test_a_turn_with_only_a_transcript_gets_no_invented_reference(self) -> None:
        """A live caller turn declares what was heard and nothing else.

        Falling back to the turn's message here would set intended equal to
        transcribed and score every real call a flawless word error rate, which
        is exactly the failure the metric exists to catch.
        """
        turns = turns_from_conversation(
            [
                {
                    "speaker": "caller",
                    "message": "book me for tuna day",
                    "text_transcribed": "book me for tuna day",
                },
            ],
        )
        assert turns[0].text_transcribed == "book me for tuna day"
        assert turns[0].text_intended is None

    def test_a_turn_with_only_intended_text_gets_no_invented_transcript(self) -> None:
        """Nothing transcribes the agent's own audio back off the line."""
        turns = turns_from_conversation(
            [{"speaker": "agent", "text_intended": "Sure, Tuesday works."}],
        )
        assert turns[0].text_intended == "Sure, Tuesday works."
        assert turns[0].text_transcribed is None

    def test_audio_runs_keep_the_two_apart(self) -> None:
        """The delta between them is the voice-specific signal."""
        turns = turns_from_conversation(
            [
                {
                    "speaker": "caller",
                    "text_intended": "book me for Tuesday",
                    "text_transcribed": "book me for tuna day",
                },
            ],
        )
        assert turns[0].text_intended != turns[0].text_transcribed

    def test_empty_conversation_yields_no_turns(self) -> None:
        assert turns_from_conversation(None) == ()
        assert turns_from_conversation([]) == ()

    def test_unparseable_latency_is_absent_not_zero(self) -> None:
        """A zero would quietly flatter every timing percentile."""
        turns = turns_from_conversation([{"speaker": "agent", "response_ms": "not a number"}])
        assert turns[0].response_ms is None

    def test_boolean_is_not_treated_as_a_number(self) -> None:
        turns = turns_from_conversation([{"speaker": "agent", "response_ms": True}])
        assert turns[0].response_ms is None


class TestToolCallConversion:
    def test_reads_an_explicit_outcome(self) -> None:
        calls = tool_calls_from_records([{"name": "book", "outcome": "invalid_args"}])
        assert calls[0].outcome is ToolOutcome.INVALID_ARGS

    def test_infers_error_from_an_error_field(self) -> None:
        calls = tool_calls_from_records([{"name": "book", "error": "upstream 500"}])
        assert calls[0].outcome is ToolOutcome.ERROR

    def test_infers_invalid_args(self) -> None:
        calls = tool_calls_from_records([{"name": "book", "invalid_args": True}])
        assert calls[0].outcome is ToolOutcome.INVALID_ARGS

    def test_infers_timeout(self) -> None:
        calls = tool_calls_from_records([{"name": "book", "timed_out": True}])
        assert calls[0].outcome is ToolOutcome.TIMEOUT

    def test_defaults_to_ok_when_there_is_no_evidence_of_failure(self) -> None:
        """Never invent a failure — that is how a metric stops being trusted."""
        assert tool_calls_from_records([{"name": "book"}])[0].outcome is ToolOutcome.OK

    def test_an_unrecognised_outcome_string_falls_back_to_inference(self) -> None:
        calls = tool_calls_from_records([{"name": "book", "outcome": "weird", "error": "boom"}])
        assert calls[0].outcome is ToolOutcome.ERROR

    def test_accepts_tool_as_well_as_name(self) -> None:
        assert tool_calls_from_records([{"tool": "book"}])[0].name == "book"

    def test_drops_records_without_a_name(self) -> None:
        assert tool_calls_from_records([{"arguments": {}}]) == ()


class TestBuildContext:
    def test_reads_expected_db_state_from_success_criteria(self) -> None:
        """Scenarios declare it there, so no schema change is needed to start
        asserting on database state."""
        context = build_context(
            run_id="r",
            success_criteria={"expected_db_state": {"appointments": [{"status": "scheduled"}]}},
        )
        assert context.expected_db_state == {"appointments": [{"status": "scheduled"}]}

    def test_explicit_expected_state_wins_over_criteria(self) -> None:
        context = build_context(
            run_id="r",
            expected_db_state={"contacts": []},
            success_criteria={"expected_db_state": {"appointments": []}},
        )
        assert context.expected_db_state == {"contacts": []}

    def test_defaults_are_safe_for_an_empty_run(self) -> None:
        context = build_context(run_id="r")
        assert context.turns == ()
        assert context.tool_calls == ()
        assert context.termination_reason is TerminationReason.UNKNOWN
        assert context.expected_db_state is None

    def test_agent_and_caller_turns_are_separable(self) -> None:
        context = build_context(
            run_id="r",
            conversation=[
                {"speaker": "user", "message": "hi"},
                {"speaker": "agent", "message": "hello"},
                {"speaker": "agent", "message": "how can I help?"},
            ],
        )
        assert len(context.caller_turns()) == 1
        assert len(context.agent_turns()) == 2
