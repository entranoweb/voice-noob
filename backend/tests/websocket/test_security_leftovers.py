"""Three things the browser-connection work left open, each against the real app.

1. ``/token`` handed the agent's instructions to whoever asked for a widget
   token. The owner's system prompt is theirs; the page never needed it.
2. Nothing told the browser which sites may frame the widget page, so the
   allow-list was only as good as the framing page's honesty.
3. A Telnyx webhook was verified against one global key, so in a deployment
   where workspaces bring their own Telnyx accounts either nobody's webhooks
   verified or everybody's did.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any, ClassVar, Self
from urllib.parse import urlencode

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.workspace import AgentWorkspace, Workspace

FRONTEND = "https://app.synthiq.example"
CUSTOMER = "https://shop.example.com"
SYSTEM_PROMPT = "You are the receptionist. SECRET-HOUSE-RULE: never quote prices below list."
OWNER_NUMBER = "+15559990001"
OTHER_NUMBER = "+15559990002"
OPENAI_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
    ).decode()
    return private, public


def _signed(private: Ed25519PrivateKey, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = private.sign(f"{timestamp}|".encode() + body)
    return {
        "telnyx-signature-ed25519": base64.b64encode(signature).decode(),
        "telnyx-timestamp": timestamp,
        "content-type": "application/x-www-form-urlencoded",
    }


class _FakeOpenAI:
    """Stands in for the model provider's session endpoint and keeps what it was sent."""

    requests: ClassVar[list[dict[str, Any]]] = []
    refuse: ClassVar[bool] = False

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], **_: Any) -> httpx.Response:
        type(self).requests.append({"url": url, "json": json})
        if type(self).refuse:
            # A provider error that quotes the request back, as validation errors do.
            return httpx.Response(
                400, json={"error": {"message": str(json["session"]["instructions"])}}
            )
        return httpx.Response(200, json={"value": "ek_test", "expires_at": 0})


async def _tenant(
    session: AsyncSession, *, number: str, telnyx_public_key: str | None, domains: list[str]
) -> dict[str, Any]:
    """One customer: a user, a workspace with its own keys, and an agent on a number."""
    user = User(
        email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",  # noqa: S106
        full_name="Owner",
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    workspace = Workspace(id=uuid.uuid4(), user_id=user.id, name="Main", settings={})
    agent = Agent(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Front Desk",
        system_prompt=SYSTEM_PROMPT,
        is_active=True,
        voice="shimmer",
        language="en",
        enabled_tools=[],
        pricing_tier="premium",
        public_id=f"ag_{uuid.uuid4().hex[:10]}",
        embed_enabled=True,
        allowed_domains=domains,
        embed_settings={},
        phone_number_id=number,
    )
    session.add_all([workspace, agent])
    await session.commit()
    session.add(AgentWorkspace(id=uuid.uuid4(), agent_id=agent.id, workspace_id=workspace.id))
    session.add(
        UserSettings(
            user_id=user_id_to_uuid(user.id),
            workspace_id=workspace.id,
            openai_api_key="sk-workspace-key",
            telnyx_public_key=telnyx_public_key,
        )
    )
    await session.commit()
    await session.refresh(agent)
    return {"user": user, "workspace": workspace, "agent": agent, "number": number}


@pytest_asyncio.fixture
async def env(test_engine: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    owner_key, owner_public = _keypair()
    other_key, other_public = _keypair()

    async with session_maker() as session:
        owner = await _tenant(
            session,
            number=OWNER_NUMBER,
            telnyx_public_key=owner_public,
            domains=["shop.example.com", "*.partner.example"],
        )
        other = await _tenant(
            session, number=OTHER_NUMBER, telnyx_public_key=other_public, domains=[]
        )

    monkeypatch.setattr(settings, "FRONTEND_URL", FRONTEND)
    monkeypatch.setattr(settings, "CORS_ORIGINS", [FRONTEND])
    monkeypatch.setattr(settings, "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET", False)
    monkeypatch.setattr(settings, "EMBED_TOOLS_DEFAULT_ENABLED", False)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)
    monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", None)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeOpenAI)
    _FakeOpenAI.requests = []
    _FakeOpenAI.refuse = False

    async def override_get_db() -> Any:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {
            "owner": owner,
            "other": other,
            "owner_key": owner_key,
            "other_key": other_key,
            "sessions": session_maker,
        }
    finally:
        app.dependency_overrides.clear()


def _client() -> AsyncClient:
    # Built from the real class by name: the fixture replaces httpx.AsyncClient
    # (the endpoint's outbound client), and this import was bound before that.
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://internal:8000")


class TestTheTokenEndpointKeepsTheInstructions:
    async def _token(self, env: dict[str, Any]) -> httpx.Response:
        public_id = env["owner"]["agent"].public_id
        async with _client() as client:
            return await client.post(
                f"/api/public/embed/{public_id}/token", headers={"origin": CUSTOMER}
            )

    @pytest.mark.asyncio
    async def test_the_response_carries_no_instructions(self, env: dict[str, Any]) -> None:
        response = await self._token(env)

        assert response.status_code == 200, response.text
        body = response.json()
        assert "instructions" not in body["agent"]
        # Not under another name either: the owner's words appear nowhere.
        assert "SECRET-HOUSE-RULE" not in response.text
        assert "receptionist" not in response.text

    @pytest.mark.asyncio
    async def test_the_model_provider_is_given_them_by_the_server(
        self, env: dict[str, Any]
    ) -> None:
        """Removing them from the response must not leave the agent without them."""
        await self._token(env)

        assert len(_FakeOpenAI.requests) == 1
        sent = _FakeOpenAI.requests[0]
        assert sent["url"] == OPENAI_SECRETS_URL
        session = sent["json"]["session"]
        assert session["type"] == "realtime"
        assert "SECRET-HOUSE-RULE" in session["instructions"]
        assert session["audio"]["output"]["voice"] == "shimmer"
        assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"

    @pytest.mark.asyncio
    async def test_a_provider_error_does_not_carry_them_out_either(
        self, env: dict[str, Any]
    ) -> None:
        _FakeOpenAI.refuse = True

        response = await self._token(env)

        assert response.status_code == 502
        assert "SECRET-HOUSE-RULE" not in response.text

    @pytest.mark.asyncio
    async def test_the_page_still_gets_what_it_shows_and_needs(self, env: dict[str, Any]) -> None:
        body = (await self._token(env)).json()

        assert body["value"] == "ek_test"
        assert body["agent"]["name"] == "Front Desk"
        assert body["agent"]["voice"] == "shimmer"
        assert "initial_greeting" in body["agent"]
        assert body["session_token"]
        assert body["tools"] == []


class TestWhoMayFrameTheWidgetPage:
    async def _policy(self, public_id: str) -> str:
        async with _client() as client:
            response = await client.get(f"/api/public/embed/{public_id}/frame-policy")
        assert response.status_code == 200, response.text
        return str(response.json()["frame_ancestors"])

    @pytest.mark.asyncio
    async def test_the_agents_allowed_sites_and_this_deployment(self, env: dict[str, Any]) -> None:
        policy = await self._policy(env["owner"]["agent"].public_id)

        assert policy == "'self' https://shop.example.com https://*.partner.example"

    @pytest.mark.asyncio
    async def test_an_agent_with_no_allowed_sites_is_framed_by_nobody_else(
        self, env: dict[str, Any]
    ) -> None:
        assert await self._policy(env["other"]["agent"].public_id) == "'self'"

    @pytest.mark.asyncio
    async def test_the_override_widens_an_empty_list_only(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET", True)

        assert await self._policy(env["other"]["agent"].public_id) == "*"
        assert "*" not in (await self._policy(env["owner"]["agent"].public_id)).split()

    @pytest.mark.asyncio
    async def test_an_unknown_or_switched_off_agent_is_framed_by_nobody_else(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even with the override on: it speaks for agents with an empty list,
        # not for ones that are not embeddable at all.
        monkeypatch.setattr(settings, "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET", True)
        async with env["sessions"]() as session:
            agent = await session.get(Agent, env["owner"]["agent"].id)
            agent.embed_enabled = False
            await session.commit()

        assert await self._policy("ag_does_not_exist") == "'self'"
        assert await self._policy(env["owner"]["agent"].public_id) == "'self'"


def _inbound(to_number: str, call_id: str) -> bytes:
    return urlencode(
        {"CallSid": call_id, "From": "+15551230000", "To": to_number, "CallStatus": "ringing"}
    ).encode()


class TestTelnyxIsVerifiedPerWorkspace:
    async def _post(self, path: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        async with _client() as client:
            return await client.post(path, content=body, headers=headers)

    async def _calls(self, env: dict[str, Any]) -> list[CallRecord]:
        async with env["sessions"]() as session:
            return list((await session.execute(select(CallRecord))).scalars().all())

    @pytest.mark.asyncio
    async def test_a_call_signed_by_the_numbers_own_account_is_answered(
        self, env: dict[str, Any]
    ) -> None:
        """No global key is configured: the workspace's own key is what verifies it."""
        body = _inbound(OWNER_NUMBER, "v3:owner-call")

        response = await self._post("/webhooks/telnyx/voice", body, _signed(env["owner_key"], body))

        assert response.status_code == 200, response.text
        assert "<Stream" in response.text
        assert [c.provider_call_id for c in await self._calls(env)] == ["v3:owner-call"]

    @pytest.mark.asyncio
    async def test_another_tenants_signature_is_refused_for_this_number(
        self, env: dict[str, Any]
    ) -> None:
        """Genuinely signed by Telnyx, but for someone else's account."""
        body = _inbound(OWNER_NUMBER, "v3:cross-tenant")

        response = await self._post("/webhooks/telnyx/voice", body, _signed(env["other_key"], body))

        assert response.status_code == 403
        assert await self._calls(env) == []

    @pytest.mark.asyncio
    async def test_a_number_nobody_owns_is_refused_not_answered(self, env: dict[str, Any]) -> None:
        body = _inbound("+15550000000", "v3:nobody")

        response = await self._post("/webhooks/telnyx/voice", body, _signed(env["owner_key"], body))

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_the_global_key_still_serves_a_single_account_deployment(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        platform_key, platform_public = _keypair()
        monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", platform_public)
        body = _inbound(OWNER_NUMBER, "v3:platform")

        response = await self._post("/webhooks/telnyx/voice", body, _signed(platform_key, body))

        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_the_outbound_answer_is_checked_against_the_named_agents_workspace(
        self, env: dict[str, Any]
    ) -> None:
        agent_id = env["owner"]["agent"].id
        body = _inbound(OWNER_NUMBER, "v3:outbound")
        path = f"/webhooks/telnyx/answer?agent_id={agent_id}"

        theirs = await self._post(path, body, _signed(env["other_key"], body))
        ours = await self._post(path, body, _signed(env["owner_key"], body))

        assert theirs.status_code == 403
        assert ours.status_code == 200, ours.text

    @pytest.mark.asyncio
    async def test_a_status_update_cannot_be_signed_by_another_tenant(
        self, env: dict[str, Any]
    ) -> None:
        owner = env["owner"]
        async with env["sessions"]() as session:
            session.add(
                CallRecord(
                    user_id=user_id_to_uuid(owner["user"].id),
                    workspace_id=owner["workspace"].id,
                    provider="telnyx",
                    provider_call_id="v3:live-call",
                    agent_id=owner["agent"].id,
                    direction=CallDirection.INBOUND.value,
                    status=CallStatus.IN_PROGRESS.value,
                    from_number="+15551230000",
                    to_number=OWNER_NUMBER,
                )
            )
            await session.commit()
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.hangup",
                    "payload": {"call_control_id": "v3:live-call", "duration_seconds": 5},
                }
            }
        ).encode()

        theirs = await self._post("/webhooks/telnyx/status", body, _signed(env["other_key"], body))
        after_theirs = [c.status for c in await self._calls(env)]
        ours = await self._post("/webhooks/telnyx/status", body, _signed(env["owner_key"], body))

        assert theirs.status_code == 403
        # The other tenant's webhook ended nothing.
        assert after_theirs == [CallStatus.IN_PROGRESS.value]
        assert ours.status_code == 200, ours.text
        assert [c.status for c in await self._calls(env)] != [CallStatus.IN_PROGRESS.value]


class TestTheWidgetKeyIsShortLivedAndCapped:
    """A public page mints a key billed to the owner: short expiry, one per session, hourly cap."""

    async def _token(
        self, agent: Agent, *, headers: dict[str, str] | None = None, origin: str | None = CUSTOMER
    ) -> httpx.Response:
        sent = dict(headers or {})
        if origin:
            sent["origin"] = origin
        async with _client() as client:
            return await client.post(f"/api/public/embed/{agent.public_id}/token", headers=sent)

    @pytest.mark.asyncio
    async def test_the_request_asks_for_a_one_minute_key(self, env: dict[str, Any]) -> None:
        response = await self._token(env["owner"]["agent"])

        assert response.status_code == 200, response.text
        sent = _FakeOpenAI.requests[0]["json"]
        assert sent["expires_after"] == {"anchor": "created_at", "seconds": 60}

    @pytest.mark.asyncio
    async def test_a_session_that_holds_a_key_cannot_mint_another(
        self, env: dict[str, Any]
    ) -> None:
        agent = env["owner"]["agent"]
        first = await self._token(agent)
        assert first.status_code == 200, first.text

        token = first.json()["session_token"]
        second = await self._token(agent, headers={"x-embed-session": token})

        assert second.status_code == 409
        assert len(_FakeOpenAI.requests) == 1

    @pytest.mark.asyncio
    async def test_the_hourly_limit_refuses_with_retry_after(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "EMBED_KEYS_PER_AGENT_PER_HOUR", 3)
        agent = env["owner"]["agent"]
        for _ in range(3):
            assert (await self._token(agent)).status_code == 200

        refused = await self._token(agent)

        assert refused.status_code == 429
        assert "3" in refused.json()["detail"]
        assert "hour" in refused.json()["detail"]
        assert 0 < int(refused.headers["retry-after"]) <= 3601
        assert len(_FakeOpenAI.requests) == 3

    @pytest.mark.asyncio
    async def test_agents_count_separately(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "EMBED_KEYS_PER_AGENT_PER_HOUR", 2)
        owner, other = env["owner"]["agent"], env["other"]["agent"]
        for _ in range(2):
            assert (await self._token(owner)).status_code == 200
        assert (await self._token(owner)).status_code == 429

        # The other agent is embedded nowhere, so it is reached as the widget page itself.
        assert (await self._token(other, origin=None)).status_code == 200

    @pytest.mark.asyncio
    async def test_a_provider_refusal_does_not_use_up_the_count(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "EMBED_KEYS_PER_AGENT_PER_HOUR", 1)
        agent = env["owner"]["agent"]
        _FakeOpenAI.refuse = True
        assert (await self._token(agent)).status_code == 502
        _FakeOpenAI.refuse = False

        assert (await self._token(agent)).status_code == 200

    @pytest.mark.asyncio
    async def test_without_redis_no_key_is_minted(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def down() -> Any:
            raise ConnectionError("redis is down")

        monkeypatch.setattr("app.core.embed_key_limit._store", down)

        response = await self._token(env["owner"]["agent"])

        assert response.status_code == 503
        assert int(response.headers["retry-after"]) > 0
        assert _FakeOpenAI.requests == []

    @pytest.mark.asyncio
    async def test_one_address_cannot_use_up_the_agents_hour(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "EMBED_KEYS_PER_AGENT_PER_HOUR", 10)
        monkeypatch.setattr(settings, "EMBED_KEYS_PER_IP_PER_AGENT_PER_HOUR", 2)
        agent = env["owner"]["agent"]
        for _ in range(2):
            assert (await self._token(agent)).status_code == 200

        refused = await self._token(agent)

        assert refused.status_code == 429
        assert "2" in refused.json()["detail"]
        assert "your address" in refused.json()["detail"]
        assert int(refused.headers["retry-after"]) > 0
        assert len(_FakeOpenAI.requests) == 2


class TestEmbedKeyCounters:
    """The counters themselves, against a store, at the edges."""

    @pytest.mark.asyncio
    async def test_every_counter_has_a_ttl_the_moment_it_exists(
        self, embed_key_limit_store: Any
    ) -> None:
        from app.core.embed_key_limit import reserve_embed_key

        decision = await reserve_embed_key("agent-1", "203.0.113.7")

        assert decision.allowed
        assert len(decision.keys) == 2
        for key in decision.keys:
            assert await embed_key_limit_store.ttl(key) > 0

    @pytest.mark.asyncio
    async def test_a_refund_after_the_hour_turns_hits_the_counter_it_took(
        self, embed_key_limit_store: Any
    ) -> None:
        from app.core.embed_key_limit import release_embed_key, reserve_embed_key

        before_boundary = 3600 * 100 - 1.0
        decision = await reserve_embed_key("agent-1", "203.0.113.7", now=before_boundary)
        # The next hour's counter already exists (another visitor took one).
        next_hour = await reserve_embed_key("agent-1", "198.51.100.9", now=before_boundary + 2)

        await release_embed_key(decision)

        for key in decision.keys:
            assert await embed_key_limit_store.get(key) == "0"
        # Untouched: not decremented below what its own mint made it.
        agent_next = next_hour.keys[1]
        assert await embed_key_limit_store.get(agent_next) == "1"
