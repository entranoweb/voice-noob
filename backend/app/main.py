"""Main FastAPI application entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import (
    agents,
    audit,
    auth,
    caller_runs,
    calls,
    campaigns,
    compliance,
    crm,
    embed,
    health,
    integrations,
    phone_numbers,
    qa,
    realtime,
    telephony,
    telephony_ws,
    testing,
    tools,
    workspaces,
)
from app.api import settings as settings_api
from app.core.config import InsecureDefaultCredentialsError, settings
from app.core.first_admin import ensure_first_admin
from app.core.limiter import limiter
from app.db.redis import close_redis, get_redis, require_min_redis_version
from app.db.session import AsyncSessionLocal, engine
from app.middleware.request_tracing import RequestTracingMiddleware
from app.middleware.security import SecurityHeadersMiddleware
from app.monitoring import get_metrics_router, loop_lag
from app.monitoring.tracing import configure_tracing, shutdown_tracing
from app.services.call_registry import set_shutting_down, wait_for_calls_to_drain
from app.services.caller_audio import start_audio_expiry_worker, stop_audio_expiry_worker
from app.services.campaign_worker import start_campaign_worker, stop_campaign_worker
from app.services.data_protection import start_retention_worker, stop_retention_worker

# Configure structured logging with async processors
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.WARNING if not settings.DEBUG else logging.DEBUG
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: PLR0912, PLR0915
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info("Starting application", app_name=settings.APP_NAME)

    # Install the tracer provider before anything can emit a span. Without it the
    # OpenTelemetry API hands out non-recording spans and every call trace is
    # created and dropped.
    configure_tracing()

    try:
        # Initialize Redis (fatal if fails)
        redis_for_check = await get_redis()
        logger.info("Redis connection established")
        # An unreachable Redis already refuses to start; so does one too old for
        # the embed key limiter, with a message naming the requirement.
        logger.info("Redis version ok", version=await require_min_redis_version(redis_for_check))

        # Clear any stale shutdown flag from previous crash
        await set_shutting_down(False)
        logger.info("Startup: shutdown flag cleared")
    except Exception:
        logger.exception("Failed to initialize Redis - application cannot start")
        raise  # Re-raise to prevent app startup

    # Create the first administrator if no users exist, and refuse to serve on
    # a password shipped in the repository. A refusal is fatal on purpose: the
    # alternative is a deployment that works and has a public superuser login.
    try:
        async with AsyncSessionLocal() as db:
            await ensure_first_admin(db)
    except InsecureDefaultCredentialsError:
        raise
    except Exception:
        logger.exception("Failed to check/create admin user - continuing anyway")

    # Initialize Sentry if configured (non-fatal)
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                environment=settings.SENTRY_ENVIRONMENT,
                traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            )
            logger.info("Sentry initialized")
        except Exception:
            logger.exception("Failed to initialize Sentry - continuing without error tracking")

    # Start campaign worker (non-fatal)
    try:
        # Use PUBLIC_URL from settings if available, otherwise default to localhost
        base_url = settings.PUBLIC_URL or f"http://{settings.HOST}:{settings.PORT}"
        await start_campaign_worker(base_url=base_url)
        logger.info("Campaign worker started", base_url=base_url)
    except Exception:
        logger.exception("Failed to start campaign worker - campaigns will not process")

    # Start the retention sweep (non-fatal). It deletes call data past each
    # account's retention period; without it the period is only a number.
    if settings.RETENTION_SWEEP_ENABLED:
        try:
            await start_retention_worker()
            logger.info("Retention worker started")
        except Exception:
            logger.exception("Failed to start retention worker - retention will not run")

    # Expiry of call audio. Always on, with a pass at start-up, so that the
    # retention period the product states is carried out without anyone
    # scheduling anything. (The sweep above is a separate, opt-in thing.)
    try:
        await start_audio_expiry_worker()
        logger.info("Audio expiry worker started")
    except Exception:
        logger.exception("Failed to start audio expiry worker - audio will not expire")

    # Event-loop lag gauge: the saturation signal for real-time audio.
    # Cheap (one 100ms-interval task) and safe to run everywhere.
    loop_lag.start()
    logger.info("Event-loop lag monitor started")

    yield

    # Shutdown
    logger.info("Shutting down application")

    await loop_lag.stop()

    # Connection draining - wait for active calls to complete
    if settings.ENABLE_CONNECTION_DRAINING:
        try:
            logger.info(
                "connection_draining_started",
                timeout=settings.SHUTDOWN_DRAIN_TIMEOUT,
            )
            await set_shutting_down(True)
            drained = await wait_for_calls_to_drain(settings.SHUTDOWN_DRAIN_TIMEOUT)
            if drained:
                logger.info("connection_draining_complete")
            else:
                logger.warning(
                    "connection_draining_timeout",
                    timeout=settings.SHUTDOWN_DRAIN_TIMEOUT,
                )
        except Exception:
            logger.exception("connection_draining_error")

    try:
        await stop_audio_expiry_worker()
    except Exception:
        logger.exception("Error stopping audio expiry worker")

    # Stop retention worker
    try:
        await stop_retention_worker()
    except Exception:
        logger.exception("Error stopping retention worker")

    # Stop campaign worker
    try:
        await stop_campaign_worker()
        logger.info("Campaign worker stopped")
    except Exception:
        logger.exception("Error stopping campaign worker")

    # Close Redis connection
    try:
        await close_redis()
        logger.info("Redis connection closed")
    except Exception:
        logger.exception("Error closing Redis connection")

    # Dispose database engine and close all connections
    try:
        await engine.dispose()
        logger.info("Database connections closed")
    except Exception:
        logger.exception("Error closing database connections")

    # Flush any spans the batch processor is still holding. A call that ended as
    # the process did still produced a trace.
    shutdown_tracing()


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    debug=settings.DEBUG,
    lifespan=lifespan,
    # The interactive documentation lists every route and schema. It is for
    # development; in production it is off unless ENABLE_API_DOCS says otherwise.
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)

# Add rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Add request tracing middleware (runs first, wraps everything)
app.add_middleware(RequestTracingMiddleware)

# Add security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Add CORS middleware (must be added AFTER security headers so it runs first)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)

# Include routers
app.include_router(health.router, tags=["health"])
app.include_router(crm.router, prefix=settings.API_V1_PREFIX)
app.include_router(workspaces.router, prefix=settings.API_V1_PREFIX)
app.include_router(agents.router)
app.include_router(settings_api.router)
app.include_router(realtime.router)
app.include_router(realtime.webrtc_router)  # WebRTC session endpoint
app.include_router(tools.router)  # Tool execution endpoint
app.include_router(telephony.router)  # Telephony API (phone numbers, calls)
app.include_router(telephony.webhook_router)  # Twilio/Telnyx webhooks
app.include_router(telephony_ws.router)  # Telephony WebSocket for media streams
app.include_router(calls.router)  # Call history API
app.include_router(campaigns.router, prefix=settings.API_V1_PREFIX)  # Campaigns API
app.include_router(phone_numbers.router)  # Phone numbers API
app.include_router(auth.router)  # Authentication API
app.include_router(compliance.router)  # Compliance API (GDPR/CCPA)
app.include_router(audit.router)  # Audit trail (read only)
app.include_router(integrations.router)  # Integrations API (external tools)
app.include_router(embed.router)  # Public embed API (unauthenticated)
app.include_router(embed.ws_router)  # Public embed WebSocket
app.include_router(qa.router)  # QA Testing Framework API
app.include_router(testing.router)  # Pre-deployment Testing API
app.include_router(caller_runs.router)  # Results uploaded from the caller rig

# Include Prometheus metrics router (feature-flagged)
if settings.ENABLE_PROMETHEUS_METRICS:
    app.include_router(get_metrics_router(), tags=["monitoring"])


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
    )
