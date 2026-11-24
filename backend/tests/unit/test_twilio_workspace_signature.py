"""A Twilio webhook is checked against the account that owns its target.

One global auth token cannot verify webhooks for workspaces that each bring
their own Twilio account. The credential is chosen by the caller from the
webhook's target and passed in; these tests pin what the verifier then does with
it, including the cross-tenant case that choosing by the request's own
``AccountSid`` would have opened.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.config import settings
from app.core.webhook_security import verify_twilio_webhook

GLOBAL_TOKEN = "global-platform-twilio-token"
WORKSPACE_TOKEN = "workspace-a-twilio-token"
OTHER_WORKSPACE_TOKEN = "workspace-b-twilio-token"
PUBLIC_ORIGIN = "https://voice.example.com"
PATH = "/webhooks/twilio/voice"
PARAMS = {"CallSid": "CA123", "From": "+15551230000", "To": "+15558886666"}


def _sign(token: str) -> str:
    data = f"{PUBLIC_ORIGIN}{PATH}" + "".join(f"{k}{v}" for k, v in sorted(PARAMS.items()))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def _request(signature: str | None) -> Request:
    headers = {"host": "internal:8000", "content-type": "application/x-www-form-urlencoded"}
    if signature is not None:
        headers["x-twilio-signature"] = signature
    body = urlencode(PARAMS).encode()
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
            "path": PATH,
            "raw_path": PATH.encode(),
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "server": ("internal", 8000),
        },
        receive,
    )


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_URL", PUBLIC_ORIGIN)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", GLOBAL_TOKEN)


@pytest.mark.asyncio
async def test_a_webhook_signed_by_the_owning_workspace_is_accepted() -> None:
    assert await verify_twilio_webhook(_request(_sign(WORKSPACE_TOKEN)), WORKSPACE_TOKEN) is True


@pytest.mark.asyncio
async def test_a_workspace_token_verifies_with_no_global_token_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case that could not work before: every webhook was refused."""
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    assert await verify_twilio_webhook(_request(_sign(WORKSPACE_TOKEN)), WORKSPACE_TOKEN) is True


@pytest.mark.asyncio
async def test_the_global_token_still_verifies_as_a_fallback() -> None:
    """Numbers on the platform's own account keep working."""
    assert await verify_twilio_webhook(_request(_sign(GLOBAL_TOKEN)), WORKSPACE_TOKEN) is True
    assert await verify_twilio_webhook(_request(_sign(GLOBAL_TOKEN)), None) is True


@pytest.mark.asyncio
async def test_another_tenants_valid_signature_is_refused() -> None:
    """Workspace B signs correctly with its own token a webhook aimed at
    workspace A's number. Verified against A's credential, it fails."""
    with pytest.raises(HTTPException) as refused:
        await verify_twilio_webhook(_request(_sign(OTHER_WORKSPACE_TOKEN)), WORKSPACE_TOKEN)
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_no_credential_anywhere_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    with pytest.raises(HTTPException) as refused:
        await verify_twilio_webhook(_request(_sign(WORKSPACE_TOKEN)), None)
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_a_missing_signature_is_refused_even_with_credentials() -> None:
    with pytest.raises(HTTPException) as refused:
        await verify_twilio_webhook(_request(None), WORKSPACE_TOKEN)
    assert refused.value.status_code == 403
