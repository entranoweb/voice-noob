"""Call registry for tracking active calls.

Redis-backed registry for tracking active voice calls.
Supports graceful shutdown by tracking call state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from app.core.config import settings
from app.db.redis import get_redis

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger()

# Redis key prefix for call registry
CALL_REGISTRY_PREFIX = "voicenoob:calls:"
SHUTDOWN_FLAG_KEY = "voicenoob:shutdown"

# Module-level state
_shutdown_flag = False
_registry_lock = asyncio.Lock()


@dataclass
class CallInfo:
    """Information about an active call."""

    call_id: str
    agent_id: str
    started_at: float
    phone_number: str | None = None
    metadata: dict[str, str] | None = None


async def register_call(
    call_id: str,
    agent_id: str,
    phone_number: str | None = None,
    metadata: dict[str, str] | None = None,
) -> bool:
    """Register a new active call.

    Args:
        call_id: Unique call identifier.
        agent_id: Agent handling the call.
        phone_number: Optional phone number.
        metadata: Optional additional metadata.

    Returns:
        True if registered successfully, False otherwise.
    """
    if not settings.ENABLE_CALL_REGISTRY:
        return True

    try:
        redis: Redis = await get_redis()
        key = f"{CALL_REGISTRY_PREFIX}{call_id}"

        call_data = {
            "call_id": call_id,
            "agent_id": agent_id,
            "started_at": str(time.time()),
            "phone_number": phone_number or "",
        }

        if metadata:
            for k, v in metadata.items():
                call_data[f"meta:{k}"] = v

        async with _registry_lock:
            await redis.hset(key, mapping=call_data)  # type: ignore[misc]
            await redis.expire(key, settings.CALL_REGISTRY_TTL)

        logger.info(
            "call_registered",
            call_id=call_id,
            agent_id=agent_id,
            ttl=settings.CALL_REGISTRY_TTL,
        )
        return True

    except Exception:
        logger.exception("call_register_failed", call_id=call_id)
        return False


async def unregister_call(call_id: str) -> bool:
    """Unregister a call when it ends.

    Args:
        call_id: Call identifier to remove.

    Returns:
        True if unregistered successfully, False otherwise.
    """
    if not settings.ENABLE_CALL_REGISTRY:
        return True

    try:
        redis: Redis = await get_redis()
        key = f"{CALL_REGISTRY_PREFIX}{call_id}"

        async with _registry_lock:
            deleted = await redis.delete(key)

        if deleted:
            logger.info("call_unregistered", call_id=call_id)
        else:
            logger.warning("call_not_found", call_id=call_id)

        return True

    except Exception:
        logger.exception("call_unregister_failed", call_id=call_id)
        return False


async def get_active_calls() -> list[CallInfo]:
    """Get all active calls.

    Returns:
        List of CallInfo objects for active calls.
    """
    if not settings.ENABLE_CALL_REGISTRY:
        return []

    try:
        redis: Redis = await get_redis()
        pattern = f"{CALL_REGISTRY_PREFIX}*"

        calls: list[CallInfo] = []
        async for key in redis.scan_iter(match=pattern):
            data = await redis.hgetall(key)  # type: ignore[misc]
            if data:
                metadata = {
                    k.replace("meta:", ""): v for k, v in data.items() if k.startswith("meta:")
                }

                calls.append(
                    CallInfo(
                        call_id=data.get("call_id", ""),
                        agent_id=data.get("agent_id", ""),
                        started_at=float(data.get("started_at", 0)),
                        phone_number=data.get("phone_number") or None,
                        metadata=metadata or None,
                    )
                )

        return calls

    except Exception:
        logger.exception("get_active_calls_failed")
        return []


async def get_call_count() -> int:
    """Get count of active calls.

    Returns:
        Number of active calls.
    """
    if not settings.ENABLE_CALL_REGISTRY:
        return 0

    try:
        redis: Redis = await get_redis()
        pattern = f"{CALL_REGISTRY_PREFIX}*"

        count = 0
        async for _ in redis.scan_iter(match=pattern):
            count += 1

        return count

    except Exception:
        logger.exception("get_call_count_failed")
        return 0


async def set_shutting_down(shutting_down: bool = True) -> None:
    """Set shutdown flag in Redis.

    Args:
        shutting_down: Whether shutdown is in progress.
    """
    global _shutdown_flag
    _shutdown_flag = shutting_down

    if not settings.ENABLE_CALL_REGISTRY:
        return

    try:
        redis: Redis = await get_redis()
        if shutting_down:
            await redis.set(SHUTDOWN_FLAG_KEY, "1", ex=settings.SHUTDOWN_DRAIN_TIMEOUT)
            logger.info("shutdown_flag_set")
        else:
            await redis.delete(SHUTDOWN_FLAG_KEY)
            logger.info("shutdown_flag_cleared")

    except Exception:
        logger.exception("shutdown_flag_update_failed")


def is_shutting_down() -> bool:
    """Check if shutdown is in progress.

    Returns:
        True if shutdown is in progress.
    """
    return _shutdown_flag


async def wait_for_calls_to_drain(drain_timeout: int | None = None) -> bool:
    """Wait for all active calls to complete.

    Args:
        drain_timeout: Max seconds to wait (defaults to SHUTDOWN_DRAIN_TIMEOUT).

    Returns:
        True if all calls drained, False if timeout reached.
    """
    if not settings.ENABLE_CALL_REGISTRY:
        return True

    timeout = drain_timeout or settings.SHUTDOWN_DRAIN_TIMEOUT
    start_time = time.time()

    logger.info("drain_started", timeout=timeout)

    while time.time() - start_time < timeout:
        count = await get_call_count()
        if count == 0:
            logger.info("drain_complete", elapsed=time.time() - start_time)
            return True

        logger.info("drain_waiting", active_calls=count)
        await asyncio.sleep(1)

    remaining = await get_call_count()
    logger.warning(
        "drain_timeout",
        timeout=timeout,
        remaining_calls=remaining,
    )
    return False


__all__ = [
    "CallInfo",
    "get_active_calls",
    "get_call_count",
    "is_shutting_down",
    "register_call",
    "set_shutting_down",
    "unregister_call",
    "wait_for_calls_to_drain",
]
