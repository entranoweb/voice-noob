"""Request tracing middleware for correlation IDs and logging."""

import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.log_redaction import redact_mapping

logger = structlog.get_logger()


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """Middleware to add request tracing with correlation IDs.

    This middleware:
    1. Generates or extracts a correlation ID for each request
    2. Adds the correlation ID to structlog context
    3. Logs request start and completion with timing
    4. Adds correlation ID to response headers
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        """Process request with tracing."""
        # Get or generate correlation ID
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request_id = str(uuid.uuid4())[:8]  # Short ID for this specific request

        # Start timing
        start_time = time.perf_counter()

        # Bind context for all logs during this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=self._get_client_ip(request),
        )

        # Log request start
        logger.info(
            "request_started",
            query_params=redact_mapping(request.query_params) if request.query_params else None,
        )

        try:
            response: Response = await call_next(request)

            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Log request completion
            logger.info(
                "request_completed",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )

            # Add correlation ID to response headers
            response.headers["X-Correlation-ID"] = correlation_id
            response.headers["X-Request-ID"] = request_id

            return response

        except Exception as e:
            # Calculate duration even on error
            duration_ms = (time.perf_counter() - start_time) * 1000

            logger.exception(
                "request_failed",
                error=str(e),
                error_type=type(e).__name__,
                duration_ms=round(duration_ms, 2),
            )
            raise

    def _get_client_ip(self, request: Request) -> str:
        """The client address the server resolved.

        Not read from X-Forwarded-For here: a header the client can set would
        let it write any address into the log. When a trusted proxy fronts the
        app, the server has already put the proxy-reported client in
        ``request.client`` (see ``app.core.proxy``).
        """
        if request.client:
            return request.client.host
        return "unknown"
