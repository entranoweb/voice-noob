"""Telephony WebSocket endpoints for Twilio and Telnyx media streaming.

These WebSocket endpoints handle the audio streams from Twilio and Telnyx,
connecting them to our AI voice agent pipeline.

Neither endpoint serves a connection that cannot present the stream token the
answering webhook minted for this agent and this call (``app.core.stream_token``).
Before that, knowing an agent's UUID was enough to open a conversation with it,
tools included. The check runs before the agent is loaded, before any database
read, and before a realtime session is opened, so a refused connection costs
nothing and learns nothing — not even whether the agent exists.

The two carriers deliver the token differently, so the two endpoints check it
at different moments:

* **Telnyx** keeps the stream URL's query string, so the token is there and the
  connection is refused before it is accepted.
* **Twilio** drops the query string and delivers custom values in the start
  frame, so the socket has to be accepted first; the token is then required in
  the first start frame, within a deadline, before anything else happens.

Both refusals close with 4401.
"""

import asyncio
import base64
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.limits import TRANSCRIPT_MAX_CHARS, truncate_with_marker
from app.core.log_redaction import log_exception
from app.core.stream_token import STREAM_TOKEN_REJECTED, verify_stream_token
from app.core.stream_token_use import claim_stream_token
from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallRecord
from app.models.workspace import AgentWorkspace
from app.monitoring.audio_turns import AudioTurnRecorder
from app.monitoring.call_trace import CallStatus as TraceCallStatus
from app.monitoring.call_trace import Direction, TerminationReason, ToolOutcome
from app.monitoring.call_trace_emitter import CallTraceEmitter
from app.monitoring.metrics import (
    record_benign_provider_error,
    record_call_completed,
    record_call_failed,
    record_call_initiated,
)
from app.services.call_registry import is_shutting_down, register_call, unregister_call
from app.services.gpt_realtime import GPTRealtimeSession

router = APIRouter(prefix="/ws/telephony", tags=["telephony-ws"])
logger = structlog.get_logger()

# Constants for event logging
EVENT_LOG_THRESHOLD = 20  # Log first N events, then every 100th

# How long an accepted Twilio socket may stay silent before its start frame.
# Twilio sends `connected` and `start` back to back on connect; anything slower
# than this is not a carrier, and an unauthenticated socket should not be held
# open waiting for one.
STREAM_START_DEADLINE_S = 10.0

# What counts as the call being alive for the idle clock: someone speaking, or
# the agent producing audio or words. Not carrier media frames: those arrive
# every 20 ms for as long as the line is open, silent or not.
ACTIVITY_EVENTS = frozenset(
    {
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "response.output_audio.delta",
        "response.output_audio_transcript.delta",
        "conversation.item.input_audio_transcription.completed",
    }
)


@dataclass
class _BridgeState:
    """What the two directions of one call share.

    ``reason`` is why the call ended, set by whichever side saw it first and
    never overwritten. ``last_activity`` feeds the idle timeout. A tool running
    is tracked so that neither the idle timeout nor a teardown treats the quiet
    of a booking write as the end of the conversation.
    """

    reason: TerminationReason | None = None
    last_activity: float = field(default_factory=time.monotonic)
    tools_in_flight: int = 0
    tools_running: list[str] = field(default_factory=list)
    tools_idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.tools_idle.set()

    def end(self, reason: TerminationReason) -> None:
        if self.reason is None:
            self.reason = reason

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    @contextlib.asynccontextmanager
    async def tool_call(self, name: str = "") -> AsyncIterator[None]:
        self.tools_in_flight += 1
        self.tools_running.append(name)
        self.tools_idle.clear()
        try:
            yield
        finally:
            self.tools_in_flight -= 1
            self.tools_running.remove(name)
            self.touch()
            if self.tools_in_flight == 0:
                self.tools_idle.set()


# What uvicorn closes every websocket with when the worker is told to stop
# (SIGTERM): "service restart". No carrier ends a call with it.
_SERVER_RESTART_CLOSE_CODE = 1012


def _carrier_disconnect_reason(disconnect: WebSocketDisconnect) -> TerminationReason:
    """Why a carrier socket went away: the caller, or this server stopping.

    A restart ends the call from our side, so it is not the caller's doing and
    must not be scored as one. No dedicated reason exists for it, and
    ``PIPELINE_ERROR`` is the one that says "our side ended it, do not grade the
    agent on this".
    """
    if disconnect.code == _SERVER_RESTART_CLOSE_CODE:
        return TerminationReason.PIPELINE_ERROR
    return TerminationReason.CALLER_HANGUP


@dataclass
class _CallRegistration:
    """The registry write for one call, which a cancelled task cannot orphan.

    The registration is issued by the carrier task. A call that dies within
    milliseconds of its start frame has that task cancelled while the write is
    in flight: the key is written (or about to be) and the task never gets to
    note it, so nothing ever removes it and it stalls shutdown drain and inflates
    the active count for the whole TTL. The write therefore runs as its own
    task, shielded from that cancellation, and ``settle`` waits for it so the
    teardown can remove what it wrote.
    """

    task: asyncio.Task[bool] | None = None

    async def start(self, **call: Any) -> bool:
        """Register; whether the registry took the call (``register_call``'s answer)."""
        self.task = asyncio.create_task(register_call(**call), name="call_registration")
        return await asyncio.shield(self.task)

    async def settle(self) -> bool:
        """Wait for the write to land; whether the call is in the registry.

        A second cancellation while waiting is absorbed: this runs in teardown,
        and giving up here would skip the unregister that follows and leave the
        key for its whole TTL. The wait is bounded by the registry's own timeout.
        """
        if self.task is None:
            return False
        while not self.task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({self.task})
        if self.task.cancelled() or self.task.exception() is not None:
            return False
        return self.task.result()


# Error codes the Realtime API raises for a race the call survives: the model was
# asked to respond while it already was, or the buffer was committed with nothing
# in it. The session carries on after either. Everything else, including every
# code not listed here, ends the call. Keep this list small and explicit.
BENIGN_PROVIDER_ERROR_CODES = frozenset(
    {
        "conversation_already_has_active_response",
        "input_audio_buffer_commit_empty",
    }
)


def _provider_error_ends_call(event: Any, log: Any) -> bool:
    """Log an OpenAI Realtime ``error`` event and say whether the call must end.

    The payload is ``{"error": {"type", "code", "message", "param"}}``. ``param``
    is left out: it names the offending field and can quote what was sent.

    A code on the benign allowlist is logged at WARNING and counted; anything
    else is logged at ERROR and ends the call.
    """
    error = getattr(event, "error", None)
    code = getattr(error, "code", None)
    fields = {
        "error_type": getattr(error, "type", None),
        "error_code": code,
        "error_message": getattr(error, "message", None),
    }
    if code in BENIGN_PROVIDER_ERROR_CODES:
        log.warning("realtime_provider_error_ignored", **fields)
        record_benign_provider_error(code)
        return False
    log.error("realtime_provider_error", **fields)
    return True


async def _run_bridge(
    carrier: Callable[[], Coroutine[Any, Any, None]],
    model: Callable[[], Coroutine[Any, Any, None]],
    *,
    state: _BridgeState,
    websocket: WebSocket,
    log: Any,
) -> TerminationReason:
    """Run both directions of a call until either ends, then end the other.

    ``gather`` waits for both, so a caller who hung up left the model loop
    waiting on a quiet socket (and a model that closed left the carrier loop
    feeding a dead one). ``wait(FIRST_COMPLETED)`` returns on the first, the
    other is cancelled and awaited, and the carrier socket is closed with 1000:
    Telnyx reconnects on any code outside 1000, 1007, 1008 and 1011.

    Also ends the call at ``CALL_MAX_DURATION_S`` and after ``CALL_IDLE_TIMEOUT_S``
    with no conversation. If a tool is running when that happens it is given
    ``CALL_TOOL_GRACE_S`` to finish before being cancelled, so a booking is not
    abandoned half-written.
    """
    carrier_task = asyncio.create_task(carrier(), name="carrier_to_model")
    model_task = asyncio.create_task(model(), name="model_to_carrier")
    tasks = {carrier_task, model_task}
    started = time.monotonic()
    max_s = settings.CALL_MAX_DURATION_S
    idle_s = settings.CALL_IDLE_TIMEOUT_S

    try:
        while True:
            now = time.monotonic()
            timeout = started + max_s - now
            if timeout <= 0:
                state.end(TerminationReason.MAX_DURATION)
                log.warning("call_max_duration_reached", max_duration_s=max_s)
                break
            if idle_s > 0:
                # While a tool runs the line is quiet on purpose. Look again
                # after a full idle period; the tool's end restarts the clock.
                idle_left = idle_s if state.tools_in_flight else state.last_activity + idle_s - now
                if idle_left <= 0:
                    state.end(TerminationReason.SILENCE_TIMEOUT)
                    log.warning("call_idle_timeout_reached", idle_timeout_s=idle_s)
                    break
                timeout = min(timeout, idle_left)

            done, _ = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                continue
            if model_task in done and state.reason is None:
                # The model socket closed on its own. Nothing the call did.
                log.warning("realtime_session_closed_unexpectedly")
                state.end(TerminationReason.PIPELINE_ERROR)
            state.end(TerminationReason.CALLER_HANGUP)
            break

        pending = [task for task in tasks if not task.done()]
        if model_task in pending and state.tools_in_flight:
            log.info("waiting_for_tool_call_before_teardown", grace_s=settings.CALL_TOOL_GRACE_S)
            try:
                await asyncio.wait_for(state.tools_idle.wait(), settings.CALL_TOOL_GRACE_S)
            except TimeoutError:
                log.warning(
                    "tool_call_cancelled_after_grace",
                    tools=list(state.tools_running),
                    grace_s=settings.CALL_TOOL_GRACE_S,
                )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    with contextlib.suppress(Exception):
        await websocket.close(code=1000, reason="Call ended")

    return state.reason or TerminationReason.UNKNOWN


async def _authorise_twilio_stream(
    websocket: WebSocket,
    agent_id: str,
    log: Any,
) -> dict[str, Any] | None:
    """Read up to the start frame and admit the stream only on a valid token.

    Returns the start frame, which the bridge still needs (it carries the
    stream and call identifiers), or None once the socket has been closed.

    Twilio gives no way to authenticate at the handshake: it strips the query
    string from a ``<Stream>`` URL and sends custom values as
    ``start.customParameters``. So the token is checked on the first start
    frame, against the ``callSid`` that frame announces — the same CallSid the
    signed webhook minted it for. Anything other than `connected` arriving
    before a start frame is refused: audio is not accepted from a stream that
    has not said who it is.
    """
    reason = "no_start_frame"
    try:
        async with asyncio.timeout(STREAM_START_DEADLINE_S):
            while True:
                data = json.loads(await websocket.receive_text())
                if not isinstance(data, dict):
                    reason = "malformed"
                    break
                event = data.get("event", "")
                if event == "connected":
                    continue
                if event != "start":
                    reason = "frame_before_start"
                    break
                start = data.get("start")
                start = start if isinstance(start, dict) else {}
                params = start.get("customParameters")
                token = params.get("token") if isinstance(params, dict) else None
                result = verify_stream_token(
                    token if isinstance(token, str) else None,
                    provider="twilio",
                    agent_id=agent_id,
                    call_id=str(start.get("callSid", "")),
                )
                if not result.ok:
                    reason = result.verdict.value
                    break
                # Valid, and now it has to be the first time it is used: a
                # second stream presenting the same token is refused, and so
                # is this one if Redis cannot record the claim.
                claim = await claim_stream_token(str(token))
                if claim.ok:
                    return data
                reason = claim.value
                break
    except TimeoutError:
        reason = "no_start_frame"
    except WebSocketDisconnect:
        log.info("twilio_websocket_disconnected_before_start")
        return None
    except ValueError:
        # json.JSONDecodeError is a ValueError: a frame that is not JSON.
        reason = "malformed"

    log.warning("twilio_stream_token_rejected", reason=reason)
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=STREAM_TOKEN_REJECTED, reason="Stream token rejected")
    return None


def _emit_call_trace(
    *,
    provider: str,
    call_id: str,
    provider_call_id: str,
    agent_id: str,
    recorder: AudioTurnRecorder,
    call_start_time: float,
    call_duration_s: float,
    failed: bool,
    termination: TerminationReason,
    direction: Direction,
) -> None:
    """Write the call's ``voice.call`` span tree.

    Emitted at the end of the call, with each turn carrying the times it was
    actually measured at rather than the time this ran. Failures here are
    swallowed: losing a trace is a reporting problem, and raising out of a
    finally block after a call has already ended would turn it into a user-facing
    one.
    """
    base_time_ns = int(call_start_time * 1_000_000_000)
    try:
        with CallTraceEmitter(
            call_id=call_id,
            provider=provider,
            provider_call_id=provider_call_id,
            agent_id=agent_id,
            direction=direction,
            engine="openai_realtime",
            carrier=provider,
            # The tree is written once the call has ended, so the root has to be
            # told when the call actually began or it would span only the moment
            # spent writing it, with children starting long before their parent.
            start_time_ns=base_time_ns,
        ) as emitter:
            emitter.record_turns(recorder.conversation(), base_time_ns=base_time_ns)
            for call in recorder.tool_calls():
                emitter.record_tool_call(
                    name=str(call["name"]),
                    outcome=call["outcome"],
                    arguments=call["arguments"],
                    duration_ms=call["duration_ms"],
                    error=call["error"],
                    start_time_ns=base_time_ns + int(float(call["offset_ms"]) * 1_000_000),
                )
            emitter.end(
                status=TraceCallStatus.FAILED if failed else TraceCallStatus.COMPLETED,
                termination_reason=(TerminationReason.PIPELINE_ERROR if failed else termination),
                duration_ms=call_duration_s * 1000.0,
                end_time_ns=base_time_ns + int(call_duration_s * 1_000_000_000),
            )
    except Exception:  # pragma: no cover - defensive; the emitter does not raise
        log_exception(logger, "call_trace_emit_failed", None, call_id=call_id)


def _record_transcript_event(
    event: Any,
    event_type: str,
    *,
    turns: AudioTurnRecorder,
    realtime_session: GPTRealtimeSession,
    enable_transcript: bool,
) -> bool:
    """Feed a transcript event to the recorder, and to storage if enabled.

    Returns whether the event was one of the transcript events, so the caller's
    dispatch chain can move on.

    The recorder is fed whether or not transcripts are being stored. Storing a
    transcript is a product feature the agent can have switched off; measuring
    the call is not, and the metrics need the text to know who said what.
    """
    if event_type == "conversation.item.input_audio_transcription.completed":
        transcript = getattr(event, "transcript", None)
        if transcript:
            turns.caller_transcript(transcript)
            if enable_transcript:
                realtime_session.add_user_transcript(transcript)
        return True

    if event_type == "response.output_audio_transcript.delta":
        delta = getattr(event, "delta", None)
        if delta:
            turns.agent_transcript_delta(delta)
            if enable_transcript:
                realtime_session.accumulate_assistant_text(delta)
        return True

    if event_type == "response.output_audio_transcript.done":
        if enable_transcript:
            realtime_session.flush_assistant_text()
        return True

    return False


async def _run_tool_call(
    event: Any,
    *,
    realtime_session: GPTRealtimeSession,
    turns: AudioTurnRecorder,
    log: Any,
    state: _BridgeState | None = None,
) -> bool:
    """Execute a tool the model asked for and record it for the trace.

    Returns whether the tool asked for the call to end.
    """
    log.info("handling_function_call", call_id=event.call_id, name=event.name)
    started_at = time.monotonic()
    if state is None:
        result = await realtime_session.handle_function_call_event(event)
    else:
        async with state.tool_call(event.name):
            result = await realtime_session.handle_function_call_event(event)
    _record_tool_call(turns, event, result, started_at=started_at)

    if result.get("action") == "end_call":
        log.info("end_call_action_received", reason=result.get("reason"))
        return True
    return False


def _record_tool_call(
    turns: AudioTurnRecorder,
    event: Any,
    result: dict[str, Any],
    *,
    started_at: float,
) -> None:
    """Record a tool invocation so it reaches the ``voice.tool_call`` span.

    Held on the recorder rather than traced immediately: the span tree is built
    once the call ends, and a span emitted mid-call would have no parent.

    The outcome is inferred from the result the tool actually returned. It
    defaults to OK only when there is no evidence of failure, never the reverse —
    inventing failures is how tool-call validity stops being worth reading.
    """
    error = result.get("error")
    outcome = ToolOutcome.ERROR if error else ToolOutcome.OK

    arguments: dict[str, Any] = {}
    raw_arguments = getattr(event, "arguments", None)
    if isinstance(raw_arguments, str):
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(raw_arguments)
            if isinstance(parsed, dict):
                arguments = parsed
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments

    turns.tool_called(
        str(getattr(event, "name", "") or "unknown"),
        outcome=outcome,
        arguments=arguments,
        duration_ms=(time.monotonic() - started_at) * 1000.0,
        error=str(error) if error else None,
        # When it was called, not when it returned. Without this the span starts
        # at completion and its duration runs off the end of the tool it timed.
        at=started_at,
    )


def _response_was_cancelled(event: Any) -> bool:
    """Whether a ``response.done`` reports a response that was cut short.

    Server-side turn detection cancels the agent's response when the caller
    starts speaking, and reports it here. That is the only reliable signal that a
    turn was interrupted rather than finished — the audio simply stops either
    way, so timing cannot tell them apart.
    """
    response = getattr(event, "response", None)
    return getattr(response, "status", None) == "cancelled"


# Close code for a stream whose call has no usable workspace: the call record
# the webhook wrote is missing, has no workspace, or names one the agent is no
# longer in. 4403 sits beside the token refusal (4401) and the agent refusals
# (4003/4004) and, like them, is outside what Telnyx retries on.
STREAM_WORKSPACE_REJECTED = 4403


async def call_workspace_id(
    db: AsyncSession, *, provider: str, provider_call_id: str, agent_id: uuid.UUID, log: Any
) -> uuid.UUID | None:
    """The workspace the answering webhook put this call in, or None.

    The webhook verified the carrier's signature with the key of the number's
    workspace and wrote that workspace on the call record. The stream must use
    the same one (its OpenAI key and its bills), so it is read from that row,
    looked up by the call identifier the stream token is bound to. It is never
    taken from the agent's own workspace links, which can hold several.

    None (and a logged reason, never the number) when the row is missing, has no
    workspace, or the agent is no longer linked to that workspace.
    """
    record = (
        await db.execute(
            select(CallRecord.workspace_id)
            .where(
                CallRecord.provider == provider,
                CallRecord.provider_call_id == provider_call_id,
                CallRecord.agent_id == agent_id,
            )
            .order_by(CallRecord.created_at.desc())
            .limit(1)
        )
    ).first()
    if record is None:
        log.warning("stream_workspace_rejected", reason="no_call_record")
        return None
    workspace_id = record[0]
    if workspace_id is None:
        log.warning("stream_workspace_rejected", reason="call_record_has_no_workspace")
        return None
    linked = (
        await db.execute(
            select(AgentWorkspace.agent_id).where(
                AgentWorkspace.agent_id == agent_id, AgentWorkspace.workspace_id == workspace_id
            )
        )
    ).first()
    if linked is None:
        log.warning("stream_workspace_rejected", reason="agent_not_in_call_workspace")
        return None
    return uuid.UUID(str(workspace_id))


async def save_turns_to_call_record(
    call_sid: str,
    recorder: AudioTurnRecorder,
    db: AsyncSession,
    log: Any,
    agent_id: uuid.UUID,
) -> None:
    """Store the recorded turns on the call record.

    Written only when audio actually moved. A call that connected and carried
    nothing leaves the column null, and null is what makes the audio metrics
    report ``not_measurable`` rather than a score of zero.

    Scoped to the agent serving this websocket. The call identifier arrives from
    the connection rather than from anything this process authenticated, so
    without the agent predicate a connection could name any call in the database
    and overwrite its turns.
    """
    if not recorder.has_audio:
        log.info("no_audio_recorded_turns_not_saved")
        return

    result = await db.execute(
        select(CallRecord).where(
            CallRecord.provider_call_id == call_sid,
            CallRecord.agent_id == agent_id,
        )
    )
    call_record = result.scalar_one_or_none()

    if call_record is None:
        log.warning("call_record_not_found_for_turns", call_sid=call_sid)
        return

    call_record.turns = recorder.conversation()
    await db.commit()
    log.info("turns_saved", record_id=str(call_record.id), turns=recorder.turn_count())


async def save_termination_reason(
    call_sid: str,
    termination: TerminationReason,
    db: AsyncSession,
    log: Any,
    agent_id: uuid.UUID,
) -> None:
    """Record why the conversation ended.

    The bridge is the only thing that observes this. Nothing downstream may infer
    it from the call's terminal status — a completed call may have been ended by
    the caller, by the agent, or by a duration cap — so if it is not written here
    it stays null and the run reads as "we do not know", which is true.
    """
    if termination is TerminationReason.UNKNOWN:
        log.info("termination_reason_not_observed")
        return

    result = await db.execute(
        select(CallRecord).where(
            CallRecord.provider_call_id == call_sid,
            CallRecord.agent_id == agent_id,
        )
    )
    call_record = result.scalar_one_or_none()

    if call_record is None:
        log.warning("call_record_not_found_for_termination", call_sid=call_sid)
        return

    call_record.termination_reason = termination.value
    await db.commit()
    log.info("termination_reason_saved", reason=termination.value)


async def _persist_step(
    name: str, write: Callable[[], Coroutine[Any, Any, None]], db: AsyncSession, log: Any
) -> None:
    """Run one write on its own, so a failure here does not skip the others.

    A write that fails is rolled back and logged at ERROR. It does not fail the
    call: the call already happened, and the failure is the database's, not the
    conversation's.
    """
    try:
        await write()
    except Exception:
        log_exception(log, "call_persistence_failed", None, step=name)
        with contextlib.suppress(Exception):
            await db.rollback()


async def _persist_call_artifacts(
    *,
    record_key: str,
    agent: Agent,
    realtime_session: GPTRealtimeSession,
    recorder: AudioTurnRecorder | None,
    termination: TerminationReason,
    db: AsyncSession,
    log: Any,
) -> None:
    """Write what the call produced onto its record.

    The transcript only when the agent stores transcripts; the turn timings
    (when there is a recorder) because they are telemetry about the call rather
    than its content and are what the audio metrics read once the websocket has
    closed; and why the call ended. Each is written independently.
    """
    if not record_key:
        log.warning("no_call_reference_nothing_persisted")
        return

    # Read once, before any write: a rollback expires every loaded object, and a
    # lazy load on the expired agent would fail the writes that follow.
    agent_id = agent.id
    store_transcript = agent.enable_transcript

    # A tool cancelled mid-query (the grace period ran out) leaves the session's
    # connection invalidated, and the session refuses every statement with
    # PendingRollbackError until it is rolled back. The first write, the
    # transcript, took that error and the rollback that followed only cleared it
    # for the writes after, so the transcript was the one thing lost. Whatever
    # that tool had pending is not committed with the call.
    try:
        await db.rollback()
    except Exception:
        log_exception(log, "pre_persist_rollback_failed", level="warning")

    if store_transcript:
        transcript = realtime_session.get_transcript()
        await _persist_step(
            "transcript",
            lambda: save_transcript_to_call_record(
                record_key, transcript, db, log, agent_id=agent_id
            ),
            db,
            log,
        )

    if recorder is not None:
        await _persist_step(
            "turns",
            lambda: save_turns_to_call_record(record_key, recorder, db, log, agent_id=agent_id),
            db,
            log,
        )
    await _persist_step(
        "termination_reason",
        lambda: save_termination_reason(record_key, termination, db, log, agent_id=agent_id),
        db,
        log,
    )


async def save_transcript_to_call_record(
    call_sid: str,
    transcript: str,
    db: AsyncSession,
    log: Any,
    agent_id: uuid.UUID,
) -> None:
    """Save transcript to the call record.

    Args:
        call_sid: Provider call ID (CallSid for Twilio, call_control_id for Telnyx)
        transcript: Formatted transcript text
        db: Database session
        log: Logger instance
        agent_id: Restricts the write to this agent's call records. Required,
            not optional: the call identifier comes off the connection rather
            than from anything this process authenticated, so an unscoped write
            lets a connection name any call in the database and overwrite its
            transcript. A default here would have left that door open on every
            call site that forgot to pass it — which is how the Twilio path kept
            the hole after the Telnyx one was closed.
    """
    if not transcript.strip():
        log.debug("empty_transcript_skipped")
        return
    # The session already stops at the cap; this is the same rule at the write,
    # for any caller that hands in its own text.
    transcript = truncate_with_marker(transcript, TRANSCRIPT_MAX_CHARS)

    result = await db.execute(
        select(CallRecord).where(
            CallRecord.provider_call_id == call_sid,
            CallRecord.agent_id == agent_id,
        )
    )
    call_record = result.scalar_one_or_none()

    if call_record:
        call_record.transcript = transcript
        await db.commit()
        log.info("transcript_saved", record_id=str(call_record.id), length=len(transcript))
    else:
        log.warning("call_record_not_found_for_transcript", call_sid=call_sid)


@router.websocket("/twilio/{agent_id}")
async def twilio_media_stream(  # noqa: PLR0912, PLR0915
    websocket: WebSocket,
    agent_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """WebSocket endpoint for Twilio Media Streams.

    Twilio sends audio via Media Streams in mulaw format at 8kHz.
    This endpoint bridges that audio to our GPT Realtime session.

    Message format from Twilio:
    - {"event": "connected", "protocol": "Call", "version": "1.0.0"}
    - {"event": "start", "start": {"streamSid": "...", "callSid": "..."}}
    - {"event": "media", "media": {"payload": "base64_audio"}}
    - {"event": "stop"}
    """
    session_id = str(uuid.uuid4())
    log = logger.bind(
        endpoint="twilio_media_stream",
        agent_id=agent_id,
        session_id=session_id,
    )

    await websocket.accept()
    log.info("twilio_websocket_connected")

    # Reject new connections during graceful shutdown
    if is_shutting_down():
        log.info("twilio_websocket_rejected_shutdown")
        await websocket.close(code=1012, reason="Server is shutting down")
        return

    # Nothing below runs for a stream that cannot present the token the
    # answering webhook minted: no agent lookup, no realtime session.
    start_frame = await _authorise_twilio_stream(websocket, agent_id, log)
    if start_frame is None:
        return

    stream_sid: str = ""
    call_sid: str = ""
    call_start_time: float = time.time()
    call_registered: bool = False
    registration = _CallRegistration()
    call_failed: bool = False

    try:
        # Load agent configuration
        result = await db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
        agent = result.scalar_one_or_none()

        if not agent:
            log.error("agent_not_found")
            await websocket.close(code=4004, reason="Agent not found")
            return

        if not agent.is_active:
            log.error("agent_not_active")
            await websocket.close(code=4003, reason="Agent is not active")
            return

        log.info("agent_loaded", agent_name=agent.name)

        # agent.user_id is now directly the integer user ID
        user_id_int = agent.user_id

        # The workspace the answering webhook put this call in (see call_workspace_id).
        workspace_id = await call_workspace_id(
            db,
            provider="twilio",
            provider_call_id=str(start_frame.get("start", {}).get("callSid", "")),
            agent_id=agent.id,
            log=log,
        )
        if workspace_id is None:
            await websocket.close(code=STREAM_WORKSPACE_REJECTED, reason="Call workspace mismatch")
            return

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "enabled_tool_ids": agent.enabled_tool_ids,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
            "pricing_tier": agent.pricing_tier,
        }

        # Callback to register call when it starts
        async def on_call_start(sid: str) -> None:
            nonlocal call_sid, call_registered
            call_sid = sid
            try:
                registered = await registration.start(
                    call_id=sid,
                    agent_id=agent_id,
                    metadata={"provider": "twilio", "session_id": session_id},
                )
                if registered:
                    record_call_initiated(agent_id)
                    call_registered = True
                    log.info("call_registered", call_sid=sid)
                else:
                    log.warning("call_registration_refused", call_sid=sid)
            except Exception:
                log_exception(log, "call_registration_failed", None, call_sid=sid)

        # Initialize GPT Realtime session
        conversation_over = False
        try:
            async with GPTRealtimeSession(
                db=db,
                user_id=user_id_int,
                agent_config=agent_config,
                session_id=session_id,
                workspace_id=workspace_id,
            ) as realtime_session:
                # Handle Twilio media stream and capture call_sid
                call_sid, termination = await _handle_twilio_stream(
                    websocket=websocket,
                    realtime_session=realtime_session,
                    log=log,
                    enable_transcript=agent.enable_transcript,
                    on_call_start=on_call_start,
                    initial_start=start_frame,
                )
                conversation_over = True
        finally:
            # After the session has closed, because a slow database must not keep
            # a model session open for a caller who has already gone; in a
            # ``finally``, because a close that fails or is cancelled must not
            # lose the call. A conversation that raised has no outcome to save.
            if conversation_over:
                await _persist_call_artifacts(
                    record_key=call_sid,
                    agent=agent,
                    realtime_session=realtime_session,
                    recorder=None,
                    termination=termination,
                    db=db,
                    log=log,
                )

    except WebSocketDisconnect:
        log.info("twilio_websocket_disconnected")
    except Exception as e:
        call_failed = True
        log_exception(log, "twilio_websocket_error", e)
    finally:
        # Unregister call and record metrics
        call_duration = time.time() - call_start_time
        if not call_registered and await registration.settle():
            # The carrier task was cancelled while registering; the key is there
            # all the same and is this call's to remove.
            record_call_initiated(agent_id)
            call_registered = True
        if call_registered and call_sid:
            try:
                await unregister_call(call_sid)
                if call_failed:
                    record_call_failed(agent_id, error_type="websocket_error")
                else:
                    record_call_completed(agent_id, duration_seconds=call_duration)
                log.info(
                    "call_unregistered",
                    call_sid=call_sid,
                    duration_seconds=round(call_duration, 2),
                )
            except Exception:
                log_exception(log, "call_unregistration_failed", None, call_sid=call_sid)
        log.info("twilio_websocket_closed", stream_sid=stream_sid, call_sid=call_sid)


async def _handle_twilio_stream(  # noqa: PLR0915
    websocket: WebSocket,
    realtime_session: GPTRealtimeSession,
    log: Any,
    enable_transcript: bool = False,
    on_call_start: Any | None = None,
    initial_start: dict[str, Any] | None = None,
) -> tuple[str, TerminationReason]:
    """Handle Twilio Media Stream messages.

    Args:
        websocket: WebSocket connection from Twilio
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript
        on_call_start: Optional callback when call starts (receives call_sid)
        initial_start: The start frame the endpoint already read off the socket
            in order to authenticate the stream. It is processed here first, so
            the identifiers it carries are not lost by having been consumed.

    Returns:
        The call_sid for transcript saving, and why the call ended.
    """
    stream_sid = ""
    call_sid = ""
    should_end_call = False  # Flag to signal call should end
    state = _BridgeState()
    already_read: list[dict[str, Any]] = [initial_start] if initial_start else []

    async def twilio_to_realtime() -> None:
        """Forward audio from Twilio to GPT Realtime."""
        nonlocal stream_sid, call_sid, should_end_call

        try:
            while not should_end_call:
                if already_read:
                    data = already_read.pop(0)
                else:
                    message = await websocket.receive_text()
                    data = json.loads(message)
                event = data.get("event", "")

                if event == "connected":
                    log.info("twilio_stream_connected")

                elif event == "start":
                    start_data = data.get("start", {})
                    stream_sid = start_data.get("streamSid", "")
                    call_sid = start_data.get("callSid", "")
                    log.info(
                        "twilio_stream_started",
                        stream_sid=stream_sid,
                        call_sid=call_sid,
                    )
                    # Notify that call has started (for registry/metrics)
                    if on_call_start and call_sid:
                        await on_call_start(call_sid)

                elif event == "media":
                    # Decode base64 mulaw audio and forward to Realtime
                    media = data.get("media", {})
                    payload = media.get("payload", "")
                    if payload:
                        audio_bytes = base64.b64decode(payload)
                        await realtime_session.send_audio(audio_bytes)

                elif event == "stop":
                    log.info("twilio_stream_stopped")
                    state.end(TerminationReason.CALLER_HANGUP)
                    break

                elif event == "mark":
                    # Mark events indicate playback position
                    log.debug("twilio_mark_event", name=data.get("mark", {}).get("name"))

        except WebSocketDisconnect as disconnect:
            log.info("twilio_to_realtime_disconnected", close_code=disconnect.code)
            state.end(_carrier_disconnect_reason(disconnect))
        except Exception as e:
            log_exception(log, "twilio_to_realtime_error", e)
            state.end(TerminationReason.PIPELINE_ERROR)

    async def realtime_to_twilio() -> None:  # noqa: PLR0912, PLR0915
        """Forward audio from GPT Realtime to Twilio."""
        nonlocal should_end_call

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            log.info("realtime_to_twilio_started", waiting_for_events=True)
            event_count = 0
            pending_end_call = False  # True when end_call requested but waiting for AI to finish
            greeting_triggered = False  # Track if we've triggered the greeting

            async for event in realtime_session.connection:
                event_type = event.type
                event_count += 1
                if event_type in ACTIVITY_EVENTS:
                    state.touch()

                # Log all events for debugging
                if event_count <= EVENT_LOG_THRESHOLD or event_count % 100 == 0:
                    log.info("realtime_event_received", event_type=event_type, count=event_count)

                # Trigger initial greeting after session is configured
                # This avoids race condition where audio events arrive before listener is ready
                if event_type == "session.updated" and not greeting_triggered:
                    greeting_triggered = True
                    triggered = await realtime_session.trigger_initial_greeting()
                    if triggered:
                        log.info("initial_greeting_triggered_after_session_update")

                elif event_type == "error":
                    if _provider_error_ends_call(event, log):
                        state.end(TerminationReason.PIPELINE_ERROR)
                        break

                # Handle audio output
                elif event_type == "response.output_audio.delta":
                    # Get audio delta and send to Twilio
                    # Check various possible attribute names for the audio data
                    delta_data = getattr(event, "delta", None)
                    if not delta_data:
                        # Log event attributes for debugging
                        log.warning(
                            "audio_delta_missing",
                            event_attrs=dir(event),
                            has_delta=hasattr(event, "delta"),
                        )
                        continue

                    try:
                        audio_bytes = base64.b64decode(delta_data)
                        # Encode for Twilio (already in g711_ulaw format now)
                        payload = base64.b64encode(audio_bytes).decode("utf-8")
                        log.info(
                            "sending_audio_to_twilio",
                            audio_size=len(audio_bytes),
                            stream_sid=stream_sid,
                        )
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": payload},
                                }
                            )
                        )
                    except Exception as audio_err:
                        log_exception(log, "audio_send_error", audio_err)

                # Handle tool calls
                elif event_type == "response.function_call_arguments.done":
                    log.info(
                        "handling_function_call",
                        call_id=event.call_id,
                        name=event.name,
                    )
                    async with state.tool_call(event.name):
                        result = await realtime_session.handle_function_call_event(event)
                    # Check if this is an end_call action
                    if result.get("action") == "end_call":
                        log.info("end_call_action_received", reason=result.get("reason"))
                        pending_end_call = True

                # Capture transcript events
                elif (
                    enable_transcript
                    and event_type == "conversation.item.input_audio_transcription.completed"
                ):
                    # User speech transcription
                    if hasattr(event, "transcript") and event.transcript:
                        realtime_session.add_user_transcript(event.transcript)
                        log.debug("user_transcript_captured", length=len(event.transcript))

                elif enable_transcript and event_type == "response.output_audio_transcript.delta":
                    # Assistant speech transcript delta
                    if hasattr(event, "delta") and event.delta:
                        realtime_session.accumulate_assistant_text(event.delta)

                elif enable_transcript and event_type == "response.output_audio_transcript.done":
                    # Assistant speech transcript complete
                    realtime_session.flush_assistant_text()

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
                    # Log full response details for debugging
                    response_data = getattr(event, "response", None)
                    if response_data:
                        status = getattr(response_data, "status", "unknown")
                        status_details = getattr(response_data, "status_details", None)
                        output = getattr(response_data, "output", [])
                        output_count = len(output) if output else 0
                        log.info(
                            "response_done_details",
                            status=status,
                            status_details=str(status_details) if status_details else None,
                            output_count=output_count,
                        )
                    else:
                        log.debug("realtime_event", event_type=event_type)
                    if pending_end_call:
                        log.info("ending_call_after_response_complete")
                        should_end_call = True
                        state.end(TerminationReason.AGENT_ENDED)
                        break

                # Log other events
                elif event_type in [
                    "response.output_audio.done",
                    "input_audio_buffer.speech_started",
                    "input_audio_buffer.speech_stopped",
                ]:
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log_exception(log, "realtime_to_twilio_error", e)
            state.end(TerminationReason.PIPELINE_ERROR)

    # Run both directions until either ends; the other is then ended with it
    # and the carrier socket closed (which is what hangs up a Twilio call).
    termination = await _run_bridge(
        twilio_to_realtime,
        realtime_to_twilio,
        state=state,
        websocket=websocket,
        log=log,
    )
    log.info("twilio_bridge_ended", termination=termination.value)

    return call_sid, termination


@router.websocket("/telnyx/{agent_id}")
async def telnyx_media_stream(  # noqa: PLR0912, PLR0915
    websocket: WebSocket,
    agent_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """WebSocket endpoint for Telnyx Media Streams.

    Telnyx sends audio via Media Streams in PCMU format at 8kHz.
    This endpoint bridges that audio to our GPT Realtime session.

    Message format from Telnyx:
    - {"event": "start", "stream_id": "...", "call_control_id": "..."}
    - {"event": "media", "media": {"payload": "base64_audio"}}
    - {"event": "stop"}
    """
    session_id = str(uuid.uuid4())
    # The webhook that answered this call put its own identifier for the call in
    # the stream URL. The start frame's call_control_id is not guaranteed to be
    # the same string on a TeXML application, and this is the one the call record
    # was written under.
    webhook_call_id = websocket.query_params.get("call_id", "")
    # Outbound calls reach this same endpoint, so the direction travels with the
    # stream URL. Defaulting to inbound would label every outbound call's trace
    # as inbound, which is worse than a missing attribute.
    direction = (
        Direction.OUTBOUND
        if websocket.query_params.get("direction", "") == Direction.OUTBOUND.value
        else Direction.INBOUND
    )
    log = logger.bind(
        endpoint="telnyx_media_stream",
        agent_id=agent_id,
        session_id=session_id,
        webhook_call_id=webhook_call_id,
        direction=direction.value,
    )

    # The token rides in the stream URL beside the call identifier it is bound
    # to, so this is decided before the connection is accepted and before the
    # database is touched. Bound to `call_id` as well as the agent: without
    # that, a token for one call could be presented beside another call's
    # identifier to reach that call's row (DECISIONS §33).
    token_result = verify_stream_token(
        websocket.query_params.get("token"),
        provider="telnyx",
        agent_id=agent_id,
        call_id=webhook_call_id,
    )
    if not token_result.ok:
        log.warning("telnyx_stream_token_rejected", reason=token_result.verdict.value)
        await websocket.close(code=STREAM_TOKEN_REJECTED, reason="Stream token rejected")
        return

    # A valid token admits one stream. The first socket to present it claims it;
    # a replay inside its two-minute life is refused, and so is any stream while
    # Redis cannot record the claim (app.core.stream_token_use says why that is
    # the right way round).
    token_claim = await claim_stream_token(websocket.query_params.get("token") or "")
    if not token_claim.ok:
        log.warning("telnyx_stream_token_rejected", reason=token_claim.value)
        await websocket.close(code=STREAM_TOKEN_REJECTED, reason="Stream token rejected")
        return

    await websocket.accept()
    log.info("telnyx_websocket_connected")

    # Reject new connections during graceful shutdown
    if is_shutting_down():
        log.info("telnyx_websocket_rejected_shutdown")
        await websocket.close(code=1012, reason="Server is shutting down")
        return

    stream_id: str = ""
    call_control_id: str = ""
    call_start_time: float = time.time()
    call_registered: bool = False
    registration = _CallRegistration()
    call_failed: bool = False
    recorder = AudioTurnRecorder()
    # The recorder times on the monotonic clock and the trace is anchored to the
    # wall clock; both start here, so turn offsets and span timestamps line up
    # instead of being shifted by however long the first speech took to arrive.
    recorder.mark_call_start(time.monotonic())
    # Stays UNKNOWN unless the bridge sees something that says why the call
    # ended. Guessing here would put an invented reason into the trace that a
    # dashboard would then group and count.
    termination = TerminationReason.UNKNOWN

    try:
        # Load agent configuration
        result = await db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
        agent = result.scalar_one_or_none()

        if not agent:
            log.error("agent_not_found")
            await websocket.close(code=4004, reason="Agent not found")
            return

        if not agent.is_active:
            log.error("agent_not_active")
            await websocket.close(code=4003, reason="Agent is not active")
            return

        log.info("agent_loaded", agent_name=agent.name)

        # An agent with transcripts switched off has had its owner decline to
        # store what was said. That covers the metrics and the trace too, so the
        # recorder keeps the timings and drops the words.
        recorder.retain_text = agent.enable_transcript

        # agent.user_id is now directly the integer user ID
        user_id_int = agent.user_id

        # The workspace the answering webhook put this call in (see call_workspace_id).
        workspace_id = await call_workspace_id(
            db,
            provider="telnyx",
            provider_call_id=webhook_call_id,
            agent_id=agent.id,
            log=log,
        )
        if workspace_id is None:
            await websocket.close(code=STREAM_WORKSPACE_REJECTED, reason="Call workspace mismatch")
            return

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "enabled_tool_ids": agent.enabled_tool_ids,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
            "pricing_tier": agent.pricing_tier,
        }

        # Callback to register call when it starts
        async def on_call_start(cid: str) -> None:
            nonlocal call_control_id, call_registered
            call_control_id = cid
            try:
                registered = await registration.start(
                    call_id=cid,
                    agent_id=agent_id,
                    metadata={"provider": "telnyx", "session_id": session_id},
                )
                if registered:
                    record_call_initiated(agent_id)
                    call_registered = True
                    log.info("call_registered", call_control_id=cid)
                else:
                    log.warning("call_registration_refused", call_control_id=cid)
            except Exception:
                log_exception(log, "call_registration_failed", None, call_control_id=cid)

        # Initialize GPT Realtime session
        conversation_over = False
        try:
            async with GPTRealtimeSession(
                db=db,
                user_id=user_id_int,
                agent_config=agent_config,
                session_id=session_id,
                workspace_id=workspace_id,
            ) as realtime_session:
                # Handle Telnyx media stream and capture call_control_id
                call_control_id, termination = await _handle_telnyx_stream(
                    websocket=websocket,
                    realtime_session=realtime_session,
                    log=log,
                    enable_transcript=agent.enable_transcript,
                    on_call_start=on_call_start,
                    recorder=recorder,
                )
                conversation_over = True
        finally:
            # After the session has closed, because a slow database must not keep
            # a model session open for a caller who has already gone; in a
            # ``finally``, because a close that fails or is cancelled must not
            # lose the call. A conversation that raised has no outcome to save.
            if conversation_over:
                await _persist_call_artifacts(
                    record_key=webhook_call_id or call_control_id,
                    agent=agent,
                    realtime_session=realtime_session,
                    recorder=recorder,
                    termination=termination,
                    db=db,
                    log=log,
                )

    except WebSocketDisconnect:
        log.info("telnyx_websocket_disconnected")
    except Exception as e:
        call_failed = True
        log_exception(log, "telnyx_websocket_error", e)
    finally:
        # Unregister call and record metrics
        call_duration = time.time() - call_start_time
        if not call_registered and await registration.settle():
            # The carrier task was cancelled while registering; the key is there
            # all the same and is this call's to remove.
            record_call_initiated(agent_id)
            call_registered = True
        if call_registered and call_control_id:
            try:
                await unregister_call(call_control_id)
                if call_failed:
                    record_call_failed(agent_id, error_type="websocket_error")
                else:
                    record_call_completed(agent_id, duration_seconds=call_duration)
                log.info(
                    "call_unregistered",
                    call_control_id=call_control_id,
                    duration_seconds=round(call_duration, 2),
                )
            except Exception:
                log_exception(
                    log, "call_unregistration_failed", None, call_control_id=call_control_id
                )

        # Only for a call that actually carried something: a stream that
        # announced itself, audio that moved, or a failure worth recording. A
        # connection rejected for an unknown agent, or one that opened and said
        # nothing, returns through this same `finally`, and a completed
        # voice.call span for it would put calls that never happened into every
        # dashboard that counts them.
        if call_failed or call_registered or recorder.has_audio:
            _emit_call_trace(
                provider="telnyx",
                call_id=webhook_call_id or call_control_id or session_id,
                provider_call_id=call_control_id or webhook_call_id,
                agent_id=agent_id,
                recorder=recorder,
                call_start_time=call_start_time,
                call_duration_s=call_duration,
                failed=call_failed,
                termination=termination,
                direction=direction,
            )
        log.info("telnyx_websocket_closed", stream_id=stream_id, call_control_id=call_control_id)


async def _handle_telnyx_stream(  # noqa: PLR0915
    websocket: WebSocket,
    realtime_session: GPTRealtimeSession,
    log: Any,
    enable_transcript: bool = False,
    on_call_start: Any | None = None,
    recorder: AudioTurnRecorder | None = None,
) -> tuple[str, TerminationReason]:
    """Handle Telnyx Media Stream messages.

    Args:
        websocket: WebSocket connection from Telnyx
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript
        on_call_start: Optional callback when call starts (receives call_control_id)
        recorder: Collects the turn timings the audio metrics are computed from

    Returns:
        The call_control_id for transcript saving, and why the conversation
        ended — UNKNOWN unless the bridge saw evidence of a reason.
    """
    turns = recorder if recorder is not None else AudioTurnRecorder()
    stream_id = ""
    call_control_id = ""
    should_end_call = False  # Flag to signal call should end
    state = _BridgeState()

    async def telnyx_to_realtime() -> None:
        """Forward audio from Telnyx to GPT Realtime."""
        nonlocal stream_id, call_control_id, should_end_call

        try:
            while not should_end_call:
                message = await websocket.receive_text()
                data = json.loads(message)
                event = data.get("event", "")

                if event == "start":
                    stream_id = data.get("stream_id", "")
                    start_data = data.get("start", {})
                    call_control_id = start_data.get("call_control_id", "")
                    log.info(
                        "telnyx_stream_started",
                        stream_id=stream_id,
                        call_control_id=call_control_id,
                    )
                    # Notify that call has started (for registry/metrics)
                    if on_call_start and call_control_id:
                        await on_call_start(call_control_id)

                elif event == "media":
                    # Decode base64 PCMU audio and forward to Realtime
                    media = data.get("media", {})
                    payload = media.get("payload", "")
                    if payload:
                        audio_bytes = base64.b64decode(payload)
                        await realtime_session.send_audio(audio_bytes)

                elif event == "stop":
                    log.info("telnyx_stream_stopped")
                    # The carrier tore the stream down. On an inbound call that
                    # is the caller hanging up; the agent ending the call takes
                    # the other branch and overwrites this.
                    state.end(TerminationReason.CALLER_HANGUP)
                    break

        except WebSocketDisconnect as disconnect:
            log.info("telnyx_to_realtime_disconnected", close_code=disconnect.code)
            state.end(_carrier_disconnect_reason(disconnect))
        except Exception as e:
            log_exception(log, "telnyx_to_realtime_error", e)
            state.end(TerminationReason.PIPELINE_ERROR)

    async def realtime_to_telnyx() -> None:  # noqa: PLR0912, PLR0915
        """Forward audio from GPT Realtime to Telnyx."""
        nonlocal should_end_call

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                state.end(TerminationReason.PIPELINE_ERROR)
                return

            pending_end_call = False  # True when end_call requested but waiting for AI to finish
            greeting_triggered = False  # Track if we've triggered the greeting

            async for event in realtime_session.connection:
                event_type = event.type
                if event_type in ACTIVITY_EVENTS:
                    state.touch()

                # Trigger initial greeting after session is configured
                # This avoids race condition where audio events arrive before listener is ready
                if event_type == "session.updated" and not greeting_triggered:
                    greeting_triggered = True
                    triggered = await realtime_session.trigger_initial_greeting()
                    if triggered:
                        log.info("initial_greeting_triggered_after_session_update")

                elif event_type == "error":
                    if _provider_error_ends_call(event, log):
                        state.end(TerminationReason.PIPELINE_ERROR)
                        break

                # Handle audio output
                elif event_type == "response.output_audio.delta":
                    if hasattr(event, "delta") and event.delta:
                        audio_bytes = base64.b64decode(event.delta)
                        payload = base64.b64encode(audio_bytes).decode("utf-8")
                        turns.agent_audio_delta(byte_count=len(audio_bytes))
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "stream_id": stream_id,
                                    "media": {"payload": payload},
                                }
                            )
                        )

                # Handle tool calls
                elif event_type == "response.function_call_arguments.done":
                    pending_end_call = (
                        await _run_tool_call(
                            event,
                            realtime_session=realtime_session,
                            turns=turns,
                            log=log,
                            state=state,
                        )
                        or pending_end_call
                    )

                # Capture transcript events. The recorder is fed regardless of
                # whether transcripts are stored: it needs the text to attribute
                # turns, and the agent's own words are the only ground truth
                # there is for what it meant to say.
                elif _record_transcript_event(
                    event,
                    event_type,
                    turns=turns,
                    realtime_session=realtime_session,
                    enable_transcript=enable_transcript,
                ):
                    pass

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
                    log.debug("realtime_event", event_type=event_type)
                    turns.agent_turn_ended(interrupted=_response_was_cancelled(event))
                    if pending_end_call:
                        log.info("ending_call_after_response_complete")
                        should_end_call = True
                        state.end(TerminationReason.AGENT_ENDED)
                        break

                elif event_type == "input_audio_buffer.speech_started":
                    log.debug("realtime_event", event_type=event_type)
                    # Order matters: ask before recording, because recording the
                    # caller's turn closes the agent's.
                    talked_over = turns.agent_is_speaking
                    turns.caller_speech_started()
                    if talked_over:
                        # Server VAD cancels the response upstream, but Telnyx has
                        # already buffered whatever audio was sent and will keep
                        # playing it over the caller unless the buffer is flushed.
                        # This is the difference between an agent that stops when
                        # interrupted and one that talks through it.
                        await websocket.send_text(json.dumps({"event": "clear"}))
                        log.info("barge_in_cleared_playback")

                elif event_type == "input_audio_buffer.speech_stopped":
                    log.debug("realtime_event", event_type=event_type)
                    turns.caller_speech_stopped()

                elif event_type == "response.output_audio.done":
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log_exception(log, "realtime_to_telnyx_error", e)
            state.end(TerminationReason.PIPELINE_ERROR)

    # Run both directions until either ends; the other is then ended with it
    # and the carrier socket closed with 1000, which is the code Telnyx does not
    # answer by reconnecting.
    termination = await _run_bridge(
        telnyx_to_realtime,
        realtime_to_telnyx,
        state=state,
        websocket=websocket,
        log=log,
    )
    log.info("telnyx_bridge_ended", termination=termination.value)

    return call_control_id, termination
