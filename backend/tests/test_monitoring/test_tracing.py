"""Tests for installing the tracer provider.

The endpoint test is the point of this file. `OTEL_EXPORTER_OTLP_ENDPOINT` is a
base URL by convention, and the HTTP exporter appends the signal path only when
it reads that variable itself — an `endpoint` passed to its constructor is used
verbatim. Handing it the base URL posts every span to the collector's root,
which 404s and drops it: tracing that reports itself configured and delivers
nothing, which is the failure the module exists to close.
"""

from __future__ import annotations

import pytest

from app.monitoring.tracing import _traces_endpoint


class TestTracesEndpoint:
    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("http://collector:4318", "http://collector:4318/v1/traces"),
            ("http://collector:4318/", "http://collector:4318/v1/traces"),
            ("https://otel.example.com/v1/traces", "https://otel.example.com/v1/traces"),
            ("https://otel.example.com/v1/traces/", "https://otel.example.com/v1/traces"),
        ],
    )
    def test_the_signal_path_is_present_exactly_once(
        self,
        configured: str,
        expected: str,
    ) -> None:
        assert _traces_endpoint(configured, "v1/traces") == expected

    def test_a_base_url_is_never_used_as_the_span_endpoint(self) -> None:
        """The whole bug in one assertion."""
        assert _traces_endpoint("http://collector:4318", "v1/traces") != "http://collector:4318"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (
                "https://otel.example.com?key=abc",
                "https://otel.example.com/v1/traces?key=abc",
            ),
            (
                "https://otel.example.com/ingest?key=abc#frag",
                "https://otel.example.com/ingest/v1/traces?key=abc#frag",
            ),
        ],
    )
    def test_a_query_string_stays_behind_the_path(
        self,
        configured: str,
        expected: str,
    ) -> None:
        """Appending to the whole string would put the signal path after the
        query and produce a URL no collector has ever heard of."""
        assert _traces_endpoint(configured, "v1/traces") == expected


class TestCleartextTransport:
    """A call trace carries transcripts and tool arguments built from them.

    Plain HTTP to another machine puts caller speech on the wire in the clear, so
    it is refused rather than warned about: a warning on a data-exposure path
    reports the exposure, it does not stop it.
    """

    def test_plain_http_to_a_remote_host_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from structlog.testing import capture_logs

        from app.core.config import settings
        from app.monitoring.tracing import _transport_is_permitted

        monkeypatch.setattr(settings, "OTEL_ALLOW_INSECURE_EXPORT", False)

        with capture_logs() as logs:
            permitted = _transport_is_permitted("http://otel.example.com/v1/traces")

        assert permitted is False
        assert [entry for entry in logs if entry["event"] == "tracing_refused_cleartext_endpoint"]

    def test_the_operator_can_permit_a_trusted_network_and_is_told(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An in-cluster collector is a real deployment and a real exposure at the
        same time, and only the operator knows whether that network is trusted."""
        from structlog.testing import capture_logs

        from app.core.config import settings
        from app.monitoring.tracing import _transport_is_permitted

        monkeypatch.setattr(settings, "OTEL_ALLOW_INSECURE_EXPORT", True)

        with capture_logs() as logs:
            permitted = _transport_is_permitted("http://collector.observability.svc:4318/v1/traces")

        assert permitted is True
        assert [entry for entry in logs if entry["event"] == "tracing_endpoint_is_cleartext"]

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://localhost:4318/v1/traces",
            "http://127.0.0.1:4318/v1/traces",
            "http://[::1]:4318/v1/traces",
            "https://otel.example.com/v1/traces",
        ],
    )
    def test_a_local_collector_or_tls_passes_without_ceremony(
        self,
        endpoint: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ordinary deployment is a sidecar over loopback HTTP. Refusing it,
        or warning about it, would break or desensitise the common case."""
        from structlog.testing import capture_logs

        from app.core.config import settings
        from app.monitoring.tracing import _transport_is_permitted

        monkeypatch.setattr(settings, "OTEL_ALLOW_INSECURE_EXPORT", False)

        with capture_logs() as logs:
            permitted = _transport_is_permitted(endpoint)

        assert permitted is True
        assert not [
            entry
            for entry in logs
            if entry["event"]
            in {"tracing_endpoint_is_cleartext", "tracing_refused_cleartext_endpoint"}
        ]


class TestConfigureTracing:
    def test_disabled_by_default_and_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "OTEL_ENABLED", False)
        from app.monitoring.tracing import configure_tracing

        assert configure_tracing() is False

    def test_enabled_without_an_endpoint_does_not_claim_to_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A provider with no exporter buffers spans and drops them at a limit,
        which looks like tracing and is not."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "OTEL_ENABLED", True)
        monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", None)
        from app.monitoring.tracing import configure_tracing

        assert configure_tracing() is False

    def test_a_remote_cleartext_endpoint_installs_no_exporter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The refusal has to happen before the exporter exists. An exporter
        attached to a provider is already a delivery path; returning False while
        one is installed would refuse on paper and export in fact."""
        from opentelemetry.exporter.otlp.proto.http import trace_exporter

        from app.core.config import settings

        monkeypatch.setattr(settings, "OTEL_ENABLED", True)
        monkeypatch.setattr(settings, "OTEL_ALLOW_INSECURE_EXPORT", False)
        monkeypatch.setattr(
            settings,
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://otel.example.com:4318",
        )

        constructed: list[object] = []

        class _Recording(trace_exporter.OTLPSpanExporter):  # type: ignore[misc]
            def __init__(self, *args: object, **kwargs: object) -> None:
                constructed.append(self)

        monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", _Recording)

        from app.monitoring import tracing

        monkeypatch.setattr(tracing, "_provider_installed", False)

        assert tracing.configure_tracing() is False
        assert constructed == []
        assert tracing._provider_installed is False


class TestEndpointRedaction:
    """The exporter needs the collector's credential. The log does not.

    `_traces_endpoint` preserves the query string on purpose, because that is
    where Honeycomb, Grafana Cloud and Dash0 all put the API key. Logging the
    result verbatim would put that key into application logs, which are shipped
    and retained far more widely than the config holding the secret.
    """

    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            (
                "https://api.honeycomb.io/v1/traces?x-honeycomb-team=SECRET",
                "https://api.honeycomb.io/v1/traces (credentials redacted)",
            ),
            (
                "https://user:hunter2@otel.example.com/v1/traces",
                "https://otel.example.com/v1/traces (credentials redacted)",
            ),
            (
                "http://localhost:4318/v1/traces",
                "http://localhost:4318/v1/traces",
            ),
            (
                "https://otel.example.com:4318/v1/traces",
                "https://otel.example.com:4318/v1/traces",
            ),
            (
                "http://[::1]:4318/v1/traces",
                "http://[::1]:4318/v1/traces",
            ),
        ],
    )
    def test_the_credential_never_reaches_the_log(
        self,
        endpoint: str,
        expected: str,
    ) -> None:
        from app.monitoring.tracing import _loggable

        assert _loggable(endpoint) == expected

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://collector:99999/v1/traces",
            "http://collector:abc/v1/traces",
        ],
    )
    def test_a_malformed_port_does_not_raise(self, endpoint: str) -> None:
        """`SplitResult.port` parses, so it raises on these. One of the three
        call sites is the success log, which runs *after* the setup try/except —
        so a typo in the endpoint would have crashed startup from inside the
        logging of an optional feature. Redaction must not validate."""
        from app.monitoring.tracing import _loggable

        assert "collector" in _loggable(endpoint)

    def test_configure_tracing_survives_a_malformed_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The module's promise is that a broken telemetry configuration never
        stops the application answering calls."""
        from app.core.config import settings
        from app.monitoring import tracing

        monkeypatch.setattr(settings, "OTEL_ENABLED", True)
        monkeypatch.setattr(settings, "OTEL_ALLOW_INSECURE_EXPORT", False)
        monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:99999")
        monkeypatch.setattr(tracing, "_provider_installed", False)

        assert tracing.configure_tracing() is False

    def test_no_secret_survives_any_of_the_three_log_sites(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An assertion on the emitted events, not on the helper: a fourth log
        site added later that forgets the helper is the way this regresses."""
        from opentelemetry.exporter.otlp.proto.http import trace_exporter
        from structlog.testing import capture_logs

        from app.core.config import settings
        from app.monitoring import tracing

        # Two of the three cases install a provider for real, and
        # `set_tracer_provider` is a process-global one-shot: a live exporter and
        # its batch worker would outlive this test and be inherited by every test
        # after it. Stub the exporter, as the sibling test does.
        class _Stub(trace_exporter.OTLPSpanExporter):  # type: ignore[misc]
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def export(self, *args: object, **kwargs: object) -> None:
                return None

            def shutdown(self) -> None:
                return None

        monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", _Stub)

        secret = "s3cr3t-collector-key"

        for endpoint, allow_insecure in (
            (f"https://otel.example.com/v1/traces?key={secret}", False),
            (f"http://otel.example.com/v1/traces?key={secret}", True),
            (f"http://otel.example.com/v1/traces?key={secret}", False),
        ):
            monkeypatch.setattr(settings, "OTEL_ENABLED", True)
            monkeypatch.setattr(settings, "OTEL_ALLOW_INSECURE_EXPORT", allow_insecure)
            monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
            monkeypatch.setattr(tracing, "_provider_installed", False)

            with capture_logs() as logs:
                tracing.configure_tracing()

            assert secret not in repr(logs), endpoint
