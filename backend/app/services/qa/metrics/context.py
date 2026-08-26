"""Build a MetricContext from what a run produced.

Metrics are pure functions over a context, so all the messy shape-wrangling —
provider payloads, conversation dicts, tool records — happens here, once. Adding
a provider adapter later means teaching this module a new input shape, not
touching a single metric.
"""

from __future__ import annotations

from typing import Any

from app.monitoring.call_trace import Speaker, TerminationReason, ToolOutcome
from app.services.qa.metrics.base import MetricContext, ToolCallData, TurnData

# How the conversation records speakers today. The trace vocabulary says
# caller/agent; the existing runner says user/agent. Map rather than rename, so
# stored transcripts keep working.
_SPEAKER_ALIASES = {
    "user": Speaker.CALLER,
    "caller": Speaker.CALLER,
    "human": Speaker.CALLER,
    "agent": Speaker.AGENT,
    "assistant": Speaker.AGENT,
    "bot": Speaker.AGENT,
}


def _speaker(value: Any) -> Speaker | None:
    if isinstance(value, Speaker):
        return value
    if isinstance(value, str):
        return _SPEAKER_ALIASES.get(value.strip().lower())
    return None


def _outcome(record: dict[str, Any]) -> ToolOutcome:
    """Infer a tool outcome from a recorded invocation.

    Accepts an explicit ``outcome`` when the runtime supplies one, and otherwise
    infers from the presence of an error. Defaults to OK only when there is no
    evidence of failure — never the reverse, since inventing failures is how a
    metric stops being trustworthy.
    """
    explicit = record.get("outcome")
    if isinstance(explicit, ToolOutcome):
        return explicit
    if isinstance(explicit, str):
        try:
            return ToolOutcome(explicit.strip().lower())
        except ValueError:
            pass

    if record.get("invalid_args") or record.get("validation_error"):
        return ToolOutcome.INVALID_ARGS
    if record.get("timed_out"):
        return ToolOutcome.TIMEOUT
    if record.get("error"):
        return ToolOutcome.ERROR
    return ToolOutcome.OK


def turns_from_conversation(conversation: list[Any] | None) -> tuple[TurnData, ...]:
    """Convert recorded conversation turns into TurnData.

    Turns whose speaker cannot be resolved are dropped rather than guessed at: a
    turn attributed to the wrong side would corrupt every per-speaker metric.
    """
    if not conversation:
        return ()

    turns: list[TurnData] = []
    for index, entry in enumerate(conversation):
        if not isinstance(entry, dict):
            continue
        speaker = _speaker(entry.get("speaker") or entry.get("role"))
        if speaker is None:
            continue

        message = entry.get("message") or entry.get("content")
        turns.append(
            TurnData(
                index=index,
                speaker=speaker,
                # A text-only run has no STT, so what was said is also what was
                # heard. Recording both keeps the shape stable for audio runs,
                # where they genuinely differ.
                text_intended=entry.get("text_intended") or message,
                text_transcribed=entry.get("text_transcribed") or message,
                response_ms=_as_float(entry.get("response_ms")),
                ttfb_ms=_as_float(entry.get("ttfb_ms")),
                audio_duration_ms=_as_float(entry.get("audio_duration_ms")),
                interrupted=bool(entry.get("interrupted", False)),
                barge_in=bool(entry.get("barge_in", False)),
            ),
        )
    return tuple(turns)


def tool_calls_from_records(
    records: list[Any] | None,
) -> tuple[ToolCallData, ...]:
    """Convert recorded tool invocations into ToolCallData."""
    if not records:
        return ()

    calls: list[ToolCallData] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name") or record.get("tool")
        if not name:
            continue
        calls.append(
            ToolCallData(
                name=str(name),
                outcome=_outcome(record),
                arguments=record.get("arguments") or record.get("args") or {},
                duration_ms=_as_float(record.get("duration_ms")),
                error=record.get("error"),
            ),
        )
    return tuple(calls)


def build_context(
    *,
    run_id: str,
    conversation: list[Any] | None = None,
    tool_calls: list[Any] | None = None,
    expected_tool_calls: list[dict[str, Any]] | None = None,
    success_criteria: dict[str, Any] | None = None,
    termination_reason: TerminationReason = TerminationReason.UNKNOWN,
    duration_ms: float | None = None,
    expected_db_state: dict[str, Any] | None = None,
    final_db_state: dict[str, Any] | None = None,
    has_audio: bool = False,
) -> MetricContext:
    """Assemble the capsule the metrics read."""
    criteria = success_criteria or {}
    return MetricContext(
        run_id=run_id,
        turns=turns_from_conversation(conversation),
        tool_calls=tool_calls_from_records(tool_calls),
        expected_tool_calls=tuple(expected_tool_calls or ()),
        success_criteria=criteria,
        termination_reason=termination_reason,
        duration_ms=duration_ms,
        # A scenario declares its expected end state alongside its other
        # criteria, so no schema change is needed to start asserting on it.
        expected_db_state=expected_db_state or criteria.get("expected_db_state"),
        final_db_state=final_db_state,
        has_audio=has_audio,
    )


def _as_float(value: Any) -> float | None:
    """Coerce a recorded number, treating anything unparseable as absent.

    Absent is correct here: a latency that cannot be read is not a latency of
    zero, and a zero would quietly flatter every timing percentile.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
