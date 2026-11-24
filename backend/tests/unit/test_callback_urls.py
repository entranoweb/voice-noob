"""Every URL handed to a carrier or a customer names a host they can reach.

``PUBLIC_URL`` decided the Telnyx stream URL and nothing else: the Twilio stream
URL and both outbound answer webhooks were built from ``request.base_url``, which
behind a proxy is the address the app is bound to. These assert the host in each
one, because the scheme is forced and so proves nothing (DECISIONS §37).
"""

from __future__ import annotations

from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import pytest
from starlette.requests import Request

from app.api.telephony import (
    build_answer_webhook_url,
    build_telnyx_stream_url,
    build_twilio_stream_url,
)
from app.core.config import settings
from app.core.public_url import (
    frontend_base_url,
    public_base_url,
    public_request_url,
    public_ws_base_url,
)
from app.core.stream_token import verify_stream_token
from app.services.telephony.twilio_service import TwilioService

INTERNAL = "http://internal:8000/"
AGENT = "3f1c6f0e-7c1e-4b9a-9d55-0a1b2c3d4e5f"


def _request(base_url: str = INTERNAL) -> Mock:
    request = Mock()
    request.base_url = base_url
    return request


def _real_request(path: str, query: str = "", host: str = "internal:8000") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(b"host", host.encode())],
            "server": ("internal", 8000),
        }
    )


@pytest.fixture
def public_url(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://voice.example.com")
    return "https://voice.example.com"


@pytest.fixture
def no_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_URL", None)


class TestTheSharedRule:
    def test_public_url_beats_the_request(self, public_url: str) -> None:
        assert public_base_url(_request()) == "https://voice.example.com"
        assert public_ws_base_url(_request()) == "wss://voice.example.com"

    def test_the_request_is_the_fallback(self, no_public_url: None) -> None:
        assert public_base_url(_request("https://derived.example.com/")) == (
            "https://derived.example.com"
        )

    def test_a_blank_public_url_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "PUBLIC_URL", "   ")

        assert public_base_url(_request("https://derived.example.com/")) == (
            "https://derived.example.com"
        )


class TestTwilioStreamUrl:
    def test_it_names_the_public_host_not_the_internal_one(self, public_url: str) -> None:
        url = build_twilio_stream_url(_request(), AGENT)

        assert urlsplit(url).netloc == "voice.example.com", url
        assert url == f"wss://voice.example.com/ws/telephony/twilio/{AGENT}"

    def test_it_carries_no_query_string(self, public_url: str) -> None:
        """Twilio drops one from a <Stream> URL, so anything put there is lost
        on a real call while surviving every test that uses another client."""
        assert urlsplit(build_twilio_stream_url(_request(), AGENT)).query == ""


class TestTwilioAnswerDocument:
    def test_the_token_travels_as_a_parameter(self, public_url: str) -> None:
        twiml = TwilioService("", "").generate_answer_response(
            build_twilio_stream_url(_request(), AGENT),
            AGENT,
            stream_token="1800000000.sig",  # noqa: S106
        )

        stream = ET.fromstring(twiml).find("./Connect/Stream")  # noqa: S314
        assert stream is not None
        assert urlsplit(stream.attrib["url"]).netloc == "voice.example.com"
        parameters = {p.attrib["name"]: p.attrib["value"] for p in stream.findall("Parameter")}
        assert parameters["token"] == "1800000000.sig"
        assert parameters["agent_id"] == AGENT


class TestOutboundAnswerWebhookUrl:
    @pytest.mark.parametrize("provider", ["telnyx", "twilio"])
    def test_it_names_the_public_host(self, public_url: str, provider: str) -> None:
        """The carrier fetches this when the callee answers. From request.base_url
        it named the internal address, and the call was answered into nothing."""
        url = build_answer_webhook_url(_request(), provider, AGENT)

        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.netloc == "voice.example.com", url
        assert parts.path == f"/webhooks/{provider}/answer"
        assert parse_qs(parts.query) == {"agent_id": [AGENT]}

    def test_the_request_is_the_fallback(self, no_public_url: None) -> None:
        url = build_answer_webhook_url(_request("https://derived.example.com/"), "telnyx", AGENT)

        assert urlsplit(url).netloc == "derived.example.com"


class TestTelnyxStreamUrlToken:
    def test_the_url_carries_a_token_bound_to_this_agent_and_call(self, public_url: str) -> None:
        url = build_telnyx_stream_url(_request(), AGENT, call_id="v3:abc/def")

        query = parse_qs(urlsplit(url).query)
        (token,) = query["token"]
        assert query["call_id"] == ["v3:abc/def"]
        assert verify_stream_token(
            token, provider="telnyx", agent_id=AGENT, call_id="v3:abc/def"
        ).ok
        # And to nothing else: neither another call nor another agent.
        assert not verify_stream_token(
            token, provider="telnyx", agent_id=AGENT, call_id="v3:other"
        ).ok
        assert not verify_stream_token(
            token, provider="telnyx", agent_id="another-agent", call_id="v3:abc/def"
        ).ok


class TestTheUrlTwilioSigned:
    def test_it_is_rebuilt_on_the_public_host(self, public_url: str) -> None:
        """Twilio signs the URL it posted to. Behind a proxy request.url is the
        internal one, so a check against it could only ever fail."""
        request = _real_request("/webhooks/twilio/answer", "agent_id=abc")

        assert public_request_url(request) == (
            "https://voice.example.com/webhooks/twilio/answer?agent_id=abc"
        )

    def test_without_public_url_it_is_the_request_as_seen(self, no_public_url: None) -> None:
        request = _real_request("/webhooks/twilio/voice")

        assert public_request_url(request) == "http://internal:8000/webhooks/twilio/voice"


class TestEmbedSnippetHost:
    def test_frontend_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com/")

        assert frontend_base_url(_request()) == "https://app.example.com"

    def test_then_the_first_cors_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The production compose file sets CORS_ORIGINS from the frontend's URL,
        so with nothing else configured that origin is the frontend."""
        monkeypatch.setattr(settings, "FRONTEND_URL", None)
        monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://app.example.com"])

        assert frontend_base_url(_request()) == "https://app.example.com"

    def test_then_this_apis_own_public_origin(
        self, monkeypatch: pytest.MonkeyPatch, public_url: str
    ) -> None:
        monkeypatch.setattr(settings, "FRONTEND_URL", None)
        monkeypatch.setattr(settings, "CORS_ORIGINS", ["*"])

        assert frontend_base_url(_request()) == "https://voice.example.com"

    def test_it_is_never_the_placeholder_domain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The snippet used to point customers' pages at a domain this project
        does not own."""
        monkeypatch.setattr(settings, "FRONTEND_URL", None)

        assert "yourplatform" not in frontend_base_url(_request())
