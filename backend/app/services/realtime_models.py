"""Which Realtime model a session uses, and how a browser key is minted.

Model names live in `app.core.config.settings` and nowhere else. A tier picks
the model at session start, so a stored row naming a model OpenAI has retired
(`agents.llm_model`, a workspace's Azure deployment name) is only a label: it is
never sent to the provider, and needs no migration.

Browser keys use the GA endpoint, `POST /v1/realtime/client_secrets`, with the
session wrapped as `{"session": {"type": "realtime", ...}}`, and the response
carries the key as `value`. The old beta sessions endpoint, which put the key under
`client_secret.value`, is gone.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings

CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


def realtime_model_for_tier(pricing_tier: str) -> str:
    """The Realtime model for an agent's pricing tier."""
    if pricing_tier == "premium-mini":
        return settings.OPENAI_REALTIME_MODEL_PREMIUM_MINI
    return settings.OPENAI_REALTIME_MODEL_PREMIUM


def input_transcription() -> dict[str, str] | None:
    """The `audio.input.transcription` block, or None when none is configured."""
    model = settings.OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL.strip()
    return {"model": model} if model else None


def build_client_secret_body(session: dict[str, Any]) -> dict[str, Any]:
    """The request body for a browser key, refused here if it is not GA-shaped."""
    # Imported here because gpt_realtime imports this module for the model names.
    from app.services.gpt_realtime import _assert_ga_session

    _assert_ga_session(session)
    return {"session": session}


class ClientSecretError(Exception):
    """The provider answered, but not with a usable key."""


def parse_client_secret(response: httpx.Response) -> dict[str, Any]:
    """`{"value", "expires_at"}` from a GA response; raises if there is no key."""
    try:
        data = response.json()
    except ValueError as exc:
        raise ClientSecretError("response was not JSON") from exc
    value = data.get("value") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise ClientSecretError("response carried no `value`")
    return {"value": value, "expires_at": data.get("expires_at")}


async def post_client_secret(
    api_key: str,
    session: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Ask OpenAI for a short-lived browser key for `session`.

    `transport` exists so tests can supply a fake; nothing here is reached in
    tests without one.
    """
    body = build_client_secret_body(session)
    async with httpx.AsyncClient(transport=transport) as client:
        return await client.post(
            CLIENT_SECRETS_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
