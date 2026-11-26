"""A per-agent hourly ceiling on the OpenAI keys minted for the public widget.

``POST /embed/{id}/token`` is public and each key it mints can open Realtime
sessions billed to the agent's owner. The per-IP limit on the route stops one
address; this stops the sum: no more than ``EMBED_KEYS_PER_AGENT_PER_HOUR`` keys
for one agent in one clock hour, whoever asks, and no more than
``EMBED_KEYS_PER_IP_PER_AGENT_PER_HOUR`` of those for one client address, so one
address cannot use up an agent's whole allowance (the route's 10/minute per IP
would otherwise allow 600 an hour). The address share is checked first.

Counts live in Redis (``INCR`` on a key per agent per hour, expiring with the
hour), so the limit is one limit for the deployment. It fails closed: when Redis
cannot be reached the mint is refused with 503, because the alternative is that a
Redis outage removes the only cap on spend. The general rate limiter falls back to
per-process memory, which is right for a login page and wrong here. Same reasoning
as ``app.core.stream_token_use``.
"""

from __future__ import annotations

import hashlib
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
    scope: str = "agent"  # "agent" or "ip": which allowance ran out
    # The exact counter keys this reservation incremented, for `release_embed_key`.
    # Releasing them, not a recomputed key, keeps a refund from landing in the
    # next hour's counter when it straddles a boundary.
    keys: tuple[str, ...] = ()


async def _store() -> Any:
    """The Redis client counts are kept in. One seam, so tests can replace it."""
    return await redis_module.get_redis()


def _window(now: float) -> tuple[int, int]:
    """The hour bucket ``now`` falls in, and the seconds until it ends."""
    bucket = int(now // WINDOW_SECONDS)
    return bucket, int((bucket + 1) * WINDOW_SECONDS - now) + 1


async def _count(redis: Any, key: str) -> int:
    """Add one to ``key`` and make sure it expires, in one atomic step.

    ``EXPIRE ... NX`` only sets a TTL on a key that has none, inside the same
    MULTI as the ``INCR``, so a counter can never exist without one.
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, WINDOW_SECONDS + 60, nx=True)
        results = await pipe.execute()
    return int(results[0])


async def reserve_embed_key(
    agent_id: str, client_ip: str, *, now: float | None = None
) -> MintDecision:
    """Count one mint for ``agent_id`` from ``client_ip``, or say why it may not happen."""
    agent_limit = int(settings.EMBED_KEYS_PER_AGENT_PER_HOUR)
    ip_limit = int(settings.EMBED_KEYS_PER_IP_PER_AGENT_PER_HOUR)
    bucket, remaining = _window(time.time() if now is None else now)
    ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:32]
    ip_key = f"{_KEY_PREFIX}ip:{agent_id}:{ip_hash}:{bucket}"
    agent_key = f"{_KEY_PREFIX}{agent_id}:{bucket}"
    taken: list[str] = []
    try:
        redis = await _store()
        for scope, key, limit in (("ip", ip_key, ip_limit), ("agent", agent_key, agent_limit)):
            count = await _count(redis, key)
            taken.append(key)
            if count > limit:
                # A refused attempt is not a mint: give back everything taken so
                # refusals do not extend the lockout by themselves.
                await release_embed_key(MintDecision(True, keys=tuple(taken)))
                return MintDecision(False, retry_after=remaining, limit=limit, scope=scope)
    except Exception as exc:
        logger.error(  # noqa: TRY400
            "embed_key_limit_store_unavailable",
            error=type(exc).__name__,
            detail="Redis could not count this mint, so no key is minted: failing closed.",
        )
        return MintDecision(False, store_unavailable=True, retry_after=STORE_RETRY_SECONDS)
    return MintDecision(True, limit=agent_limit, keys=tuple(taken))


async def release_embed_key(reservation: MintDecision) -> None:
    """Give back the counts of a mint that did not happen (the provider refused)."""
    try:
        redis = await _store()
        for key in reservation.keys:
            await redis.decr(key)
    except Exception as exc:
        logger.warning("embed_key_limit_release_failed", error=type(exc).__name__)


__all__ = ["MintDecision", "release_embed_key", "reserve_embed_key"]
