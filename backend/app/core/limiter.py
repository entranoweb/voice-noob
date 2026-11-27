"""Rate limiter configuration.

Counts live in Redis, so a limit is one limit for the deployment and not one per
worker process. They used to live in each process's memory: with four workers a
"10/minute" endpoint served forty a minute, more with more workers, and the
number written next to each route described nothing that was enforced.

When Redis cannot be reached the limiter does not stop limiting and does not
start refusing everything. It falls back to counting in this process's memory,
with the same limits, and goes back to Redis when Redis answers again (it
re-checks on a backoff of 1 to 8 seconds). During an outage a limit is therefore
per worker again: looser than configured by a factor of the worker count, never
absent. That is a deliberate difference from the single-use stream tokens
(``app.core.stream_token_use``), which refuse outright without Redis: there the
loss is one phone call, here it would be every request to the API, the login
page included. The fallback is logged once per outage by the ``slowapi`` logger
("Rate limit storage unreachable").

``RATE_LIMIT_STORAGE_URI`` overrides where counts are kept. ``memory://`` is the
old per-process behaviour, for a single-process deployment with no Redis and for
the test suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

if TYPE_CHECKING:
    from fastapi import Request

MEMORY_STORAGE_URI = "memory://"
KEY_PREFIX = "ratelimit"

# The limiter's Redis client is synchronous and is called on the event loop, so
# an unreachable Redis must fail in a fraction of a second rather than hold
# every request for the default socket timeout.
_REDIS_TIMEOUT_SECONDS = 0.25


def user_or_ip_key(request: Request) -> str:
    """Rate-limit key: the signed-in user when the bearer token verifies, else the address.

    Used for routes that spend the account's own provider key, where one user
    behind a shared address (an office, a proxy) must not use up everyone's
    allowance and one user must not get a fresh allowance per address. The token
    is checked with the same secret and algorithm as authentication, so a forged
    or expired ``sub`` cannot choose its own bucket: it lands in the address
    bucket. The route's own dependency still rejects it with 401 before the
    limit is counted.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        except jwt.PyJWTError:
            payload = {}
        sub = payload.get("sub")
        if sub is not None:
            return f"user:{sub}"
    return get_remote_address(request)


def rate_limit_storage_uri() -> str:
    """Where rate-limit counts are kept: the override if set, else Redis."""
    configured = (settings.RATE_LIMIT_STORAGE_URI or "").strip()
    if configured:
        return configured
    return str(settings.REDIS_URL)


def build_limiter(storage_uri: str | None = None) -> Limiter:
    """A limiter that counts in ``storage_uri`` (default: the configured store).

    Every worker process builds one of these from the same settings, which is
    what makes them share counts.
    """
    uri = storage_uri or rate_limit_storage_uri()
    options: dict[str, Any] = {}
    if not uri.startswith(MEMORY_STORAGE_URI):
        options = {
            "socket_connect_timeout": _REDIS_TIMEOUT_SECONDS,
            "socket_timeout": _REDIS_TIMEOUT_SECONDS,
        }
    return Limiter(
        key_func=get_remote_address,
        storage_uri=uri,
        storage_options=options,
        key_prefix=KEY_PREFIX,
        # Same limits, counted in this process, while the store is unreachable.
        in_memory_fallback_enabled=True,
    )


limiter = build_limiter()


# Limit strings for the /testing routes that spend the workspace's model key.
# Read from settings on each request, through this module's own reference, so a
# changed setting applies without a restart of the decorator and a test that
# patches another module's ``settings`` does not break the limit string.
def testing_run_limit() -> str:
    return f"{settings.TESTING_RUN_RATE_LIMIT_PER_MINUTE}/minute"


def testing_check_limit() -> str:
    return f"{settings.TESTING_CHECK_RATE_LIMIT_PER_MINUTE}/minute"


def testing_run_all_limit() -> str:
    return f"{settings.TESTING_RUN_ALL_RATE_LIMIT_PER_MINUTE}/minute"
