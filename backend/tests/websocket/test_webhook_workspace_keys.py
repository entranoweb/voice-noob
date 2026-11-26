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
        record = await _seed_call(env, provider="telnyx", call_id="stored-id")
        body = self._voice_body("different-carrier-sid")
        other_agent = uuid.uuid4()
        path = f"/webhooks/telnyx/answer?agent_id={other_agent}&record_id={record.id}"

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
        """Same id, other carrier: that row is not this call's, so nothing verifies."""
        await _seed_call(env, provider="twilio", call_id="shared-id")
        body = json.dumps(
            {"data": {"event_type": "call.hangup", "payload": {"call_control_id": "shared-id"}}}
        ).encode()

        response = await self._post("/webhooks/telnyx/status", body, env["keys"]["w2"])

        assert response.status_code == 403
        assert await _status_of(env, "shared-id") == CallStatus.INITIATED.value


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

        response = await self._post("/webhooks/twilio/status", params, TOKENS["w2"])

        assert response.status_code == 403
        assert await _status_of(env, "CA6") == CallStatus.INITIATED.value


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
