"""Prometheus metrics for call monitoring.

Provides counters, histograms, and gauges for tracking call metrics.
Feature-flagged via ENABLE_PROMETHEUS_METRICS.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.core.config import settings

logger = structlog.get_logger()

# Create custom registry to avoid conflicts
REGISTRY = CollectorRegistry(auto_describe=True)

# Counters
CALLS_INITIATED = Counter(
    "voicenoob_calls_initiated_total",
    "Total number of calls initiated",
    ["agent_id"],
    registry=REGISTRY,
)

CALLS_COMPLETED = Counter(
    "voicenoob_calls_completed_total",
    "Total number of calls completed successfully",
    ["agent_id"],
    registry=REGISTRY,
)

CALLS_FAILED = Counter(
    "voicenoob_calls_failed_total",
    "Total number of calls that failed",
    ["agent_id", "error_type"],
    registry=REGISTRY,
)

# Histograms
CALLS_DURATION = Histogram(
    "voicenoob_call_duration_seconds",
    "Call duration in seconds",
    ["agent_id"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800),
    registry=REGISTRY,
)

# Gauges
ACTIVE_CALLS = Gauge(
    "voicenoob_active_calls_current",
    "Current number of active calls",
    registry=REGISTRY,
)


def record_call_initiated(agent_id: str) -> None:
    """Record a call initiation event.

    Args:
        agent_id: Agent handling the call.
    """
    if not settings.ENABLE_PROMETHEUS_METRICS:
        return

    CALLS_INITIATED.labels(agent_id=agent_id).inc()
    ACTIVE_CALLS.inc()
    logger.debug("metric_call_initiated", agent_id=agent_id)


def record_call_completed(agent_id: str, duration_seconds: float) -> None:
    """Record a successful call completion.

    Args:
        agent_id: Agent that handled the call.
        duration_seconds: Call duration in seconds.
    """
    if not settings.ENABLE_PROMETHEUS_METRICS:
        return

    CALLS_COMPLETED.labels(agent_id=agent_id).inc()
    CALLS_DURATION.labels(agent_id=agent_id).observe(duration_seconds)
    ACTIVE_CALLS.dec()
    logger.debug(
        "metric_call_completed",
        agent_id=agent_id,
        duration=duration_seconds,
    )


def record_call_failed(agent_id: str, error_type: str) -> None:
    """Record a failed call.

    Args:
        agent_id: Agent that handled the call.
        error_type: Type of error that occurred.
    """
    if not settings.ENABLE_PROMETHEUS_METRICS:
        return

    CALLS_FAILED.labels(agent_id=agent_id, error_type=error_type).inc()
    ACTIVE_CALLS.dec()
    logger.debug(
        "metric_call_failed",
        agent_id=agent_id,
        error_type=error_type,
    )


def get_metrics_router() -> APIRouter:
    """Get router with /metrics endpoint.

    Returns:
        FastAPI router with Prometheus metrics endpoint.
    """
    router = APIRouter()

    @router.get("/metrics")
    async def metrics() -> Response:
        """Prometheus metrics endpoint."""
        if not settings.ENABLE_PROMETHEUS_METRICS:
            return Response(
                content="Prometheus metrics disabled",
                status_code=503,
            )

        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )

    return router


__all__ = [
    "ACTIVE_CALLS",
    "CALLS_COMPLETED",
    "CALLS_DURATION",
    "CALLS_FAILED",
    "CALLS_INITIATED",
    "get_metrics_router",
    "record_call_completed",
    "record_call_failed",
    "record_call_initiated",
]
