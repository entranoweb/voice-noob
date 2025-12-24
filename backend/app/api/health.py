"""Health check endpoints.

Provides standard and Kubernetes-style health probes:
- /health - Basic health check
- /health/db - Database connectivity
- /health/redis - Redis connectivity
- /health/ready - Kubernetes readiness probe
- /health/live - Kubernetes liveness probe
- /health/detailed - Full service status with metrics
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.redis import get_redis
from app.db.session import get_db
from app.services.call_registry import get_call_count, is_shutting_down

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Basic health check endpoint."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


@router.get("/health/db")
async def health_check_db(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Database health check endpoint."""
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar()
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        logger.exception("Database health check failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "database": str(e)}


@router.get("/health/redis")
async def health_check_redis(response: Response) -> dict[str, str]:
    """Redis health check endpoint."""
    try:
        redis = await get_redis()
        await redis.ping()  # type: ignore[misc]
        return {"status": "healthy", "redis": "connected"}
    except Exception as e:
        logger.exception("Redis health check failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "redis": str(e)}


@router.get("/health/ready")
async def readiness_probe(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Kubernetes readiness probe.

    Returns 503 if:
    - Database is unavailable
    - Redis is unavailable
    - Shutdown is in progress
    """
    # Check if shutdown is in progress
    if is_shutting_down():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "shutdown_in_progress"}

    # Check database
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar()
    except Exception as e:
        logger.warning("Readiness check failed: database", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": f"database: {e}"}

    # Check Redis
    try:
        redis = await get_redis()
        await redis.ping()  # type: ignore[misc]
    except Exception as e:
        logger.warning("Readiness check failed: redis", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": f"redis: {e}"}

    return {"status": "ready"}


@router.get("/health/live")
async def liveness_probe() -> dict[str, str]:
    """Kubernetes liveness probe.

    Returns 200 if the service is alive.
    This should be a simple, fast check that doesn't depend on external services.
    """
    return {"status": "alive"}


@router.get("/health/detailed")
async def detailed_health(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Detailed health check with call registry stats.

    Returns comprehensive status including:
    - Database status
    - Redis status
    - Active calls count
    - Shutdown status
    - Feature flags
    """
    result: dict[str, Any] = {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "services": {},
        "features": {
            "call_registry": settings.ENABLE_CALL_REGISTRY,
            "prometheus_metrics": settings.ENABLE_PROMETHEUS_METRICS,
            "connection_draining": settings.ENABLE_CONNECTION_DRAINING,
        },
    }

    # Check database
    try:
        db_result = await db.execute(text("SELECT 1"))
        db_result.scalar()
        result["services"]["database"] = "healthy"
    except Exception as e:
        logger.warning("Detailed health: database unhealthy", exc_info=True)
        result["services"]["database"] = f"unhealthy: {e}"
        result["status"] = "degraded"

    # Check Redis
    try:
        redis = await get_redis()
        await redis.ping()  # type: ignore[misc]
        result["services"]["redis"] = "healthy"
    except Exception as e:
        logger.warning("Detailed health: redis unhealthy", exc_info=True)
        result["services"]["redis"] = f"unhealthy: {e}"
        result["status"] = "degraded"

    # Call registry stats
    if settings.ENABLE_CALL_REGISTRY:
        try:
            call_count = await get_call_count()
            result["calls"] = {
                "active": call_count,
                "shutting_down": is_shutting_down(),
            }
        except Exception as e:
            logger.warning("Detailed health: call registry error", exc_info=True)
            result["calls"] = {"error": str(e)}

    if result["status"] != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return result
