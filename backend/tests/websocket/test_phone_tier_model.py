"""The phone bridge opens the Realtime model its agent's pricing tier names.

The browser and embed paths chose the model by tier; the bridge always used the
premium one, so a `premium-mini` agent was served (and billed) as `premium` on
every phone call, with nothing in any log to say so. These tests drive the real
Telnyx and Twilio endpoints with the real `GPTRealtimeSession` and a fake OpenAI
client, and assert on the model argument handed to `client.realtime.connect`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import telephony_ws
from app.core.config import settings
from app.main import app
from app.models.agent import Agent
from app.services import gpt_realtime
from tests.websocket.asgi_ws import ASGIWebSocketClient
from tests.websocket.test_telnyx_inbound_call import (
    CALL_SID as TELNYX_CALL_SID,
)
from tests.websocket.test_telnyx_inbound_call import (
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

PREMIUM = "model-premium-under-test"
MINI = "model-mini-under-test"


class _RefusingConnect:
    """What `client.realtime.connect(model=...)` returns: it refuses to open."""

    async def __aenter__(self) -> Any:
        raise ConnectionError("fake socket: no network in tests")

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.fixture
def connected_models(
    monkeypatch: pytest.MonkeyPatch,
    inbound_call_env: dict[str, Any],  # noqa: F811
    twilio_env: dict[str, Any],  # noqa: F811
) -> list[str]:
    """Real session class, fake provider client; records every model asked for."""
    models: list[str] = []

    def connect(*, model: str, **_: Any) -> _RefusingConnect:
        models.append(model)
        return _RefusingConnect()

    fake_client = SimpleNamespace(realtime=SimpleNamespace(connect=connect))

    async def get_user_api_keys(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(openai_provider="openai", openai_api_key="sk-fake")

    async def _register(call_id: str, **_: Any) -> bool:
        return True

    async def _unregister(call_id: str) -> bool:
        return True

    # The call registry is not what is under test.
    monkeypatch.setattr(telephony_ws, "register_call", _register)
    monkeypatch.setattr(telephony_ws, "unregister_call", _unregister)
    monkeypatch.setattr(telephony_ws, "GPTRealtimeSession", gpt_realtime.GPTRealtimeSession)
    monkeypatch.setattr(gpt_realtime, "get_user_api_keys", get_user_api_keys)
    monkeypatch.setattr(gpt_realtime, "AsyncOpenAI", lambda **_: fake_client)
    monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM", PREMIUM)
    monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM_MINI", MINI)
    return models


async def _set_tier(test_engine: Any, env: dict[str, Any], tier: str) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        agent = await session.get(Agent, env["agent"].id)
        assert agent is not None
        agent.pricing_tier = tier
        await session.commit()


async def _wait_for(models: list[str]) -> None:
    for _ in range(300):
        if models:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the bridge never asked the provider for a model")


async def _call_telnyx(env: dict[str, Any], models: list[str]) -> None:
    agent_id = str(env["agent"].id)
    query = await _place_webhook(env)
    async with ASGIWebSocketClient(
        app, f"/ws/telephony/telnyx/{agent_id}", query_string=query
    ) as ws:
        await ws.send_json(
            {"event": "start", "stream_id": "s-1", "start": {"call_control_id": TELNYX_CALL_SID}}
        )
        await _wait_for(models)


async def _call_twilio(env: dict[str, Any], models: list[str]) -> None:
    agent_id = str(env["agent"].id)
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
        p.attrib["value"] for p in stream.findall("Parameter") if p.attrib["name"] == "token"
    )
    async with ASGIWebSocketClient(app, f"/ws/telephony/twilio/{agent_id}") as ws:
        await ws.send_json(_start(TWILIO_CALL_SID, token))
        await _wait_for(models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "expected"),
    [("premium-mini", MINI), ("premium", PREMIUM)],
)
class TestPhoneBridgeModel:
    async def test_telnyx(
        self,
        tier: str,
        expected: str,
        connected_models: list[str],
        test_engine: Any,
        inbound_call_env: dict[str, Any],  # noqa: F811
    ) -> None:
        await _set_tier(test_engine, inbound_call_env, tier)
        await _call_telnyx(inbound_call_env, connected_models)

        assert connected_models == [expected]

    async def test_twilio(
        self,
        tier: str,
        expected: str,
        connected_models: list[str],
        test_engine: Any,
        twilio_env: dict[str, Any],  # noqa: F811
    ) -> None:
        await _set_tier(test_engine, twilio_env, tier)
        await _call_twilio(twilio_env, connected_models)

        assert connected_models == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "expected", "deployment"),
    [
        ("premium-mini", MINI, None),
        ("premium", PREMIUM, None),
        ("premium-mini", "my-saved-deployment", "my-saved-deployment"),
    ],
)
async def test_azure_uses_the_tier_model_only_when_no_deployment_is_saved(
    tier: str,
    expected: str,
    deployment: str | None,
    connected_models: list[str],
    test_engine: Any,
    inbound_call_env: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Azure fallback follows the tier; a saved deployment name still wins."""

    async def azure_keys(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(
            openai_provider="azure",
            azure_openai_endpoint="https://azure.example.com",
            azure_openai_api_key="fake",
            azure_openai_deployment_name=deployment,
        )

    async def public_url(_: str) -> None:
        return None

    fake_client = SimpleNamespace(
        realtime=SimpleNamespace(
            connect=lambda *, model, **_: (connected_models.append(model), _RefusingConnect())[1]
        )
    )
    monkeypatch.setattr(gpt_realtime, "get_user_api_keys", azure_keys)
    monkeypatch.setattr(gpt_realtime, "assert_public_url", public_url)
    monkeypatch.setattr(gpt_realtime, "AsyncAzureOpenAI", lambda **_: fake_client)

    await _set_tier(test_engine, inbound_call_env, tier)
    await _call_telnyx(inbound_call_env, connected_models)

    assert connected_models == [expected]
