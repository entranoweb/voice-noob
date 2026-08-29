"""Emit the span tree that ``call_trace`` defines.

``call_trace`` is a vocabulary: constants, enums, and a documented span tree. Up
to now that is all it was — nothing in the codebase produced a ``voice.call``
span, so the schema the dashboard and the evaluation framework both claim to read
had no producer. This module is the producer.

It talks to the OpenTelemetry *API* only, never the SDK. When no tracer provider
is configured — the default, and the case in most tests — the API hands back
non-recording spans and every call here becomes close to free. Nothing needs to
check whether tracing is on before recording a call.

Turn spans are emitted with explicit start and end timestamps taken from the
recorder rather than from when this code happens to run. A turn that took 300ms
must appear as 300ms in the trace even though the whole tree is written at the
end of the call, once the bridge knows how each turn terminated.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from types import TracebackType

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.monitoring import call_trace as schema
from app.monitoring.call_trace import (
    CallStatus,
    Direction,
    Speaker,
    TerminationReason,
    ToolOutcome,
)

_tracer = trace.get_tracer(schema.INSTRUMENTATION_NAME, schema.INSTRUMENTATION_VERSION)

_MS_TO_NS = 1_000_000


def _set(span: Span, key: str, value: Any) -> None:
    """Set an attribute unless the value is absent.

    An unmeasured attribute is left off the span entirely. Writing ``0`` or ``""``
    for it would be indistinguishable, downstream, from a real measurement.
    """
    if value is None:
        return
    span.set_attribute(key, value)


class CallTraceEmitter:
    """One call's span tree.

    Use as a context manager around the life of a call; the ``voice.call`` span
    opens on entry and closes on exit, whatever happened in between.
    """

    def __init__(
        self,
        *,
        call_id: str,
        provider: str,
        agent_id: str | None = None,
        workspace_id: str | None = None,
        provider_call_id: str | None = None,
        direction: Direction = Direction.INBOUND,
        from_number: str | None = None,
        to_number: str | None = None,
        engine: str | None = None,
        engine_model: str | None = None,
        stt_vendor: str | None = None,
        tts_vendor: str | None = None,
        carrier: str | None = None,
    ) -> None:
        self._span = _tracer.start_span(schema.SPAN_CALL, kind=SpanKind.SERVER)
        self._context = trace.set_span_in_context(self._span)
        self._turn_count = 0
        self._ended = False

        _set(self._span, schema.CALL_ID, call_id)
        _set(self._span, schema.CALL_PROVIDER, provider)
        _set(self._span, schema.CALL_PROVIDER_CALL_ID, provider_call_id or call_id)
        _set(self._span, schema.CALL_AGENT_ID, agent_id)
        _set(self._span, schema.CALL_WORKSPACE_ID, workspace_id)
        _set(self._span, schema.CALL_DIRECTION, direction.value)
        _set(self._span, schema.CALL_FROM, from_number)
        _set(self._span, schema.CALL_TO, to_number)
        _set(self._span, schema.CALL_ENGINE, engine)
        _set(self._span, schema.CALL_ENGINE_MODEL, engine_model)
        _set(self._span, schema.CALL_STT_VENDOR, stt_vendor)
        _set(self._span, schema.CALL_TTS_VENDOR, tts_vendor)
        _set(self._span, schema.CALL_CARRIER, carrier)

    # -- turns and tools --------------------------------------------------

    def record_turn(
        self,
        *,
        index: int,
        speaker: Speaker,
        start_time_ns: int | None = None,
        end_time_ns: int | None = None,
        text_intended: str | None = None,
        text_transcribed: str | None = None,
        response_ms: float | None = None,
        ttfb_ms: float | None = None,
        audio_duration_ms: float | None = None,
        interrupted: bool = False,
        barge_in: bool = False,
    ) -> None:
        """Emit one ``voice.turn`` span, parented to this call."""
        span = _tracer.start_span(
            schema.SPAN_TURN,
            context=self._context,
            kind=SpanKind.INTERNAL,
            start_time=start_time_ns,
        )
        _set(span, schema.TURN_INDEX, index)
        _set(span, schema.TURN_SPEAKER, speaker.value)
        _set(span, schema.TURN_TEXT_INTENDED, text_intended)
        _set(span, schema.TURN_TEXT_TRANSCRIBED, text_transcribed)
        _set(span, schema.TURN_RESPONSE_MS, response_ms)
        _set(span, schema.TURN_TTFB_MS, ttfb_ms)
        _set(span, schema.TURN_AUDIO_DURATION_MS, audio_duration_ms)
        span.set_attribute(schema.TURN_INTERRUPTED, interrupted)
        span.set_attribute(schema.TURN_BARGE_IN, barge_in)
        span.end(end_time=end_time_ns)
        self._turn_count += 1

    def record_tool_call(
        self,
        *,
        name: str,
        outcome: ToolOutcome,
        arguments: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
        start_time_ns: int | None = None,
    ) -> None:
        """Emit one ``voice.tool_call`` span.

        Parented to the call rather than to a turn: the bridge learns a tool ran
        from the model's event stream, which does not reliably say which turn it
        belonged to. A wrong parent would be worse than a flat one.
        """
        span = _tracer.start_span(
            schema.SPAN_TOOL_CALL,
            context=self._context,
            kind=SpanKind.INTERNAL,
            start_time=start_time_ns,
        )
        _set(span, schema.TOOL_NAME, name)
        _set(span, schema.TOOL_OUTCOME, outcome.value)
        _set(span, schema.TOOL_DURATION_MS, duration_ms)
        if arguments is not None:
            _set(span, schema.TOOL_ARGUMENTS, _safe_json(arguments))
        _set(span, schema.TOOL_ERROR, error)
        if outcome is not ToolOutcome.OK:
            span.set_status(Status(StatusCode.ERROR, error or outcome.value))
        span.end()

    def record_turns(self, conversation: list[dict[str, Any]], *, base_time_ns: int) -> None:
        """Emit turn spans for a recorder's conversation.

        ``base_time_ns`` anchors the relative turn times onto the wall clock.
        Turn records carry no absolute timestamps of their own — they are
        measured on a monotonic clock, which is correct for durations and
        meaningless as a date.
        """
        offset_ns = 0
        for index, turn in enumerate(conversation):
            speaker = _speaker(turn.get("speaker"))
            if speaker is None:
                continue
            response_ms = _as_float(turn.get("response_ms"))
            # Span length is how long the turn lasted, which is not its latency.
            duration_ms = _as_float(turn.get("duration_ms")) or 0.0
            duration_ns = int(duration_ms * _MS_TO_NS)
            start_ns = base_time_ns + offset_ns
            self.record_turn(
                index=index,
                speaker=speaker,
                start_time_ns=start_ns,
                end_time_ns=start_ns + duration_ns,
                text_intended=turn.get("text_intended"),
                text_transcribed=turn.get("text_transcribed"),
                response_ms=response_ms,
                ttfb_ms=_as_float(turn.get("ttfb_ms")),
                audio_duration_ms=_as_float(turn.get("audio_duration_ms")),
                interrupted=bool(turn.get("interrupted", False)),
                barge_in=bool(turn.get("barge_in", False)),
            )
            offset_ns += duration_ns

    # -- lifecycle --------------------------------------------------------

    def end(
        self,
        *,
        status: CallStatus = CallStatus.COMPLETED,
        termination_reason: TerminationReason = TerminationReason.UNKNOWN,
        termination_provider_value: str | None = None,
        duration_ms: float | None = None,
        answer_latency_ms: float | None = None,
        recording_url: str | None = None,
        cost_micros: int | None = None,
        cost_currency: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        provider_raw: dict[str, Any] | None = None,
    ) -> None:
        """Close the call span. Safe to call twice; the second call is a no-op."""
        if self._ended:
            return
        self._ended = True

        _set(self._span, schema.CALL_STATUS, status.value)
        _set(self._span, schema.CALL_TERMINATION_REASON, termination_reason.value)
        _set(self._span, schema.CALL_TERMINATION_PROVIDER_VALUE, termination_provider_value)
        _set(self._span, schema.CALL_DURATION_MS, duration_ms)
        _set(self._span, schema.CALL_ANSWER_LATENCY_MS, answer_latency_ms)
        _set(self._span, schema.CALL_RECORDING_URL, recording_url)
        _set(self._span, schema.CALL_COST_MICROS, cost_micros)
        _set(self._span, schema.CALL_COST_CURRENCY, cost_currency)
        _set(self._span, schema.CALL_TOKENS_INPUT, tokens_input)
        _set(self._span, schema.CALL_TOKENS_OUTPUT, tokens_output)
        self._span.set_attribute(schema.CALL_TURN_COUNT, self._turn_count)
        if provider_raw is not None:
            _set(self._span, schema.CALL_PROVIDER_RAW, _safe_json(provider_raw))

        if status is not CallStatus.COMPLETED:
            self._span.set_status(Status(StatusCode.ERROR, status.value))
        self._span.end()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None and not self._ended:
            self._span.record_exception(exc)
            self.end(
                status=CallStatus.FAILED,
                termination_reason=TerminationReason.PIPELINE_ERROR,
            )
        self.end()


def _speaker(value: Any) -> Speaker | None:
    if isinstance(value, Speaker):
        return value
    if isinstance(value, str):
        try:
            return Speaker(value)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_json(value: dict[str, Any]) -> str:
    """JSON, or a description of why it is not.

    A span attribute is not worth raising over, and an unserialisable payload is
    still evidence of what the provider sent.
    """
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers nearly all
        return json.dumps({"unserialisable": repr(value)[:512]})


__all__ = ["CallTraceEmitter"]
