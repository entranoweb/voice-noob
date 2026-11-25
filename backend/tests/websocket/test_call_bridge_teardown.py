"""When one side of a phone call ends, the other ends with it.

The bridge used to wait for both directions (``gather``) under a fixed five
minute timer. A caller who hung up left the model session, a database connection
and the registry entry held until the timer fired; a model that closed left the
carrier leg open and silent. These tests drive the real endpoints, Twilio and
Telnyx, with a model that stays quiet until the test says otherwise, and check
what is torn down, how soon, and what was recorded.

Nothing sleeps for minutes: the limits are settings, and the tests shorten them.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, ClassVar, Self

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import telephony_ws
from app.core.config import Settings, settings
from app.main import app
from app.models.agent import Agent
from app.models.call_record import CallRecord
from tests.websocket.asgi_ws import ASGIWebSocketClient

# Fixtures and helpers of the two existing end-to-end suites, so these tests run
# against the same real application, signed webhooks and tokens.
from tests.websocket.test_telnyx_inbound_call import (
    CALL_SID as TELNYX_CALL_SID,
)
from tests.websocket.test_telnyx_inbound_call import (
    ULAW_FRAME,
    _place_webhook,
    inbound_call_env,  # noqa: F401
)
from tests.websocket.test_twilio_stream_auth import (
    CALL_SID as TWILIO_CALL_SID,
)
from tests.websocket.test_twilio_stream_auth import (
    _answer_document,
    _start,
    twilio_env,  # noqa: F401
)

CARRIERS = ["telnyx", "twilio"]
HANG_UP_BUDGET_S = 1.0
_EOF = object()


class _LiveConnection:
    """A model socket the test drives: quiet until pushed to, closed on demand."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    def push(self, type: str, **fields: Any) -> None:  # noqa: A002
        self._queue.put_nowait(SimpleNamespace(type=type, **fields))

    def close(self) -> None:
        self._queue.put_nowait(_EOF)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        item = await self._queue.get()
        if item is _EOF:
            raise StopAsyncIteration
        return item


class _LiveSession:
    """Stands in for GPTRealtimeSession and records how it was ended."""

    instances: ClassVar[list[_LiveSession]] = []
    tool_seconds: ClassVar[float] = 0.0
    transcript: ClassVar[str] = "User: hello"

    def __init__(self, **_: Any) -> None:
        self.connection = _LiveConnection()
        self.closed = asyncio.Event()
        self.closed_at: float | None = None
        self.audio_in = 0
        self.tool_started = False
        self.tool_finished = False
        self.tool_cancelled = False
        type(self).instances.append(self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed_at = time.monotonic()
        self.closed.set()

    async def send_audio(self, audio: bytes) -> None:
        self.audio_in += 1

    async def trigger_initial_greeting(self) -> bool:
        return True

    async def handle_function_call_event(self, event: Any) -> dict[str, Any]:
        self.tool_started = True
        try:
            await asyncio.sleep(type(self).tool_seconds)
        except asyncio.CancelledError:
            self.tool_cancelled = True
            raise
        self.tool_finished = True
        return {"success": True}

    def add_user_transcript(self, text: str) -> None:
        return None

    def accumulate_assistant_text(self, delta: str) -> None:
        return None

    def flush_assistant_text(self) -> None:
        return None

    def get_transcript(self) -> str:
        return type(self).transcript


class _Call:
    """One open call on either carrier, from the carrier's side of the socket."""

    def __init__(self, carrier: str, ws: ASGIWebSocketClient) -> None:
        self.carrier = carrier
        self.ws = ws

    async def start(self) -> None:
        if self.carrier == "telnyx":
            await self.ws.send_json(
                {
                    "event": "start",
                    "stream_id": "s-1",
                    "start": {"call_control_id": TELNYX_CALL_SID},
                }
            )
        # Twilio's start frame was sent before the socket was handed over: the
        # endpoint reads it to authenticate.

    async def media(self) -> None:
        await self.ws.send_json({"event": "media", "media": {"payload": ULAW_FRAME}})

    async def stop(self) -> None:
        await self.ws.send_json({"event": "stop"})


@pytest.fixture
def registry(
    monkeypatch: pytest.MonkeyPatch,
    inbound_call_env: dict[str, Any],  # noqa: F811
    twilio_env: dict[str, Any],  # noqa: F811
) -> dict[str, list[str]]:
    """Record registrations; the registry itself is not what is under test.

    Depends on both environments so that this patch is applied after theirs.
    """
    calls: dict[str, list[str]] = {"registered": [], "unregistered": []}

    async def _register(call_id: str, **_: Any) -> bool:
        calls["registered"].append(call_id)
        return True

    async def _unregister(call_id: str) -> bool:
        calls["unregistered"].append(call_id)
        return True

    monkeypatch.setattr(telephony_ws, "register_call", _register)
    monkeypatch.setattr(telephony_ws, "unregister_call", _unregister)
    return calls


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idle off and a roomy cap by default; each test narrows what it tests."""
    monkeypatch.setattr(settings, "CALL_MAX_DURATION_S", 30.0)
    monkeypatch.setattr(settings, "CALL_IDLE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(settings, "CALL_TOOL_GRACE_S", 5.0)


@pytest_asyncio.fixture
async def open_call(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    registry: dict[str, list[str]],
    limits: None,
    test_engine: Any,
    inbound_call_env: dict[str, Any],  # noqa: F811
    twilio_env: dict[str, Any],  # noqa: F811
) -> Any:
    """Open a call on a carrier; returns an async context manager factory.

    Both carriers' environments are built (an agent each, one database); the
    parameter picks which one the test places its call on.
    """
    carrier: str = request.param
    env = inbound_call_env if carrier == "telnyx" else twilio_env
    monkeypatch.setattr(telephony_ws, "GPTRealtimeSession", _LiveSession)
    _LiveSession.instances = []
    _LiveSession.tool_seconds = 0.0

    if carrier == "twilio":
        # The Twilio fixture agent stores no transcript; this one is asked to.
        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            agent = await session.get(Agent, env["agent"].id)
            assert agent is not None
            agent.enable_transcript = True
            await session.commit()

    class _Opened:
        async def __aenter__(self) -> _Call:
            agent_id = str(env["agent"].id)
            if carrier == "telnyx":
                query = await _place_webhook(env)
                self.ws = ASGIWebSocketClient(
                    app, f"/ws/telephony/telnyx/{agent_id}", query_string=query
                )
                await self.ws.__aenter__()
                call = _Call(carrier, self.ws)
            else:
                stream = await _answer_document(
                    "/webhooks/twilio/voice",
                    {
                        "CallSid": TWILIO_CALL_SID,
                        "From": "+15551230000",
                        "To": "+15558886666",
                        "CallStatus": "ringing",
                    },
                )
                token = next(
                    p.attrib["value"]
                    for p in stream.findall("Parameter")
                    if p.attrib["name"] == "token"
                )
                self.ws = ASGIWebSocketClient(app, f"/ws/telephony/twilio/{agent_id}")
                await self.ws.__aenter__()
                await self.ws.send_json(_start(TWILIO_CALL_SID, token))
                call = _Call(carrier, self.ws)
            assert self.ws.accepted
            await call.start()
            # The model session opens once the agent is loaded; wait for it.
            for _ in range(200):
                if _LiveSession.instances:
                    break
                await asyncio.sleep(0.01)
            assert _LiveSession.instances, "the endpoint never opened a model session"
            return call

        async def __aexit__(self, *_: object) -> None:
            await self.ws.__aexit__(None, None, None)

    class _Env:
        carrier_name = carrier
        maker_ = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        provider_call_id = TELNYX_CALL_SID if carrier == "telnyx" else TWILIO_CALL_SID

        @staticmethod
        def call() -> _Opened:
            return _Opened()

        @classmethod
        async def record(cls) -> CallRecord:
            async with cls.maker_() as session:
                result = await session.execute(
                    select(CallRecord).where(CallRecord.provider_call_id == cls.provider_call_id)
                )
                return result.scalar_one()

    return _Env


def _session() -> _LiveSession:
    return _LiveSession.instances[-1]


async def _closed_within(session: _LiveSession, seconds: float) -> None:
    await asyncio.wait_for(session.closed.wait(), timeout=seconds)


async def _carrier_closed(call: _Call, *, deadline_s: float = 1.0) -> int | None:
    await call.ws.drain(deadline_s=deadline_s)
    return call.ws.close_code


async def _no_bridge_tasks_left() -> None:
    await asyncio.sleep(0.05)
    names = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    assert not names & {"carrier_to_model", "model_to_carrier"}, names


@pytest.mark.parametrize("open_call", CARRIERS, indirect=True)
class TestCallerHangsUp:
    @pytest.mark.asyncio
    async def test_the_model_session_closes_within_a_second_and_the_transcript_is_saved(
        self, open_call: Any, registry: dict[str, list[str]]
    ) -> None:
        async with open_call.call() as call:
            await call.media()
            session = _session()

            sent_at = time.monotonic()
            await call.stop()
            await _closed_within(session, HANG_UP_BUDGET_S)

            assert session.closed_at is not None
            assert session.closed_at - sent_at < HANG_UP_BUDGET_S

        record = await open_call.record()
        assert record.transcript == "User: hello"
        assert record.termination_reason == "caller_hangup"
        assert registry["registered"] == [open_call.provider_call_id]
        assert registry["unregistered"] == [open_call.provider_call_id]
        await _no_bridge_tasks_left()

    @pytest.mark.asyncio
    async def test_a_dropped_socket_ends_it_the_same_way(
        self, open_call: Any, registry: dict[str, list[str]]
    ) -> None:
        async with open_call.call() as call:
            await call.media()
            session = _session()

            await call.ws.disconnect()
            await _closed_within(session, HANG_UP_BUDGET_S)

        assert registry["unregistered"] == [open_call.provider_call_id]
        assert (await open_call.record()).termination_reason == "caller_hangup"
        await _no_bridge_tasks_left()


@pytest.mark.parametrize("open_call", CARRIERS, indirect=True)
class TestModelEnds:
    @pytest.mark.asyncio
    async def test_a_model_side_close_ends_the_carrier_leg(
        self, open_call: Any, registry: dict[str, list[str]]
    ) -> None:
        async with open_call.call() as call:
            await call.media()
            session = _session()

            session.connection.close()
            # 1000: on any other code Telnyx reconnects to a handler that is gone.
            assert await _carrier_closed(call) == 1000
            await _closed_within(session, HANG_UP_BUDGET_S)

        record = await open_call.record()
        assert record.termination_reason == "pipeline_error"
        assert record.transcript == "User: hello"
        assert registry["unregistered"] == [open_call.provider_call_id]
        await _no_bridge_tasks_left()

    @pytest.mark.asyncio
    async def test_a_provider_error_event_ends_the_call_and_is_logged_as_an_error(
        self, open_call: Any, registry: dict[str, list[str]]
    ) -> None:
        async with open_call.call() as call:
            session = _session()

            with structlog.testing.capture_logs() as logs:
                session.connection.push(
                    "error",
                    error=SimpleNamespace(
                        type="server_error", code="boom_code", message="the model fell over"
                    ),
                )
                assert await _carrier_closed(call) == 1000
                await _closed_within(session, HANG_UP_BUDGET_S)

        errors = [e for e in logs if e.get("log_level") == "error"]
        (logged,) = [e for e in errors if e.get("event") == "realtime_provider_error"]
        assert logged["error_type"] == "server_error"
        assert logged["error_code"] == "boom_code"
        assert logged["error_message"] == "the model fell over"
        assert (await open_call.record()).termination_reason == "pipeline_error"
        assert registry["unregistered"] == [open_call.provider_call_id]
        await _no_bridge_tasks_left()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            ("invalid_api_key", "invalid_request_error"),
            ("rate_limit_exceeded", "rate_limit_error"),
            ("insufficient_quota", "insufficient_quota"),
            (None, "server_error"),
        ],
    )
    async def test_every_fatal_or_unknown_error_ends_the_call(
        self, open_call: Any, code: str | None, kind: str
    ) -> None:
        async with open_call.call() as call:
            session = _session()
            session.connection.push(
                "error", error=SimpleNamespace(type=kind, code=code, message="no")
            )

            assert await _carrier_closed(call) == 1000
            await _closed_within(session, HANG_UP_BUDGET_S)

        assert (await open_call.record()).termination_reason == "pipeline_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "code",
        ["conversation_already_has_active_response", "input_audio_buffer_commit_empty"],
    )
    async def test_a_benign_race_is_logged_as_a_warning_counted_and_the_call_goes_on(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch, code: str
    ) -> None:
        counted: list[str] = []
        monkeypatch.setattr(telephony_ws, "record_benign_provider_error", counted.append)
        async with open_call.call() as call:
            session = _session()
            with structlog.testing.capture_logs() as logs:
                session.connection.push(
                    "error",
                    error=SimpleNamespace(type="invalid_request_error", code=code, message="race"),
                )
                await asyncio.sleep(0.3)

            assert not session.closed.is_set(), "a benign race ended the call"
            await call.stop()
            await _closed_within(session, HANG_UP_BUDGET_S)

        assert counted == [code]
        ignored = [e for e in logs if e.get("event") == "realtime_provider_error_ignored"]
        assert [e["log_level"] for e in ignored] == ["warning"]
        assert not [e for e in logs if e.get("log_level") == "error"]
        assert (await open_call.record()).termination_reason == "caller_hangup"

    @pytest.mark.asyncio
    async def test_the_agent_ending_the_call_is_still_recorded_as_the_agent(
        self, open_call: Any
    ) -> None:
        async with open_call.call() as call:
            session = _session()
            session.connection.push(
                "response.function_call_arguments.done",
                call_id="fc-1",
                name="end_call",
                arguments="{}",
            )
            # The fake tool result carries no end_call action, so end it the way
            # a real one does: the result asks for it, then the response ends.
            _LiveSession.tool_seconds = 0.0

            async def _end_call(event: Any) -> dict[str, Any]:
                return {"success": True, "action": "end_call", "reason": "done"}

            session.handle_function_call_event = _end_call  # type: ignore[method-assign]
            session.connection.push("response.done", response=SimpleNamespace(status="completed"))

            assert await _carrier_closed(call) == 1000
            await _closed_within(session, HANG_UP_BUDGET_S)

        assert (await open_call.record()).termination_reason == "agent_ended"


def test_the_default_cap_is_thirty_minutes_not_five() -> None:
    assert Settings.model_fields["CALL_MAX_DURATION_S"].default == 1800
    assert Settings.model_fields["CALL_IDLE_TIMEOUT_S"].default > 0


@pytest.mark.parametrize("open_call", CARRIERS, indirect=True)
class TestCallLength:
    @pytest.mark.asyncio
    async def test_a_call_runs_past_the_old_fixed_cap_while_the_cap_is_the_setting(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap is read from settings, not the literal 300 it used to be.

        Time is the setting's, not real minutes: with a cap of 3 s the call is
        still up at 1.2 s, and is ended at the cap, not before.
        """
        monkeypatch.setattr(settings, "CALL_MAX_DURATION_S", 3.0)
        async with open_call.call():
            session = _session()

            await asyncio.sleep(1.2)
            assert not session.closed.is_set()

            await _closed_within(session, 4.0)

        assert (await open_call.record()).termination_reason == "max_duration"

    @pytest.mark.asyncio
    async def test_the_cap_ends_the_call_and_closes_the_carrier(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CALL_MAX_DURATION_S", 0.5)
        async with open_call.call() as call:
            assert await _carrier_closed(call, deadline_s=2.0) == 1000
            await _closed_within(_session(), HANG_UP_BUDGET_S)

        assert (await open_call.record()).termination_reason == "max_duration"
        await _no_bridge_tasks_left()


@pytest.mark.parametrize("open_call", CARRIERS, indirect=True)
class TestIdleTimeout:
    @pytest.mark.asyncio
    async def test_a_silent_call_ends_even_while_the_carrier_streams_frames(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Carrier media frames arrive every 20 ms on a silent line; they are not
        conversation and must not keep the call alive."""
        monkeypatch.setattr(settings, "CALL_IDLE_TIMEOUT_S", 0.5)
        async with open_call.call() as call:
            session = _session()

            async def _line_noise() -> None:
                while True:
                    await call.media()
                    await asyncio.sleep(0.02)

            noise = asyncio.create_task(_line_noise())
            try:
                await _closed_within(session, 3.0)
            finally:
                noise.cancel()
                await asyncio.gather(noise, return_exceptions=True)

        assert (await open_call.record()).termination_reason == "silence_timeout"
        await _no_bridge_tasks_left()

    @pytest.mark.asyncio
    async def test_a_call_with_the_agent_speaking_is_not_idle(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CALL_IDLE_TIMEOUT_S", 0.5)
        async with open_call.call():
            session = _session()
            started = time.monotonic()

            while time.monotonic() - started < 1.5:
                session.connection.push("response.output_audio.delta", delta=ULAW_FRAME)
                await asyncio.sleep(0.1)
                assert not session.closed.is_set(), "ended while the agent was speaking"

            # Once the talking stops the idle clock runs out.
            await _closed_within(session, 3.0)

    @pytest.mark.asyncio
    async def test_a_tool_that_outlasts_the_idle_timeout_is_not_cut_off(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CALL_IDLE_TIMEOUT_S", 0.3)
        _LiveSession.tool_seconds = 1.0
        async with open_call.call():
            session = _session()
            session.connection.push(
                "response.function_call_arguments.done",
                call_id="fc-1",
                name="book_appointment",
                arguments="{}",
            )

            await asyncio.sleep(0.8)
            assert session.tool_started
            assert not session.closed.is_set(), "idle fired during a tool call"

            # The tool ends; the quiet that follows is idle again.
            await _closed_within(session, 3.0)
            assert session.tool_finished
            assert not session.tool_cancelled


@pytest.mark.parametrize("open_call", CARRIERS, indirect=True)
class TestToolInFlightAtTeardown:
    @pytest.mark.asyncio
    async def test_a_tool_is_allowed_to_finish_when_the_caller_hangs_up(
        self, open_call: Any
    ) -> None:
        """A booking write that has started is not abandoned half-way."""
        _LiveSession.tool_seconds = 0.6
        async with open_call.call() as call:
            session = _session()
            session.connection.push(
                "response.function_call_arguments.done",
                call_id="fc-1",
                name="book_appointment",
                arguments="{}",
            )
            await asyncio.sleep(0.1)
            assert session.tool_started
            await call.stop()

            await _closed_within(session, 3.0)

            assert session.tool_finished, "the tool was cancelled mid-write"
            assert not session.tool_cancelled

    @pytest.mark.asyncio
    async def test_a_tool_that_never_returns_is_cancelled_after_the_grace(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CALL_TOOL_GRACE_S", 0.3)
        _LiveSession.tool_seconds = 60.0
        async with open_call.call() as call:
            session = _session()
            session.connection.push(
                "response.function_call_arguments.done",
                call_id="fc-1",
                name="book_appointment",
                arguments="{}",
            )
            await asyncio.sleep(0.1)
            with structlog.testing.capture_logs() as logs:
                await call.stop()

                await _closed_within(session, 2.0)

            assert session.tool_cancelled
        cancelled = [e for e in logs if e.get("event") == "tool_call_cancelled_after_grace"]
        assert len(cancelled) == 1
        assert cancelled[0]["tools"] == ["book_appointment"]
        assert cancelled[0]["log_level"] == "warning"
        await _no_bridge_tasks_left()

    @pytest.mark.asyncio
    async def test_the_duration_cap_landing_mid_tool_lets_the_tool_finish(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CALL_MAX_DURATION_S", 0.5)
        _LiveSession.tool_seconds = 1.0
        async with open_call.call() as call:
            session = _session()
            session.connection.push(
                "response.function_call_arguments.done",
                call_id="fc-1",
                name="book_appointment",
                arguments="{}",
            )
            await asyncio.sleep(0.2)
            assert session.tool_started

            await asyncio.sleep(0.5)
            assert not session.closed.is_set(), "the cap cut a tool that was writing"
            await _closed_within(session, 3.0)

            assert session.tool_finished
            assert not session.tool_cancelled
            assert await _carrier_closed(call) == 1000

        assert (await open_call.record()).termination_reason == "max_duration"
        await _no_bridge_tasks_left()


@pytest.mark.parametrize("open_call", CARRIERS, indirect=True)
class TestPersistenceFailure:
    @staticmethod
    async def _hang_up(open_call: Any) -> None:
        async with open_call.call() as call:
            await call.media()
            session = _session()
            await call.stop()
            await _closed_within(session, HANG_UP_BUDGET_S)

    @pytest.mark.asyncio
    async def test_a_slow_database_does_not_hold_the_model_session_open(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save = telephony_ws.save_transcript_to_call_record
        writing = asyncio.Event()
        release = asyncio.Event()

        async def _slow(*args: Any, **kwargs: Any) -> None:
            writing.set()
            await release.wait()
            await save(*args, **kwargs)

        monkeypatch.setattr(telephony_ws, "save_transcript_to_call_record", _slow)
        async with open_call.call() as call:
            session = _session()
            await call.media()
            await call.stop()
            try:
                # The write is held until released, so this is not a race: the
                # session is either closed before the write starts or never.
                await asyncio.wait_for(writing.wait(), timeout=10.0)
                closed_before_write = session.closed.is_set()
            finally:
                release.set()

        assert closed_before_write, "the model session was still open during the write"
        record = await open_call.record()
        assert record.transcript == "User: hello"
        assert record.termination_reason == "caller_hangup"
        await _no_bridge_tasks_left()

    @pytest.mark.asyncio
    async def test_a_failed_transcript_write_does_not_skip_the_termination_reason(
        self,
        open_call: Any,
        registry: dict[str, list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failed: list[tuple[str, str]] = []
        monkeypatch.setattr(
            telephony_ws,
            "record_call_failed",
            lambda agent_id, error_type: failed.append((agent_id, error_type)),
        )

        async def _boom(*_: Any, **__: Any) -> None:
            message = "database went away"
            raise RuntimeError(message)

        monkeypatch.setattr(telephony_ws, "save_transcript_to_call_record", _boom)
        with structlog.testing.capture_logs() as logs:
            await self._hang_up(open_call)

        record = await open_call.record()
        assert record.termination_reason == "caller_hangup"
        assert record.transcript in (None, "")
        errors = [e for e in logs if e.get("event") == "call_persistence_failed"]
        assert [(e["step"], e["log_level"]) for e in errors] == [("transcript", "error")]
        assert failed == [], "a persistence failure marked the call as failed"
        assert registry["unregistered"] == [open_call.provider_call_id]
        await _no_bridge_tasks_left()

    @pytest.mark.asyncio
    async def test_a_failed_termination_write_does_not_lose_the_transcript(
        self, open_call: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(*_: Any, **__: Any) -> None:
            message = "database went away"
            raise RuntimeError(message)

        monkeypatch.setattr(telephony_ws, "save_termination_reason", _boom)
        with structlog.testing.capture_logs() as logs:
            await self._hang_up(open_call)

        record = await open_call.record()
        assert record.transcript == "User: hello"
        assert record.termination_reason is None
        errors = [e for e in logs if e.get("event") == "call_persistence_failed"]
        assert [e["step"] for e in errors] == ["termination_reason"]
