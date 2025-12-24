"""Unit tests for Prometheus metrics module."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.monitoring import metrics
from app.monitoring.metrics import (
    ACTIVE_CALLS,
    CALLS_COMPLETED,
    CALLS_DURATION,
    CALLS_FAILED,
    CALLS_INITIATED,
    REGISTRY,
    get_metrics_router,
    record_call_completed,
    record_call_failed,
    record_call_initiated,
)


@pytest.fixture(autouse=True)
def reset_metrics() -> Any:
    """Reset metrics counters before each test."""
    # Reset all metrics by clearing samples
    # Note: Prometheus client doesn't provide easy reset, so we use labels
    return


@pytest.fixture
def enable_metrics() -> Any:
    """Fixture to temporarily enable Prometheus metrics."""
    with patch.object(
        metrics.settings,
        "ENABLE_PROMETHEUS_METRICS",
        True,
    ):
        yield


@pytest.fixture
def disable_metrics() -> Any:
    """Fixture to temporarily disable Prometheus metrics."""
    with patch.object(
        metrics.settings,
        "ENABLE_PROMETHEUS_METRICS",
        False,
    ):
        yield


class TestRecordCallInitiated:
    """Tests for record_call_initiated function."""

    def test_call_initiated_increments_counter(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that call initiation increments counter."""
        initial_value = CALLS_INITIATED.labels(agent_id="test-agent")._value.get()

        record_call_initiated("test-agent")

        final_value = CALLS_INITIATED.labels(agent_id="test-agent")._value.get()
        assert final_value == initial_value + 1

    def test_call_initiated_increments_active_gauge(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that call initiation increments active calls gauge."""
        initial_value = ACTIVE_CALLS._value.get()

        record_call_initiated("test-agent")

        final_value = ACTIVE_CALLS._value.get()
        assert final_value == initial_value + 1

    def test_call_initiated_disabled(
        self,
        disable_metrics: Any,
    ) -> None:
        """Test no-op when metrics disabled."""
        # Should not raise an error
        record_call_initiated("test-agent")


class TestRecordCallCompleted:
    """Tests for record_call_completed function."""

    def test_call_completed_increments_counter(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that call completion increments counter."""
        initial_value = CALLS_COMPLETED.labels(agent_id="test-agent")._value.get()

        record_call_completed("test-agent", duration_seconds=60.0)

        final_value = CALLS_COMPLETED.labels(agent_id="test-agent")._value.get()
        assert final_value == initial_value + 1

    def test_call_completed_records_duration(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that call completion records duration histogram."""
        # Record a call with specific duration
        record_call_completed("duration-test-agent", duration_seconds=45.0)

        # Get histogram sum
        histogram = CALLS_DURATION.labels(agent_id="duration-test-agent")
        assert histogram._sum.get() >= 45.0

    def test_call_completed_decrements_active_gauge(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that call completion decrements active calls gauge."""
        # First increment
        record_call_initiated("gauge-test-agent")
        initial_value = ACTIVE_CALLS._value.get()

        # Then complete
        record_call_completed("gauge-test-agent", duration_seconds=30.0)

        final_value = ACTIVE_CALLS._value.get()
        assert final_value == initial_value - 1

    def test_call_completed_disabled(
        self,
        disable_metrics: Any,
    ) -> None:
        """Test no-op when metrics disabled."""
        record_call_completed("test-agent", duration_seconds=60.0)


class TestRecordCallFailed:
    """Tests for record_call_failed function."""

    def test_call_failed_increments_counter(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that call failure increments counter."""
        initial_value = CALLS_FAILED.labels(
            agent_id="test-agent",
            error_type="timeout",
        )._value.get()

        record_call_failed("test-agent", error_type="timeout")

        final_value = CALLS_FAILED.labels(
            agent_id="test-agent",
            error_type="timeout",
        )._value.get()
        assert final_value == initial_value + 1

    def test_call_failed_tracks_error_types(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that different error types are tracked separately."""
        record_call_failed("error-test-agent", error_type="connection_error")
        record_call_failed("error-test-agent", error_type="api_error")
        record_call_failed("error-test-agent", error_type="connection_error")

        conn_errors = CALLS_FAILED.labels(
            agent_id="error-test-agent",
            error_type="connection_error",
        )._value.get()

        api_errors = CALLS_FAILED.labels(
            agent_id="error-test-agent",
            error_type="api_error",
        )._value.get()

        # Note: Values include any previous test runs
        assert conn_errors >= 2
        assert api_errors >= 1

    def test_call_failed_decrements_active_gauge(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that call failure decrements active calls gauge."""
        # First increment
        record_call_initiated("fail-gauge-agent")
        initial_value = ACTIVE_CALLS._value.get()

        # Then fail
        record_call_failed("fail-gauge-agent", error_type="crash")

        final_value = ACTIVE_CALLS._value.get()
        assert final_value == initial_value - 1

    def test_call_failed_disabled(
        self,
        disable_metrics: Any,
    ) -> None:
        """Test no-op when metrics disabled."""
        record_call_failed("test-agent", error_type="test_error")


class TestMetricsEndpoint:
    """Tests for /metrics endpoint."""

    def test_metrics_endpoint_returns_prometheus_format(
        self,
        enable_metrics: Any,
    ) -> None:
        """Test that metrics endpoint returns Prometheus format."""
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(get_metrics_router())

        client = TestClient(app)
        response = client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

        # Check for expected metric names in output
        content = response.text
        assert "voicenoob_calls_initiated_total" in content
        assert "voicenoob_calls_completed_total" in content
        assert "voicenoob_calls_failed_total" in content
        assert "voicenoob_call_duration_seconds" in content
        assert "voicenoob_active_calls_current" in content

    def test_metrics_endpoint_disabled(
        self,
        disable_metrics: Any,
    ) -> None:
        """Test that metrics endpoint returns 503 when disabled."""
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(get_metrics_router())

        client = TestClient(app)
        response = client.get("/metrics")

        assert response.status_code == 503
        assert "disabled" in response.text.lower()


class TestMetricsBuckets:
    """Tests for histogram buckets configuration."""

    def test_duration_histogram_buckets(self) -> None:
        """Test that duration histogram has expected buckets."""
        # Check the configured buckets
        expected_buckets = (5, 15, 30, 60, 120, 300, 600, 1800)

        # Verify buckets are configured (checking the histogram description)
        assert CALLS_DURATION._upper_bounds == (*expected_buckets, float("inf"))


class TestMetricsRegistry:
    """Tests for custom metrics registry."""

    def test_custom_registry_used(self) -> None:
        """Test that custom registry is used for all metrics."""
        # All metrics should be in our custom registry
        metric_families = list(REGISTRY.collect())
        metric_names = [mf.name for mf in metric_families]

        assert "voicenoob_calls_initiated" in metric_names
        assert "voicenoob_calls_completed" in metric_names
        assert "voicenoob_calls_failed" in metric_names
        assert "voicenoob_call_duration_seconds" in metric_names
        assert "voicenoob_active_calls_current" in metric_names
