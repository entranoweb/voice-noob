"""Unit tests for call queue service."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest
import pytest_asyncio

from app.services import call_queue
from app.services.call_queue import (
    QueuedCall,
    clear_queue,
    dequeue_call,
    enqueue_call,
    get_queue_depth,
    get_queue_stats,
    peek_queue,
    remove_from_queue,
)


@pytest_asyncio.fixture
async def mock_redis() -> Any:
    """Create fake async Redis client for testing."""
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield redis
    await redis.aclose()


@pytest.fixture
def enable_call_queue() -> Any:
    """Fixture to temporarily enable call queue."""
    with (
        patch.object(call_queue.settings, "ENABLE_CALL_QUEUE", True),
        patch.object(call_queue.settings, "MAX_CALL_QUEUE_SIZE", 100),
    ):
        yield


@pytest.fixture
def disable_call_queue() -> Any:
    """Fixture to temporarily disable call queue."""
    with patch.object(call_queue.settings, "ENABLE_CALL_QUEUE", False):
        yield


class TestQueuedCall:
    """Tests for QueuedCall dataclass."""

    def test_to_dict(self) -> None:
        """Test conversion to dictionary."""
        call = QueuedCall(
            call_id="test-123",
            agent_id="agent-456",
            phone_number="+14155551234",
            queued_at=1000000.0,
            priority=5,
            metadata={"key": "value"},
        )

        data = call.to_dict()

        assert data["call_id"] == "test-123"
        assert data["agent_id"] == "agent-456"
        assert data["phone_number"] == "+14155551234"
        assert data["queued_at"] == 1000000.0
        assert data["priority"] == 5
        assert data["metadata"] == {"key": "value"}

    def test_from_dict(self) -> None:
        """Test creation from dictionary."""
        data = {
            "call_id": "test-123",
            "agent_id": "agent-456",
            "phone_number": "+14155551234",
            "queued_at": 1000000.0,
            "priority": 5,
            "metadata": {"key": "value"},
        }

        call = QueuedCall.from_dict(data)

        assert call.call_id == "test-123"
        assert call.agent_id == "agent-456"
        assert call.phone_number == "+14155551234"
        assert call.queued_at == 1000000.0
        assert call.priority == 5
        assert call.metadata == {"key": "value"}

    def test_from_dict_missing_optional(self) -> None:
        """Test creation from dictionary with missing optional fields."""
        data = {
            "call_id": "test-123",
            "agent_id": "agent-456",
            "queued_at": 1000000.0,
        }

        call = QueuedCall.from_dict(data)

        assert call.call_id == "test-123"
        assert call.agent_id == "agent-456"
        assert call.phone_number is None
        assert call.priority == 0
        assert call.metadata is None


class TestEnqueueCall:
    """Tests for enqueue_call function."""

    @pytest.mark.asyncio
    async def test_enqueue_call_success(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test successful call enqueue."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await enqueue_call(
                call_id="test-call-123",
                agent_id="agent-456",
                phone_number="+14155551234",
            )

            assert result is True

            # Verify call was added to queue
            queue_len = await mock_redis.llen("voicenoob:queue:calls")
            assert queue_len == 1

    @pytest.mark.asyncio
    async def test_enqueue_call_with_metadata(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test enqueue with metadata."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await enqueue_call(
                call_id="test-call-789",
                agent_id="agent-456",
                metadata={"campaign": "outbound"},
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_enqueue_call_queue_full(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test enqueue fails when queue is full."""
        # Set small max size for test
        with (
            patch.object(call_queue.settings, "ENABLE_CALL_QUEUE", True),
            patch.object(call_queue.settings, "MAX_CALL_QUEUE_SIZE", 2),
            patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)),
        ):
            # Fill the queue
            await enqueue_call(call_id="call-1", agent_id="agent")
            await enqueue_call(call_id="call-2", agent_id="agent")

            # This should fail
            result = await enqueue_call(call_id="call-3", agent_id="agent")
            assert result is False

    @pytest.mark.asyncio
    async def test_enqueue_call_disabled(
        self,
        disable_call_queue: Any,
    ) -> None:
        """Test enqueue returns False when disabled."""
        result = await enqueue_call(call_id="test-call", agent_id="agent")
        assert result is False

    @pytest.mark.asyncio
    async def test_enqueue_call_redis_error(
        self,
        enable_call_queue: Any,
    ) -> None:
        """Test enqueue handles Redis errors gracefully."""
        mock_redis = MagicMock()
        mock_redis.llen = AsyncMock(side_effect=Exception("Redis connection failed"))

        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await enqueue_call(call_id="test-call", agent_id="agent")
            assert result is False


class TestDequeueCall:
    """Tests for dequeue_call function."""

    @pytest.mark.asyncio
    async def test_dequeue_call_success(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test successful call dequeue."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            # First enqueue a call
            await enqueue_call(
                call_id="test-call-123",
                agent_id="agent-456",
            )

            # Then dequeue
            call = await dequeue_call()

            assert call is not None
            assert call.call_id == "test-call-123"
            assert call.agent_id == "agent-456"

            # Queue should be empty
            queue_len = await mock_redis.llen("voicenoob:queue:calls")
            assert queue_len == 0

    @pytest.mark.asyncio
    async def test_dequeue_call_empty(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test dequeue returns None when queue is empty."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            call = await dequeue_call()
            assert call is None

    @pytest.mark.asyncio
    async def test_dequeue_call_fifo_order(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test calls are dequeued in FIFO order."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            # Enqueue multiple calls
            await enqueue_call(call_id="first", agent_id="agent")
            await enqueue_call(call_id="second", agent_id="agent")
            await enqueue_call(call_id="third", agent_id="agent")

            # Dequeue should return in order
            call1 = await dequeue_call()
            call2 = await dequeue_call()
            call3 = await dequeue_call()

            assert call1 is not None
            assert call1.call_id == "first"
            assert call2 is not None
            assert call2.call_id == "second"
            assert call3 is not None
            assert call3.call_id == "third"

    @pytest.mark.asyncio
    async def test_dequeue_call_disabled(
        self,
        disable_call_queue: Any,
    ) -> None:
        """Test dequeue returns None when disabled."""
        call = await dequeue_call()
        assert call is None


class TestPeekQueue:
    """Tests for peek_queue function."""

    @pytest.mark.asyncio
    async def test_peek_queue_success(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test peeking at queue."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            # Enqueue calls
            await enqueue_call(call_id="call-1", agent_id="agent")
            await enqueue_call(call_id="call-2", agent_id="agent")

            # Peek at queue
            calls = await peek_queue(count=5)

            assert len(calls) == 2
            assert calls[0].call_id == "call-1"
            assert calls[1].call_id == "call-2"

            # Queue should not be modified
            queue_len = await mock_redis.llen("voicenoob:queue:calls")
            assert queue_len == 2

    @pytest.mark.asyncio
    async def test_peek_queue_empty(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test peeking at empty queue."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            calls = await peek_queue()
            assert calls == []

    @pytest.mark.asyncio
    async def test_peek_queue_disabled(
        self,
        disable_call_queue: Any,
    ) -> None:
        """Test peek returns empty when disabled."""
        calls = await peek_queue()
        assert calls == []


class TestGetQueueDepth:
    """Tests for get_queue_depth function."""

    @pytest.mark.asyncio
    async def test_get_queue_depth_success(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test getting queue depth."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            # Enqueue calls
            await enqueue_call(call_id="call-1", agent_id="agent")
            await enqueue_call(call_id="call-2", agent_id="agent")
            await enqueue_call(call_id="call-3", agent_id="agent")

            depth = await get_queue_depth()
            assert depth == 3

    @pytest.mark.asyncio
    async def test_get_queue_depth_empty(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test queue depth is 0 when empty."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            depth = await get_queue_depth()
            assert depth == 0

    @pytest.mark.asyncio
    async def test_get_queue_depth_disabled(
        self,
        disable_call_queue: Any,
    ) -> None:
        """Test depth returns 0 when disabled."""
        depth = await get_queue_depth()
        assert depth == 0


class TestGetQueueStats:
    """Tests for get_queue_stats function."""

    @pytest.mark.asyncio
    async def test_get_queue_stats_success(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test getting queue stats."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            # Enqueue and dequeue some calls
            await enqueue_call(call_id="call-1", agent_id="agent")
            await enqueue_call(call_id="call-2", agent_id="agent")
            await dequeue_call()

            stats = await get_queue_stats()

            assert stats["enabled"] == 1
            assert stats["depth"] == 1
            assert stats["total_queued"] == 2
            assert stats["total_dequeued"] == 1

    @pytest.mark.asyncio
    async def test_get_queue_stats_disabled(
        self,
        disable_call_queue: Any,
    ) -> None:
        """Test stats returns disabled indicator."""
        stats = await get_queue_stats()
        assert stats["enabled"] == 0
        assert stats["depth"] == 0


class TestRemoveFromQueue:
    """Tests for remove_from_queue function."""

    @pytest.mark.asyncio
    async def test_remove_from_queue_success(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test removing specific call from queue."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            # Enqueue calls
            await enqueue_call(call_id="call-1", agent_id="agent")
            await enqueue_call(call_id="call-2", agent_id="agent")
            await enqueue_call(call_id="call-3", agent_id="agent")

            # Remove middle call
            result = await remove_from_queue("call-2")

            assert result is True

            # Verify removal
            depth = await get_queue_depth()
            assert depth == 2

            # Remaining calls should be call-1 and call-3
            calls = await peek_queue()
            call_ids = [c.call_id for c in calls]
            assert "call-2" not in call_ids

    @pytest.mark.asyncio
    async def test_remove_from_queue_not_found(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test removing non-existent call."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await remove_from_queue("nonexistent-call")
            assert result is False

    @pytest.mark.asyncio
    async def test_remove_from_queue_disabled(
        self,
        disable_call_queue: Any,
    ) -> None:
        """Test remove returns False when disabled."""
        result = await remove_from_queue("call-id")
        assert result is False


class TestClearQueue:
    """Tests for clear_queue function."""

    @pytest.mark.asyncio
    async def test_clear_queue_success(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test clearing queue."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            # Enqueue calls
            await enqueue_call(call_id="call-1", agent_id="agent")
            await enqueue_call(call_id="call-2", agent_id="agent")
            await enqueue_call(call_id="call-3", agent_id="agent")

            # Clear queue
            cleared = await clear_queue()

            assert cleared == 3

            # Queue should be empty
            depth = await get_queue_depth()
            assert depth == 0

    @pytest.mark.asyncio
    async def test_clear_queue_empty(
        self,
        mock_redis: Any,
        enable_call_queue: Any,
    ) -> None:
        """Test clearing empty queue."""
        with patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)):
            cleared = await clear_queue()
            assert cleared == 0

    @pytest.mark.asyncio
    async def test_clear_queue_disabled(
        self,
        disable_call_queue: Any,
    ) -> None:
        """Test clear returns 0 when disabled."""
        cleared = await clear_queue()
        assert cleared == 0
