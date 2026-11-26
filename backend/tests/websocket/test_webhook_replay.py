"""A signed carrier webhook accepted once is not accepted again.

Each replay test is the failing-first kind: before the endpoints honoured the
guard, the second identical request created a second ``call_records`` row and
minted a second stream token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import fakeredis
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.call_record import CallRecord
from app.models.user import User

AGENT_NUMBER = "+15559997777"
CALLER_NUMBER = "+15551230000"
TWILIO_TOKEN = "twilio-auth-token-for-replay-tests"
BASE = "https://voice.example.com"


def _form(params: dict[str, str]) -> bytes:
    return "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items()).encode()


@pytest_asyncio.fixture
async def env(test_engine: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        user = User(
            email=f"replay-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",  # noqa: S106
            full_name="Replay Test",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        agent = Agent(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Replay Agent",
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

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", public)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", TWILIO_TOKEN)
    monkeypatch.setattr(settings, "PUBLIC_URL", BASE)
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)

    async def override_get_db() -> Any:
        async with session_maker() as session:
            yield session

    async def override_get_redis() -> Any:
        return redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    try:
        yield {"key": key, "session_maker": session_maker}
    finally:
        app.dependency_overrides.clear()
        await redis.aclose()


def _telnyx_headers(
    env: dict[str, Any], body: bytes, timestamp: str | None = None
) -> dict[str, str]:
    ts = timestamp or str(int(time.time()))
    sig = env["key"].sign(f"{ts}|".encode() + body)
    return {
        "content-type": "application/x-www-form-urlencoded",
        "telnyx-signature-ed25519": base64.b64encode(sig).decode(),
        "telnyx-timestamp": ts,
    }


def _twilio_headers(path: str, params: dict[str, str]) -> dict[str, str]:
    data = BASE + path + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    digest = hmac.new(TWILIO_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return {
        "content-type": "application/x-www-form-urlencoded",
        "X-Twilio-Signature": base64.b64encode(digest).decode(),
    }


async def _rows(env: dict[str, Any]) -> int:
    async with env["session_maker"]() as session:
        return int(await session.scalar(select(func.count()).select_from(CallRecord)) or 0)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE)


def _telnyx_body(call_id: str) -> bytes:
    return _form(
        {
            "CallSid": call_id,
            "From": CALLER_NUMBER,
            "To": AGENT_NUMBER,
            "CallStatus": "ringing",
            "Direction": "inbound",
        }
    )


class TestTelnyxReplay:
    @pytest.mark.asyncio
    async def test_replay_inside_the_window_is_a_noop(self, env: dict[str, Any]) -> None:
        body = _telnyx_body("v3:call-one")
        headers = _telnyx_headers(env, body)
        async with _client() as client:
            first = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
            second = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)

        assert first.status_code == 200
        assert "token=" in first.text
        # A retry must not be answered with an error: that can drop a live call.
        assert second.status_code == 200
        assert "token=" not in second.text
        assert "<Stream" not in second.text
        assert await _rows(env) == 1

    @pytest.mark.asyncio
    async def test_distinct_events_are_both_accepted(self, env: dict[str, Any]) -> None:
        async with _client() as client:
            for call_id in ("v3:call-a", "v3:call-b"):
                body = _telnyx_body(call_id)
                response = await client.post(
                    "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
                )
                assert "token=" in response.text
        assert await _rows(env) == 2

    @pytest.mark.asyncio
    async def test_a_failed_handler_releases_the_claim_so_the_retry_is_answered(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = _telnyx_body("v3:call-flaky")
        headers = _telnyx_headers(env, body)

        async def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError("database blip")

        with monkeypatch.context() as patch:
            patch.setattr("app.api.telephony.get_agent_workspace_id", boom)
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url=BASE
            ) as client:
                failed = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
        assert failed.status_code == 500

        async with _client() as client:
            retry = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
        assert "token=" in retry.text
        assert await _rows(env) == 1


class TestTwilioReplay:
    PATH = "/webhooks/twilio/voice"

    def _params(self, sid: str = "CA1") -> dict[str, str]:
        return {
            "CallSid": sid,
            "From": CALLER_NUMBER,
            "To": AGENT_NUMBER,
            "CallStatus": "ringing",
        }

    @pytest.mark.asyncio
    async def test_replay_is_a_noop_without_a_second_row_or_token(
        self, env: dict[str, Any]
    ) -> None:
        params = self._params()
        headers = _twilio_headers(self.PATH, params)
        async with _client() as client:
            first = await client.post(self.PATH, content=_form(params), headers=headers)
            second = await client.post(self.PATH, content=_form(params), headers=headers)

        assert first.status_code == 200
        assert 'name="token"' in first.text
        assert second.status_code == 200
        assert 'name="token"' not in second.text
        assert "<Stream" not in second.text
        assert await _rows(env) == 1

    @pytest.mark.asyncio
    async def test_distinct_calls_are_both_accepted(self, env: dict[str, Any]) -> None:
        async with _client() as client:
            for sid in ("CA1", "CA2"):
                params = self._params(sid)
                response = await client.post(
                    self.PATH, content=_form(params), headers=_twilio_headers(self.PATH, params)
                )
                assert 'name="token"' in response.text
        assert await _rows(env) == 2

    @pytest.mark.asyncio
    async def test_a_late_in_progress_callback_cannot_reopen_a_completed_call(
        self, env: dict[str, Any]
    ) -> None:
        path = "/webhooks/twilio/status"
        async with _client() as client:
            voice = self._params("CA9")
            await client.post(
                self.PATH, content=_form(voice), headers=_twilio_headers(self.PATH, voice)
            )
            for status in ("completed", "in-progress"):
                params = {"CallSid": "CA9", "CallStatus": status, "CallDuration": "12"}
                response = await client.post(
                    path, content=_form(params), headers=_twilio_headers(path, params)
                )
                assert response.status_code == 200
        async with env["session_maker"]() as session:
            record = (await session.execute(select(CallRecord))).scalar_one()
        assert record.status == "completed"


class TestRedisDown:
    @pytest.fixture(autouse=True)
    def _redis_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def down() -> Any:
            raise ConnectionError("redis is down")

        monkeypatch.setattr("app.core.webhook_replay._store", down)

    @pytest.mark.asyncio
    async def test_fails_open_by_default_and_the_call_is_answered(
        self, env: dict[str, Any]
    ) -> None:
        body = _telnyx_body("v3:call-redis-down")
        async with _client() as client:
            response = await client.post(
                "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
            )
        assert response.status_code == 200
        assert "token=" in response.text
        assert await _rows(env) == 1

    @pytest.mark.asyncio
    async def test_unsigned_is_still_refused_while_redis_is_down(self, env: dict[str, Any]) -> None:
        body = _telnyx_body("v3:forged")
        async with _client() as client:
            response = await client.post(
                "/webhooks/telnyx/voice",
                content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_fail_closed_setting_refuses_with_503_and_writes_nothing(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "WEBHOOK_REPLAY_FAIL_CLOSED", True)
        body = _telnyx_body("v3:call-closed")
        async with _client() as client:
            response = await client.post(
                "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
            )
        assert response.status_code == 503
        assert await _rows(env) == 0


def test_the_xml_helper_is_parseable() -> None:
    from app.core.webhook_replay import EMPTY_DOCUMENT

    assert ET.fromstring(EMPTY_DOCUMENT).tag == "Response"  # noqa: S314


class TestExistingCallRowIsPermanent:
    """The Redis memory expires and can be down; the row does not and is not."""

    @pytest.mark.asyncio
    async def test_telnyx_answer_with_no_redis_key_but_a_row_is_a_noop(
        self, env: dict[str, Any], webhook_replay_store: Any
    ) -> None:
        body = _telnyx_body("v3:call-expired")
        async with _client() as client:
            first = await client.post(
                "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
            )
            await webhook_replay_store.flushall()  # the key has expired
            second = await client.post(
                "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
            )
        assert "token=" in first.text
        assert second.status_code == 200
        assert "token=" not in second.text
        assert await _rows(env) == 1

    @pytest.mark.asyncio
    async def test_twilio_answer_with_no_redis_key_but_a_row_is_a_noop(
        self, env: dict[str, Any], webhook_replay_store: Any
    ) -> None:
        path = "/webhooks/twilio/voice"
        params = {
            "CallSid": "CA77",
            "From": CALLER_NUMBER,
            "To": AGENT_NUMBER,
            "CallStatus": "ringing",
        }
        headers = _twilio_headers(path, params)
        async with _client() as client:
            first = await client.post(path, content=_form(params), headers=headers)
            await webhook_replay_store.flushall()
            second = await client.post(path, content=_form(params), headers=headers)
        assert 'name="token"' in first.text
        assert second.status_code == 200
        assert 'name="token"' not in second.text
        assert await _rows(env) == 1
