"""Integration tests for voice pipeline components.

Tests the integration between:
- Call registry
- Prometheus metrics
- Connection draining
- Health endpoints
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import fakeredis
import pytest
import pytest_asyncio

from app.services import call_queue, call_registry
from app.services.call_registry import (
    get_active_calls,
    get_call_count,
    is_shutting_down,
    register_call,
    set_shutting_down,
    unregister_call,
    wait_for_calls_to_drain,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User


@pytest_asyncio.fixture
async def mock_redis() -> Any:
    """Create fake async Redis client for testing."""
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield redis
    await redis.aclose()


@pytest.fixture
def enable_features() -> Any:
    """Enable all production hardening features."""
    with (
        patch.object(call_registry.settings, "ENABLE_CALL_REGISTRY", True),
        patch.object(call_registry.settings, "ENABLE_CONNECTION_DRAINING", True),
        patch.object(call_registry.settings, "SHUTDOWN_DRAIN_TIMEOUT", 5),
        patch.object(call_registry.settings, "CALL_REGISTRY_TTL", 300),
    ):
        yield


class TestCallLifecycle:
    """Integration tests for complete call lifecycle."""

    @pytest.mark.asyncio
    async def test_complete_call_lifecycle(
        self,
        mock_redis: Any,
        enable_features: Any,  # noqa: ARG002
    ) -> None:
        """Test a complete call lifecycle: register -> active -> unregister."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            # 1. Register call
            result = await register_call(
                call_id="lifecycle-call-001",
                agent_id="agent-test",
                phone_number="+14155551234",
                metadata={"provider": "twilio"},
            )
            assert result is True

            # 2. Verify call is active
            count = await get_call_count()
            assert count == 1

            calls = await get_active_calls()
            assert len(calls) == 1
            assert calls[0].call_id == "lifecycle-call-001"

            # 3. Unregister call
            result = await unregister_call("lifecycle-call-001")
            assert result is True

            # 4. Verify call is removed
            count = await get_call_count()
            assert count == 0

    @pytest.mark.asyncio
    async def test_multiple_concurrent_calls(
        self,
        mock_redis: Any,
        enable_features: Any,  # noqa: ARG002
    ) -> None:
        """Test handling multiple concurrent calls."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            # Register multiple calls
            call_ids = [f"concurrent-call-{i}" for i in range(5)]

            for call_id in call_ids:
                result = await register_call(
                    call_id=call_id,
                    agent_id="agent-multi",
                )
                assert result is True

            # Verify all calls are active
            count = await get_call_count()
            assert count == 5

            # Unregister some calls
            await unregister_call("concurrent-call-0")
            await unregister_call("concurrent-call-2")
            await unregister_call("concurrent-call-4")

            # Verify correct count
            count = await get_call_count()
            assert count == 2

            # Cleanup remaining
            await unregister_call("concurrent-call-1")
            await unregister_call("concurrent-call-3")

            count = await get_call_count()
            assert count == 0

    @pytest.mark.asyncio
    async def test_call_metadata_persistence(
        self,
        mock_redis: Any,
        enable_features: Any,  # noqa: ARG002
    ) -> None:
        """Test that call metadata is properly stored and retrieved."""
        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            await register_call(
                call_id="metadata-call",
                agent_id="agent-meta",
                phone_number="+14155559999",
                metadata={
                    "campaign_id": "camp-123",
                    "source": "inbound",
                    "priority": "high",
                },
            )

            calls = await get_active_calls()
            assert len(calls) == 1

            call = calls[0]
            assert call.call_id == "metadata-call"
            assert call.agent_id == "agent-meta"
            assert call.phone_number == "+14155559999"
            assert call.metadata is not None
            assert call.metadata.get("campaign_id") == "camp-123"
            assert call.metadata.get("source") == "inbound"

            await unregister_call("metadata-call")


class TestConnectionDraining:
    """Integration tests for connection draining during shutdown."""

    @pytest.mark.asyncio
    async def test_graceful_drain_no_calls(
        self,
        mock_redis: Any,
        enable_features: Any,  # noqa: ARG002
    ) -> None:
        """Test graceful drain completes immediately with no active calls."""
        # Reset shutdown flag (accessing private for test setup)
        call_registry._shutdown_flag = False  # noqa: SLF001

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            # Set shutdown flag
            await set_shutting_down(True)
            assert is_shutting_down() is True

            # Drain should complete immediately
            drained = await wait_for_calls_to_drain(drain_timeout=5)
            assert drained is True

            # Clean up
            await set_shutting_down(False)

    @pytest.mark.asyncio
    async def test_graceful_drain_waits_for_calls(
        self,
        mock_redis: Any,
        enable_features: Any,  # noqa: ARG002
    ) -> None:
        """Test graceful drain waits for active calls to complete."""
        call_registry._shutdown_flag = False  # noqa: SLF001

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            # Register active call
            await register_call(
                call_id="drain-wait-call",
                agent_id="agent-drain",
            )

            # Start drain in background
            async def complete_call_after_delay() -> None:
                await asyncio.sleep(1)
                await unregister_call("drain-wait-call")

            task = asyncio.create_task(complete_call_after_delay())

            # Set shutdown and wait for drain
            await set_shutting_down(True)
            drained = await wait_for_calls_to_drain(drain_timeout=5)

            await task

            assert drained is True
            await set_shutting_down(False)

    @pytest.mark.asyncio
    async def test_drain_timeout_exceeded(
        self,
        mock_redis: Any,
        enable_features: Any,  # noqa: ARG002
    ) -> None:
        """Test drain timeout when calls don't complete."""
        call_registry._shutdown_flag = False  # noqa: SLF001

        with patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)):
            # Register call that won't complete
            await register_call(
                call_id="stuck-call",
                agent_id="agent-stuck",
            )

            await set_shutting_down(True)

            # Use short timeout
            drained = await wait_for_calls_to_drain(drain_timeout=1)

            assert drained is False

            # Cleanup
            await unregister_call("stuck-call")
            await set_shutting_down(False)


class TestHealthEndpointIntegration:
    """Integration tests for health endpoints with call registry."""

    @pytest.mark.asyncio
    async def test_health_detailed_with_active_calls(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test detailed health endpoint shows active call count."""
        client, _user = authenticated_test_client

        response = await client.get("/health/detailed")

        assert response.status_code == 200
        data = response.json()

        assert "features" in data
        assert "call_registry" in data["features"]

        # When registry is enabled, calls info should be present
        if data["features"]["call_registry"]:
            assert "calls" in data

    @pytest.mark.asyncio
    async def test_readiness_probe_during_shutdown(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test readiness probe returns 503 during shutdown."""
        client, _user = authenticated_test_client

        # Mock shutdown state
        original_is_shutting_down = call_registry.is_shutting_down

        try:
            call_registry.is_shutting_down = lambda: True  # type: ignore[method-assign]

            response = await client.get("/health/ready")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert "shutdown" in data["reason"]

        finally:
            call_registry.is_shutting_down = original_is_shutting_down  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_liveness_probe_always_succeeds(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test liveness probe succeeds regardless of state."""
        client, _user = authenticated_test_client

        response = await client.get("/health/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"


class TestCallRegistryAndQueueIntegration:
    """Integration tests for call registry with call queue."""

    @pytest.mark.asyncio
    async def test_registry_and_queue_coexistence(
        self,
        mock_redis: Any,
    ) -> None:
        """Test call registry and queue can work together."""
        with (
            patch.object(call_registry.settings, "ENABLE_CALL_REGISTRY", True),
            patch.object(call_queue.settings, "ENABLE_CALL_QUEUE", True),
            patch.object(call_queue.settings, "MAX_CALL_QUEUE_SIZE", 10),
            patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)),
            patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)),
        ):
            # Register active call in registry
            await register_call(
                call_id="active-call",
                agent_id="agent-1",
            )

            # Queue a call
            from app.services.call_queue import enqueue_call, get_queue_depth

            queued = await enqueue_call(
                call_id="queued-call",
                agent_id="agent-2",
            )

            # Both should work independently
            active_count = await get_call_count()
            queue_depth = await get_queue_depth()

            assert active_count == 1
            assert queued is True
            assert queue_depth == 1

            # Cleanup
            await unregister_call("active-call")

    @pytest.mark.asyncio
    async def test_queue_while_at_capacity(
        self,
        mock_redis: Any,
    ) -> None:
        """Test queuing calls while active calls are at capacity."""
        with (
            patch.object(call_registry.settings, "ENABLE_CALL_REGISTRY", True),
            patch.object(call_queue.settings, "ENABLE_CALL_QUEUE", True),
            patch.object(call_queue.settings, "MAX_CALL_QUEUE_SIZE", 3),
            patch.object(call_registry, "get_redis", AsyncMock(return_value=mock_redis)),
            patch.object(call_queue, "get_redis", AsyncMock(return_value=mock_redis)),
        ):
            from app.services.call_queue import (
                clear_queue,
                dequeue_call,
                enqueue_call,
                get_queue_depth,
            )

            # Register some active calls
            for i in range(3):
                await register_call(
                    call_id=f"active-{i}",
                    agent_id="agent",
                )

            # Queue additional calls
            for i in range(3):
                await enqueue_call(
                    call_id=f"queued-{i}",
                    agent_id="agent",
                )

            # Verify state
            active = await get_call_count()
            queued = await get_queue_depth()

            assert active == 3
            assert queued == 3

            # Simulate processing: dequeue and register
            queued_call = await dequeue_call()
            assert queued_call is not None

            # Complete an active call
            await unregister_call("active-0")

            # Register the dequeued call
            await register_call(
                call_id=queued_call.call_id,
                agent_id=queued_call.agent_id,
            )

            # Check updated state
            active = await get_call_count()
            assert active == 3  # Still 3 active (1 removed, 1 added)

            # Cleanup
            for i in range(3):
                await unregister_call(f"active-{i + 1}" if i > 0 else "queued-0")
            await clear_queue()
