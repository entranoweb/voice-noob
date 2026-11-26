"""The browser-facing sockets and the embed endpoints admit only who they should.

Two surfaces, two kinds of caller.

The dashboard socket is for a signed-in user talking to their own agent. It used
to accept anyone who knew an agent id and a workspace id, and open a model
session on the owner's key with the agent's tools.

The embed surface is public by design, so it cannot ask who you are. It asks
instead for a session that an origin-checked endpoint minted, and it bounds what
a session can do. Its tool endpoint used to run any enabled tool for anyone who
sent the agent's public id.

Every refusal here is asserted together with "no model session was opened",
because that is the cost an attacker was able to impose.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Self

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import embed, realtime
from app.core import stream_token_use
from app.core.config import settings
from app.core.embed_session import (
    DIRECT,
    EMBED_SESSION_REJECTED,
    mint_embed_session,
)
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.call_record import CallRecord
from app.models.user import User
from app.models.workspace import AgentWorkspace, Workspace
from tests.websocket.asgi_ws import ASGIWebSocketClient

FRONTEND = "https://app.synthiq.example"
CUSTOMER = "https://shop.example.com"
STRANGER = "https://evil.example"


class _Event:
    type = "session.updated"


class _Connection:
    def __init__(self) -> None:
        self._events = [_Event()]

    def __aiter__(self) -> _Connection:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeRealtimeSession:
    """Stands in for the model session and records what it was opened with."""

    opened: ClassVar[int] = 0
    configs: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).opened += 1
        type(self).configs.append(kwargs.get("agent_config", {}))
        self.connection = _Connection()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_audio(self, _: bytes) -> None:
        return None

    async def handle_function_call_event(self, _: Any) -> None:
        return None


def _access_token(user_id: int, *, expires_in: int = 600) -> str:
    return jwt.encode(
        {"sub": str(user_id), "exp": datetime.now(UTC) + timedelta(seconds=expires_in)},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


@pytest_asyncio.fixture
async def env(test_engine: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        owner = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",  # noqa: S106
            full_name="Owner",
            is_active=True,
        )
        other = User(
            email=f"other-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",  # noqa: S106
            full_name="Someone Else",
            is_active=True,
        )
        session.add_all([owner, other])
        await session.commit()
        await session.refresh(owner)
        await session.refresh(other)

        workspace = Workspace(id=uuid.uuid4(), user_id=owner.id, name="Main")
        agent = Agent(
            id=uuid.uuid4(),
            user_id=owner.id,
            name="Front Desk",
            system_prompt="You are a receptionist.",
            is_active=True,
            voice="shimmer",
            language="en",
            enabled_tools=["crm"],
            enable_transcript=True,
            pricing_tier="premium",
            public_id=f"ag_{uuid.uuid4().hex[:10]}",
            embed_enabled=True,
            allowed_domains=["shop.example.com"],
            embed_settings={},
        )
        session.add_all([workspace, agent])
        await session.commit()
        session.add(AgentWorkspace(id=uuid.uuid4(), agent_id=agent.id, workspace_id=workspace.id))
        await session.commit()
        await session.refresh(agent)

    monkeypatch.setattr(settings, "FRONTEND_URL", FRONTEND)
    monkeypatch.setattr(settings, "CORS_ORIGINS", [FRONTEND])
    monkeypatch.setattr(settings, "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET", False)
    monkeypatch.setattr(settings, "EMBED_TOOLS_DEFAULT_ENABLED", False)
    monkeypatch.setattr(settings, "EMBED_SESSION_MAX_SECONDS", 900)
    monkeypatch.setattr(realtime, "GPTRealtimeSession", _FakeRealtimeSession)
    monkeypatch.setattr(embed, "GPTRealtimeSession", _FakeRealtimeSession)
    _FakeRealtimeSession.opened = 0
    _FakeRealtimeSession.configs = []

    async def override_get_db() -> Any:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {
            "owner": owner,
            "other": other,
            "agent": agent,
            "workspace": workspace,
            "sessions": session_maker,
        }
    finally:
        app.dependency_overrides.clear()


def _dashboard_path(env: dict[str, Any]) -> str:
    return f"/ws/realtime/{env['agent'].id}"


def _dashboard_query(env: dict[str, Any], token: str | None) -> str:
    query = f"workspace_id={env['workspace'].id}"
    return f"{query}&token={token}" if token else query


class TestDashboardSocket:
    async def _refused(self, env: dict[str, Any], token: str | None, code: int) -> None:
        async with ASGIWebSocketClient(
            app, _dashboard_path(env), query_string=_dashboard_query(env, token)
        ) as ws:
            assert not ws.accepted, "an unauthorised socket was accepted"
            assert ws.close_code == code
        assert _FakeRealtimeSession.opened == 0

    @pytest.mark.asyncio
    async def test_knowing_the_agent_and_workspace_ids_is_not_enough(
        self, env: dict[str, Any]
    ) -> None:
        """The hole as it stood: no credential at all."""
        await self._refused(env, None, realtime.REALTIME_SOCKET_UNAUTHENTICATED)

    @pytest.mark.asyncio
    async def test_an_expired_login_is_refused(self, env: dict[str, Any]) -> None:
        stale = _access_token(env["owner"].id, expires_in=-60)
        await self._refused(env, stale, realtime.REALTIME_SOCKET_UNAUTHENTICATED)

    @pytest.mark.asyncio
    async def test_a_token_signed_with_another_key_is_refused(self, env: dict[str, Any]) -> None:
        forged = jwt.encode(
            {"sub": str(env["owner"].id), "exp": datetime.now(UTC) + timedelta(minutes=5)},
            "not-this-deployments-key",
            algorithm=settings.ALGORITHM,
        )
        await self._refused(env, forged, realtime.REALTIME_SOCKET_UNAUTHENTICATED)

    @pytest.mark.asyncio
    async def test_another_signed_in_user_cannot_reach_this_agent(
        self, env: dict[str, Any]
    ) -> None:
        await self._refused(env, _access_token(env["other"].id), realtime.REALTIME_SOCKET_FORBIDDEN)

    @pytest.mark.asyncio
    async def test_the_owner_is_admitted(self, env: dict[str, Any]) -> None:
        """The control: the same request with the owner's login opens a session."""
        async with ASGIWebSocketClient(
            app,
            _dashboard_path(env),
            query_string=_dashboard_query(env, _access_token(env["owner"].id)),
        ) as ws:
            assert ws.accepted
            ready = await ws.receive_json()
            assert ready is not None
            assert ready["type"] == "session.ready"
        assert _FakeRealtimeSession.opened == 1

    @pytest.mark.asyncio
    async def test_the_session_is_told_the_agents_tier(self, env: dict[str, Any]) -> None:
        """Without the tier the session cannot pick the mini model for a mini agent."""
        async with env["sessions"]() as session:
            agent = await session.get(Agent, env["agent"].id)
            assert agent is not None
            agent.pricing_tier = "premium-mini"
            await session.commit()
        async with ASGIWebSocketClient(
            app,
            _dashboard_path(env),
            query_string=_dashboard_query(env, _access_token(env["owner"].id)),
        ) as ws:
            assert ws.accepted
            await ws.receive_json()
        assert _FakeRealtimeSession.configs[0]["pricing_tier"] == "premium-mini"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://internal:8000")


class TestEmbedSessionMinting:
    @pytest.mark.asyncio
    async def test_an_allowed_site_gets_a_session(self, env: dict[str, Any]) -> None:
        async with _client() as client:
            response = await client.post(
                f"/api/public/embed/{env['agent'].public_id}/session", headers={"Origin": CUSTOMER}
            )
        assert response.status_code == 200, response.text
        assert response.json()["session_id"]

    @pytest.mark.asyncio
    async def test_a_site_not_on_the_list_is_refused(self, env: dict[str, Any]) -> None:
        async with _client() as client:
            response = await client.post(
                f"/api/public/embed/{env['agent'].public_id}/session", headers={"Origin": STRANGER}
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_our_own_iframe_is_judged_by_the_page_around_it(
        self, env: dict[str, Any]
    ) -> None:
        url = f"/api/public/embed/{env['agent'].public_id}/session"
        async with _client() as client:
            allowed = await client.post(
                url, headers={"Origin": FRONTEND, "X-Embed-Parent-Origin": CUSTOMER}
            )
            refused = await client.post(
                url, headers={"Origin": FRONTEND, "X-Embed-Parent-Origin": STRANGER}
            )
        assert allowed.status_code == 200, allowed.text
        assert refused.status_code == 403

    @pytest.mark.asyncio
    async def test_an_agent_with_no_allowed_domains_admits_no_other_site(
        self, env: dict[str, Any]
    ) -> None:
        """An empty list used to mean every site."""
        async with env["sessions"]() as session:
            agent = (
                await session.execute(select(Agent).where(Agent.id == env["agent"].id))
            ).scalar_one()
            agent.allowed_domains = []
            await session.commit()

        async with _client() as client:
            response = await client.post(
                f"/api/public/embed/{env['agent'].public_id}/session", headers={"Origin": CUSTOMER}
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_minting_is_rate_limited_per_address(self, env: dict[str, Any]) -> None:
        url = f"/api/public/embed/{env['agent'].public_id}/session"
        async with _client() as client:
            statuses = [
                (await client.post(url, headers={"Origin": CUSTOMER})).status_code
                for _ in range(12)
            ]
        assert statuses[:10] == [200] * 10
        assert 429 in statuses[10:]


class TestEmbedToolCall:
    def _url(self, env: dict[str, Any]) -> str:
        return f"/api/public/embed/{env['agent'].public_id}/tool-call"

    def _session(self, env: dict[str, Any], site: str = "shop.example.com") -> str:
        return mint_embed_session(agent_id=str(env["agent"].id), site=site).token

    @pytest.mark.asyncio
    async def test_the_public_id_alone_runs_nothing(self, env: dict[str, Any]) -> None:
        """The hole as it stood: this request used to execute the tool."""
        async with _client() as client:
            response = await client.post(
                self._url(env),
                headers={"Origin": CUSTOMER},
                json={"tool_name": "crm_search", "arguments": {}},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_session_from_another_site_is_refused(self, env: dict[str, Any]) -> None:
        async with _client() as client:
            response = await client.post(
                self._url(env),
                headers={"Origin": STRANGER, "X-Embed-Session": self._session(env)},
                json={"tool_name": "crm_search", "arguments": {}},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_an_expired_session_is_refused(self, env: dict[str, Any]) -> None:
        stale = mint_embed_session(
            agent_id=str(env["agent"].id), site="shop.example.com", now=time.time() - 3600
        ).token
        async with _client() as client:
            response = await client.post(
                self._url(env),
                headers={"Origin": CUSTOMER, "X-Embed-Session": stale},
                json={"tool_name": "crm_search", "arguments": {}},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_valid_session_still_runs_no_tool_until_the_owner_allows_it(
        self, env: dict[str, Any]
    ) -> None:
        async with _client() as client:
            response = await client.post(
                self._url(env),
                headers={"Origin": CUSTOMER, "X-Embed-Session": self._session(env)},
                json={"tool_name": "crm_search", "arguments": {}},
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_with_tools_allowed_the_request_reaches_the_tool_check(
        self, env: dict[str, Any]
    ) -> None:
        """Past both gates, a tool the agent does not have is declined by name
        rather than by status: the control that the gates are not simply shut."""
        async with env["sessions"]() as session:
            agent = (
                await session.execute(select(Agent).where(Agent.id == env["agent"].id))
            ).scalar_one()
            agent.embed_settings = {"allow_tools": True}
            await session.commit()

        async with _client() as client:
            response = await client.post(
                self._url(env),
                headers={"Origin": CUSTOMER, "X-Embed-Session": self._session(env)},
                json={"tool_name": "definitely_not_a_tool", "arguments": {}},
            )
        assert response.status_code == 200, response.text
        assert response.json()["success"] is False


class TestEmbedTranscript:
    @pytest.mark.asyncio
    async def test_without_a_session_nothing_is_written(self, env: dict[str, Any]) -> None:
        async with _client() as client:
            response = await client.post(
                f"/api/public/embed/{env['agent'].public_id}/transcript",
                headers={"Origin": CUSTOMER},
                json={"session_id": "x", "transcript": "[User]: hello", "duration_seconds": 5},
            )
        assert response.status_code == 401
        async with env["sessions"]() as session:
            rows = (await session.execute(select(CallRecord))).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_a_session_saves_a_record_under_the_servers_own_session_id(
        self, env: dict[str, Any]
    ) -> None:
        minted = mint_embed_session(agent_id=str(env["agent"].id), site="shop.example.com")
        async with _client() as client:
            response = await client.post(
                f"/api/public/embed/{env['agent'].public_id}/transcript",
                headers={"Origin": CUSTOMER, "X-Embed-Session": minted.token},
                json={
                    "session_id": "chosen-by-the-browser",
                    "transcript": "[User]: hello",
                    "duration_seconds": 5,
                },
            )
        assert response.status_code == 200, response.text
        async with env["sessions"]() as session:
            record = (await session.execute(select(CallRecord))).scalar_one()
        assert record.provider_call_id == f"embed-{minted.session_id}"
        assert record.agent_id == env["agent"].id


class TestEmbedSocket:
    def _path(self, env: dict[str, Any]) -> str:
        return f"/ws/public/embed/{env['agent'].public_id}"

    async def _refused(self, env: dict[str, Any], query: str) -> None:
        async with ASGIWebSocketClient(app, self._path(env), query_string=query) as ws:
            assert not ws.accepted, "an embed socket was accepted without a valid session"
            assert ws.close_code == EMBED_SESSION_REJECTED
        assert _FakeRealtimeSession.opened == 0

    @pytest.mark.asyncio
    async def test_no_session_no_socket(self, env: dict[str, Any]) -> None:
        await self._refused(env, "")

    @pytest.mark.asyncio
    async def test_a_made_up_session_is_refused(self, env: dict[str, Any]) -> None:
        await self._refused(env, "session=abc.123.def")

    @pytest.mark.asyncio
    async def test_a_session_minted_for_one_site_does_not_open_from_another(
        self, env: dict[str, Any]
    ) -> None:
        token = mint_embed_session(agent_id=str(env["agent"].id), site="shop.example.com").token
        await self._refused(env, f"session={token}&parent_origin={STRANGER}")

    @pytest.mark.asyncio
    async def test_a_valid_session_opens_one_socket_and_only_one(self, env: dict[str, Any]) -> None:
        token = mint_embed_session(agent_id=str(env["agent"].id), site=DIRECT).token

        async with ASGIWebSocketClient(app, self._path(env), query_string=f"session={token}") as ws:
            assert ws.accepted
            ready = await ws.receive_json()
            assert ready is not None
            assert ready["type"] == "session.ready"
        assert _FakeRealtimeSession.opened == 1
        # Tools are off for embedded sessions until the owner allows them, so
        # the model is given none to call.
        assert _FakeRealtimeSession.configs[0]["enabled_tools"] == []

        async with ASGIWebSocketClient(app, self._path(env), query_string=f"session={token}") as ws:
            assert not ws.accepted, "the same session opened a second model session"
            assert ws.close_code == EMBED_SESSION_REJECTED
        assert _FakeRealtimeSession.opened == 1

    @pytest.mark.asyncio
    async def test_when_the_claim_store_is_down_the_socket_is_refused(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _down() -> Any:
            raise ConnectionError("redis is unreachable")

        monkeypatch.setattr(stream_token_use, "_store", _down)
        token = mint_embed_session(agent_id=str(env["agent"].id), site=DIRECT).token
        await self._refused(env, f"session={token}")
