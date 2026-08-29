"""Install the OpenTelemetry SDK, so the call traces reach somewhere.

``call_trace_emitter`` talks to the OpenTelemetry *API*, which is the right
dependency for a library: with no provider installed it returns non-recording
spans and costs almost nothing. The consequence is that without this module the
emitter is a no-op — the spans are created and dropped, and a deployment would
report a tracing feature it does not have.

``OTEL_ENABLED`` and ``OTEL_EXPORTER_OTLP_ENDPOINT`` have existed in settings
since before any of this and were read by nothing. This is what reads them.

Configuring is deliberately all-or-nothing. A provider with no exporter buffers
spans and drops them at a limit, which looks like tracing and is not, so an
endpoint that is missing or unreachable is reported at startup rather than
discovered later from an empty dashboard.
"""

from __future__ import annotations

import structlog

from app.core.config import settings

logger = structlog.get_logger()

_provider_installed = False


def configure_tracing() -> bool:
    """Install a tracer provider if tracing is switched on.

    Returns whether spans will now be exported. Never raises: a broken telemetry
    configuration must not stop the application from answering calls, which is
    the job it exists to do.
    """
    global _provider_installed

    if not settings.OTEL_ENABLED:
        logger.info("tracing_disabled", reason="OTEL_ENABLED is false")
        return False

    if _provider_installed:
        return True

    if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        logger.warning(
            "tracing_not_configured",
            reason="OTEL_ENABLED is true but OTEL_EXPORTER_OTLP_ENDPOINT is unset",
        )
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME}),
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT),
            ),
        )
        trace.set_tracer_provider(provider)
        _provider_installed = True
    except Exception:
        logger.exception("tracing_setup_failed")
        return False

    logger.info(
        "tracing_configured",
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        service=settings.OTEL_SERVICE_NAME,
    )
    return True


def shutdown_tracing() -> None:
    """Flush anything the batch processor is still holding.

    A call that ends as the process does still produced a trace, and the batch
    processor holds spans for up to five seconds by default.
    """
    if not _provider_installed:
        return

    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception:
        logger.exception("tracing_shutdown_failed")


__all__ = ["configure_tracing", "shutdown_tracing"]
