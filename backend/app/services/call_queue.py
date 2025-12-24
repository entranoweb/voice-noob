"""Call queue for capacity management.

Redis-backed queue for managing call capacity.
Feature-flagged via ENABLE_CALL_QUEUE (default OFF).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import structlog

from app.core.config import settings
from app.db.redis import get_redis

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger()

# Redis key prefixes
CALL_QUEUE_KEY = "voicenoob:queue:calls"
QUEUE_STATS_KEY = "voicenoob:queue:stats"

# Queue lock for thread safety
_queue_lock = asyncio.Lock()


@dataclass
class QueuedCall:
    """Represents a call waiting in queue."""

    call_id: str
    agent_id: str
    phone_number: str | None
    queued_at: float
    priority: int = 0  # Higher = more urgent
    metadata: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueuedCall:
        """Create from dictionary."""
        queued_at_val = data.get("queued_at", 0)
        priority_val = data.get("priority", 0)
        metadata_val = data.get("metadata")

        return cls(
            call_id=str(data.get("call_id", "")),
            agent_id=str(data.get("agent_id", "")),
            phone_number=str(data["phone_number"]) if data.get("phone_number") else None,
            queued_at=float(queued_at_val) if queued_at_val is not None else 0.0,
            priority=int(priority_val) if priority_val is not None else 0,
            metadata=metadata_val if isinstance(metadata_val, dict) else None,
        )


async def enqueue_call(
    call_id: str,
    agent_id: str,
    phone_number: str | None = None,
    priority: int = 0,
    metadata: dict[str, str] | None = None,
) -> bool:
    """Add a call to the queue.

    Args:
        call_id: Unique call identifier.
        agent_id: Agent to handle the call.
        phone_number: Optional phone number.
        priority: Priority level (higher = more urgent).
        metadata: Optional additional metadata.

    Returns:
        True if queued successfully, False if queue is full or disabled.
    """
    if not settings.ENABLE_CALL_QUEUE:
        return False

    try:
        redis: Redis = await get_redis()

        # Check queue size limit
        queue_size: int = await redis.llen(CALL_QUEUE_KEY)  # type: ignore[misc]
        if queue_size >= settings.MAX_CALL_QUEUE_SIZE:
            logger.warning(
                "queue_full",
                call_id=call_id,
                queue_size=queue_size,
                max_size=settings.MAX_CALL_QUEUE_SIZE,
            )
            return False

        queued_call = QueuedCall(
            call_id=call_id,
            agent_id=agent_id,
            phone_number=phone_number,
            queued_at=time.time(),
            priority=priority,
            metadata=metadata,
        )

        async with _queue_lock:
            # Add to queue (priority-based insertion not implemented for simplicity)
            await redis.rpush(CALL_QUEUE_KEY, json.dumps(queued_call.to_dict()))  # type: ignore[misc]

            # Update stats
            await redis.hincrby(QUEUE_STATS_KEY, "total_queued", 1)  # type: ignore[misc]

        logger.info(
            "call_queued",
            call_id=call_id,
            agent_id=agent_id,
            priority=priority,
            queue_position=queue_size + 1,
        )
        return True

    except Exception:
        logger.exception("queue_enqueue_failed", call_id=call_id)
        return False


async def dequeue_call() -> QueuedCall | None:
    """Remove and return the next call from the queue.

    Returns:
        QueuedCall if available, None if queue is empty or disabled.
    """
    if not settings.ENABLE_CALL_QUEUE:
        return None

    try:
        redis: Redis = await get_redis()

        async with _queue_lock:
            data: str | None = await redis.lpop(CALL_QUEUE_KEY)  # type: ignore[misc]

        if not data:
            return None

        call_data = json.loads(data)
        queued_call = QueuedCall.from_dict(call_data)

        # Update stats
        await redis.hincrby(QUEUE_STATS_KEY, "total_dequeued", 1)  # type: ignore[misc]

        wait_time = time.time() - queued_call.queued_at
        logger.info(
            "call_dequeued",
            call_id=queued_call.call_id,
            agent_id=queued_call.agent_id,
            wait_time_seconds=round(wait_time, 2),
        )

        return queued_call

    except Exception:
        logger.exception("queue_dequeue_failed")
        return None


async def peek_queue(count: int = 10) -> list[QueuedCall]:
    """Peek at calls in the queue without removing them.

    Args:
        count: Number of calls to peek at.

    Returns:
        List of QueuedCall objects.
    """
    if not settings.ENABLE_CALL_QUEUE:
        return []

    try:
        redis: Redis = await get_redis()
        items: list[str] = await redis.lrange(CALL_QUEUE_KEY, 0, count - 1)  # type: ignore[misc]

        calls: list[QueuedCall] = []
        for item in items:
            try:
                call_data = json.loads(item)
                calls.append(QueuedCall.from_dict(call_data))
            except (json.JSONDecodeError, KeyError):
                continue

        return calls

    except Exception:
        logger.exception("queue_peek_failed")
        return []


async def get_queue_depth() -> int:
    """Get current queue depth.

    Returns:
        Number of calls in queue.
    """
    if not settings.ENABLE_CALL_QUEUE:
        return 0

    try:
        redis: Redis = await get_redis()
        depth: int = await redis.llen(CALL_QUEUE_KEY)  # type: ignore[misc]
        return depth

    except Exception:
        logger.exception("queue_depth_failed")
        return 0


async def get_queue_stats() -> dict[str, int | float]:
    """Get queue statistics.

    Returns:
        Dictionary with queue stats.
    """
    if not settings.ENABLE_CALL_QUEUE:
        return {"enabled": 0, "depth": 0}

    try:
        redis: Redis = await get_redis()

        depth: int = await redis.llen(CALL_QUEUE_KEY)  # type: ignore[misc]
        stats: dict[str, str] = await redis.hgetall(QUEUE_STATS_KEY)  # type: ignore[misc]

        return {
            "enabled": 1,
            "depth": depth,
            "max_size": settings.MAX_CALL_QUEUE_SIZE,
            "total_queued": int(stats.get("total_queued", 0)),
            "total_dequeued": int(stats.get("total_dequeued", 0)),
        }

    except Exception:
        logger.exception("queue_stats_failed")
        return {"enabled": 1, "depth": 0, "error": 1}


async def remove_from_queue(call_id: str) -> bool:
    """Remove a specific call from the queue.

    Args:
        call_id: Call identifier to remove.

    Returns:
        True if removed, False otherwise.
    """
    if not settings.ENABLE_CALL_QUEUE:
        return False

    try:
        redis: Redis = await get_redis()

        # Get all items and find the one to remove
        async with _queue_lock:
            items: list[str] = await redis.lrange(CALL_QUEUE_KEY, 0, -1)  # type: ignore[misc]

            for item in items:
                try:
                    call_data = json.loads(item)
                    if call_data.get("call_id") == call_id:
                        removed: int = await redis.lrem(CALL_QUEUE_KEY, 1, item)  # type: ignore[misc]
                        if removed:
                            logger.info("call_removed_from_queue", call_id=call_id)
                            return True
                except (json.JSONDecodeError, KeyError):
                    continue

        logger.warning("call_not_in_queue", call_id=call_id)
        return False

    except Exception:
        logger.exception("queue_remove_failed", call_id=call_id)
        return False


async def clear_queue() -> int:
    """Clear all calls from the queue.

    Returns:
        Number of calls cleared.
    """
    if not settings.ENABLE_CALL_QUEUE:
        return 0

    try:
        redis: Redis = await get_redis()

        async with _queue_lock:
            depth: int = await redis.llen(CALL_QUEUE_KEY)  # type: ignore[misc]
            await redis.delete(CALL_QUEUE_KEY)

        logger.info("queue_cleared", cleared_count=depth)
        return depth

    except Exception:
        logger.exception("queue_clear_failed")
        return 0


__all__ = [
    "QueuedCall",
    "clear_queue",
    "dequeue_call",
    "enqueue_call",
    "get_queue_depth",
    "get_queue_stats",
    "peek_queue",
    "remove_from_queue",
]
