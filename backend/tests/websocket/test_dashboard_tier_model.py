"""The dashboard test socket opens the model its agent's pricing tier names.

The socket built its model session config without `pricing_tier`, so a
`premium-mini` agent was served the premium model. This drives the real endpoint
with the real `GPTRealtimeSession` and a fake OpenAI client, and asserts on the
model handed to `client.realtime.connect`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.api import realtime
from app.core.config import settings
from app.main import app
from app.models.agent import Agent
from app.services import gpt_realtime
from tests.websocket.asgi_ws import ASGIWebSocketClient
from tests.websocket.test_browser_socket_auth import (
    _access_token,
    _dashboard_path,
    _dashboard_query,
    env,  # noqa: F401
)

PREMIUM = "model-premium-under-test"
MINI = "model-mini-under-test"


class _RefusingConnect:
    async def __aenter__(self) -> Any:
        raise ConnectionError("fake socket: no network in tests")

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.fixture
def connected_models(monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]) -> list[str]:  # noqa: F811
    models: list[str] = []

    def connect(*, model: str, **_: Any) -> _RefusingConnect:
        models.append(model)
        return _RefusingConnect()

    fake_client = SimpleNamespace(realtime=SimpleNamespace(connect=connect))

    async def get_user_api_keys(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(openai_provider="openai", openai_api_key="sk-fake")

    monkeypatch.setattr(realtime, "GPTRealtimeSession", gpt_realtime.GPTRealtimeSession)
    monkeypatch.setattr(gpt_realtime, "get_user_api_keys", get_user_api_keys)
    monkeypatch.setattr(gpt_realtime, "AsyncOpenAI", lambda **_: fake_client)
    monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM", PREMIUM)
    monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM_MINI", MINI)
    return models


@pytest.mark.asyncio
@pytest.mark.parametrize(("tier", "expected"), [("premium-mini", MINI), ("premium", PREMIUM)])
async def test_the_dashboard_session_connects_with_the_agents_tier_model(
    tier: str,
    expected: str,
    connected_models: list[str],
    env: dict[str, Any],  # noqa: F811
) -> None:
    async with env["sessions"]() as session:
        agent = await session.get(Agent, env["agent"].id)
        assert agent is not None
        agent.pricing_tier = tier
        await session.commit()

    async with ASGIWebSocketClient(
        app,
        _dashboard_path(env),
        query_string=_dashboard_query(env, _access_token(env["owner"].id)),
    ) as ws:
        assert ws.accepted
        for _ in range(300):
            if connected_models:
                break
            await asyncio.sleep(0.01)

    assert connected_models == [expected]
