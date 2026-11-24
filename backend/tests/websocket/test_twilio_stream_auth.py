"""The Twilio media stream admits only the call its webhook answered.

Twilio cannot authenticate at the handshake: it drops the query string from a
``<Stream>`` URL and delivers custom values in the start frame instead. So the
socket has to be accepted before it can be judged, and the judgement happens on
the first start frame. These tests pin what that leaves exposed in between —
nothing: no agent is loaded and no realtime session is opened until a start frame
carrying a valid token, bound to that frame's ``callSid``, has arrived.

The webhook half is real too: a genuinely signed Twilio request, the TwiML it
returns, and the token taken out of that TwiML the way Twilio would.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any, ClassVar, Self
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import telephony_ws
from app.core.config import settings
from app.core.stream_token import STREAM_TOKEN_REJECTED, mint_stream_token
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.user import User
from tests.websocket.asgi_ws import ASGIWebSocketClient

AGENT_NUMBER = "+15558886666"
CALLER_NUMBER = "+15551230000"
CALL_SID = "CA0123456789abcdef0123456789abcdef"
AUTH_TOKEN = "twilio-auth-token-for-tests"
PUBLIC_ORIGIN = "https://voice.example.com"
ULAW_FRAME = base64.b64encode(b"\xff" * 160).decode()
AGENT_NOT_FOUND = 4004


class _Event:
    def __init__(self, type: str) -> None:  # noqa: A002
        self.type = type


class _Connection:
    def __init__(self) -> None:
        self._events = [_Event("session.updated")]

    def __aiter__(self) -> _Connection:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeRealtimeSession:
    """Stands in for the realtime session and counts how many were opened."""

    opened: ClassVar[int] = 0
    audio_in: ClassVar[list[bytes]] = []

    def __init__(self, **_: Any) -> None:
        type(self).opened += 1
        self.connection = _Connection()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_audio(self, audio: bytes) -> None:
        type(self).audio_in.append(audio)

    async def trigger_initial_greeting(self) -> bool:
        return True

    def get_transcript(self) -> str:
        return ""


def _signature(url: str, params: dict[str, str]) -> str:
    data = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    digest = hmac.new(AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


@pytest_asyncio.fixture
async def twilio_env(test_engine: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        user = User(
            email=f"twilio-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",  # noqa: S106
            full_name="Twilio Test",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        agent = Agent(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Twilio Agent",
            system_prompt="You are a receptionist.",
            is_active=True,
            voice="shimmer",
            language="en",
            enabled_tools=[],
            enable_transcript=False,
            pricing_tier="starter",
            phone_number_id=AGENT_NUMBER,
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)

    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(settings, "PUBLIC_URL", PUBLIC_ORIGIN)
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)
    monkeypatch.setattr(telephony_ws, "GPTRealtimeSession", _FakeRealtimeSession)
    _FakeRealtimeSession.opened = 0
    _FakeRealtimeSession.audio_in = []

    # The call registry is not what is under test, and it reaches for a Redis the
    # dependency override does not cover. Recording the calls keeps the proof
    # that the start frame consumed for authentication was still acted on.
    registered: list[str] = []

    async def _register(call_id: str, **_: Any) -> None:
        registered.append(call_id)

    async def _unregister(_: str) -> None:
        return None

    monkeypatch.setattr(telephony_ws, "register_call", _register)
    monkeypatch.setattr(telephony_ws, "unregister_call", _unregister)

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)

    async def override_get_db() -> Any:
        async with session_maker() as session:
            yield session

    async def override_get_redis() -> Any:
        return redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    try:
        yield {"agent": agent, "registered": registered}
    finally:
        app.dependency_overrides.clear()
        await redis.aclose()


async def _answer_document(path: str, params: dict[str, str], *, query: str = "") -> ET.Element:
    """Post a genuinely signed Twilio webhook and return its <Stream> element.

    The request reaches the app on an internal host, as it would behind a proxy;
    the signature is over the public URL, as Twilio's would be.
    """
    signed_url = f"{PUBLIC_ORIGIN}{path}" + (f"?{query}" if query else "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://internal:8000") as client:
        response = await client.post(
            path + (f"?{query}" if query else ""),
            content=urlencode(params).encode(),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": _signature(signed_url, params),
            },
        )
    assert response.status_code == 200, response.text
    stream = ET.fromstring(response.text).find("./Connect/Stream")  # noqa: S314
    assert stream is not None, "no <Stream> in the answer document"
    return stream


def _start(call_sid: str, token: str | None) -> dict[str, Any]:
    custom: dict[str, str] = {"agent_id": "ignored"}
    if token is not None:
        custom["token"] = token
    return {
        "event": "start",
        "start": {"streamSid": "MZ1", "callSid": call_sid, "customParameters": custom},
    }


class TestTheAnswerDocument:
    @pytest.mark.asyncio
    async def test_inbound_names_the_public_host_and_carries_the_token(
        self, twilio_env: dict[str, Any]
    ) -> None:
        stream = await _answer_document(
            "/webhooks/twilio/voice",
            {
                "CallSid": CALL_SID,
                "From": CALLER_NUMBER,
                "To": AGENT_NUMBER,
                "CallStatus": "ringing",
            },
        )

        url = urlsplit(stream.attrib["url"])
        # The host, not the scheme: this was built from the request, which here
        # is the internal address a carrier cannot reach.
        assert url.netloc == "voice.example.com", stream.attrib["url"]
        assert url.path == f"/ws/telephony/twilio/{twilio_env['agent'].id}"
        # Twilio would drop a query string, so nothing may depend on one.
        assert url.query == ""
        parameters = {p.attrib["name"]: p.attrib["value"] for p in stream.findall("Parameter")}
        assert parameters.get("token"), "the stream token is not in the answer document"

    @pytest.mark.asyncio
    async def test_outbound_answer_does_the_same(self, twilio_env: dict[str, Any]) -> None:
        agent_id = str(twilio_env["agent"].id)

        stream = await _answer_document(
            "/webhooks/twilio/answer",
            {"CallSid": CALL_SID, "From": AGENT_NUMBER, "To": CALLER_NUMBER},
            query=f"agent_id={agent_id}",
        )

        assert urlsplit(stream.attrib["url"]).netloc == "voice.example.com"
        parameters = {p.attrib["name"]: p.attrib["value"] for p in stream.findall("Parameter")}
        assert parameters.get("token")


class TestStreamAdmission:
    async def _token_from_webhook(self) -> str:
        stream = await _answer_document(
            "/webhooks/twilio/voice",
            {
                "CallSid": CALL_SID,
                "From": CALLER_NUMBER,
                "To": AGENT_NUMBER,
                "CallStatus": "ringing",
            },
        )
        return next(
            p.attrib["value"] for p in stream.findall("Parameter") if p.attrib["name"] == "token"
        )

    async def _refused(self, agent_id: str, frames: list[dict[str, Any]]) -> None:
        async with ASGIWebSocketClient(app, f"/ws/telephony/twilio/{agent_id}") as ws:
            assert ws.accepted  # Twilio cannot be judged before this.
            for frame in frames:
                await ws.send_json(frame)
            await ws.drain(deadline_s=1.0)
            assert ws.close_code == STREAM_TOKEN_REJECTED
        assert _FakeRealtimeSession.opened == 0, (
            "a realtime session was opened for a refused stream"
        )
        assert _FakeRealtimeSession.audio_in == []

    @pytest.mark.asyncio
    async def test_the_webhooks_own_token_is_admitted_and_audio_flows(
        self, twilio_env: dict[str, Any]
    ) -> None:
        """The control, end to end: the token out of the TwiML opens the stream,
        and the start frame consumed to check it is still acted on."""
        token = await self._token_from_webhook()
        agent_id = str(twilio_env["agent"].id)

        async with ASGIWebSocketClient(app, f"/ws/telephony/twilio/{agent_id}") as ws:
            assert ws.accepted
            await ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
            await ws.send_json(_start(CALL_SID, token))
            await ws.send_json({"event": "media", "media": {"payload": ULAW_FRAME}})
            await ws.send_json({"event": "stop"})
            await ws.drain(deadline_s=1.0)
            assert ws.close_code != STREAM_TOKEN_REJECTED

        assert _FakeRealtimeSession.opened == 1
        assert _FakeRealtimeSession.audio_in == [b"\xff" * 160]
        # The start frame was read off the socket to authenticate the stream. It
        # still has to reach the bridge, or the call is never registered.
        assert twilio_env["registered"] == [CALL_SID]

    @pytest.mark.asyncio
    async def test_knowing_the_agent_id_is_not_enough(self, twilio_env: dict[str, Any]) -> None:
        """The hole as it stood: a start frame with no token at all."""
        await self._refused(str(twilio_env["agent"].id), [_start(CALL_SID, None)])

    @pytest.mark.asyncio
    async def test_audio_before_a_start_frame_is_refused(self, twilio_env: dict[str, Any]) -> None:
        """A stream that has not said who it is does not get to send audio."""
        await self._refused(
            str(twilio_env["agent"].id),
            [{"event": "media", "media": {"payload": ULAW_FRAME}}],
        )

    @pytest.mark.asyncio
    async def test_a_tampered_token_is_refused(self, twilio_env: dict[str, Any]) -> None:
        token = await self._token_from_webhook()
        expires_at, signature = token.split(".")
        forged = f"{expires_at}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"

        await self._refused(str(twilio_env["agent"].id), [_start(CALL_SID, forged)])

    @pytest.mark.asyncio
    async def test_an_expired_token_is_refused(self, twilio_env: dict[str, Any]) -> None:
        agent_id = str(twilio_env["agent"].id)
        stale = mint_stream_token(
            provider="twilio", agent_id=agent_id, call_id=CALL_SID, now=time.time() - 3600
        )

        await self._refused(agent_id, [_start(CALL_SID, stale)])

    @pytest.mark.asyncio
    async def test_another_agents_token_is_refused(self, twilio_env: dict[str, Any]) -> None:
        borrowed = mint_stream_token(
            provider="twilio", agent_id=str(uuid.uuid4()), call_id=CALL_SID
        )

        await self._refused(str(twilio_env["agent"].id), [_start(CALL_SID, borrowed)])

    @pytest.mark.asyncio
    async def test_a_token_replayed_on_another_call_is_refused(
        self, twilio_env: dict[str, Any]
    ) -> None:
        """The token is bound to the CallSid the webhook saw. Presented on a
        stream that announces a different call, it does not verify."""
        token = await self._token_from_webhook()

        await self._refused(
            str(twilio_env["agent"].id),
            [_start("CAffffffffffffffffffffffffffffffff", token)],
        )

    @pytest.mark.asyncio
    async def test_a_telnyx_token_does_not_open_the_twilio_socket(
        self, twilio_env: dict[str, Any]
    ) -> None:
        agent_id = str(twilio_env["agent"].id)
        wrong_carrier = mint_stream_token(provider="telnyx", agent_id=agent_id, call_id=CALL_SID)

        await self._refused(agent_id, [_start(CALL_SID, wrong_carrier)])

    @pytest.mark.asyncio
    async def test_a_silent_socket_is_closed_at_the_deadline(
        self, twilio_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An accepted socket that never sends a start frame is not held open."""
        monkeypatch.setattr(telephony_ws, "STREAM_START_DEADLINE_S", 0.2)

        await self._refused(str(twilio_env["agent"].id), [])

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_reveal_whether_the_agent_exists(
        self, twilio_env: dict[str, Any]
    ) -> None:
        await self._refused(str(uuid.uuid4()), [_start(CALL_SID, None)])

    @pytest.mark.asyncio
    async def test_a_valid_token_for_an_unknown_agent_reaches_the_lookup(
        self, twilio_env: dict[str, Any]
    ) -> None:
        """Past the door the old behaviour is unchanged: 4004 for an unknown id."""
        unknown = str(uuid.uuid4())
        token = mint_stream_token(provider="twilio", agent_id=unknown, call_id=CALL_SID)

        async with ASGIWebSocketClient(app, f"/ws/telephony/twilio/{unknown}") as ws:
            await ws.send_json(_start(CALL_SID, token))
            await ws.drain(deadline_s=1.0)
            assert ws.close_code == AGENT_NOT_FOUND
        assert _FakeRealtimeSession.opened == 0


class TestATokenAdmitsOneStream:
    """A valid token used to open as many streams as anyone cared to inside its
    two minutes. It is claimed on first use now (``app.core.stream_token_use``)."""

    @pytest.mark.asyncio
    async def test_a_replayed_token_is_refused(self, twilio_env: dict[str, Any]) -> None:
        agent_id = str(twilio_env["agent"].id)
        token = mint_stream_token(provider="twilio", agent_id=agent_id, call_id=CALL_SID)

        async with ASGIWebSocketClient(app, f"/ws/telephony/twilio/{agent_id}") as ws:
            await ws.send_json(_start(CALL_SID, token))
            await ws.send_json({"event": "stop"})
            await ws.drain(deadline_s=1.0)
            assert ws.close_code != STREAM_TOKEN_REJECTED
        assert _FakeRealtimeSession.opened == 1

        async with ASGIWebSocketClient(app, f"/ws/telephony/twilio/{agent_id}") as ws:
            await ws.send_json(_start(CALL_SID, token))
            await ws.drain(deadline_s=1.0)
            assert ws.close_code == STREAM_TOKEN_REJECTED
        assert _FakeRealtimeSession.opened == 1, "the replay opened a second realtime session"

    @pytest.mark.asyncio
    async def test_a_valid_token_is_refused_while_the_claim_store_is_down(
        self, twilio_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: a call that cannot be shown to be the first use of its
        token does not connect."""
        from app.core import stream_token_use

        async def _down() -> Any:
            raise ConnectionError("redis is unreachable")

        monkeypatch.setattr(stream_token_use, "_store", _down)
        agent_id = str(twilio_env["agent"].id)
        token = mint_stream_token(provider="twilio", agent_id=agent_id, call_id=CALL_SID)

        async with ASGIWebSocketClient(app, f"/ws/telephony/twilio/{agent_id}") as ws:
            await ws.send_json(_start(CALL_SID, token))
            await ws.drain(deadline_s=1.0)
            assert ws.close_code == STREAM_TOKEN_REJECTED
        assert _FakeRealtimeSession.opened == 0
