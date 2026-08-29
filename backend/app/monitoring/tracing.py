"""Install the OpenTelemetry SDK, so the call traces reach somewhere.

``call_trace_emitter`` talks to the OpenTelemetry *API*, which is the right
dependency for a library: with no provider installed it returns non-recording
spans and costs almost nothing. The consequence is that without this module the
emitter is a no-op — the spans are created and dropped, and a deployment would
report a tracing feature it does not have.

``OTEL_ENABLED`` and ``OTEL_EXPORTER_OTLP_ENDPOINT`` have existed in settings
since before any of this and were read by nothing. This is what reads them.

Configuring is deliberately all-or-nothing. A provider with no exporter buffers
spans and drops them at a limit, which looks like tracing and is not, so a
missing endpoint is reported at startup rather than discovered later from an
empty dashboard.

What this does *not* do is promise delivery. Constructing an exporter opens no
connection, so a collector that is unreachable, wrongly addressed, or refusing
the payload is indistinguishable here from one that is working. The startup log
therefore says the provider was installed and where spans will be sent — not
that anything arrived. A network probe would only move the lie: a collector that
answers at startup can be gone a minute later, and a probe that failed would
either block boot or be ignored. Export failures surface from the exporter's own
logging, which is the only place that knows.
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
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            DEFAULT_TRACES_EXPORT_PATH,
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME}),
        )
        endpoint = _traces_endpoint(
            settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            DEFAULT_TRACES_EXPORT_PATH,
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)),
        )
        trace.set_tracer_provider(provider)
        _provider_installed = True
    except Exception:
        logger.exception("tracing_setup_failed")
        return False

    # Not "tracing_configured": nothing here has spoken to the collector. The
    # provider is installed and spans will be addressed to this URL.
    logger.info(
        "tracing_provider_installed",
        endpoint=endpoint,
        service=settings.OTEL_SERVICE_NAME,
    )
    return True


def _traces_endpoint(configured: str, traces_path: str) -> str:
    """The full URL spans are POSTed to, from the generic OTLP setting.

    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is by convention a *base* URL covering every
    signal — ``http://collector:4318``. The HTTP exporter appends the signal path
    itself only when it reads that variable from the environment; an ``endpoint``
    passed to its constructor is used verbatim. Handing it the base URL therefore
    posts every span to the collector's root, which answers 404 and drops it —
    tracing that reports itself configured and delivers nothing, which is the
    exact failure this module exists to close.
    """
    trimmed = configured.rstrip("/")
    if trimmed.endswith(f"/{traces_path.strip('/')}"):
        return trimmed
    return f"{trimmed}/{traces_path.strip('/')}"


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
