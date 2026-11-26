"""A per-agent hourly ceiling on the OpenAI keys minted for the public widget.

``POST /embed/{id}/token`` is public and each key it mints can open Realtime
sessions billed to the agent's owner. The per-IP limit on the route stops one
address; this stops the sum: no more than ``EMBED_KEYS_PER_AGENT_PER_HOUR`` keys
for one agent in one clock hour, whoever asks.

Counts live in Redis (``INCR`` on a key per agent per hour, expiring with the
hour), so the limit is one limit for the deployment. It fails closed: when Redis
cannot be reached the mint is refused with 503, because the alternative is that a
Redis outage removes the only cap on spend. The general rate limiter falls back to
per-process memory, which is right for a login page and wrong here. Same reasoning
as ``app.core.stream_token_use``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog

import app.db.redis as redis_module
from app.core.config import settings

logger = structlog.get_logger()

WINDOW_SECONDS = 3600
_KEY_PREFIX = "embed-key-mint:"
STORE_RETRY_SECONDS = 15


@dataclass(frozen=True)
class MintDecision:
    allowed: bool
    store_unavailable: bool = False
    retry_after: int = 0
    limit: int = 0


async def _store() -> Any:
    """The Redis client counts are kept in. One seam, so tests can replace it."""
    return await redis_module.get_redis()


def _window(now: float) -> tuple[int, int]:
    """The hour bucket ``now`` falls in, and the seconds until it ends."""
    bucket = int(now // WINDOW_SECONDS)
    return bucket, int((bucket + 1) * WINDOW_SECONDS - now) + 1


async def reserve_embed_key(agent_id: str, *, now: float | None = None) -> MintDecision:
    """Count one mint for ``agent_id`` this hour, or say why it may not happen."""
    limit = int(settings.EMBED_KEYS_PER_AGENT_PER_HOUR)
    bucket, remaining = _window(time.time() if now is None else now)
    key = f"{_KEY_PREFIX}{agent_id}:{bucket}"
    try:
        redis = await _store()
        count = int(await redis.incr(key))
        if count == 1:
            await redis.expire(key, WINDOW_SECONDS + 60)
    except Exception as exc:
        logger.error(  # noqa: TRY400
            "embed_key_limit_store_unavailable",
            error=type(exc).__name__,
            detail="Redis could not count this mint, so no key is minted: failing closed.",
        )
        return MintDecision(False, store_unavailable=True, retry_after=STORE_RETRY_SECONDS)
    if count > limit:
        # A refused attempt is not a mint: give the count back so refusals do
        # not extend the lockout by themselves.
        await release_embed_key(agent_id, now=now)
        return MintDecision(False, retry_after=remaining, limit=limit)
    return MintDecision(True, limit=limit)


async def release_embed_key(agent_id: str, *, now: float | None = None) -> None:
    """Give back a count for a mint that did not happen (the provider refused)."""
    bucket, _ = _window(time.time() if now is None else now)
    try:
        redis = await _store()
        await redis.decr(f"{_KEY_PREFIX}{agent_id}:{bucket}")
    except Exception as exc:
        logger.warning("embed_key_limit_release_failed", error=type(exc).__name__)


__all__ = ["MintDecision", "release_embed_key", "reserve_embed_key"]
