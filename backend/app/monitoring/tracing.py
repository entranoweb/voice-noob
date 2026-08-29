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

Spans are refused rather than sent over plain HTTP to a remote host, because a
call trace carries transcripts. HTTPS and a loopback collector both go through
untouched; anything else needs ``OTEL_ALLOW_INSECURE_EXPORT``, which is the
operator stating that the network in question is trusted.

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

from urllib.parse import urlsplit, urlunsplit

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
        if not _transport_is_permitted(endpoint):
            return False
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))

        # A provider may already be installed — auto-instrumentation does this,
        # and OpenTelemetry ignores a second `set_tracer_provider` with a warning
        # rather than an error. Setting one blindly and reporting success would
        # leave our exporter attached to a provider nothing uses: configured on
        # paper, delivering nothing, which is the failure this module exists to
        # close. Attach to whoever is already there instead.
        existing = trace.get_tracer_provider()
        attach = getattr(existing, "add_span_processor", None)
        if callable(attach):
            attach(processor)
            logger.info("tracing_attached_to_existing_provider")
        else:
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)
        _provider_installed = True
    except Exception:
        logger.exception("tracing_setup_failed")
        return False

    # Not "tracing_configured": nothing here has spoken to the collector. The
    # provider is installed and spans will be addressed to this URL.
    logger.info(
        "tracing_provider_installed",
        endpoint=_loggable(endpoint),
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
    parsed = urlsplit(configured)
    signal = traces_path.strip("/")
    path = parsed.path.rstrip("/")
    if not path.endswith(f"/{signal}"):
        path = f"{path}/{signal}"
    # Rebuilt from its parts rather than concatenated: an endpoint carrying a
    # query string would otherwise get the signal path appended *after* the
    # query, producing a URL the collector has never heard of.
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _loggable(endpoint: str) -> str:
    """The endpoint with its secrets removed, for the log line.

    Collectors authenticate in two places this URL can carry: a query string
    (``?api-key=...`` is how Honeycomb, Grafana Cloud and Dash0 all do it) and
    HTTP userinfo. ``_traces_endpoint`` deliberately preserves both, because the
    exporter needs them — and logging the result verbatim would put a collector
    credential into application logs, which are aggregated, shipped and retained
    far more widely than the config that holds the secret.

    Refusing cleartext transport and then printing the key is the same mistake
    twice: protecting the payload and leaking the credential to it. Scheme, host
    and path are what an operator needs to see to diagnose an endpoint; neither
    the query nor the userinfo tells them anything the rest does not.
    """
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    if ":" in host:  # IPv6 literals lose their brackets to `hostname`
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    redacted = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    if parsed.query or parsed.username or parsed.password:
        return f"{redacted} (credentials redacted)"
    return redacted


# Hosts whose traffic never leaves the machine. Everything else is a network,
# private or not.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _transport_is_permitted(endpoint: str) -> bool:
    """Whether spans may be sent to this endpoint over this transport.

    A call trace carries what was said: transcripts, and the tool arguments built
    from them. Sending that over plain HTTP to another machine puts caller speech
    on the wire in the clear, so it is refused by default rather than warned
    about — a warning on a data-exposure path is a note that the exposure is
    happening, not a control that stops it.

    Two things are still allowed. HTTPS anywhere, obviously. And plain HTTP to a
    loopback address, which never leaves the host: the sidecar-on-localhost
    deployment is the ordinary OpenTelemetry setup and needs no ceremony.

    What that leaves is the case worth a decision: a collector reached over HTTP
    across a network, private or otherwise — an in-cluster service address, say.
    That is a real deployment and a real exposure at the same time, and only the
    operator knows whether that network is trusted. ``OTEL_ALLOW_INSECURE_EXPORT``
    is how they say so, and the refusal names the setting so it is one log line
    from being resolved either way.
    """
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http":
        return True

    host = (parsed.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return True

    if settings.OTEL_ALLOW_INSECURE_EXPORT:
        logger.warning(
            "tracing_endpoint_is_cleartext",
            endpoint=_loggable(endpoint),
            detail=(
                "call traces carry transcripts and tool arguments, and this "
                "endpoint is plain HTTP to a remote host; permitted because "
                "OTEL_ALLOW_INSECURE_EXPORT is set"
            ),
        )
        return True

    logger.error(
        "tracing_refused_cleartext_endpoint",
        endpoint=_loggable(endpoint),
        detail=(
            "call traces carry transcripts and tool arguments; refusing to send "
            "them over plain HTTP to a remote host. Use https, point at a "
            "loopback collector, or set OTEL_ALLOW_INSECURE_EXPORT if that "
            "network is trusted"
        ),
    )
    return False


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
