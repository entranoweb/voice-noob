"""The preflight script's own checks.

An operator tool that reports "all clear" wrongly is worse than no tool, and
this one is run exactly once per deployment — in the moments when nobody is in a
position to notice it lying. The websocket probe is exercised against a real
server on a real port because its whole value is that it speaks the protocol.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import uvicorn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "preflight_telnyx.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("preflight_telnyx", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["preflight_telnyx"] = module
    spec.loader.exec_module(module)
    return module


preflight = _load()
Level = preflight.Level


def _levels(results: list[Any]) -> list[Any]:
    return [r.level for r in results]


class TestPublicUrl:
    def test_unset_is_a_failure(self) -> None:
        """The whole point of the script: the host that goes wrong silently."""
        assert _levels(preflight.check_public_url(None)) == [Level.FAIL]

    def test_https_passes(self) -> None:
        assert _levels(preflight.check_public_url("https://voice.example.com")) == [Level.PASS]

    def test_cleartext_warns(self) -> None:
        assert _levels(preflight.check_public_url("http://voice.example.com")) == [Level.WARN]

    def test_a_bare_host_is_a_failure(self) -> None:
        assert _levels(preflight.check_public_url("voice.example.com")) == [Level.FAIL]


class TestLoopback:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://127.0.0.1:8000", True),
            ("http://localhost:8000", True),
            ("https://voice.example.com", False),
            # The substring trap: this host is not loopback.
            ("https://localhost.evil.example.com", False),
        ],
    )
    def test_only_the_real_thing(self, url: str, expected: bool) -> None:
        assert preflight._is_loopback(url) is expected


class _StubServer:
    """A websocket endpoint that closes the way the real bridge closes."""

    def __init__(self, close_code: int) -> None:
        self.close_code = close_code

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "websocket":
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": self.close_code})


async def _serve(app: Any) -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # uvicorn exposes readiness as a plain flag, not an event, so polling it is
    # the only way to know the port is bound.
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


class TestStreamProbe:
    @pytest.mark.asyncio
    async def test_4004_is_the_bridge_answering(self) -> None:
        """4004 means the app resolved the route and queried for the agent.

        That is the whole path the carrier needs — TLS, the upgrade, routing,
        a working database session — with nothing left behind.
        """
        async for url in _serve(_StubServer(preflight.AGENT_NOT_FOUND)):
            results = await preflight.check_stream_url(url)

        assert _levels(results) == [Level.PASS]
        assert "4004" in results[0].detail

    @pytest.mark.asyncio
    async def test_another_close_code_is_not_a_pass(self) -> None:
        """1012 is a shutting-down app. It answered, but it is not ready."""
        async for url in _serve(_StubServer(1012)):
            results = await preflight.check_stream_url(url)

        assert _levels(results) == [Level.WARN]

    @pytest.mark.asyncio
    async def test_no_upgrade_is_a_failure(self) -> None:
        """A proxy that will not forward the upgrade is the other silent one."""

        async def http_only(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async for url in _serve(http_only):
            results = await preflight.check_stream_url(url)

        assert _levels(results) == [Level.FAIL]

    @pytest.mark.asyncio
    async def test_an_unreachable_host_is_a_failure(self) -> None:
        results = await preflight.check_stream_url("http://127.0.0.1:1")

        assert _levels(results) == [Level.FAIL]


class TestCredentials:
    """Which key is fatal, and which only limits what you can do."""

    @pytest.fixture(autouse=True)
    def settings(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from app.core.config import settings

        monkeypatch.setattr(settings, "TELNYX_API_KEY", None)
        monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", None)
        monkeypatch.setattr(settings, "DEBUG", False)
        monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)
        return settings

    def _by_check(self, results: list[Any], name: str) -> Any:
        return next(r for r in results if r.check == name)

    def test_a_missing_public_key_is_fatal(self) -> None:
        assert (
            self._by_check(preflight.check_credentials(), "TELNYX_PUBLIC_KEY").level is Level.FAIL
        )

    def test_a_missing_api_key_only_costs_outbound(self) -> None:
        """Inbound never reads it, so failing the run on it would be wrong."""
        assert self._by_check(preflight.check_credentials(), "TELNYX_API_KEY").level is Level.WARN

    def test_allowing_unsigned_webhooks_is_an_open_door(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one setting that accepts any webhook at all is fatal here."""
        monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", True)

        assert (
            self._by_check(preflight.check_credentials(), "ALLOW_UNSIGNED_WEBHOOKS").level
            is Level.FAIL
        )

    def test_debug_alone_no_longer_opens_the_door(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEBUG used to switch verification off when no key was set. It is now
        only a warning: the missing key is what fails, and it fails closed."""
        monkeypatch.setattr(settings, "DEBUG", True)

        results = preflight.check_credentials()
        assert self._by_check(results, "DEBUG").level is Level.WARN
        assert self._by_check(results, "TELNYX_PUBLIC_KEY").level is Level.FAIL

    def test_the_environment_openai_key_is_not_checked(self) -> None:
        """The call reads the workspace key; an env check here would be a lie."""
        assert not [r for r in preflight.check_credentials() if "OPENAI" in r.check]


class TestRealtimeModelsAndProxy:
    def test_the_models_in_use_are_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM", "model-a")
        monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM_MINI", "model-b")
        monkeypatch.setattr(settings, "OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL", "model-c")

        results = preflight.check_realtime_models()

        assert _levels(results) == [Level.PASS, Level.PASS]
        assert "model-a" in results[0].detail
        assert "model-b" in results[0].detail
        assert "model-c" in results[1].detail
        assert "unverified" in results[0].detail
        assert "unverified" in results[1].detail

    def test_no_transcription_model_is_said_plainly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL", " ")

        assert "no caller transcript" in preflight.check_realtime_models()[1].detail

    def test_a_public_host_without_trusted_proxies_warns(self) -> None:
        results = preflight.check_trusted_proxy("https://voice.example.com", None)

        assert _levels(results) == [Level.WARN]

    def test_trusted_proxies_set_passes(self) -> None:
        results = preflight.check_trusted_proxy("https://voice.example.com", "10.0.1.0/24")

        assert _levels(results) == [Level.PASS]

    def test_a_local_rehearsal_does_not_warn(self) -> None:
        assert _levels(preflight.check_trusted_proxy("http://127.0.0.1:8000", None)) == [Level.PASS]
