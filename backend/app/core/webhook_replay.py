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

A genuine retry of the same event (identical signature, our first response lost)
must still be answered, or a live call drops. So the first answer document is
kept in Redis for the stream token's lifetime (two minutes) beside the replay key
and an identical request inside that window is given that same document. The
token in it is single-use at the socket (``stream_token_use``), so handing the
same document back gains a replayer nothing once the real stream has connected.
After the window a duplicate gets the empty document.

The key is built from what was *signed* (Telnyx: timestamp and body; Twilio: full
URL and sorted parameters), never from the signature string: base64 has
spellings that decode to the same bytes, and a key on the header would let one
signed request be replayed under a new key.

It holds a hash only: no phone number, name, or any other personal data. Erasure
and retention therefore leave it, deliberately. For Twilio, whose signatures carry
no timestamp, it must outlive the call row (which erasure and retention delete),
so its TTL is the longest retention period plus a day.

Redis down: **fail open by default** (``WEBHOOK_REPLAY_FAIL_CLOSED`` flips it).
The signature is still verified and the Telnyx timestamp window still applies;
only the "seen before" memory is lost. Fail closed would refuse every call the
moment Redis blinks, which is a worse outage than a briefly weaker replay
defence. Logged at error level on every webhook so it cannot go unnoticed.

If a handler fails after the claim (a database error, say) the claim is released
so the carrier's retry of the same event is not mistaken for a replay.
"""

from __future__ import annotations

import asyncio
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
_DOC_PREFIX = "webhook-replay-doc:"
_ANSWER_PREFIX = "webhook-answer-issued:"
_STATE_KEY = "webhook_replay_claim_key"
_STATE_FRESH = "webhook_replay_fresh"
_STATE_DUPLICATE = "webhook_duplicate"
_STATE_COMMITTED = "webhook_replay_committed"
# The answer document is kept for as long as the stream token in it is valid.
DOCUMENT_TTL_SECONDS = 120
# Replay-guard Redis calls must not outlast a carrier's own timeout.
STORE_TIMEOUT_SECONDS = 0.5
_DOC_POLLS = 5


class ReplayClaim(StrEnum):
    FRESH = "fresh"
    DUPLICATE = "duplicate"
    STORE_UNAVAILABLE = "store_unavailable"


async def _store() -> Any:
    """The Redis client claims are written to. One seam, so tests can replace it."""
    return await redis_module.get_redis()


def telnyx_replay_key(timestamp: str, body: bytes) -> str:
    """Keyed on the signed content (``{timestamp}|{body}``), not the signature."""
    return _digest("telnyx", timestamp, hashlib.sha256(body).hexdigest())


def twilio_replay_key(url: str, params: dict[str, Any]) -> str:
    """Keyed on the signed content: full URL and the sorted parameters (which
    carry CallSid/MessageSid and CallStatus), not the signature."""
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return _digest("twilio", url, sorted_params)


def _digest(*parts: str) -> str:
    return _KEY_PREFIX + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def telnyx_ttl_seconds() -> int:
    # A timestamp may be up to MAX_AGE in the past or the future, so a captured
    # webhook stays acceptable for up to twice the window.
    return settings.TELNYX_WEBHOOK_MAX_AGE_SECONDS * 2


def twilio_ttl_seconds() -> int:
    return settings.TWILIO_WEBHOOK_REPLAY_TTL_SECONDS


def answer_issued_key(provider: str, provider_call_id: str) -> str:
    return _digest("answer", provider, provider_call_id).replace(_KEY_PREFIX, _ANSWER_PREFIX, 1)


async def _set_nx(key: str, ttl_seconds: int) -> Any:
    redis = await _store()
    return await redis.set(key, "1", nx=True, ex=ttl_seconds)


async def claim_webhook(key: str, *, ttl_seconds: int) -> ReplayClaim:
    """Record ``key``. Call only after the signature has verified."""
    try:
        claimed = await asyncio.wait_for(_set_nx(key, ttl_seconds), STORE_TIMEOUT_SECONDS)
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
        setattr(request.state, _STATE_KEY, key)
        logger.warning("webhook_replay_duplicate", path=request.url.path)
    elif claim is ReplayClaim.FRESH:
        setattr(request.state, _STATE_KEY, key)
        setattr(request.state, _STATE_FRESH, True)


def mark_committed(request: Request) -> None:
    """Call right after the handler's database commit: from then on a failure
    must not release the claim, because the event has been acted on."""
    setattr(request.state, _STATE_COMMITTED, True)


async def store_answer_document(request: Request, document: str) -> None:
    """Keep the first answer so an identical retry inside the token lifetime gets it."""
    key = getattr(request.state, _STATE_KEY, None)
    if not key or not getattr(request.state, _STATE_FRESH, False):
        return
    try:
        redis = await _store()
        await asyncio.wait_for(
            redis.set(_DOC_PREFIX + key, document, ex=DOCUMENT_TTL_SECONDS), STORE_TIMEOUT_SECONDS
        )
    except Exception as exc:
        logger.error("webhook_replay_document_not_stored", error=type(exc).__name__)  # noqa: TRY400


async def stored_answer_document(request: Request) -> str | None:
    """The first answer to this exact request, if still inside its window.

    The first attempt may still be running when its retry arrives, so wait a
    moment for it rather than answer the retry with nothing.
    """
    key = getattr(request.state, _STATE_KEY, None)
    if not key:
        return None
    for attempt in range(_DOC_POLLS):
        try:
            redis = await _store()
            found = await asyncio.wait_for(redis.get(_DOC_PREFIX + key), STORE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.error("webhook_replay_document_unreadable", error=type(exc).__name__)  # noqa: TRY400
            return None
        if found:
            return str(found)
        if attempt < _DOC_POLLS - 1:
            await asyncio.sleep(0.2)
    return None


async def claim_answer_issued(
    provider: str, provider_call_id: str, ttl_seconds: int
) -> ReplayClaim:
    """Row-less answer claim: ``SET NX`` a long-lived key for this call id."""
    return await claim_webhook(
        answer_issued_key(provider, provider_call_id), ttl_seconds=ttl_seconds
    )


async def answer_already_issued(provider: str, provider_call_id: str) -> bool:
    """Whether a row-less answer already issued a token for this call. Fails open."""
    try:
        redis = await _store()
        return bool(
            await asyncio.wait_for(
                redis.exists(answer_issued_key(provider, provider_call_id)), STORE_TIMEOUT_SECONDS
            )
        )
    except Exception as exc:
        logger.error("webhook_answer_key_unreadable", error=type(exc).__name__)  # noqa: TRY400
        return False


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
        fresh = getattr(request.state, _STATE_FRESH, False)
        committed = getattr(request.state, _STATE_COMMITTED, False)
        if key and fresh and not committed:
            try:
                redis = await _store()
                await asyncio.wait_for(redis.delete(key), STORE_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.error("webhook_replay_release_failed", error=type(exc).__name__)  # noqa: TRY400
        raise


# What a duplicate answers: HTTP 200, a document that does nothing.
EMPTY_DOCUMENT = '<?xml version="1.0" encoding="UTF-8"?>\n<Response></Response>'
