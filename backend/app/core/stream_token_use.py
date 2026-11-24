"""Make a stream token good for one connection.

``app.core.stream_token`` proves a token was minted by the webhook that answered
a call. On its own that token stays valid until it expires, so anything that sees
it inside those two minutes can open a second stream with it. This module is the
shared state that closes that: the first socket to present a token claims it in
Redis with ``SET NX``, and every later presentation finds the claim and is
refused.

It fails closed. When Redis cannot be reached the claim cannot be made, and a
stream that cannot be shown to be the first use of its token is not admitted. A
dropped call is the cost of a Redis outage here; the alternative is that a Redis
outage silently reopens the replay window, and an attacker who can cause one gets
the hole back. The log line says which happened, because "refused: replayed" and
"refused: Redis is down" need different people.

Kept apart from ``stream_token`` on purpose: that module is standard library only
and must stay importable with nothing running. This one needs Redis.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

import structlog

import app.db.redis as redis_module
from app.core.stream_token import DEFAULT_TTL_SECONDS

logger = structlog.get_logger()

_KEY_PREFIX = "stream-token-used:"


class TokenClaim(StrEnum):
    """What happened when a socket tried to claim its token. The value is logged."""

    CLAIMED = "claimed"
    REPLAYED = "replayed"
    STORE_UNAVAILABLE = "store_unavailable"

    @property
    def ok(self) -> bool:
        return self is TokenClaim.CLAIMED


async def _store() -> Any:
    """The Redis client claims are written to. One seam, so tests can replace it."""
    return await redis_module.get_redis()


def _claim_key(token: str) -> str:
    # The token itself never becomes a Redis key: a key listing would otherwise
    # hand out live credentials for as long as they have left to run.
    return _KEY_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


async def claim_stream_token(token: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> TokenClaim:
    """Claim ``token`` for this connection, once.

    Call only after the token has verified. The claim outlives the token (the
    token cannot be valid for longer than its mint-time lifetime, which is the
    default here), so there is no window in which the claim has expired and the
    token has not.
    """
    try:
        redis = await _store()
        claimed = await redis.set(_claim_key(token), "1", nx=True, ex=ttl_seconds)
    except Exception as exc:
        # Not logger.exception: the reason is in the fields, and a traceback on
        # every refused call during an outage buries the one line that matters.
        logger.error(  # noqa: TRY400
            "stream_token_claim_store_unavailable",
            error=type(exc).__name__,
            detail=(
                "Redis could not record this stream token as used, so the stream "
                "is refused: a call that cannot be authenticated as the first use "
                "of its token does not connect."
            ),
        )
        return TokenClaim.STORE_UNAVAILABLE
    return TokenClaim.CLAIMED if claimed else TokenClaim.REPLAYED


__all__ = ["TokenClaim", "claim_stream_token"]
