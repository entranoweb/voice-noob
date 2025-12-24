"""Unit tests for call registry service."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest
import pytest_asyncio

from app.services import call_registry
from app.services.call_registry import (
    CallInfo,
    get_active_calls,
    get_call_count,
    is_shutting_down,
    register_call,
    set_shutting_down,
    unregister_call,
    wait_for_calls_to_drain,
)


@pytest_asyncio.fixture
async def mock_redis() -> Any:
    """Create fake async Redis client for testing."""
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield redis
    await redis.aclose()


@pytest.fixture
def enable_call_registry() -> Any:
    """Fixture to temporarily enable call registry."""
    with patch.object(
        call_registry.settings,
        "ENABLE_CALL_REGISTRY",
        True,
    ):
        yield


@pytest.fixture
def disable_call_registry() -> Any:
    """Fixture to temporarily disable call registry."""
    with patch.object(
        call_registry.settings,
        "ENABLE_CALL_REGISTRY",
        False,
    ):
        yield


class TestRegisterCall:
    """Tests for register_call function."""

    @pytest.mark.asyncio
    async def test_register_call_success(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test successful call registration."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await register_call(
                call_id="test-call-123",
                agent_id="agent-456",
                phone_number="+14155551234",
            )

            assert result is True

            # Verify data was stored in Redis
            key = "voicenoob:calls:test-call-123"
            data = await mock_redis.hgetall(key)
            assert data["call_id"] == "test-call-123"
            assert data["agent_id"] == "agent-456"
            assert data["phone_number"] == "+14155551234"

    @pytest.mark.asyncio
    async def test_register_call_with_metadata(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test call registration with metadata."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await register_call(
                call_id="test-call-789",
                agent_id="agent-456",
                metadata={"campaign": "outbound", "source": "crm"},
            )

            assert result is True

            key = "voicenoob:calls:test-call-789"
            data = await mock_redis.hgetall(key)
            assert data["meta:campaign"] == "outbound"
            assert data["meta:source"] == "crm"

    @pytest.mark.asyncio
    async def test_register_call_disabled(
        self,
        mock_redis: Any,
        disable_call_registry: Any,
    ) -> None:
        """Test registration returns True when disabled."""
        result = await register_call(
            call_id="test-call-999",
            agent_id="agent-456",
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_register_call_redis_error(
        self,
        enable_call_registry: Any,
    ) -> None:
        """Test registration handles Redis errors gracefully."""
        mock_redis = MagicMock()
        mock_redis.hset = AsyncMock(side_effect=Exception("Redis connection failed"))

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await register_call(
                call_id="test-call-error",
                agent_id="agent-456",
            )

            assert result is False


class TestUnregisterCall:
    """Tests for unregister_call function."""

    @pytest.mark.asyncio
    async def test_unregister_call_success(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test successful call unregistration."""
        # First register a call
        key = "voicenoob:calls:test-call-123"
        await mock_redis.hset(key, mapping={"call_id": "test-call-123", "agent_id": "agent-456"})

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await unregister_call("test-call-123")

            assert result is True

            # Verify call was removed
            exists = await mock_redis.exists(key)
            assert exists == 0

    @pytest.mark.asyncio
    async def test_unregister_call_not_found(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test unregistration of non-existent call."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await unregister_call("nonexistent-call")

            # Should return True (no error), just logs warning
            assert result is True

    @pytest.mark.asyncio
    async def test_unregister_call_disabled(
        self,
        disable_call_registry: Any,
    ) -> None:
        """Test unregistration returns True when disabled."""
        result = await unregister_call("test-call-999")
        assert result is True


class TestGetActiveCalls:
    """Tests for get_active_calls function."""

    @pytest.mark.asyncio
    async def test_get_active_calls_empty(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test getting calls when none are registered."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            calls = await get_active_calls()
            assert calls == []

    @pytest.mark.asyncio
    async def test_get_active_calls_multiple(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test getting multiple active calls."""
        # Register multiple calls
        for i in range(3):
            key = f"voicenoob:calls:call-{i}"
            await mock_redis.hset(
                key,
                mapping={
                    "call_id": f"call-{i}",
                    "agent_id": f"agent-{i}",
                    "started_at": str(1000000 + i),
                    "phone_number": f"+1415555{i:04d}",
                },
            )

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            calls = await get_active_calls()

            assert len(calls) == 3
            assert all(isinstance(c, CallInfo) for c in calls)

    @pytest.mark.asyncio
    async def test_get_active_calls_disabled(
        self,
        disable_call_registry: Any,
    ) -> None:
        """Test get_active_calls returns empty when disabled."""
        calls = await get_active_calls()
        assert calls == []


class TestGetCallCount:
    """Tests for get_call_count function."""

    @pytest.mark.asyncio
    async def test_get_call_count_empty(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test count is zero when no calls registered."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            count = await get_call_count()
            assert count == 0

    @pytest.mark.asyncio
    async def test_get_call_count_multiple(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test counting multiple calls."""
        for i in range(5):
            key = f"voicenoob:calls:call-{i}"
            await mock_redis.hset(key, mapping={"call_id": f"call-{i}", "agent_id": "agent"})

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            count = await get_call_count()
            assert count == 5

    @pytest.mark.asyncio
    async def test_get_call_count_disabled(
        self,
        disable_call_registry: Any,
    ) -> None:
        """Test count returns 0 when disabled."""
        count = await get_call_count()
        assert count == 0


class TestShutdownFlag:
    """Tests for shutdown flag functions."""

    @pytest.mark.asyncio
    async def test_set_shutting_down_enabled(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test setting shutdown flag."""
        # Reset module-level flag
        call_registry._shutdown_flag = False

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            await set_shutting_down(True)

            assert is_shutting_down() is True

            # Check Redis flag
            flag = await mock_redis.get("voicenoob:shutdown")
            assert flag == "1"

    @pytest.mark.asyncio
    async def test_clear_shutting_down(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test clearing shutdown flag."""
        call_registry._shutdown_flag = True
        await mock_redis.set("voicenoob:shutdown", "1")

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            await set_shutting_down(False)

            assert is_shutting_down() is False

    def test_is_shutting_down(self) -> None:
        """Test is_shutting_down returns current state."""
        call_registry._shutdown_flag = False
        assert is_shutting_down() is False

        call_registry._shutdown_flag = True
        assert is_shutting_down() is True

        # Reset for other tests
        call_registry._shutdown_flag = False


class TestWaitForCallsToDrain:
    """Tests for wait_for_calls_to_drain function."""

    @pytest.mark.asyncio
    async def test_drain_immediate_success(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test immediate drain when no calls."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await wait_for_calls_to_drain(drain_timeout=5)
            assert result is True

    @pytest.mark.asyncio
    async def test_drain_with_calls_completion(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test drain waits for calls to complete."""
        # Register a call
        key = "voicenoob:calls:drain-test"
        await mock_redis.hset(key, mapping={"call_id": "drain-test", "agent_id": "agent"})

        async def remove_call_after_delay() -> None:
            await asyncio.sleep(0.5)
            await mock_redis.delete(key)

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            # Start background task to remove call
            task = asyncio.create_task(remove_call_after_delay())

            result = await wait_for_calls_to_drain(drain_timeout=3)

            await task
            assert result is True

    @pytest.mark.asyncio
    async def test_drain_timeout(
        self,
        mock_redis: Any,
        enable_call_registry: Any,
    ) -> None:
        """Test drain returns False on timeout."""
        # Register a call that won't be removed
        key = "voicenoob:calls:timeout-test"
        await mock_redis.hset(key, mapping={"call_id": "timeout-test", "agent_id": "agent"})

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await wait_for_calls_to_drain(drain_timeout=1)
            assert result is False

    @pytest.mark.asyncio
    async def test_drain_disabled(
        self,
        disable_call_registry: Any,
    ) -> None:
        """Test drain returns True when disabled."""
        result = await wait_for_calls_to_drain(drain_timeout=1)
        assert result is True
