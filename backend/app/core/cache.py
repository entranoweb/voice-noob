"""Redis caching utilities and decorators."""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

# Import the module rather than the function: a bound reference is fixed at
# import time, so patching app.db.redis.get_redis (as the test fixtures do)
# would never reach this module and the cache would hit a real Redis.
from app.db import redis as redis_module

logger = logging.getLogger(__name__)

# Type variables for generic function signatures
P = ParamSpec("P")
T = TypeVar("T")


def _generate_cache_key(prefix: str, *args: Any, **kwargs: Any) -> str:
    """Generate a unique cache key from function arguments.

    Args:
        prefix: Cache key prefix (usually function name)
        *args: Positional arguments
        **kwargs: Keyword arguments

    Returns:
        Unique cache key string
    """
    # Create a string representation of args and kwargs
    key_data = {
        "args": [str(arg) for arg in args if not callable(arg)],
        "kwargs": {k: str(v) for k, v in kwargs.items() if not callable(v)},
    }

    # Generate hash for cache key (MD5 is safe for cache keys, not crypto)
    key_string = json.dumps(key_data, sort_keys=True)
    key_hash = hashlib.md5(key_string.encode()).hexdigest()  # noqa: S324

    return f"{prefix}:{key_hash}"


async def cache_get(key: str) -> Any | None:
    """Get value from cache.

    Args:
        key: Cache key

    Returns:
        Cached value or None if not found or error occurred
    """
    try:
        redis = await redis_module.get_redis()
        value = await redis.get(key)

        if value is not None:
            logger.debug("Cache hit: %s", key)
            return json.loads(value)

        logger.debug("Cache miss: %s", key)
        return None

    except Exception:
        logger.exception("Error getting from cache key '%s'", key)
        return None


async def cache_set(key: str, value: Any, ttl: int = 300) -> bool:
    """Set value in cache with TTL.

    Args:
        key: Cache key
        value: Value to cache (must be JSON serializable)
        ttl: Time to live in seconds (default: 300)

    Returns:
        True if successful, False otherwise
    """
    try:
        redis = await redis_module.get_redis()
        serialized = json.dumps(value, default=str)
        await redis.setex(key, ttl, serialized)
        logger.debug("Cache set: %s (TTL: %ss)", key, ttl)
        return True

    except Exception:
        logger.exception("Error setting cache key '%s'", key)
        return False


async def cache_delete(key: str) -> bool:
    """Delete value from cache.

    Args:
        key: Cache key

    Returns:
        True if successful, False otherwise
    """
    try:
        redis = await redis_module.get_redis()
        await redis.delete(key)
        logger.debug("Cache deleted: %s", key)
        return True

    except Exception:
        logger.exception("Error deleting cache key '%s'", key)
        return False


async def cache_invalidate(pattern: str) -> int:
    """Invalidate all cache keys matching a pattern.

    Args:
        pattern: Redis key pattern (e.g., "crm:stats:*")

    Returns:
        Number of keys deleted
    """
    try:
        redis = await redis_module.get_redis()
        keys = []

        # Scan for keys matching pattern
        async for key in redis.scan_iter(match=pattern):
            keys.append(key)

        if keys:
            deleted: int = await redis.delete(*keys)
            logger.info("Cache invalidated: %s keys matching '%s'", deleted, pattern)
            return deleted

        return 0

    except Exception:
        logger.exception("Error invalidating cache pattern '%s'", pattern)
        return 0


def cached(
    prefix: str,
    ttl: int = 300,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator to cache async function results.

    Args:
        prefix: Cache key prefix
        ttl: Time to live in seconds (default: 300)

    Returns:
        Decorated function with caching

    Example:
        @cached(prefix="user:profile", ttl=60)
        async def get_user_profile(user_id: int) -> dict:
            # Expensive operation
            return {"user_id": user_id, "name": "John"}
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Generate cache key
            cache_key = _generate_cache_key(prefix, *args, **kwargs)

            # Try to get from cache
            cached_value = await cache_get(cache_key)
            if cached_value is not None:
                return cached_value  # type: ignore[no-any-return]

            # Execute function
            result = await func(*args, **kwargs)

            # Cache the result
            await cache_set(cache_key, result, ttl)

            return result

        return wrapper

    return decorator


async def cache_stats() -> dict[str, Any]:
    """Get Redis cache statistics.

    Returns:
        Dictionary with cache stats
    """
    try:
        redis = await redis_module.get_redis()
        info = await redis.info("stats")

        return {
            "total_connections_received": info.get("total_connections_received", 0),
            "total_commands_processed": info.get("total_commands_processed", 0),
            "keyspace_hits": info.get("keyspace_hits", 0),
            "keyspace_misses": info.get("keyspace_misses", 0),
            "hit_rate": (
                info.get("keyspace_hits", 0)
                / max(
                    info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0),
                    1,
                )
                * 100
            ),
        }

    except Exception:
        logger.exception("Error getting cache stats")
        return {}
