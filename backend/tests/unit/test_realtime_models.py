"""Realtime model names and the browser-key request.

OpenAI retires model names on a schedule (see the PR for the dated sources). A
retired name in a session does not raise locally: the session is refused
remotely and the call is silent. These tests keep the names in one place, keep
the retired ones out of application code, and check the body minted for a
browser is the GA shape. Nothing here touches the network: HTTP goes through
`httpx.MockTransport`, and any real socket connect fails the test.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai.types.realtime.realtime_session_create_request import (
    RealtimeSessionCreateRequest,
)

from app.api.agents import _get_provider_config
from app.api.embed import embed_model_session_config
from app.api.realtime import dashboard_token_session_config, get_realtime_model_for_tier
from app.core.config import settings
from app.services.realtime_models import (
    CLIENT_SECRETS_URL,
    ClientSecretError,
    build_client_secret_body,
    input_transcription,
    parse_client_secret,
    post_client_secret,
    realtime_model_for_tier,
)

REPO = Path(__file__).resolve().parents[3]
APP_CODE = [REPO / "backend" / "app", REPO / "frontend" / "src"]
SKIP_DIRS = {"node_modules", ".next", "__pycache__"}
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".css"}

# Retired, or with a published shutdown date, as of the PR's sources.
# `gpt-realtime` is only allowed as the prefix of the 2.1 names.
RETIRED = re.compile(
    r"gpt-realtime(?!-2\.1)"
    r"|gpt-4o-(?:mini-)?realtime"
    r"|whisper-1"
    r"|gpt-4o-(?:mini-)?transcribe"
    r"|OpenAI-Beta"
    r"|realtime=v1"
    r"|v1/realtime/sessions"
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _app_files() -> list[Path]:
    found: list[Path] = []
    for root in APP_CODE:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in SUFFIXES and not SKIP_DIRS & set(path.parts):
                found.append(path)
    return found


class TestNoRetiredNames:
    def test_the_scan_sees_the_code(self) -> None:
        names = {p.name for p in _app_files()}
        assert "gpt_realtime.py" in names
        assert "pricing-tiers.ts" in names

    def test_no_retired_model_or_beta_request_in_app_code(self) -> None:
        hits = [
            f"{path.relative_to(REPO)}:{n}: {line.strip()}"
            for path in _app_files()
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if RETIRED.search(line)
        ]
        assert not hits, "retired model name or beta-shaped request:\n" + "\n".join(hits)

    def test_the_pattern_does_catch_each_retired_name(self) -> None:
        for name in (
            "gpt-realtime",
            "gpt-realtime-2025-08-28",
            "gpt-realtime-mini",
            "gpt-realtime-mini-2025-12-15",
            "gpt-4o-mini-realtime-preview-2024-12-17",
            "gpt-4o-mini-realtime",
            "whisper-1",
            "gpt-4o-transcribe",
            "OpenAI-Beta",
            "https://api.openai.com/v1/realtime/sessions",
        ):
            assert RETIRED.search(name), name
        assert not RETIRED.search("gpt-realtime-2.1 gpt-realtime-2.1-mini gpt-transcribe")


class TestOneSourceOfModels:
    def test_defaults(self) -> None:
        assert settings.OPENAI_REALTIME_MODEL_PREMIUM == "gpt-realtime-2.1"
        assert settings.OPENAI_REALTIME_MODEL_PREMIUM_MINI == "gpt-realtime-2.1-mini"

    def test_transcription_default_is_gpt_transcribe_not_live(self) -> None:
        """gpt-live-transcribe does not support server_vad, which every session sets."""
        assert settings.OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL == "gpt-transcribe"
        assert settings.OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL != "gpt-live-transcribe"

    def test_tier_picks_the_model_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM", "premium-x")
        monkeypatch.setattr(settings, "OPENAI_REALTIME_MODEL_PREMIUM_MINI", "mini-x")

        assert realtime_model_for_tier("premium") == "premium-x"
        assert realtime_model_for_tier("premium-mini") == "mini-x"
        assert get_realtime_model_for_tier("premium-mini") == "mini-x"
        assert _get_provider_config("premium")["llm_model"] == "premium-x"
        assert _get_provider_config("premium-mini")["llm_model"] == "mini-x"

    def test_a_stored_retired_name_is_never_what_a_session_uses(self) -> None:
        """Rows keep whatever name they were saved with; the tier decides at run time."""
        stored_label = "gpt-4o-mini-realtime-preview-2024-12-17"
        assert stored_label not in {
            realtime_model_for_tier("premium"),
            realtime_model_for_tier("premium-mini"),
        }

    def test_transcription_is_configurable_and_can_be_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert input_transcription() == {"model": "gpt-transcribe"}
        monkeypatch.setattr(settings, "OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL", "other")
        assert input_transcription() == {"model": "other"}
        monkeypatch.setattr(settings, "OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL", "")
        assert input_transcription() is None


def _assert_valid_ga_body(body: dict[str, Any]) -> None:
    assert set(body) == {"session"}
    session = body["session"]
    assert session["type"] == "realtime"
    assert set(session) <= set(RealtimeSessionCreateRequest.model_fields), session
    RealtimeSessionCreateRequest.model_validate(session)
    # Beta spellings, at the top level of the session, where they used to live.
    for beta in ("modalities", "voice", "input_audio_transcription", "turn_detection"):
        assert beta not in session, beta


class TestMintedRequestIsGa:
    def _embed_session(self) -> dict[str, Any]:
        return embed_model_session_config(
            model=realtime_model_for_tier("premium"),
            voice="marin",
            instructions="Be brief.",
            tools=[{"type": "function", "name": "t", "description": "d", "parameters": {}}],
        )

    def test_the_embed_session_is_ga(self) -> None:
        body = build_client_secret_body(self._embed_session())

        _assert_valid_ga_body(body)
        session = body["session"]
        assert session["model"] == "gpt-realtime-2.1"
        assert session["audio"]["output"]["voice"] == "marin"
        assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
        assert session["audio"]["input"]["transcription"] == {"model": "gpt-transcribe"}
        assert session["output_modalities"] == ["audio"]

    def test_the_dashboard_session_is_ga(self) -> None:
        body = build_client_secret_body(dashboard_token_session_config("m", "cedar"))

        _assert_valid_ga_body(body)
        assert body["session"]["audio"]["output"]["voice"] == "cedar"

    def test_a_beta_shaped_session_is_refused_before_it_is_sent(self) -> None:
        beta = {"model": "m", "modalities": ["audio", "text"], "voice": "marin"}
        with pytest.raises(ValueError, match="GA realtime session"):
            build_client_secret_body(beta)

    def test_no_transcription_block_when_none_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL", "")
        session = embed_model_session_config(model="m", voice="marin", instructions="x", tools=[])
        assert "transcription" not in session["audio"]["input"]
        _assert_valid_ga_body(build_client_secret_body(session))

    @pytest.mark.asyncio
    async def test_the_request_on_the_wire(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"value": "ek_test", "expires_at": 123})

        response = await post_client_secret(
            "sk-test", self._embed_session(), transport=httpx.MockTransport(handler)
        )

        assert len(seen) == 1
        request = seen[0]
        assert str(request.url) == CLIENT_SECRETS_URL
        assert CLIENT_SECRETS_URL == "https://api.openai.com/v1/realtime/client_secrets"
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert "openai-beta" not in request.headers
        _assert_valid_ga_body(json.loads(request.content))
        assert parse_client_secret(response) == {"value": "ek_test", "expires_at": 123}

    def test_the_key_is_read_from_value_not_client_secret(self) -> None:
        beta_reply = httpx.Response(200, json={"client_secret": {"value": "ek_old"}})
        with pytest.raises(ClientSecretError):
            parse_client_secret(beta_reply)
        with pytest.raises(ClientSecretError):
            parse_client_secret(httpx.Response(200, text="not json"))
        with pytest.raises(ClientSecretError):
            parse_client_secret(httpx.Response(200, json={"value": ""}))


class TestKeyExpiry:
    def test_expires_after_is_sent_in_the_ga_shape_and_is_valid(self) -> None:
        from openai.types.realtime.client_secret_create_params import ClientSecretCreateParams

        body = build_client_secret_body(
            dashboard_token_session_config("m", "cedar"), expires_after_seconds=60
        )

        assert body["expires_after"] == {"anchor": "created_at", "seconds": 60}
        assert set(body) <= set(ClientSecretCreateParams.__annotations__)

    def test_no_expiry_is_sent_unless_asked_for(self) -> None:
        body = build_client_secret_body(dashboard_token_session_config("m", "cedar"))
        assert "expires_after" not in body
