"""A webhook that cannot be verified is refused, whatever DEBUG says.

Verification used to fall back to ``settings.DEBUG`` when no key was configured,
so one flag meant both "log verbosely" and "accept calls from anyone". Each test
here sets DEBUG **on**, because that is the configuration that used to open the
door: with DEBUG off these passed before the fix and would prove nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import webhook_security
from app.core.config import (
    InsecureSecretKeyError,
    Settings,
    refuse_placeholder_secret_key,
    settings,
)
from app.core.webhook_security import (
    validate_telnyx_signature,
    verify_telnyx_webhook,
    verify_twilio_webhook,
)

AUTH_TOKEN = "twilio-auth-token-for-tests"


def _request(
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    query: str = "",
    host: str = "internal:8000",
) -> Request:
    all_headers = {"host": host, "content-type": "application/x-www-form-urlencoded"}
    all_headers.update(headers or {})
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in all_headers.items()],
            "server": ("internal", 8000),
        },
        receive,
    )


def _twilio_signature(url: str, params: dict[str, str]) -> str:
    data = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    digest = hmac.new(AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


@pytest.fixture
def debug_on_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact configuration that used to accept every webhook unverified."""
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)
    monkeypatch.setattr(settings, "PUBLIC_URL", None)


@pytest.mark.usefixtures("debug_on_nothing_configured")
class TestTelnyxFailsClosed:
    def test_no_key_never_validates_even_in_debug(self) -> None:
        assert validate_telnyx_signature("c2ln", "1800000000", b"body") is False

    @pytest.mark.asyncio
    async def test_a_signed_looking_webhook_with_no_key_is_refused(self) -> None:
        request = _request(
            "/webhooks/telnyx/voice",
            headers={"telnyx-signature-ed25519": "c2ln", "telnyx-timestamp": "1800000000"},
        )

        with pytest.raises(HTTPException) as refused:
            await verify_telnyx_webhook(request)
        assert refused.value.status_code == 403

    @pytest.mark.asyncio
    async def test_an_unsigned_webhook_is_refused(self) -> None:
        with pytest.raises(HTTPException) as refused:
            await verify_telnyx_webhook(_request("/webhooks/telnyx/voice"))
        assert refused.value.status_code == 403

    @pytest.mark.asyncio
    async def test_only_the_explicit_override_lets_one_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", True)

        assert await verify_telnyx_webhook(_request("/webhooks/telnyx/voice")) is True

    def test_the_override_does_not_reach_the_validator_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate_telnyx_signature returns True only for a signature it
        checked, so a new caller of it cannot inherit the hole (DECISIONS §29)."""
        monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", True)

        assert validate_telnyx_signature("c2ln", "1800000000", b"body") is False


@pytest.mark.usefixtures("debug_on_nothing_configured")
class TestTwilioFailsClosed:
    @pytest.mark.asyncio
    async def test_no_auth_token_is_refused(self) -> None:
        with pytest.raises(HTTPException) as refused:
            await verify_twilio_webhook(_request("/webhooks/twilio/voice"))
        assert refused.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_missing_signature_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", AUTH_TOKEN)

        with pytest.raises(HTTPException) as refused:
            await verify_twilio_webhook(_request("/webhooks/twilio/voice", body=b"CallSid=CA1"))
        assert refused.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_wrong_signature_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", AUTH_TOKEN)
        request = _request(
            "/webhooks/twilio/voice",
            body=b"CallSid=CA1",
            headers={"X-Twilio-Signature": "bm90LXRoZS1zaWduYXR1cmU="},
        )

        with pytest.raises(HTTPException) as refused:
            await verify_twilio_webhook(request)
        assert refused.value.status_code == 403

    @pytest.mark.asyncio
    async def test_only_the_explicit_override_lets_one_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", True)

        assert await verify_twilio_webhook(_request("/webhooks/twilio/voice")) is True

    @pytest.mark.asyncio
    async def test_a_genuine_signature_over_the_public_url_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twilio signed https://voice.example.com/...; the app, behind a proxy,
        sees http://internal:8000/.... Checked against the latter this could
        only fail, so every Twilio webhook behind a proxy was refused."""
        monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", AUTH_TOKEN)
        monkeypatch.setattr(settings, "PUBLIC_URL", "https://voice.example.com")
        params = {"CallSid": "CA123", "From": "+15551230000", "To": "+15559997777"}
        signature = _twilio_signature("https://voice.example.com/webhooks/twilio/voice", params)
        request = _request(
            "/webhooks/twilio/voice",
            body="&".join(f"{k}={v.replace('+', '%2B')}" for k, v in params.items()).encode(),
            headers={"X-Twilio-Signature": signature},
        )

        assert await verify_twilio_webhook(request) is True


class TestTheOverrideAnnouncesItself:
    def test_it_is_off_by_default(self) -> None:
        assert Settings.model_fields["ALLOW_UNSIGNED_WEBHOOKS"].default is False

    def test_it_logs_at_critical_when_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        class _Recorder:
            def critical(self, event: str, **fields: Any) -> None:
                seen.append((event, fields))

        monkeypatch.setattr(webhook_security, "logger", _Recorder())
        monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", True)

        assert webhook_security.warn_if_unsigned_webhooks_allowed() is True
        assert [event for event, _ in seen] == ["unsigned_webhooks_allowed"]

    def test_it_says_nothing_when_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)

        assert webhook_security.warn_if_unsigned_webhooks_allowed() is False


class TestTheSigningKeyHasNoUsableDefault:
    @pytest.mark.parametrize(
        "key",
        [
            "change-this-to-a-random-secret-key-in-production",
            "generate_a_secure_key_here",
            "your_64_character_secret_key_here",
            "your_64_character_hex_string_here",
            "",
            "   ",
        ],
    )
    def test_startup_refuses_a_shipped_or_empty_key(self, key: str) -> None:
        candidate = Settings(SECRET_KEY=key, DEBUG=False)

        with pytest.raises(InsecureSecretKeyError, match="SECRET_KEY"):
            refuse_placeholder_secret_key(candidate)

    def test_a_real_key_starts(self) -> None:
        refuse_placeholder_secret_key(Settings(SECRET_KEY="k" * 64, DEBUG=False))

    def test_debug_is_the_only_way_past_it(self) -> None:
        refuse_placeholder_secret_key(
            Settings(
                SECRET_KEY="change-this-to-a-random-secret-key-in-production",  # noqa: S106
                DEBUG=True,
            )
        )

    def test_the_refusal_does_not_echo_the_environment(self) -> None:
        """The first version was a pydantic validator, whose error printed the
        rejected input — the process environment, other secrets included."""
        candidate = Settings(
            SECRET_KEY="generate_a_secure_key_here",  # noqa: S106
            DEBUG=False,
            OPENAI_API_KEY="sk-this-must-never-appear-in-a-startup-error",
        )

        with pytest.raises(InsecureSecretKeyError) as refused:
            refuse_placeholder_secret_key(candidate)
        assert "sk-this-must-never-appear" not in str(refused.value)
        assert "generate_a_secure_key_here" not in str(refused.value)
