"""Redis connection management with connection pooling."""

import asyncio
import logging
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from app.core.config import settings
from app.core.log_redaction import describe_exception

if TYPE_CHECKING:
    from redis.asyncio import Redis
else:
    Redis = object  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

redis_client: "Redis | None" = None
redis_pool: ConnectionPool | None = None
_redis_lock = asyncio.Lock()


async def get_redis() -> "Redis":
    """Get Redis client instance with connection pooling."""
    global redis_client, redis_pool

    # Use lock to prevent race condition during initialization
    async with _redis_lock:
        if redis_client is None:
            try:
                # Create connection pool with production settings
                redis_pool = ConnectionPool.from_url(
                    str(settings.REDIS_URL),
                    encoding="utf-8",
                    decode_responses=True,
                    max_connections=100,  # Production: increased from 50 for concurrent agents
                    socket_timeout=10.0,  # Production: increased from 5s for slow networks
                    socket_connect_timeout=5.0,  # Connection timeout
                    socket_keepalive=True,  # Enable TCP keepalive
                    retry_on_timeout=True,  # Retry on timeout
                    health_check_interval=30,  # Health check every 30 seconds
                )

                # Create Redis client with retry logic
                retry = Retry(ExponentialBackoff(), retries=3)
                redis_client = aioredis.Redis(
                    connection_pool=redis_pool,
                    retry=retry,
                    retry_on_error=[
                        aioredis.ConnectionError,
                        aioredis.TimeoutError,
                    ],
                )

                # Test connection
                await redis_client.ping()
                logger.info("Redis connection pool initialized successfully")

            except Exception as exc:
                logger.error("Failed to initialize Redis connection: %s", describe_exception(exc))  # noqa: TRY400
                raise

    return redis_client


async def close_redis() -> None:
    """Close Redis connection and connection pool."""
    global redis_client, redis_pool

    if redis_client:
        try:
            await redis_client.close()
            logger.info("Redis client closed")
        except Exception as exc:
            logger.error("Error closing Redis client: %s", describe_exception(exc))  # noqa: TRY400
        finally:
            redis_client = None

    if redis_pool:
        try:
            await redis_pool.disconnect()
            logger.info("Redis connection pool closed")
        except Exception as exc:
            logger.error("Error closing Redis pool: %s", describe_exception(exc))  # noqa: TRY400
        finally:
            redis_pool = None
