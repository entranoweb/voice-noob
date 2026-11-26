"""A signed carrier webhook accepted once is not accepted again.

Each replay test is the failing-first kind: before the endpoints honoured the
guard, the second identical request created a second ``call_records`` row and
minted a second stream token.
"""

from __future__ import annotations

import asyncio
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
from app.models.call_record import CallDirection, CallRecord, CallStatus
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
        yield {"key": key, "session_maker": session_maker, "agent_id": str(agent.id)}
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
        # An identical retry inside the token's life is a genuine retry of a lost
        # answer: it gets the same document, not a second token and not a new row.
        assert second.text == first.text
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
        assert second.text == first.text
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


async def _seed_outbound(env: dict[str, Any], call_id: str, status: str = "initiated") -> None:
    """What the dial API writes: an outbound row, not yet answered."""
    async with env["session_maker"]() as session:
        agent = await session.get(Agent, uuid.UUID(env["agent_id"]))
        assert agent is not None
        session.add(
            CallRecord(
                user_id=uuid.uuid4(),
                provider="twilio" if call_id.startswith("CA") else "telnyx",
                provider_call_id=call_id,
                agent_id=agent.id,
                direction=CallDirection.OUTBOUND.value,
                status=status,
                from_number=AGENT_NUMBER,
                to_number=CALLER_NUMBER,
            )
        )
        await session.commit()


def _telnyx_answer(env: dict[str, Any], call_id: str, ts: str | None = None) -> Any:
    body = _form({"CallSid": call_id})
    return body, _telnyx_headers(env, body, ts)


class TestOutboundAnswerIsOneShot:
    """The answer webhook mints a token only by winning an UPDATE on the row."""

    @pytest.mark.asyncio
    async def test_telnyx_second_answer_with_redis_flushed_gets_no_token(
        self, env: dict[str, Any], webhook_replay_store: Any
    ) -> None:
        await _seed_outbound(env, "v3:out-1")
        url = f"/webhooks/telnyx/answer?agent_id={env['agent_id']}"
        async with _client() as client:
            body, headers = _telnyx_answer(env, "v3:out-1")
            first = await client.post(url, content=body, headers=headers)
            await webhook_replay_store.flushall()
            second = await client.post(url, content=body, headers=headers)
        assert "token=" in first.text
        assert second.status_code == 200
        assert "token=" not in second.text

    @pytest.mark.asyncio
    async def test_twilio_second_answer_with_redis_flushed_gets_no_token(
        self, env: dict[str, Any], webhook_replay_store: Any
    ) -> None:
        await _seed_outbound(env, "CA500")
        path = "/webhooks/twilio/answer"
        query = f"?agent_id={env['agent_id']}"
        params = {"CallSid": "CA500"}
        data = BASE + path + query + "CallSidCA500"
        digest = hmac.new(TWILIO_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": base64.b64encode(digest).decode(),
        }
        async with _client() as client:
            first = await client.post(path + query, content=_form(params), headers=headers)
            await webhook_replay_store.flushall()
            second = await client.post(path + query, content=_form(params), headers=headers)
        assert 'name="token"' in first.text
        assert 'name="token"' not in second.text

    @pytest.mark.asyncio
    async def test_two_concurrent_answers_mint_exactly_one_token(self, env: dict[str, Any]) -> None:
        await _seed_outbound(env, "v3:out-race")
        url = f"/webhooks/telnyx/answer?agent_id={env['agent_id']}"
        now = int(time.time())
        async with _client() as client:
            # Distinct timestamps, so distinct Redis keys: only the row can decide.
            responses = await asyncio.gather(
                *[
                    client.post(
                        url,
                        content=_telnyx_answer(env, "v3:out-race", str(now - i))[0],
                        headers=_telnyx_answer(env, "v3:out-race", str(now - i))[1],
                    )
                    for i in range(4)
                ]
            )
        assert sum("token=" in r.text for r in responses) == 1
        assert all(r.status_code == 200 for r in responses)

    @pytest.mark.asyncio
    async def test_a_ringing_callback_before_the_answer_still_leaves_it_answerable(
        self, env: dict[str, Any]
    ) -> None:
        await _seed_outbound(env, "v3:out-cb")
        cb = _form({"CallSid": "v3:out-cb", "CallStatus": "in-progress"})
        url = f"/webhooks/telnyx/answer?agent_id={env['agent_id']}"
        async with _client() as client:
            await client.post(
                "/webhooks/telnyx/status", content=cb, headers=_telnyx_headers(env, cb)
            )
            body, headers = _telnyx_answer(env, "v3:out-cb", str(int(time.time()) - 1))
            answered = await client.post(url, content=body, headers=headers)
        assert "token=" in answered.text

    @pytest.mark.asyncio
    async def test_an_answer_after_the_call_ended_gets_no_token(self, env: dict[str, Any]) -> None:
        await _seed_outbound(env, "v3:out-done", status=CallStatus.COMPLETED.value)
        async with env["session_maker"]() as session:
            record = (
                await session.execute(
                    select(CallRecord).where(CallRecord.provider_call_id == "v3:out-done")
                )
            ).scalar_one()
            from datetime import UTC, datetime

            record.ended_at = datetime.now(UTC)
            await session.commit()
        body, headers = _telnyx_answer(env, "v3:out-done")
        async with _client() as client:
            response = await client.post(
                f"/webhooks/telnyx/answer?agent_id={env['agent_id']}", content=body, headers=headers
            )
        assert "token=" not in response.text


# ---------------------------------------------------------------------------
# Security review of #71
# ---------------------------------------------------------------------------


class TestReplayKeyIsOnSignedContent:
    @pytest.mark.asyncio
    async def test_a_signature_with_junk_spliced_in_is_refused_not_a_new_key(
        self, env: dict[str, Any]
    ) -> None:
        body = _telnyx_body("v3:spliced")
        headers = _telnyx_headers(env, body)
        sig = headers["telnyx-signature-ed25519"]
        spliced = {**headers, "telnyx-signature-ed25519": sig[:10] + "-" + sig[10:]}
        async with _client() as client:
            first = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
            second = await client.post("/webhooks/telnyx/voice", content=body, headers=spliced)
        assert "token=" in first.text
        assert second.status_code == 403
        assert "token=" not in second.text
        assert await _rows(env) == 1

    def test_keys_do_not_depend_on_the_signature_string(self) -> None:
        from app.core.webhook_replay import telnyx_replay_key, twilio_replay_key

        assert telnyx_replay_key("1", b"a") == telnyx_replay_key("1", b"a")
        assert telnyx_replay_key("1", b"a") != telnyx_replay_key("2", b"a")
        assert twilio_replay_key("u", {"b": "2", "a": "1"}) == twilio_replay_key(
            "u", {"a": "1", "b": "2"}
        )
        assert twilio_replay_key("u", {"a": "1"}) != twilio_replay_key("v", {"a": "1"})


async def _fetch(env: dict[str, Any], call_id: str) -> CallRecord:
    async with env["session_maker"]() as session:
        return (
            await session.execute(select(CallRecord).where(CallRecord.provider_call_id == call_id))
        ).scalar_one()


@pytest.fixture
def side_effects(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts = {"campaign": 0, "qa": 0}

    async def campaign(**_: Any) -> None:
        counts["campaign"] += 1

    def qa(*_: Any, **__: Any) -> None:
        counts["qa"] += 1

    async def qa_async(*_: Any, **__: Any) -> None:
        counts["qa"] += 1

    monkeypatch.setattr("app.api.telephony.update_campaign_contact_from_call", campaign)
    monkeypatch.setattr("app.api.telephony.trigger_qa_evaluation", qa_async)
    return counts


class TestTerminalCallIsFinal:
    async def _status(
        self, env: dict[str, Any], client: AsyncClient, call_id: str, status: str, ts: int
    ) -> None:
        body = _form({"CallSid": call_id, "CallStatus": status})
        response = await client.post(
            "/webhooks/telnyx/status", content=body, headers=_telnyx_headers(env, body, str(ts))
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_telnyx_replayed_answered_after_hangup_does_not_reopen(
        self, env: dict[str, Any], side_effects: dict[str, int]
    ) -> None:
        await _seed_outbound(env, "v3:term-1", status="in_progress")
        now = int(time.time())
        async with _client() as client:
            await self._status(env, client, "v3:term-1", "completed", now)
            await self._status(env, client, "v3:term-1", "in-progress", now - 1)
        record = await _fetch(env, "v3:term-1")
        assert record.status == "completed"

    @pytest.mark.asyncio
    async def test_telnyx_replayed_hangup_writes_ended_at_once_and_fires_effects_once(
        self, env: dict[str, Any], side_effects: dict[str, int]
    ) -> None:
        await _seed_outbound(env, "v3:term-2", status="in_progress")
        now = int(time.time())
        async with _client() as client:
            await self._status(env, client, "v3:term-2", "completed", now)
            first = await _fetch(env, "v3:term-2")
            # A different timestamp makes a different Redis key: only the call's
            # own state can stop this one.
            await self._status(env, client, "v3:term-2", "completed", now - 1)
        again = await _fetch(env, "v3:term-2")
        assert first.ended_at is not None
        assert again.ended_at == first.ended_at
        assert again.duration_seconds == first.duration_seconds
        assert side_effects == {"campaign": 1, "qa": 1}

    @pytest.mark.asyncio
    async def test_twilio_replayed_hangup_fires_effects_once(
        self, env: dict[str, Any], side_effects: dict[str, int]
    ) -> None:
        await _seed_outbound(env, "CA800", status="in_progress")
        path = "/webhooks/twilio/status"
        async with _client() as client:
            for duration in ("12", "99"):  # distinct params: distinct keys
                params = {"CallSid": "CA800", "CallStatus": "completed", "CallDuration": duration}
                response = await client.post(
                    path, content=_form(params), headers=_twilio_headers(path, params)
                )
                assert response.status_code == 200
        record = await _fetch(env, "CA800")
        assert record.duration_seconds == 12
        assert side_effects == {"campaign": 1, "qa": 1}


class TestReplayOutlivesTheRow:
    def test_twilio_memory_outlives_the_longest_retention_plus_a_day(self) -> None:
        from app.core.webhook_replay import twilio_ttl_seconds
        from app.services.data_protection import MAX_RETENTION_DAYS

        assert twilio_ttl_seconds() >= (MAX_RETENTION_DAYS + 1) * 86400

    @pytest.mark.asyncio
    async def test_a_replay_after_the_row_is_erased_gets_no_token_and_no_row(
        self, env: dict[str, Any]
    ) -> None:
        path = "/webhooks/twilio/voice"
        params = {"CallSid": "CA900", "From": CALLER_NUMBER, "To": AGENT_NUMBER}
        headers = _twilio_headers(path, params)
        async with _client() as client:
            first = await client.post(path, content=_form(params), headers=headers)
            async with env["session_maker"]() as session:
                await session.execute(CallRecord.__table__.delete())  # erasure
                await session.commit()
            assert await _rows(env) == 0
            # Past the stored document's two minutes, but well inside the key's life.
            from app.core import webhook_replay

            store = await webhook_replay._store()
            for key in await store.keys(f"{webhook_replay._DOC_PREFIX}*"):
                await store.delete(key)
            replay = await client.post(path, content=_form(params), headers=headers)
        assert 'name="token"' in first.text
        assert 'name="token"' not in replay.text
        assert await _rows(env) == 0


class TestAnswerWithNoRow:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.api.telephony._ROW_RECHECK_DELAY_SECONDS", 0.0)

    @pytest.mark.asyncio
    async def test_a_rowless_answer_is_claimable_once_even_with_distinct_signatures(
        self, env: dict[str, Any]
    ) -> None:
        url = f"/webhooks/telnyx/answer?agent_id={env['agent_id']}"
        now = int(time.time())
        async with _client() as client:
            first_body, first_headers = _telnyx_answer(env, "v3:norow", str(now))
            first = await client.post(url, content=first_body, headers=first_headers)
            later_body, later_headers = _telnyx_answer(env, "v3:norow", str(now - 1))
            later = await client.post(url, content=later_body, headers=later_headers)
        assert "token=" in first.text
        assert "token=" not in later.text

    @pytest.mark.asyncio
    async def test_a_row_that_lands_during_the_recheck_is_answered_normally(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_sleep = asyncio.sleep
        seeded = {"done": False}

        async def sleep_then_seed(_: float) -> None:
            if not seeded["done"]:
                seeded["done"] = True
                await _seed_outbound(env, "v3:late-row")
            await real_sleep(0)

        monkeypatch.setattr("app.api.telephony.asyncio.sleep", sleep_then_seed)
        body, headers = _telnyx_answer(env, "v3:late-row")
        async with _client() as client:
            response = await client.post(
                f"/webhooks/telnyx/answer?agent_id={env['agent_id']}", content=body, headers=headers
            )
        assert "token=" in response.text
        assert (await _fetch(env, "v3:late-row")).answered_at is not None


class TestIdempotentRetry:
    @pytest.mark.asyncio
    async def test_retry_inside_two_minutes_gets_the_same_document(
        self, env: dict[str, Any]
    ) -> None:
        body = _telnyx_body("v3:retry")
        headers = _telnyx_headers(env, body)
        async with _client() as client:
            first = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
            retry = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
        assert "token=" in first.text
        assert retry.text == first.text
        assert await _rows(env) == 1

    @pytest.mark.asyncio
    async def test_after_the_document_expires_the_retry_gets_the_empty_document(
        self, env: dict[str, Any], webhook_replay_store: Any
    ) -> None:
        from app.core.webhook_replay import EMPTY_DOCUMENT

        body = _telnyx_body("v3:retry-late")
        headers = _telnyx_headers(env, body)
        async with _client() as client:
            await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
            for key in await webhook_replay_store.keys("webhook-replay-doc:*"):
                await webhook_replay_store.delete(key)  # two minutes have passed
            late = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
        assert late.text == EMPTY_DOCUMENT

    @pytest.mark.asyncio
    async def test_the_stored_document_lives_no_longer_than_the_token(
        self, env: dict[str, Any], webhook_replay_store: Any
    ) -> None:
        body = _telnyx_body("v3:ttl")
        async with _client() as client:
            await client.post(
                "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
            )
        (key,) = await webhook_replay_store.keys("webhook-replay-doc:*")
        assert 0 < await webhook_replay_store.ttl(key) <= 120


class TestHangingRedis:
    @pytest.fixture(autouse=True)
    def _hang(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Hung:
            async def _hang(self, *_: Any, **__: Any) -> Any:
                await asyncio.sleep(30)

            set = get = exists = delete = _hang  # noqa: A003

        async def store() -> Any:
            return Hung()

        monkeypatch.setattr("app.core.webhook_replay._store", store)
        monkeypatch.setattr("app.core.webhook_replay.STORE_TIMEOUT_SECONDS", 0.05)

    @pytest.mark.asyncio
    async def test_a_hung_store_fails_open_quickly(self, env: dict[str, Any]) -> None:
        body = _telnyx_body("v3:hang-open")
        started = time.monotonic()
        async with _client() as client:
            response = await client.post(
                "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
            )
        assert time.monotonic() - started < 3
        assert response.status_code == 200
        assert "token=" in response.text

    @pytest.mark.asyncio
    async def test_a_hung_store_fails_closed_quickly_when_configured(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "WEBHOOK_REPLAY_FAIL_CLOSED", True)
        body = _telnyx_body("v3:hang-closed")
        started = time.monotonic()
        async with _client() as client:
            response = await client.post(
                "/webhooks/telnyx/voice", content=body, headers=_telnyx_headers(env, body)
            )
        assert time.monotonic() - started < 3
        assert response.status_code == 503


class TestClaimReleaseAfterCommit:
    @pytest.mark.asyncio
    async def test_a_failure_after_the_commit_keeps_the_claim(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, webhook_replay_store: Any
    ) -> None:
        body = _telnyx_body("v3:after-commit")
        headers = _telnyx_headers(env, body)

        def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError("after the row was committed")

        monkeypatch.setattr("app.api.telephony.build_telnyx_stream_url", boom)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url=BASE
        ) as client:
            failed = await client.post("/webhooks/telnyx/voice", content=body, headers=headers)
        assert failed.status_code == 500
        assert await _rows(env) == 1
        assert await webhook_replay_store.keys("webhook-replay:*")


class TestRedisDuplicatePathWithoutARow:
    @pytest.mark.asyncio
    async def test_a_replayed_status_callback_for_an_unknown_call_is_a_duplicate(
        self, env: dict[str, Any]
    ) -> None:
        path = "/webhooks/twilio/status"
        params = {"CallSid": "CA-unknown", "CallStatus": "completed", "CallDuration": "3"}
        headers = _twilio_headers(path, params)
        async with _client() as client:
            first = await client.post(path, content=_form(params), headers=headers)
            second = await client.post(path, content=_form(params), headers=headers)
        assert first.json() == {"status": "received"}
        assert second.json() == {"status": "duplicate"}
