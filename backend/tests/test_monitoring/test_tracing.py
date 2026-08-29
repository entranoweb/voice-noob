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


class TestCleartextWarning:
    """A call trace carries transcripts and tool arguments built from them."""

    def test_plain_http_to_a_remote_host_is_reported(self) -> None:
        from structlog.testing import capture_logs

        from app.monitoring.tracing import _warn_if_cleartext

        with capture_logs() as logs:
            _warn_if_cleartext("http://otel.example.com/v1/traces")

        assert [entry for entry in logs if entry["event"] == "tracing_endpoint_is_cleartext"]

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://localhost:4318/v1/traces",
            "http://127.0.0.1:4318/v1/traces",
            "https://otel.example.com/v1/traces",
        ],
    )
    def test_a_local_collector_or_tls_is_not_worth_warning_about(self, endpoint: str) -> None:
        """The ordinary deployment is a sidecar over HTTP. Warning about it would
        train the operator to ignore the warning that matters."""
        from structlog.testing import capture_logs

        from app.monitoring.tracing import _warn_if_cleartext

        with capture_logs() as logs:
            _warn_if_cleartext(endpoint)

        assert not [entry for entry in logs if entry["event"] == "tracing_endpoint_is_cleartext"]


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
