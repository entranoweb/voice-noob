"""Refuse a signed carrier webhook that has already been accepted once.

A signature proves who sent a webhook, not that it is new. Telnyx signs
``{timestamp}|{body}`` and the age check bounds how old a capture can be, but an
identical request replayed inside that window verifies again. Twilio's
``X-Twilio-Signature`` covers the URL and parameters only: no timestamp, so a
captured request verifies forever.

After a signature has verified, the first request records a key in Redis with
``SET NX EX``. A later request with the same key is a *duplicate*.

What a duplicate gets is the careful part, and it is not an error:

* The carrier retries the **same** event when it did not see our answer, and it
  may reuse the same signature. A retried answer webhook that gets a 4xx/5xx
  can be treated as a failed delivery (Telnyx follows its failover URL, Twilio
  reports a webhook error and may hang the call up), so the guard never answers
  with an error status. The endpoint answers HTTP 200 with a document that
  does nothing: no ``call_records`` row, no stream token, no state change.
* A duplicate must not receive a stream token, because the sender of a replay is
  by definition not the carrier, and a token in the response would hand them the
  credential the media stream checks.

The cost, stated plainly: if our first response was lost in transit and the
carrier retries with an identical signature, the retry gets the empty document
and that call ends. That is preferred over minting credentials for a replay.

Redis down: **fail open by default** (``WEBHOOK_REPLAY_FAIL_CLOSED`` flips it).
The signature is still verified and the Telnyx timestamp window still applies;
only the "seen before" memory is lost. Fail closed would refuse every call the
moment Redis blinks, which is a worse outage than a briefly weaker replay
defence. Logged at error level on every webhook so it cannot go unnoticed.

If a handler fails after the claim (a database error, say) the claim is released
so the carrier's retry of the same event is not mistaken for a replay.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator  # noqa: TC003 (FastAPI resolves the annotation)
from enum import StrEnum
from typing import Any

import structlog
from fastapi import Request  # noqa: TC002 (FastAPI injects by annotation)

import app.db.redis as redis_module
from app.core.config import settings

logger = structlog.get_logger()

_KEY_PREFIX = "webhook-replay:"
_STATE_KEY = "webhook_replay_claim_key"
_STATE_DUPLICATE = "webhook_duplicate"


class ReplayClaim(StrEnum):
    FRESH = "fresh"
    DUPLICATE = "duplicate"
    STORE_UNAVAILABLE = "store_unavailable"


async def _store() -> Any:
    """The Redis client claims are written to. One seam, so tests can replace it."""
    return await redis_module.get_redis()


def telnyx_replay_key(signature: str, timestamp: str) -> str:
    return _digest("telnyx", signature, timestamp)


def twilio_replay_key(signature: str, url: str, params: dict[str, Any]) -> str:
    """Signature, full URL and sorted parameters, plus CallSid/MessageSid and
    CallStatus named explicitly (they are in the parameters too; saying so keeps
    the intent visible)."""
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sid = str(params.get("CallSid") or params.get("MessageSid") or "")
    status = str(params.get("CallStatus") or "")
    return _digest("twilio", signature, url, sorted_params, sid, status)


def _digest(*parts: str) -> str:
    return _KEY_PREFIX + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def telnyx_ttl_seconds() -> int:
    # A timestamp may be up to MAX_AGE in the past or the future, so a captured
    # webhook stays acceptable for up to twice the window.
    return settings.TELNYX_WEBHOOK_MAX_AGE_SECONDS * 2


def twilio_ttl_seconds() -> int:
    return settings.TWILIO_WEBHOOK_REPLAY_TTL_SECONDS


async def claim_webhook(key: str, *, ttl_seconds: int) -> ReplayClaim:
    """Record ``key``. Call only after the signature has verified."""
    try:
        redis = await _store()
        claimed = await redis.set(key, "1", nx=True, ex=ttl_seconds)
    except Exception as exc:
        logger.error(  # noqa: TRY400
            "webhook_replay_store_unavailable",
            error=type(exc).__name__,
            fail_closed=settings.WEBHOOK_REPLAY_FAIL_CLOSED,
            detail=(
                "Redis could not record this signed webhook, so replays cannot "
                "be detected right now. "
                + (
                    "Refusing it (WEBHOOK_REPLAY_FAIL_CLOSED)."
                    if settings.WEBHOOK_REPLAY_FAIL_CLOSED
                    else "Accepting it: the signature is still verified."
                )
            ),
        )
        return ReplayClaim.STORE_UNAVAILABLE
    return ReplayClaim.FRESH if claimed else ReplayClaim.DUPLICATE


def record_claim(request: Request, key: str, claim: ReplayClaim) -> None:
    if claim is ReplayClaim.DUPLICATE:
        setattr(request.state, _STATE_DUPLICATE, True)
        logger.warning("webhook_replay_duplicate", path=request.url.path)
    elif claim is ReplayClaim.FRESH:
        setattr(request.state, _STATE_KEY, key)


def webhook_is_duplicate(request: Request) -> bool:
    """True when this (already verified) request was accepted before."""
    return bool(getattr(request.state, _STATE_DUPLICATE, False))


async def release_claim_on_error(request: Request) -> AsyncIterator[None]:
    """Route dependency: free the claim if the handler raised.

    Otherwise a database blip after the claim would turn the carrier's retry of
    the same event into a "replay" and drop the call.
    """
    try:
        yield
    except BaseException:
        key = getattr(request.state, _STATE_KEY, None)
        if key:
            try:
                redis = await _store()
                await redis.delete(key)
            except Exception as exc:
                logger.error("webhook_replay_release_failed", error=type(exc).__name__)  # noqa: TRY400
        raise


# What a duplicate answers: HTTP 200, a document that does nothing.
EMPTY_DOCUMENT = '<?xml version="1.0" encoding="UTF-8"?>\n<Response></Response>'
