"""One inbound Telnyx call, end to end, against the real application.

This is the test that item #5 was actually asking for. It cannot dial a phone —
that needs a carrier and a handset — but everything on this side of the carrier
is real: the signed webhook, the routing, the TeXML document, the websocket the
document points at, µ-law frames in both directions, the row in Postgres, and the
OpenTelemetry spans. The only stub is the model, because the alternative is a
paid API call in CI.

It exists because two of the defects it covers were invisible to every test in
the suite and fatal to every real call:

1. The inbound webhook parsed the body as JSON. A TeXML application — which is
   what the purchase flow configures, and the only mode where returning a
   document does anything — posts form-encoded. Every real call got a 500 and no
   document, and was dropped by the carrier.
2. The TeXML ``<Stream>`` element set no ``bidirectionalMode``. Telnyx defaults
   that to ``mp3`` and this bridge sends µ-law, so the caller would have heard
   nothing while the logs showed audio being written.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from typing import Any, ClassVar, Self
from xml.etree import ElementTree as ET

import fakeredis
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.call_record import CallRecord, CallStatus
from app.models.user import User
from app.monitoring import call_trace as schema
from app.services.qa.call_metrics import metrics_for_call
from tests.websocket.asgi_ws import ASGIWebSocketClient

AGENT_NUMBER = "+15559997777"
CALLER_NUMBER = "+15551230000"
CALL_SID = "v3:a-real-looking-call-identifier"

# One 20ms frame of µ-law silence at 8kHz: 160 bytes, one byte per sample.
ULAW_FRAME = base64.b64encode(b"\xff" * 160).decode()


# The scripted pause between the caller falling silent and the agent's first
# audio byte. Real time, so the latency the metric reports is a real measurement
# of a real gap rather than the reading of a field that was set for it.
SCRIPTED_THINKING_S = 0.05


class _FakeRealtimeConnection:
    """Replays a scripted OpenAI Realtime event stream."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> _FakeRealtimeConnection:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        event = self._events.pop(0)
        if isinstance(event, _Pause):
            await asyncio.sleep(event.seconds)
            return await self.__anext__()
        return event


class _Pause:
    """A gap in the script, in real seconds."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class _Event:
    """A realtime event. Attribute access is what the bridge reads."""

    def __init__(self, type: str, **fields: Any) -> None:  # noqa: A002
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


class _Response:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeRealtimeSession:
    """Stands in for GPTRealtimeSession, recording the audio it was handed."""

    script: ClassVar[list[Any]] = []

    def __init__(self, **_: Any) -> None:
        self.connection = _FakeRealtimeConnection(list(self.script))
        self.audio_in: list[bytes] = []
        self.tools_called: list[str] = []
        self._transcript: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_audio(self, audio: bytes) -> None:
        self.audio_in.append(audio)

    async def trigger_initial_greeting(self) -> bool:
        return True

    async def handle_function_call_event(self, event: Any) -> dict[str, Any]:
        self.tools_called.append(str(getattr(event, "name", "")))
        return {"result": "booked"}

    def add_user_transcript(self, text: str) -> None:
        self._transcript.append(f"User: {text}")

    def accumulate_assistant_text(self, delta: str) -> None:
        self._transcript.append(delta)

    def flush_assistant_text(self) -> None:
        return None

    def get_transcript(self) -> str:
        return "\n".join(self._transcript)


def _call_script() -> list[Any]:
    """A short call: greeting, caller speaks, agent answers, caller barges in.

    Sequenced the way a real call arrives — the timings the metrics compute are
    derived from the order and the wall clock, not from anything declared here.
    """
    return [
        _Event("session.updated"),
        # Opening greeting. No caller turn precedes it, so there is no wait to
        # measure and time-to-first-audio must stay absent for this turn.
        _Event("response.output_audio.delta", delta=ULAW_FRAME),
        _Event("response.output_audio_transcript.delta", delta="Hi, how can I help?"),
        _Event("response.done", response=_Response("completed")),
        # Caller speaks.
        _Event("input_audio_buffer.speech_started"),
        _Event("input_audio_buffer.speech_stopped"),
        _Event(
            "conversation.item.input_audio_transcription.completed",
            transcript="book me for Tuesday",
        ),
        # The agent thinks. This gap is what time-to-first-audio measures.
        _Pause(SCRIPTED_THINKING_S),
        # Agent answers: this turn has a measurable time to first audio.
        _Event("response.output_audio.delta", delta=ULAW_FRAME),
        _Event("response.output_audio_transcript.delta", delta="Sure, Tuesday works."),
        # Caller barges in while the agent is still speaking.
        _Event("input_audio_buffer.speech_started"),
        _Event("response.done", response=_Response("cancelled")),
        _Event("input_audio_buffer.speech_stopped"),
    ]


@pytest_asyncio.fixture
async def inbound_call_env(test_engine: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """An agent on a number, a signing key, and the app wired to the test database."""
    session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        user = User(
            email=f"inbound-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",  # noqa: S106
            full_name="Inbound Test",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        agent = Agent(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Inbound Agent",
            system_prompt="You are a receptionist.",
            is_active=True,
            voice="shimmer",
            language="en",
            enabled_tools=[],
            enable_transcript=True,
            pricing_tier="starter",
            phone_number_id=AGENT_NUMBER,
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)

    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ),
    ).decode()
    monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", public_key)
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr("app.api.telephony_ws.GPTRealtimeSession", _FakeRealtimeSession)
    _FakeRealtimeSession.script = _call_script()

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)

    async def override_get_db() -> Any:
        async with session_maker() as session:
            yield session

    async def override_get_redis() -> Any:
        return redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis

    try:
        yield {
            "agent": agent,
            "session_maker": session_maker,
            "sign": lambda body: _sign(private_key, body),
        }
    finally:
        app.dependency_overrides.clear()
        await redis.aclose()


def _sign(private_key: Ed25519PrivateKey, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = private_key.sign(f"{timestamp}|".encode() + body)
    return {
        "telnyx-signature-ed25519": base64.b64encode(signature).decode(),
        "telnyx-timestamp": timestamp,
    }


@pytest.fixture
def captured_spans() -> Any:
    """Install a real tracer provider and hand back what was exported."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The emitter resolved its tracer at import time, so the provider has to be
    # swapped underneath it rather than set globally.
    import app.monitoring.call_trace_emitter as emitter_module

    original = emitter_module._tracer
    emitter_module._tracer = provider.get_tracer(
        schema.INSTRUMENTATION_NAME,
        schema.INSTRUMENTATION_VERSION,
    )
    yield exporter
    emitter_module._tracer = original


class TestInboundCallEndToEnd:
    """The whole inbound path, from signed webhook to computed metrics."""

    @pytest.mark.asyncio
    async def test_texml_webhook_answers_with_a_streaming_document(
        self,
        inbound_call_env: dict[str, Any],
    ) -> None:
        """A TeXML application posts form-encoded. This used to return a 500."""
        body = (
            f"CallSid={CALL_SID}&AccountSid=acct&From={CALLER_NUMBER.replace('+', '%2B')}"
            f"&To={AGENT_NUMBER.replace('+', '%2B')}&CallStatus=ringing&Direction=inbound"
        ).encode()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://voice.example.com") as client:
            response = await client.post(
                "/webhooks/telnyx/voice",
                content=body,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    **inbound_call_env["sign"](body),
                },
            )

        assert response.status_code == 200

        document = ET.fromstring(response.text)  # noqa: S314
        stream = document.find("./Connect/Stream")
        assert stream is not None, "no <Stream> in the answer document"

        # Without rtp mode Telnyx expects mp3 back and the caller hears silence.
        assert stream.attrib["bidirectionalMode"] == "rtp"
        assert stream.attrib["bidirectionalCodec"] == "PCMU"
        assert stream.attrib["bidirectionalSamplingRate"] == "8000"

        url = stream.attrib["url"]
        assert url.startswith("wss://")
        assert str(inbound_call_env["agent"].id) in url
        # The call identifier travels to the bridge so it can find this row.
        assert "call_id=" in url

    @pytest.mark.asyncio
    async def test_the_call_lands_a_row(self, inbound_call_env: dict[str, Any]) -> None:
        await _place_webhook(inbound_call_env)

        async with inbound_call_env["session_maker"]() as session:
            result = await session.execute(
                select(CallRecord).where(CallRecord.provider_call_id == CALL_SID),
            )
            record = result.scalar_one()

        assert record.provider == "telnyx"
        assert record.direction == "inbound"
        assert record.status == CallStatus.RINGING.value
        assert record.from_number == CALLER_NUMBER
        assert record.to_number == AGENT_NUMBER
        assert record.agent_id == inbound_call_env["agent"].id

    @pytest.mark.asyncio
    async def test_audio_flows_both_ways(self, inbound_call_env: dict[str, Any]) -> None:
        """Caller µ-law reaches the model and agent µ-law reaches the carrier."""
        await _place_webhook(inbound_call_env)
        agent_id = str(inbound_call_env["agent"].id)

        async with ASGIWebSocketClient(
            app,
            f"/ws/telephony/telnyx/{agent_id}",
            query_string=f"call_id={CALL_SID}",
        ) as ws:
            assert ws.accepted
            await ws.send_json(
                {
                    "event": "start",
                    "stream_id": "stream-1",
                    "start": {"call_control_id": CALL_SID, "media_format": {"encoding": "PCMU"}},
                },
            )
            await ws.send_json({"event": "media", "media": {"payload": ULAW_FRAME}})
            frames = await ws.drain(deadline_s=1.0)

        media_frames = [f for f in frames if f.get("event") == "media"]
        assert media_frames, "no audio was sent back to the carrier"
        assert base64.b64decode(media_frames[0]["media"]["payload"]) == b"\xff" * 160

        # And a barge-in flushes the carrier's playout buffer, which is the only
        # thing that stops the agent talking over the caller.
        assert any(f.get("event") == "clear" for f in frames), "no clear frame on barge-in"

    @pytest.mark.asyncio
    async def test_the_turns_are_recorded_and_measurable(
        self,
        inbound_call_env: dict[str, Any],
    ) -> None:
        await _place_webhook(inbound_call_env)
        await _run_stream(inbound_call_env)

        async with inbound_call_env["session_maker"]() as session:
            result = await session.execute(
                select(CallRecord).where(CallRecord.provider_call_id == CALL_SID),
            )
            record = result.scalar_one()

            assert record.turns, "no turns were persisted"
            scores = metrics_for_call(record).by_name()

        ttfb = scores["time_to_first_audio"]
        assert ttfb.measured, "time to first audio was not measured on a call with audio"
        assert ttfb.value is not None
        # The script paused for real before answering, and the metric found it.
        # A field that merely exists would pass an assertion against zero.
        assert ttfb.value >= SCRIPTED_THINKING_S * 1000.0
        # Only the answering turn is measured. The opening greeting answers no
        # caller turn, so it has no wait and must not contribute a zero.
        assert ttfb.detail["turns_measured"] == 1

        interruptions = scores["interruption_handling"]
        assert interruptions.measured
        assert interruptions.detail["barge_ins"] == 1
        # The response came back cancelled, so the agent did stop.
        assert interruptions.detail["talked_over"] == 0

        # No human caller has a script, so there is no reference to score
        # against. This must stay unmeasurable rather than becoming a flawless
        # zero, which is what a fallback to the transcript would produce.
        wer = scores["transcription_accuracy"]
        assert not wer.measured
        assert wer.value is None

    @pytest.mark.asyncio
    async def test_the_trace_comes_out_in_the_documented_schema(
        self,
        inbound_call_env: dict[str, Any],
        captured_spans: InMemorySpanExporter,
    ) -> None:
        await _place_webhook(inbound_call_env)
        await _run_stream(inbound_call_env)

        spans = captured_spans.get_finished_spans()
        call_spans = [s for s in spans if s.name == schema.SPAN_CALL]
        turn_spans = [s for s in spans if s.name == schema.SPAN_TURN]

        assert len(call_spans) == 1, "the call produced no voice.call span"
        call = call_spans[0]
        assert call.attributes is not None
        assert call.attributes[schema.CALL_PROVIDER] == "telnyx"
        assert call.attributes[schema.CALL_DIRECTION] == "inbound"
        assert call.attributes[schema.CALL_AGENT_ID] == str(inbound_call_env["agent"].id)
        assert call.attributes[schema.CALL_TURN_COUNT] == len(turn_spans)

        assert turn_spans, "no voice.turn spans"
        speakers = [s.attributes[schema.TURN_SPEAKER] for s in turn_spans if s.attributes]
        assert "agent" in speakers
        assert "caller" in speakers

        # Every turn span is a child of the call span, as the documented tree says.
        for turn in turn_spans:
            assert turn.parent is not None
            assert turn.parent.span_id == call.context.span_id

        # The barge-in is visible in the trace, not only in the metric.
        assert any(t.attributes and t.attributes[schema.TURN_BARGE_IN] for t in turn_spans)

    @pytest.mark.asyncio
    async def test_a_rejected_connection_emits_no_completed_call(
        self,
        inbound_call_env: dict[str, Any],
        captured_spans: InMemorySpanExporter,
    ) -> None:
        """An unknown agent is not a call that happened.

        The rejection returns through the same `finally` as a real call, and a
        completed voice.call span for it would put calls that never occurred
        into every dashboard that counts them.
        """
        await _place_webhook(inbound_call_env)

        async with ASGIWebSocketClient(
            app,
            f"/ws/telephony/telnyx/{uuid.uuid4()}",
            query_string=f"call_id={CALL_SID}",
        ) as ws:
            await ws.drain(deadline_s=1.0)

        assert [s for s in captured_spans.get_finished_spans() if s.name == schema.SPAN_CALL] == []

    @pytest.mark.asyncio
    async def test_a_connection_that_says_nothing_emits_no_call(
        self,
        inbound_call_env: dict[str, Any],
        captured_spans: InMemorySpanExporter,
    ) -> None:
        """Accepted is not the same as answered.

        A socket that opens against a real agent and then closes without a start
        frame or any audio carried no call, and a completed span for it would be
        counted by every dashboard that counts calls.
        """
        _FakeRealtimeSession.script = [_Event("session.updated")]
        await _place_webhook(inbound_call_env)
        agent_id = str(inbound_call_env["agent"].id)

        async with ASGIWebSocketClient(
            app,
            f"/ws/telephony/telnyx/{agent_id}",
            query_string=f"call_id={CALL_SID}",
        ) as ws:
            assert ws.accepted
            await ws.drain(deadline_s=0.5)

        assert [s for s in captured_spans.get_finished_spans() if s.name == schema.SPAN_CALL] == []

    @pytest.mark.asyncio
    async def test_turns_are_not_written_to_another_agents_call(
        self,
        inbound_call_env: dict[str, Any],
    ) -> None:
        """The call identifier arrives on the connection, not from anything this
        process authenticated. Without the agent predicate it is a write
        primitive against any row in the table."""
        await _place_webhook(inbound_call_env)

        maker = inbound_call_env["session_maker"]
        async with maker() as session:
            victim = CallRecord(
                id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                provider="telnyx",
                provider_call_id="v3:someone-elses-call",
                direction="inbound",
                status=CallStatus.COMPLETED.value,
                from_number="+1",
                to_number="+2",
                duration_seconds=1,
            )
            session.add(victim)
            await session.commit()
            victim_id = victim.id

        # The connection names a call belonging to nobody it serves.
        agent_id = str(inbound_call_env["agent"].id)
        async with ASGIWebSocketClient(
            app,
            f"/ws/telephony/telnyx/{agent_id}",
            query_string="call_id=v3:someone-elses-call",
        ) as ws:
            await ws.send_json(
                {"event": "start", "stream_id": "s", "start": {"call_control_id": "x"}},
            )
            await ws.send_json({"event": "media", "media": {"payload": ULAW_FRAME}})
            await ws.drain(deadline_s=1.0)
            await ws.send_json({"event": "stop"})

        async with maker() as session:
            row = await session.get(CallRecord, victim_id)
            assert row is not None
            assert row.turns is None, "the bridge wrote turns onto another agent's call"

    @pytest.mark.asyncio
    async def test_a_tool_call_reaches_the_trace(
        self,
        inbound_call_env: dict[str, Any],
        captured_spans: InMemorySpanExporter,
    ) -> None:
        """`voice.tool_call` is in the documented schema; before this it had no
        producer, so a real tool invocation never appeared in a trace."""
        _FakeRealtimeSession.script = [
            _Event("session.updated"),
            _Event("response.output_audio.delta", delta=ULAW_FRAME),
            _Event(
                "response.function_call_arguments.done",
                call_id="fc-1",
                name="book_appointment",
                arguments='{"day": "Tuesday"}',
            ),
            _Event("response.done", response=_Response("completed")),
        ]

        await _place_webhook(inbound_call_env)
        await _run_stream(inbound_call_env)

        tools = [s for s in captured_spans.get_finished_spans() if s.name == schema.SPAN_TOOL_CALL]
        assert tools, "the tool call produced no voice.tool_call span"
        assert tools[0].attributes is not None
        assert tools[0].attributes[schema.TOOL_NAME] == "book_appointment"
        assert tools[0].attributes[schema.TOOL_OUTCOME] == "ok"
        assert '"day": "Tuesday"' in str(tools[0].attributes[schema.TOOL_ARGUMENTS])

    @pytest.mark.asyncio
    async def test_the_recorded_ending_is_persisted_and_read_back(
        self,
        inbound_call_env: dict[str, Any],
    ) -> None:
        """The bridge is the only thing that sees why a call ended.

        Without it on the record, nothing downstream can know — and inferring it
        from the call's status would be a fabrication.
        """
        await _place_webhook(inbound_call_env)
        await _run_stream(inbound_call_env)

        async with inbound_call_env["session_maker"]() as session:
            result = await session.execute(
                select(CallRecord).where(CallRecord.provider_call_id == CALL_SID),
            )
            record = result.scalar_one()
            # The stream ended with an explicit stop frame from the carrier.
            assert record.termination_reason == "caller_hangup"

            results = metrics_for_call(record)

        # Measured, valid, and with nothing to pass or fail against.
        assert results.outcome.value == "observed"
        assert results.trustworthy is True

    @pytest.mark.asyncio
    async def test_an_outbound_leg_is_not_labelled_inbound(
        self,
        inbound_call_env: dict[str, Any],
        captured_spans: InMemorySpanExporter,
    ) -> None:
        """Outbound calls share this endpoint, so the direction travels with the
        stream URL rather than being assumed."""
        await _place_webhook(inbound_call_env)
        agent_id = str(inbound_call_env["agent"].id)

        async with ASGIWebSocketClient(
            app,
            f"/ws/telephony/telnyx/{agent_id}",
            query_string=f"call_id={CALL_SID}&direction=outbound",
        ) as ws:
            await ws.send_json(
                {"event": "start", "stream_id": "s", "start": {"call_control_id": CALL_SID}},
            )
            await ws.drain(deadline_s=1.0)
            await ws.send_json({"event": "stop"})

        (call,) = [s for s in captured_spans.get_finished_spans() if s.name == schema.SPAN_CALL]
        assert call.attributes is not None
        assert call.attributes[schema.CALL_DIRECTION] == "outbound"

    @pytest.mark.asyncio
    async def test_the_status_callback_completes_the_row(
        self,
        inbound_call_env: dict[str, Any],
    ) -> None:
        """TeXML status callbacks are form-encoded too, and used to be ignored."""
        await _place_webhook(inbound_call_env)

        body = f"CallSid={CALL_SID}&CallStatus=completed&CallDuration=42".encode()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://voice.example.com") as client:
            response = await client.post(
                "/webhooks/telnyx/status",
                content=body,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    **inbound_call_env["sign"](body),
                },
            )
        assert response.status_code == 200

        async with inbound_call_env["session_maker"]() as session:
            result = await session.execute(
                select(CallRecord).where(CallRecord.provider_call_id == CALL_SID),
            )
            record = result.scalar_one()

        assert record.status == CallStatus.COMPLETED.value
        assert record.duration_seconds == 42
        assert record.ended_at is not None


async def _place_webhook(env: dict[str, Any]) -> None:
    body = (
        f"CallSid={CALL_SID}&AccountSid=acct&From={CALLER_NUMBER.replace('+', '%2B')}"
        f"&To={AGENT_NUMBER.replace('+', '%2B')}&CallStatus=ringing&Direction=inbound"
    ).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://voice.example.com") as client:
        response = await client.post(
            "/webhooks/telnyx/voice",
            content=body,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                **env["sign"](body),
            },
        )
    assert response.status_code == 200


async def _run_stream(env: dict[str, Any]) -> None:
    agent_id = str(env["agent"].id)
    async with ASGIWebSocketClient(
        app,
        f"/ws/telephony/telnyx/{agent_id}",
        query_string=f"call_id={CALL_SID}",
    ) as ws:
        await ws.send_json(
            {
                "event": "start",
                "stream_id": "stream-1",
                "start": {"call_control_id": CALL_SID},
            },
        )
        await ws.send_json({"event": "media", "media": {"payload": ULAW_FRAME}})
        await ws.drain(deadline_s=1.0)
        await ws.send_json({"event": "stop"})
