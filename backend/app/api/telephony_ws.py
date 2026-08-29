"""Telephony WebSocket endpoints for Twilio and Telnyx media streaming.

These WebSocket endpoints handle the audio streams from Twilio and Telnyx,
connecting them to our AI voice agent pipeline.
"""

import asyncio
import base64
import contextlib
import json
import time
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallRecord
from app.models.workspace import AgentWorkspace
from app.monitoring.audio_turns import AudioTurnRecorder
from app.monitoring.call_trace import CallStatus as TraceCallStatus
from app.monitoring.call_trace import Direction, TerminationReason, ToolOutcome
from app.monitoring.call_trace_emitter import CallTraceEmitter
from app.monitoring.metrics import (
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
        logger.exception("call_trace_emit_failed", call_id=call_id)


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
) -> bool:
    """Execute a tool the model asked for and record it for the trace.

    Returns whether the tool asked for the call to end.
    """
    log.info("handling_function_call", call_id=event.call_id, name=event.name)
    started_at = time.monotonic()
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


async def get_agent_workspace_id(agent_id: uuid.UUID, db: AsyncSession) -> uuid.UUID | None:
    """Get workspace ID for an agent."""
    result = await db.execute(
        select(AgentWorkspace.workspace_id).where(AgentWorkspace.agent_id == agent_id).limit(1)
    )
    row = result.scalar_one_or_none()
    return row


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


async def _persist_call_artifacts(
    *,
    record_key: str,
    agent: Agent,
    realtime_session: GPTRealtimeSession,
    recorder: AudioTurnRecorder,
    termination: TerminationReason,
    db: AsyncSession,
    log: Any,
) -> None:
    """Write what the call produced onto its record.

    The transcript only when the agent stores transcripts; the turn timings
    always, because they are telemetry about the call rather than its content
    and are what the audio metrics read once the websocket has closed.
    """
    if not record_key:
        log.warning("no_call_reference_nothing_persisted")
        return

    if agent.enable_transcript:
        transcript = realtime_session.get_transcript()
        await save_transcript_to_call_record(record_key, transcript, db, log, agent_id=agent.id)

    await save_turns_to_call_record(record_key, recorder, db, log, agent_id=agent.id)
    await save_termination_reason(record_key, termination, db, log, agent_id=agent.id)


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
async def twilio_media_stream(  # noqa: PLR0915
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

    stream_sid: str = ""
    call_sid: str = ""
    call_start_time: float = time.time()
    call_registered: bool = False
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

        # Get workspace for the agent
        workspace_id = await get_agent_workspace_id(agent.id, db)

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
        }

        # Callback to register call when it starts
        async def on_call_start(sid: str) -> None:
            nonlocal call_sid, call_registered
            call_sid = sid
            try:
                await register_call(
                    call_id=sid,
                    agent_id=agent_id,
                    metadata={"provider": "twilio", "session_id": session_id},
                )
                record_call_initiated(agent_id)
                call_registered = True
                log.info("call_registered", call_sid=sid)
            except Exception:
                log.exception("call_registration_failed", call_sid=sid)

        # Initialize GPT Realtime session
        async with GPTRealtimeSession(
            db=db,
            user_id=user_id_int,
            agent_config=agent_config,
            session_id=session_id,
            workspace_id=workspace_id,
        ) as realtime_session:
            # Handle Twilio media stream and capture call_sid
            call_sid = await _handle_twilio_stream(
                websocket=websocket,
                realtime_session=realtime_session,
                log=log,
                enable_transcript=agent.enable_transcript,
                on_call_start=on_call_start,
            )

            # Save transcript to call record if enabled
            if agent.enable_transcript and call_sid:
                transcript = realtime_session.get_transcript()
                await save_transcript_to_call_record(
                    call_sid, transcript, db, log, agent_id=agent.id
                )

    except WebSocketDisconnect:
        log.info("twilio_websocket_disconnected")
    except Exception as e:
        call_failed = True
        log.exception("twilio_websocket_error", error=str(e))
    finally:
        # Unregister call and record metrics
        call_duration = time.time() - call_start_time
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
                log.exception("call_unregistration_failed", call_sid=call_sid)
        log.info("twilio_websocket_closed", stream_sid=stream_sid, call_sid=call_sid)


async def _handle_twilio_stream(  # noqa: PLR0915
    websocket: WebSocket,
    realtime_session: GPTRealtimeSession,
    log: Any,
    enable_transcript: bool = False,
    on_call_start: Any | None = None,
) -> str:
    """Handle Twilio Media Stream messages.

    Args:
        websocket: WebSocket connection from Twilio
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript
        on_call_start: Optional callback when call starts (receives call_sid)

    Returns:
        The call_sid for transcript saving
    """
    stream_sid = ""
    call_sid = ""
    should_end_call = False  # Flag to signal call should end

    async def twilio_to_realtime() -> None:
        """Forward audio from Twilio to GPT Realtime."""
        nonlocal stream_sid, call_sid, should_end_call

        try:
            while not should_end_call:
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
                    break

                elif event == "mark":
                    # Mark events indicate playback position
                    log.debug("twilio_mark_event", name=data.get("mark", {}).get("name"))

        except WebSocketDisconnect:
            log.info("twilio_to_realtime_disconnected")
        except Exception as e:
            log.exception("twilio_to_realtime_error", error=str(e))

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
                        log.exception("audio_send_error", error=str(audio_err))

                # Handle tool calls
                elif event_type == "response.function_call_arguments.done":
                    log.info(
                        "handling_function_call",
                        call_id=event.call_id,
                        name=event.name,
                    )
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
                        break

                # Log other events
                elif event_type in [
                    "response.output_audio.done",
                    "input_audio_buffer.speech_started",
                    "input_audio_buffer.speech_stopped",
                ]:
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log.exception("realtime_to_twilio_error", error=str(e))

    # Run both directions concurrently with timeout to prevent hung tasks
    try:
        await asyncio.wait_for(
            asyncio.gather(
                twilio_to_realtime(),
                realtime_to_twilio(),
                return_exceptions=True,
            ),
            timeout=300.0,  # 5 minute max call duration before forced cleanup
        )
    except TimeoutError:
        log.warning("twilio_bridge_timeout", message="Call exceeded max duration, forcing cleanup")

    # Close WebSocket to hang up the call if end_call was triggered
    if should_end_call:
        log.info("closing_websocket_for_end_call")
        with contextlib.suppress(Exception):
            await websocket.close(code=1000, reason="Call ended by agent")

    return call_sid


@router.websocket("/telnyx/{agent_id}")
async def telnyx_media_stream(  # noqa: PLR0915
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

        # Get workspace for the agent
        workspace_id = await get_agent_workspace_id(agent.id, db)

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
        }

        # Callback to register call when it starts
        async def on_call_start(cid: str) -> None:
            nonlocal call_control_id, call_registered
            call_control_id = cid
            try:
                await register_call(
                    call_id=cid,
                    agent_id=agent_id,
                    metadata={"provider": "telnyx", "session_id": session_id},
                )
                record_call_initiated(agent_id)
                call_registered = True
                log.info("call_registered", call_control_id=cid)
            except Exception:
                log.exception("call_registration_failed", call_control_id=cid)

        # Initialize GPT Realtime session
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
        log.exception("telnyx_websocket_error", error=str(e))
    finally:
        # Unregister call and record metrics
        call_duration = time.time() - call_start_time
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
                log.exception("call_unregistration_failed", call_control_id=call_control_id)

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
    termination = TerminationReason.UNKNOWN

    async def telnyx_to_realtime() -> None:
        """Forward audio from Telnyx to GPT Realtime."""
        nonlocal stream_id, call_control_id, should_end_call, termination

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
                    termination = TerminationReason.CALLER_HANGUP
                    break

        except WebSocketDisconnect:
            log.info("telnyx_to_realtime_disconnected")
        except Exception as e:
            log.exception("telnyx_to_realtime_error", error=str(e))

    async def realtime_to_telnyx() -> None:  # noqa: PLR0912
        """Forward audio from GPT Realtime to Telnyx."""
        nonlocal should_end_call, termination

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            pending_end_call = False  # True when end_call requested but waiting for AI to finish
            greeting_triggered = False  # Track if we've triggered the greeting

            async for event in realtime_session.connection:
                event_type = event.type

                # Trigger initial greeting after session is configured
                # This avoids race condition where audio events arrive before listener is ready
                if event_type == "session.updated" and not greeting_triggered:
                    greeting_triggered = True
                    triggered = await realtime_session.trigger_initial_greeting()
                    if triggered:
                        log.info("initial_greeting_triggered_after_session_update")

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
                        termination = TerminationReason.AGENT_ENDED
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
            log.exception("realtime_to_telnyx_error", error=str(e))

    # Run both directions concurrently with timeout to prevent hung tasks
    try:
        await asyncio.wait_for(
            asyncio.gather(
                telnyx_to_realtime(),
                realtime_to_telnyx(),
                return_exceptions=True,
            ),
            timeout=300.0,  # 5 minute max call duration before forced cleanup
        )
    except TimeoutError:
        log.warning("telnyx_bridge_timeout", message="Call exceeded max duration, forcing cleanup")
        termination = TerminationReason.MAX_DURATION

    # Close WebSocket to hang up the call if end_call was triggered
    if should_end_call:
        log.info("closing_websocket_for_end_call")
        with contextlib.suppress(Exception):
            await websocket.close(code=1000, reason="Call ended by agent")

    return call_control_id, termination
