"""Which host the answer document hands to Telnyx.

The stream URL is the one part of the inbound path no test could catch before a
real call: a wrong host produces an answer document that parses, a webhook that
logs healthy, and a caller who hears silence. These pin the host itself.
"""

from unittest.mock import Mock

import pytest

from app.api.telephony import build_telnyx_stream_url
from app.core.config import settings


def _request(base_url: str) -> Mock:
    request = Mock()
    request.base_url = base_url
    return request


@pytest.fixture
def no_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_URL", None)


@pytest.fixture
def public_url(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://voice.example.com")
    return "https://voice.example.com"


class TestStreamUrlHost:
    def test_public_url_beats_the_request(self, public_url: str) -> None:
        """The proxy case. Without this the carrier is handed the internal address.

        ``request.base_url`` comes from the request line and the Host header, so
        an app behind a proxy that forwards neither sees ``internal:8000``.
        """
        url = build_telnyx_stream_url(_request("http://internal:8000/"), "agent-1")

        assert url.startswith("wss://voice.example.com/"), url
        assert "internal" not in url

    def test_the_request_is_the_fallback(self, no_public_url: None) -> None:
        """Unset PUBLIC_URL keeps the previous behaviour rather than failing."""
        url = build_telnyx_stream_url(_request("https://derived.example.com/"), "agent-1")

        assert url.startswith("wss://derived.example.com/"), url

    def test_a_trailing_slash_does_not_double(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "PUBLIC_URL", "https://voice.example.com/")

        url = build_telnyx_stream_url(_request("http://internal:8000/"), "agent-1")

        assert "//ws/telephony" not in url
        assert url.startswith("wss://voice.example.com/ws/telephony/")

    def test_the_scheme_is_always_wss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plain-http PUBLIC_URL still yields wss.

        Telnyx will not open a cleartext media stream, so the alternative to a
        TLS handshake that fails loudly is a stream that never connects at all.
        """
        monkeypatch.setattr(settings, "PUBLIC_URL", "http://voice.example.com")

        url = build_telnyx_stream_url(_request("http://internal:8000/"), "agent-1")

        assert url.startswith("wss://voice.example.com/")

    def test_the_identifier_and_direction_travel_with_it(self, public_url: str) -> None:
        url = build_telnyx_stream_url(
            _request("https://voice.example.com/"),
            "agent-1",
            call_id="v3:abc/def",
            direction="outbound",
        )

        assert "call_id=v3%3Aabc%2Fdef" in url
        assert "direction=outbound" in url
