"""Tests for the producer of the ``voice.call`` span tree.

``call_trace`` defined this schema and nothing wrote it. These assert the shape a
dashboard or an evaluation consumer would read: the names, the parenting, and the
rule that an unmeasured attribute is absent rather than zero.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.monitoring.call_trace_emitter as emitter_module
from app.monitoring import call_trace as schema
from app.monitoring.audio_turns import AudioTurnRecorder
from app.monitoring.call_trace import (
    CallStatus,
    Direction,
    Speaker,
    TerminationReason,
    ToolOutcome,
)
from app.monitoring.call_trace_emitter import CallTraceEmitter


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = emitter_module._tracer  # noqa: SLF001
    emitter_module._tracer = provider.get_tracer(  # noqa: SLF001
        schema.INSTRUMENTATION_NAME,
        schema.INSTRUMENTATION_VERSION,
    )
    yield exporter
    emitter_module._tracer = original  # noqa: SLF001


class TestCallSpan:
    def test_writes_the_documented_call_attributes(self, spans: InMemorySpanExporter) -> None:
        with CallTraceEmitter(
            call_id="call-1",
            provider="telnyx",
            agent_id="agent-1",
            workspace_id="ws-1",
            direction=Direction.OUTBOUND,
            from_number="+15551230000",
            to_number="+15559997777",
            engine="openai_realtime",
        ) as emitter:
            emitter.end(
                status=CallStatus.COMPLETED,
                termination_reason=TerminationReason.CALLER_HANGUP,
                duration_ms=1234.0,
            )

        (call,) = [s for s in spans.get_finished_spans() if s.name == schema.SPAN_CALL]
        assert call.attributes is not None
        assert call.attributes[schema.CALL_ID] == "call-1"
        assert call.attributes[schema.CALL_PROVIDER] == "telnyx"
        assert call.attributes[schema.CALL_AGENT_ID] == "agent-1"
        assert call.attributes[schema.CALL_WORKSPACE_ID] == "ws-1"
        assert call.attributes[schema.CALL_DIRECTION] == "outbound"
        assert call.attributes[schema.CALL_TERMINATION_REASON] == "caller_hangup"
        assert call.attributes[schema.CALL_DURATION_MS] == 1234.0

    def test_an_unmeasured_attribute_is_absent(self, spans: InMemorySpanExporter) -> None:
        """Not zero, not an empty string — absent, so nothing downstream can
        mistake it for a measurement."""
        with CallTraceEmitter(call_id="call-1", provider="telnyx") as emitter:
            emitter.end(duration_ms=None)

        (call,) = [s for s in spans.get_finished_spans() if s.name == schema.SPAN_CALL]
        assert call.attributes is not None
        assert schema.CALL_DURATION_MS not in call.attributes
        assert schema.CALL_COST_MICROS not in call.attributes

    def test_an_exception_marks_the_call_failed(self, spans: InMemorySpanExporter) -> None:
        with pytest.raises(RuntimeError), CallTraceEmitter(call_id="c", provider="telnyx"):
            raise RuntimeError("bridge died")

        (call,) = [s for s in spans.get_finished_spans() if s.name == schema.SPAN_CALL]
        assert call.attributes is not None
        assert call.attributes[schema.CALL_STATUS] == "failed"
        assert call.attributes[schema.CALL_TERMINATION_REASON] == "pipeline_error"


class TestTurnSpans:
    def test_turns_are_children_of_the_call(self, spans: InMemorySpanExporter) -> None:
        with CallTraceEmitter(call_id="c", provider="telnyx") as emitter:
            emitter.record_turn(index=0, speaker=Speaker.AGENT, ttfb_ms=310.0)
            emitter.record_turn(index=1, speaker=Speaker.CALLER)

        finished = spans.get_finished_spans()
        (call,) = [s for s in finished if s.name == schema.SPAN_CALL]
        turns = [s for s in finished if s.name == schema.SPAN_TURN]

        assert len(turns) == 2
        for turn in turns:
            assert turn.parent is not None
            assert turn.parent.span_id == call.context.span_id
        assert call.attributes is not None
        assert call.attributes[schema.CALL_TURN_COUNT] == 2

    def test_a_turn_with_no_latency_omits_the_attribute(
        self,
        spans: InMemorySpanExporter,
    ) -> None:
        with CallTraceEmitter(call_id="c", provider="telnyx") as emitter:
            emitter.record_turn(index=0, speaker=Speaker.AGENT)

        (turn,) = [s for s in spans.get_finished_spans() if s.name == schema.SPAN_TURN]
        assert turn.attributes is not None
        assert schema.TURN_TTFB_MS not in turn.attributes
        # The booleans are always written: false is a measurement here.
        assert turn.attributes[schema.TURN_BARGE_IN] is False

    def test_a_recorders_turns_keep_their_measured_durations(
        self,
        spans: InMemorySpanExporter,
    ) -> None:
        """The tree is written at the end of the call; the times are not."""
        recorder = AudioTurnRecorder()
        recorder.agent_audio_delta(byte_count=160, at=0.0)
        recorder.agent_turn_ended(at=0.5)

        with CallTraceEmitter(call_id="c", provider="telnyx") as emitter:
            emitter.record_turns(recorder.conversation(), base_time_ns=1_000_000_000)

        (turn,) = [s for s in spans.get_finished_spans() if s.name == schema.SPAN_TURN]
        assert turn.end_time is not None
        assert turn.start_time is not None
        assert (turn.end_time - turn.start_time) == pytest.approx(500_000_000, rel=1e-6)


class TestToolSpans:
    def test_a_failing_tool_call_carries_its_error(self, spans: InMemorySpanExporter) -> None:
        with CallTraceEmitter(call_id="c", provider="telnyx") as emitter:
            emitter.record_tool_call(
                name="book_appointment",
                outcome=ToolOutcome.ERROR,
                arguments={"day": "Tuesday"},
                error="calendar unreachable",
            )

        (tool,) = [s for s in spans.get_finished_spans() if s.name == schema.SPAN_TOOL_CALL]
        assert tool.attributes is not None
        assert tool.attributes[schema.TOOL_NAME] == "book_appointment"
        assert tool.attributes[schema.TOOL_OUTCOME] == "error"
        assert tool.attributes[schema.TOOL_ERROR] == "calendar unreachable"
        assert '"day": "Tuesday"' in str(tool.attributes[schema.TOOL_ARGUMENTS])

    def test_unserialisable_arguments_do_not_raise(self, spans: InMemorySpanExporter) -> None:
        with CallTraceEmitter(call_id="c", provider="telnyx") as emitter:
            emitter.record_tool_call(
                name="t",
                outcome=ToolOutcome.OK,
                arguments={"when": object()},
            )

        (tool,) = [s for s in spans.get_finished_spans() if s.name == schema.SPAN_TOOL_CALL]
        assert tool.attributes is not None
        assert schema.TOOL_ARGUMENTS in tool.attributes
