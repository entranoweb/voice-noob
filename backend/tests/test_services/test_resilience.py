"""Tests for QA resilience patterns.

Tests configuration, helper functions, and circuit breaker state management.
Note: Integration tests with actual API calls are in separate test files.
"""

from __future__ import annotations

from unittest.mock import patch

import anthropic
import pytest

from app.services.qa.resilience import (
    get_anthropic_client,
    get_circuit_state,
    is_circuit_open,
    reset_circuit_breaker,
)


class TestGetAnthropicClient:
    """Tests for get_anthropic_client function."""

    def test_get_client_with_api_key(self) -> None:
        """Test client creation with valid API key."""
        with patch("app.services.qa.resilience.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = "sk-ant-test-key"
            mock_settings.ANTHROPIC_TIMEOUT = 30.0

            client = get_anthropic_client()

            assert client is not None
            assert isinstance(client, anthropic.AsyncAnthropic)

    def test_get_client_without_api_key(self) -> None:
        """Test client creation fails without API key."""
        with patch("app.services.qa.resilience.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = None

            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY not configured"):
                get_anthropic_client()


class TestCircuitBreaker:
    """Tests for circuit breaker functionality."""

    @pytest.fixture(autouse=True)
    def reset_circuit(self) -> None:
        """Reset circuit breaker before each test."""
        reset_circuit_breaker()
        yield
        reset_circuit_breaker()

    def test_circuit_initially_closed(self) -> None:
        """Test circuit starts in closed state."""
        assert not is_circuit_open()
        state = get_circuit_state()
        assert state["state"] == "closed"
        assert state["fail_count"] == 0

    def test_get_circuit_state(self) -> None:
        """Test circuit state retrieval."""
        state = get_circuit_state()

        assert "state" in state
        assert "fail_count" in state
        assert "fail_max" in state
        assert isinstance(state["fail_count"], int)
        assert isinstance(state["fail_max"], int)

    def test_reset_circuit_breaker(self) -> None:
        """Test circuit breaker reset."""
        # Just verify reset doesn't raise and circuit is closed after
        reset_circuit_breaker()

        state = get_circuit_state()
        assert state["state"] == "closed"


class TestCircuitBreakerState:
    """Tests for circuit breaker state checking."""

    @pytest.fixture(autouse=True)
    def reset_circuit(self) -> None:
        """Reset circuit breaker before each test."""
        reset_circuit_breaker()
        yield
        reset_circuit_breaker()

    def test_is_circuit_open_false_initially(self) -> None:
        """Test circuit is not open initially."""
        assert not is_circuit_open()

    def test_circuit_state_has_required_fields(self) -> None:
        """Test circuit state has all required fields."""
        state = get_circuit_state()

        assert "state" in state
        assert "fail_count" in state
        assert "fail_max" in state
        assert state["state"] in ["closed", "open", "half-open", "half_open"]


class TestResilienceConfiguration:
    """Tests for resilience configuration."""

    def test_settings_have_required_fields(self) -> None:
        """Test that settings have required resilience fields."""
        from app.core.config import settings

        assert hasattr(settings, "ANTHROPIC_TIMEOUT")
        assert hasattr(settings, "ANTHROPIC_MAX_RETRIES")
        assert hasattr(settings, "ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD")
        assert hasattr(settings, "ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT")

    def test_default_timeout_value(self) -> None:
        """Test default timeout is reasonable."""
        from app.core.config import settings

        assert settings.ANTHROPIC_TIMEOUT >= 10.0  # At least 10 seconds
        assert settings.ANTHROPIC_TIMEOUT <= 120.0  # At most 2 minutes

    def test_default_retry_value(self) -> None:
        """Test default retry count is reasonable."""
        from app.core.config import settings

        assert settings.ANTHROPIC_MAX_RETRIES >= 1
        assert settings.ANTHROPIC_MAX_RETRIES <= 10

    def test_circuit_breaker_thresholds(self) -> None:
        """Test circuit breaker thresholds are reasonable."""
        from app.core.config import settings

        assert settings.ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD >= 3
        assert settings.ANTHROPIC_CIRCUIT_RECOVERY_TIMEOUT >= 30


class TestResilienceExports:
    """Test that all expected exports are available."""

    def test_all_exports_available(self) -> None:
        """Test all expected functions are exported."""
        from app.services.qa.resilience import (
            CircuitBreakerError,
            call_claude_with_resilience,
            get_anthropic_client,
            get_circuit_state,
            is_circuit_open,
            reset_circuit_breaker,
        )

        # Just verify they exist and are callable
        assert callable(call_claude_with_resilience)
        assert callable(get_anthropic_client)
        assert callable(get_circuit_state)
        assert callable(is_circuit_open)
        assert callable(reset_circuit_breaker)
        assert CircuitBreakerError is not None
