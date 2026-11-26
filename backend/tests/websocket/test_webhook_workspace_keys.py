"""Which workspace's key verifies a webhook, for an agent that is in two workspaces.

The agent is in W1 (its default) and W2, and the number is registered in W2. W2's
carrier account is the one that proved it holds the number, so W2's key is the
one that must sign anything about the call. Every request here is genuinely
signed; what differs is which workspace's key signed it. Before, several
webhooks picked "the agent's first workspace" (an unordered LIMIT 1) and so
could verify against W1 for a call that billed W2.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from xml.etree import ElementTree as ET

import httpx
import pytest
import pytest_asyncio
import structlog.testing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import telephony
from app.api.auth import create_access_token
from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.phone_number import PhoneNumber, PhoneNumberStatus
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.workspace import AgentWorkspace, Workspace
from app.services.telephony.base import CallDirection as CarrierDirection
from app.services.telephony.base import CallInfo
from app.services.telephony.base import CallStatus as CarrierStatus
from app.services.telephony.outbound import OutboundRefusedError
from app.services.telephony.telnyx_service import TelnyxService
from app.services.telephony.twilio_service import TwilioService

TELNYX_NUMBER = "+15550100101"
TWILIO_NUMBER = "+15550100102"
CALLER = "+15550100199"
PUBLIC = "https://voice.example.com"
TOKENS = {"w1": "twilio-token-for-w1", "w2": "twilio-token-for-w2"}


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
    ).decode()
    return private, public


def _telnyx_headers(private: Ed25519PrivateKey, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = private.sign(f"{timestamp}|".encode() + body)
    return {
        "telnyx-signature-ed25519": base64.b64encode(signature).decode(),
        "telnyx-timestamp": timestamp,
        "content-type": "application/x-www-form-urlencoded",
    }


def _twilio_headers(token: str, path: str, params: dict[str, str]) -> dict[str, str]:
    data = f"{PUBLIC}{path}" + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    digest = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    return {
        "X-Twilio-Signature": base64.b64encode(digest).decode(),
        "content-type": "application/x-www-form-urlencoded",
    }


def _public_of(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
    ).decode()


class _Carrier:
    """Records the outbound calls it was asked to place."""

    placed: list[dict[str, str]] = []  # noqa: RUF012

    async def initiate_call(self, **kwargs: str) -> Any:
        type(self).placed.append(kwargs)

        return CallInfo(
            call_id="carrier-call-id",
            call_control_id="carrier-call-control-id",
            from_number=kwargs["from_number"],
            to_number=kwargs["to_number"],
            direction=CarrierDirection.OUTBOUND,
            status=CarrierStatus.INITIATED,
        )


@pytest_asyncio.fixture
async def env(test_engine: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    keys = {"w1": _keypair(), "w2": _keypair()}
    async with sessions() as session:
        user = User(
            email=f"two-ws-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",  # noqa: S106
            full_name="Owner",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        w1 = Workspace(user_id=user.id, name="First")
        w2 = Workspace(user_id=user.id, name="Second")
        agent = Agent(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Front Desk",
            system_prompt="You answer.",
            is_active=True,
            voice="shimmer",
            language="en",
            enabled_tools=[],
            pricing_tier="starter",
            phone_number_id=TELNYX_NUMBER,
        )
        session.add_all([w1, w2, agent])
        await session.commit()
        session.add(AgentWorkspace(agent_id=agent.id, workspace_id=w1.id, is_default=True))
        session.add(AgentWorkspace(agent_id=agent.id, workspace_id=w2.id, is_default=False))
        for number, provider in ((TELNYX_NUMBER, "telnyx"), (TWILIO_NUMBER, "twilio")):
            session.add(
                PhoneNumber(
                    user_id=user_id_to_uuid(user.id),
                    workspace_id=w2.id,
                    phone_number=number,
                    provider=provider,
                    provider_id=f"pn-{uuid.uuid4()}",
                    status=PhoneNumberStatus.ACTIVE.value,
                    assigned_agent_id=agent.id,
                )
            )
        for name, workspace in (("w1", w1), ("w2", w2)):
            session.add(
                UserSettings(
                    user_id=user_id_to_uuid(user.id),
                    workspace_id=workspace.id,
                    openai_api_key=f"sk-{name}",
                    telnyx_api_key=f"telnyx-api-{name}",
                    telnyx_public_key=keys[name][1],
                    twilio_account_sid=f"AC{name}",
                    twilio_auth_token=TOKENS[name],
                )
            )
        await session.commit()
        await session.refresh(agent)

    monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "PUBLIC_URL", PUBLIC)
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)
    _Carrier.placed = []

    async def override_get_db() -> Any:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {
            "sessions": sessions,
            "user": user,
            "agent": agent,
            "w1": w1,
            "w2": w2,
            "keys": {name: pair[0] for name, pair in keys.items()},
        }
    finally:
        app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://internal:8000")


async def _seed_call(
    env: dict[str, Any], *, provider: str, call_id: str, workspace: str = "w2", **extra: Any
) -> CallRecord:
    async with env["sessions"]() as session:
        record = CallRecord(
            user_id=user_id_to_uuid(env["user"].id),
            workspace_id=env[workspace].id,
            provider=provider,
            provider_call_id=call_id,
            agent_id=env["agent"].id,
            direction=CallDirection.OUTBOUND.value,
            status=CallStatus.INITIATED.value,
            from_number=TELNYX_NUMBER,
            to_number=CALLER,
            **extra,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def _status_of(env: dict[str, Any], call_id: str) -> str:
    async with env["sessions"]() as session:
        return (
            await session.execute(
                select(CallRecord.status).where(CallRecord.provider_call_id == call_id)
            )
        ).scalar_one()


class TestTelnyx:
    async def _post(self, path: str, body: bytes, key: Ed25519PrivateKey) -> httpx.Response:
        async with _client() as client:
            return await client.post(path, content=body, headers=_telnyx_headers(key, body))

    @staticmethod
    def _voice_body(call_id: str) -> bytes:
        return urlencode(
            {"CallSid": call_id, "From": CALLER, "To": TELNYX_NUMBER, "CallStatus": "ringing"}
        ).encode()

    @pytest.mark.asyncio
    async def test_the_voice_webhook_accepts_the_numbers_workspace_key(
        self, env: dict[str, Any]
    ) -> None:
        body = self._voice_body("v3:in-w2")
        response = await self._post("/webhooks/telnyx/voice", body, env["keys"]["w2"])
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_the_voice_webhook_refuses_the_agents_other_workspace_key(
        self, env: dict[str, Any]
    ) -> None:
        body = self._voice_body("v3:in-w1")
        response = await self._post("/webhooks/telnyx/voice", body, env["keys"]["w1"])
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_twilio_registered_number_does_not_route_a_telnyx_call(
        self, env: dict[str, Any]
    ) -> None:
        body = urlencode(
            {"CallSid": "v3:x", "From": CALLER, "To": TWILIO_NUMBER, "CallStatus": "ringing"}
        ).encode()
        response = await self._post("/webhooks/telnyx/voice", body, env["keys"]["w2"])
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_the_answer_webhook_uses_the_call_records_workspace(
        self, env: dict[str, Any]
    ) -> None:
        await _seed_call(env, provider="telnyx", call_id="v3:out")
        body = self._voice_body("v3:out")
        path = f"/webhooks/telnyx/answer?agent_id={env['agent'].id}"

        wrong = await self._post(path, body, env["keys"]["w1"])
        right = await self._post(path, body, env["keys"]["w2"])

        assert wrong.status_code == 403
        assert right.status_code == 200, right.text

    @pytest.mark.asyncio
    async def test_an_answer_with_no_call_record_is_refused(self, env: dict[str, Any]) -> None:
        body = self._voice_body("v3:unknown")
        path = f"/webhooks/telnyx/answer?agent_id={env['agent'].id}"
        response = await self._post(path, body, env["keys"]["w2"])
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_an_outbound_call_is_found_by_record_id_when_the_carrier_id_differs(
        self, env: dict[str, Any]
    ) -> None:
        """If Telnyx's CallSid is not the id stored, the stream still gets the stored one."""
        record = await _seed_call(env, provider="telnyx", call_id="stored-id")
        body = self._voice_body("different-carrier-sid")
        path = f"/webhooks/telnyx/answer?agent_id={env['agent'].id}&record_id={record.id}"

        response = await self._post(path, body, env["keys"]["w2"])

        assert response.status_code == 200, response.text
        stream = ET.fromstring(response.text).find("./Connect/Stream")  # noqa: S314
        assert stream is not None
        query = dict(parse_qsl(urlsplit(stream.attrib["url"]).query))
        assert query["call_id"] == "stored-id"

    @pytest.mark.asyncio
    async def test_the_record_id_of_another_agents_call_is_no_key(
        self, env: dict[str, Any]
    ) -> None:
        """`record_id` is a hint, not a grant: it must be this agent's own record.

        A real second agent in the same workspace, with its own call. Naming
        the first agent's record from the second agent's URL, signed with the
        right workspace key, finds nothing: only the agent filter in
        `_call_record_for` stands between this request and the record.
        """
        record = await _seed_call(env, provider="telnyx", call_id="stored-id")
        async with env["sessions"]() as session:
            other = Agent(
                id=uuid.uuid4(),
                user_id=env["user"].id,
                name="Other",
                system_prompt="You answer.",
                is_active=True,
                voice="shimmer",
                language="en",
                enabled_tools=[],
                pricing_tier="starter",
            )
            session.add(other)
            await session.commit()
            session.add(AgentWorkspace(agent_id=other.id, workspace_id=env["w2"].id))
            await session.commit()
        body = self._voice_body("different-carrier-sid")
        path = f"/webhooks/telnyx/answer?agent_id={other.id}&record_id={record.id}"

        response = await self._post(path, body, env["keys"]["w2"])

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_the_status_callback_uses_the_call_records_workspace(
        self, env: dict[str, Any]
    ) -> None:
        await _seed_call(env, provider="telnyx", call_id="v3:live")
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.hangup",
                    "payload": {"call_control_id": "v3:live", "duration_seconds": 5},
                }
            }
        ).encode()

        wrong = await self._post("/webhooks/telnyx/status", body, env["keys"]["w1"])
        assert wrong.status_code == 403
        assert await _status_of(env, "v3:live") == CallStatus.INITIATED.value
        right = await self._post("/webhooks/telnyx/status", body, env["keys"]["w2"])
        assert right.status_code == 200, right.text

    @pytest.mark.asyncio
    async def test_a_status_callback_ignores_another_carriers_record(
        self, env: dict[str, Any]
    ) -> None:
        """Same id, other carrier: that row is not this call's, so it is no record."""
        await _seed_call(env, provider="twilio", call_id="shared-id")
        body = json.dumps(
            {"data": {"event_type": "call.hangup", "payload": {"call_control_id": "shared-id"}}}
        ).encode()

        response = await self._post("/webhooks/telnyx/status", body, env["keys"]["w2"])

        assert response.status_code == 503
        assert await _status_of(env, "shared-id") == CallStatus.INITIATED.value

    @pytest.mark.asyncio
    async def test_a_status_callback_for_an_unrecorded_call_is_retryable_not_dropped(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No record: not 200 (that drops it), and the global key alone does not buy one."""
        monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", _public_of(env["keys"]["w1"]))
        body = json.dumps(
            {"data": {"event_type": "call.hangup", "payload": {"call_control_id": "v3:nobody"}}}
        ).encode()

        with structlog.testing.capture_logs() as logs:
            response = await self._post("/webhooks/telnyx/status", body, env["keys"]["w1"])

        assert response.status_code == 503
        assert response.headers["retry-after"]
        assert any(entry["event"] == "status_callback_no_record" for entry in logs)

    @pytest.mark.asyncio
    async def test_an_answer_for_an_unrecorded_call_is_refused_even_with_the_global_key(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", _public_of(env["keys"]["w1"]))
        body = self._voice_body("v3:nobody")
        path = f"/webhooks/telnyx/answer?agent_id={env['agent'].id}"

        response = await self._post(path, body, env["keys"]["w1"])

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_an_answer_that_beats_the_post_dial_write_streams_with_the_carriers_id(
        self, env: dict[str, Any]
    ) -> None:
        """A pending record must not put `pending:<id>` in the stream URL (a silent call)."""
        record = await _seed_call(env, provider="telnyx", call_id=f"pending:{uuid.uuid4()}")
        body = self._voice_body("carrier-sid-early")
        path = f"/webhooks/telnyx/answer?agent_id={env['agent'].id}&record_id={record.id}"

        response = await self._post(path, body, env["keys"]["w2"])

        assert response.status_code == 200, response.text
        stream = ET.fromstring(response.text).find("./Connect/Stream")  # noqa: S314
        assert stream is not None
        query = dict(parse_qsl(urlsplit(stream.attrib["url"]).query))
        assert query["call_id"] == "carrier-sid-early"
        async with env["sessions"]() as session:
            stored = (
                await session.execute(
                    select(CallRecord.provider_call_id).where(CallRecord.id == record.id)
                )
            ).scalar_one()
        assert stored == "carrier-sid-early"

    @pytest.mark.asyncio
    async def test_an_answer_with_no_call_id_on_a_pending_record_is_refused(
        self, env: dict[str, Any]
    ) -> None:
        """No carrier id anywhere: `pending:<id>` must never reach the stream URL."""
        record = await _seed_call(env, provider="telnyx", call_id=f"pending:{uuid.uuid4()}")
        body = self._voice_body("")
        path = f"/webhooks/telnyx/answer?agent_id={env['agent'].id}&record_id={record.id}"

        with structlog.testing.capture_logs() as logs:
            response = await self._post(path, body, env["keys"]["w2"])

        assert response.status_code == 403
        assert "pending:" not in response.text
        assert any(e["event"] == "telnyx_answer_without_call_id_on_pending_record" for e in logs)
        async with env["sessions"]() as session:
            row = (
                await session.execute(select(CallRecord).where(CallRecord.id == record.id))
            ).scalar_one()
        assert row.answered_at is None
        assert row.provider_call_id.startswith("pending:")

    @pytest.mark.asyncio
    async def test_an_answer_does_not_replace_an_id_that_is_already_stored(
        self, env: dict[str, Any]
    ) -> None:
        record = await _seed_call(env, provider="telnyx", call_id="stored-id")
        body = self._voice_body("some-other-sid")
        path = f"/webhooks/telnyx/answer?agent_id={env['agent'].id}&record_id={record.id}"

        response = await self._post(path, body, env["keys"]["w2"])

        assert response.status_code == 200, response.text
        stream = ET.fromstring(response.text).find("./Connect/Stream")  # noqa: S314
        assert stream is not None
        assert dict(parse_qsl(urlsplit(stream.attrib["url"]).query))["call_id"] == "stored-id"

    @pytest.mark.asyncio
    async def test_an_early_status_callback_finds_its_record_by_record_id(
        self, env: dict[str, Any]
    ) -> None:
        record = await _seed_call(env, provider="telnyx", call_id=f"pending:{uuid.uuid4()}")
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.hangup",
                    "payload": {"call_control_id": "carrier-sid-early", "duration_seconds": 3},
                }
            }
        ).encode()
        path = f"/webhooks/telnyx/status?agent_id={env['agent'].id}&record_id={record.id}"

        response = await self._post(path, body, env["keys"]["w2"])

        assert response.status_code == 200, response.text
        async with env["sessions"]() as session:
            row = (
                await session.execute(select(CallRecord).where(CallRecord.id == record.id))
            ).scalar_one()
        assert row.provider_call_id == "carrier-sid-early"
        assert row.ended_at is not None

    @pytest.mark.asyncio
    async def test_the_create_call_request_carries_a_status_callback_with_the_record_id(
        self,
    ) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"sid": "s1", "call_control_id": "cc1"}})

        service = TelnyxService("key")
        service._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.telnyx.com/v2"
        )

        async def connection() -> str:
            return "conn-1"

        service._get_connection_id = connection  # type: ignore[method-assign]
        await service.initiate_call(
            to_number=CALLER,
            from_number=TELNYX_NUMBER,
            webhook_url=f"{PUBLIC}/webhooks/telnyx/answer?agent_id=a&record_id=r1",
        )

        assert (
            seen[0]["status_callback"] == f"{PUBLIC}/webhooks/telnyx/status?agent_id=a&record_id=r1"
        )
        assert seen[0]["status_callback_method"] == "POST"


class TestTwilio:
    async def _post(
        self, path: str, params: dict[str, str], token: str, query: str = ""
    ) -> httpx.Response:
        full = path + (f"?{query}" if query else "")
        async with _client() as client:
            return await client.post(
                full,
                content=urlencode(params).encode(),
                headers=_twilio_headers(token, full, params),
            )

    @staticmethod
    def _voice(call_sid: str, to: str = TWILIO_NUMBER) -> dict[str, str]:
        return {"CallSid": call_sid, "From": CALLER, "To": to, "CallStatus": "ringing"}

    @pytest.mark.asyncio
    async def test_the_voice_webhook_accepts_the_numbers_workspace_token(
        self, env: dict[str, Any]
    ) -> None:
        response = await self._post("/webhooks/twilio/voice", self._voice("CA1"), TOKENS["w2"])
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_the_voice_webhook_refuses_the_agents_other_workspace_token(
        self, env: dict[str, Any]
    ) -> None:
        response = await self._post("/webhooks/twilio/voice", self._voice("CA2"), TOKENS["w1"])
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_telnyx_registered_number_does_not_route_a_twilio_call(
        self, env: dict[str, Any]
    ) -> None:
        params = self._voice("CA3", to=TELNYX_NUMBER)
        response = await self._post("/webhooks/twilio/voice", params, TOKENS["w2"])
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_the_answer_webhook_uses_the_call_records_workspace(
        self, env: dict[str, Any]
    ) -> None:
        await _seed_call(env, provider="twilio", call_id="CA4")
        query = f"agent_id={env['agent'].id}"
        params = self._voice("CA4")

        wrong = await self._post("/webhooks/twilio/answer", params, TOKENS["w1"], query)
        right = await self._post("/webhooks/twilio/answer", params, TOKENS["w2"], query)

        assert wrong.status_code == 403
        assert right.status_code == 200, right.text

    @pytest.mark.asyncio
    async def test_the_status_callback_uses_the_call_records_workspace(
        self, env: dict[str, Any]
    ) -> None:
        await _seed_call(env, provider="twilio", call_id="CA5")
        params = {"CallSid": "CA5", "CallStatus": "completed", "CallDuration": "7"}

        wrong = await self._post("/webhooks/twilio/status", params, TOKENS["w1"])
        assert wrong.status_code == 403
        assert await _status_of(env, "CA5") == CallStatus.INITIATED.value
        right = await self._post("/webhooks/twilio/status", params, TOKENS["w2"])
        assert right.status_code == 200, right.text

    @pytest.mark.asyncio
    async def test_a_status_callback_ignores_another_carriers_record(
        self, env: dict[str, Any]
    ) -> None:
        await _seed_call(env, provider="telnyx", call_id="CA6")
        params = {"CallSid": "CA6", "CallStatus": "completed", "CallDuration": "7"}

        with structlog.testing.capture_logs() as logs:
            response = await self._post("/webhooks/twilio/status", params, TOKENS["w2"])

        assert response.status_code == 503
        assert any(entry["event"] == "status_callback_no_record" for entry in logs)
        assert await _status_of(env, "CA6") == CallStatus.INITIATED.value

    @pytest.mark.asyncio
    async def test_retries_for_an_erased_call_warn_once_then_log_at_info(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Still 503 each time (the carrier's bounded retry); the log is not noisy."""
        levels: list[str] = []

        class _Recorder:
            def warning(self, event: str, **_: Any) -> None:
                levels.append(f"warning:{event}")

            def info(self, event: str, **_: Any) -> None:
                levels.append(f"info:{event}")

        monkeypatch.setattr(telephony, "logger", _Recorder())
        telephony._NO_RECORD_SEEN.clear()
        params = {"CallSid": "CAerased", "CallStatus": "completed"}
        query = f"agent_id={env['agent'].id}&record_id={uuid.uuid4()}"

        responses = [
            await self._post("/webhooks/twilio/status", params, TOKENS["w2"], query)
            for _ in range(3)
        ]

        assert [r.status_code for r in responses] == [503, 503, 503]
        seen = [lv for lv in levels if lv.endswith("status_callback_no_record")]
        assert seen == [
            "warning:status_callback_no_record",
            "info:status_callback_no_record",
            "info:status_callback_no_record",
        ]

    @pytest.mark.asyncio
    async def test_an_early_status_callback_finds_its_record_by_record_id(
        self, env: dict[str, Any]
    ) -> None:
        """The callback beat the write of the carrier's id: the record id still finds it."""
        record = await _seed_call(env, provider="twilio", call_id=f"pending:{uuid.uuid4()}")
        params = {"CallSid": "CA7", "CallStatus": "ringing"}
        query = f"agent_id={env['agent'].id}&record_id={record.id}"

        wrong = await self._post("/webhooks/twilio/status", params, TOKENS["w1"], query)
        right = await self._post("/webhooks/twilio/status", params, TOKENS["w2"], query)

        assert wrong.status_code == 403
        assert right.status_code == 200, right.text
        async with env["sessions"]() as session:
            status = (
                await session.execute(select(CallRecord.status).where(CallRecord.id == record.id))
            ).scalar_one()
        assert status == CallStatus.RINGING.value

    @pytest.mark.asyncio
    async def test_the_twilio_status_url_asks_for_a_retry_on_5xx(self) -> None:
        """Twilio retries only a failed connection by default (its connection-overrides page)."""
        calls: list[dict[str, Any]] = []

        class _Calls:
            def create(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                return type("C", (), {"sid": "CAx", "status": "queued"})()

        service = TwilioService("ACx", "tok")
        service.client = type("Cl", (), {"calls": _Calls()})()  # type: ignore[assignment]
        await service.initiate_call(
            to_number=CALLER,
            from_number=TWILIO_NUMBER,
            webhook_url=f"{PUBLIC}/webhooks/twilio/answer?agent_id=a&record_id=r",
        )

        assert calls[0]["status_callback"].startswith(f"{PUBLIC}/webhooks/twilio/status?")
        assert "record_id=r" in calls[0]["status_callback"]
        assert "rp=5xx" in calls[0]["status_callback"].split("#", 1)[1]


class TestOutboundCallsProveTheirWorkspace:
    """`/calls` takes the workspace from the query string, so it is checked first."""

    @pytest.fixture(autouse=True)
    def _carrier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake(*_: Any, **__: Any) -> _Carrier:
            return _Carrier()

        async def none(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(telephony, "get_telnyx_service", fake)
        monkeypatch.setattr(telephony, "get_twilio_service", none)

    async def _call(
        self, env: dict[str, Any], *, workspace: uuid.UUID, from_number: str = TELNYX_NUMBER
    ) -> httpx.Response:
        async with _client() as client:
            return await client.post(
                f"/api/v1/telephony/calls?workspace_id={workspace}",
                headers={"Authorization": f"Bearer {create_access_token(env['user'].id)}"},
                json={
                    "to_number": CALLER,
                    "from_number": from_number,
                    "agent_id": str(env["agent"].id),
                },
            )

    @pytest.mark.asyncio
    async def test_the_number_registered_workspace_places_the_call(
        self, env: dict[str, Any]
    ) -> None:
        response = await self._call(env, workspace=env["w2"].id)

        assert response.status_code == 200, response.text
        assert len(_Carrier.placed) == 1
        assert "record_id=" in _Carrier.placed[0]["webhook_url"]
        async with env["sessions"]() as session:
            record = (await session.execute(select(CallRecord))).scalar_one()
        assert str(record.id) in _Carrier.placed[0]["webhook_url"]
        assert record.provider_call_id == "carrier-call-id"

    @pytest.mark.asyncio
    async def test_a_workspace_the_agent_is_not_in_places_nothing(
        self, env: dict[str, Any]
    ) -> None:
        async with env["sessions"]() as session:
            stranger = Workspace(user_id=env["user"].id, name="Stranger")
            session.add(stranger)
            await session.commit()
            await session.refresh(stranger)

        response = await self._call(env, workspace=stranger.id)

        assert response.status_code == 403
        assert _Carrier.placed == []

    @pytest.mark.asyncio
    async def test_a_number_registered_in_another_workspace_places_nothing(
        self, env: dict[str, Any]
    ) -> None:
        """The agent is in W1 too, but the number is W2's: W1 cannot dial from it."""
        response = await self._call(env, workspace=env["w1"].id)

        assert response.status_code == 422
        assert _Carrier.placed == []

    @pytest.mark.asyncio
    async def test_an_unregistered_number_places_nothing(self, env: dict[str, Any]) -> None:
        response = await self._call(env, workspace=env["w2"].id, from_number="+15550100777")

        assert response.status_code == 422
        assert _Carrier.placed == []


class _FakeTelnyx(TelnyxService):
    def __init__(self, fail: bool = False) -> None:
        super().__init__("key")
        self.fail = fail
        self.placed: list[dict[str, str]] = []

    async def initiate_call(  # type: ignore[override]
        self, to_number: str, from_number: str, webhook_url: str, agent_id: str | None = None
    ) -> CallInfo:
        self.placed.append({"to": to_number, "from": from_number, "url": webhook_url})
        if self.fail:
            raise RuntimeError("carrier refused")
        return CallInfo(
            call_id="carrier-sid-1",
            call_control_id="carrier-cc-1",
            from_number=from_number,
            to_number=to_number,
            direction=CarrierDirection.OUTBOUND,
            status=CarrierStatus.INITIATED,
        )


def _campaign_objects(env: dict[str, Any], *, workspace: str, from_number: str) -> Any:
    from app.models.campaign import Campaign, CampaignContact
    from app.models.contact import Contact

    campaign = Campaign(
        id=uuid.uuid4(),
        user_id=user_id_to_uuid(env["user"].id),
        workspace_id=env[workspace].id,
        agent_id=env["agent"].id,
        name="Outreach",
        from_phone_number=from_number,
    )
    campaign.contacts_called = 0
    campaign.contacts_failed = 0
    contact = Contact(id=7, user_id=env["user"].id, first_name="Test", phone_number=CALLER)
    link = CampaignContact(id=uuid.uuid4(), campaign_id=campaign.id, contact_id=7)
    link.attempts = 0
    return campaign, contact, link


class TestCampaignCallsGoThroughTheSameGates:
    """The campaign worker dials through the shared helper, like `/calls`."""

    @pytest.mark.asyncio
    async def test_a_campaign_call_is_recorded_answered_and_admitted_in_its_workspace(
        self, env: dict[str, Any]
    ) -> None:
        from app.api.telephony_ws import call_workspace_id
        from app.services.campaign_worker import CampaignWorker

        campaign, contact, link = _campaign_objects(env, workspace="w2", from_number=TELNYX_NUMBER)
        service = _FakeTelnyx()
        worker = CampaignWorker(base_url=PUBLIC)
        async with env["sessions"]() as db:
            await worker._initiate_call(campaign, link, contact, service, db)

        # The record exists, in the campaign's workspace, with the carrier's id.
        async with env["sessions"]() as db:
            record = (
                await db.execute(
                    select(CallRecord).where(CallRecord.provider_call_id == "carrier-sid-1")
                )
            ).scalar_one()
        assert record.workspace_id == env["w2"].id
        assert record.status == CallStatus.INITIATED.value

        # The carrier fetches the answer URL it was given; signed with W2's key.
        url = service.placed[0]["url"]
        assert f"record_id={record.id}" in url
        body = urlencode({"CallSid": "carrier-sid-1", "To": CALLER, "From": TELNYX_NUMBER}).encode()
        async with _client() as client:
            answer = await client.post(
                url.removeprefix(PUBLIC),
                content=body,
                headers=_telnyx_headers(env["keys"]["w2"], body),
            )
        assert answer.status_code == 200, answer.text
        stream = ET.fromstring(answer.text).find("./Connect/Stream")  # noqa: S314
        assert stream is not None
        assert dict(parse_qsl(urlsplit(stream.attrib["url"]).query))["call_id"] == "carrier-sid-1"

        # And the stream is admitted with that workspace.
        async with env["sessions"]() as db:
            admitted = await call_workspace_id(
                db,
                provider="telnyx",
                provider_call_id="carrier-sid-1",
                agent_id=env["agent"].id,
                log=structlog.get_logger(),
            )
        assert admitted == env["w2"].id
        assert link.status == "calling"

    @pytest.mark.asyncio
    async def test_a_campaign_with_an_unregistered_from_number_is_refused_before_the_dial(
        self, env: dict[str, Any]
    ) -> None:
        from app.services.campaign_worker import CampaignWorker

        campaign, contact, link = _campaign_objects(env, workspace="w2", from_number="+15550100777")
        service = _FakeTelnyx()
        async with env["sessions"]() as db:
            with pytest.raises(OutboundRefusedError) as refused:
                await CampaignWorker(base_url=PUBLIC)._initiate_call(
                    campaign, link, contact, service, db
                )

        assert refused.value.status_code == 422
        assert service.placed == []
        async with env["sessions"]() as db:
            assert (await db.execute(select(CallRecord))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_a_campaign_cannot_dial_from_a_number_registered_in_another_workspace(
        self, env: dict[str, Any]
    ) -> None:
        from app.services.campaign_worker import CampaignWorker

        campaign, contact, link = _campaign_objects(env, workspace="w1", from_number=TELNYX_NUMBER)
        service = _FakeTelnyx()
        async with env["sessions"]() as db:
            with pytest.raises(OutboundRefusedError):
                await CampaignWorker(base_url=PUBLIC)._initiate_call(
                    campaign, link, contact, service, db
                )
        assert service.placed == []

    @pytest.mark.asyncio
    async def test_a_refused_contact_fails_without_a_retry(self, env: dict[str, Any]) -> None:
        from app.services.campaign_worker import CampaignWorker

        campaign, _contact, link = _campaign_objects(
            env, workspace="w2", from_number="+15550100777"
        )
        campaign.contacts_failed = 0

        CampaignWorker(base_url=PUBLIC)._fail_refused_contact(
            campaign, link, OutboundRefusedError(422, "nope")
        )

        assert link.status == "failed"
        assert link.attempts == 1
        assert campaign.contacts_failed == 1

    @pytest.mark.asyncio
    async def test_a_failed_dial_never_leaves_a_live_looking_record(
        self, env: dict[str, Any]
    ) -> None:
        from app.services.campaign_worker import CampaignWorker

        campaign, contact, link = _campaign_objects(env, workspace="w2", from_number=TELNYX_NUMBER)
        service = _FakeTelnyx(fail=True)
        async with env["sessions"]() as db:
            with pytest.raises(RuntimeError):
                await CampaignWorker(base_url=PUBLIC)._initiate_call(
                    campaign, link, contact, service, db
                )

        async with env["sessions"]() as db:
            record = (await db.execute(select(CallRecord))).scalar_one()
        assert record.status == CallStatus.FAILED.value
        assert record.ended_at is not None
        assert record.provider_call_id.startswith("pending:")

    @pytest.mark.asyncio
    async def test_the_post_dial_write_does_not_replace_an_id_an_early_answer_stored(
        self, env: dict[str, Any]
    ) -> None:
        from app.services.campaign_worker import CampaignWorker

        class _AnswerFirst(_FakeTelnyx):
            async def initiate_call(  # type: ignore[override]
                self,
                to_number: str,
                from_number: str,
                webhook_url: str,
                agent_id: str | None = None,
            ) -> CallInfo:
                info = await super().initiate_call(to_number, from_number, webhook_url, agent_id)
                # The answer webhook lands before the dial returns to us.
                async with env["sessions"]() as other:
                    from app.services.telephony.outbound import adopt_provider_call_id

                    row = (await other.execute(select(CallRecord))).scalar_one()
                    assert await adopt_provider_call_id(other, row, "answer-first-sid")
                return info

        campaign, contact, link = _campaign_objects(env, workspace="w2", from_number=TELNYX_NUMBER)
        async with env["sessions"]() as db:
            await CampaignWorker(base_url=PUBLIC)._initiate_call(
                campaign, link, contact, _AnswerFirst(), db
            )

        async with env["sessions"]() as db:
            record = (await db.execute(select(CallRecord))).scalar_one()
        assert record.provider_call_id == "answer-first-sid"


class TestOutboundAnswerClaimMatchesTheRecord:
    """#71's one-shot claim must find a row stored as `pending:<id>` by its record id."""

    @pytest.mark.asyncio
    async def test_a_pending_row_is_claimed_by_record_id_exactly_once(
        self, env: dict[str, Any]
    ) -> None:
        record = await _seed_call(env, provider="telnyx", call_id=f"pending:{uuid.uuid4()}")
        scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}
        from starlette.requests import Request

        async with env["sessions"]() as session:
            first = await telephony._claim_outbound_answer(
                Request(scope), "telnyx", "carrier-id-1", session, record_id=record.id
            )
        async with env["sessions"]() as session:
            second = await telephony._claim_outbound_answer(
                Request(scope), "telnyx", "carrier-id-1", session, record_id=record.id
            )

        assert first == (True, None)
        assert second[0] is False
        assert second[1] is not None

    @pytest.mark.asyncio
    async def test_the_lost_answer_age_finds_a_pending_row_by_record_id(
        self, env: dict[str, Any]
    ) -> None:
        record = await _seed_call(env, provider="twilio", call_id=f"pending:{uuid.uuid4()}")
        async with env["sessions"]() as session:
            by_id = await telephony._lost_answer_age("twilio", "CAx", session, record.id)
            without = await telephony._lost_answer_age("twilio", "CAx", session)

        assert by_id is not None
        assert by_id < 60
        assert without == float("inf")
