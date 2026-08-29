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
