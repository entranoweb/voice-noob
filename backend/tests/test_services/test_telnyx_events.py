"""Tests for reading a Telnyx webhook in whichever format it arrived in.

The bug these cover was not subtle and was not caught by anything: the handlers
called ``request.json()`` on a body a TeXML application sends form-encoded, so
every real inbound call raised before reaching a single line of the logic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.models.call_record import CallStatus
from app.services.telephony.telnyx_events import WireFormat, parse_telnyx_webhook


def _request(body: bytes, content_type: str) -> Request:
    """A real Starlette request over a canned body."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/telnyx/voice",
        "headers": Headers({"content-type": content_type}).raw,
        "query_string": b"",
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class TestTeXMLForm:
    @pytest.mark.asyncio
    async def test_reads_the_form_encoded_shape(self) -> None:
        body = b"CallSid=v3%3Aabc&From=%2B15551230000&To=%2B15559997777&CallStatus=ringing"
        event = await parse_telnyx_webhook(
            _request(body, "application/x-www-form-urlencoded"),
        )

        assert event.wire_format is WireFormat.TEXML
        assert event.call_id == "v3:abc"
        assert event.from_number == "+15551230000"
        assert event.to_number == "+15559997777"
        assert event.status is CallStatus.RINGING

    @pytest.mark.asyncio
    async def test_reads_the_carriers_own_duration(self) -> None:
        body = b"CallSid=v3%3Aabc&CallStatus=completed&CallDuration=42"
        event = await parse_telnyx_webhook(
            _request(body, "application/x-www-form-urlencoded"),
        )

        assert event.duration_seconds == 42
        assert event.is_hangup

    @pytest.mark.asyncio
    async def test_an_unreadable_duration_is_absent_not_zero(self) -> None:
        body = b"CallSid=v3%3Aabc&CallStatus=completed&CallDuration=unknown"
        event = await parse_telnyx_webhook(
            _request(body, "application/x-www-form-urlencoded"),
        )

        assert event.duration_seconds is None


class TestCallControlJSON:
    @pytest.mark.asyncio
    async def test_reads_the_json_shape(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.answered",
                    "payload": {
                        "call_control_id": "v3:abc",
                        "from": "+15551230000",
                        "to": "+15559997777",
                    },
                },
            },
        ).encode()
        event = await parse_telnyx_webhook(_request(body, "application/json"))

        assert event.wire_format is WireFormat.CALL_CONTROL
        assert event.call_id == "v3:abc"
        assert event.is_answered

    @pytest.mark.asyncio
    async def test_a_json_body_with_the_wrong_content_type_still_parses(self) -> None:
        """A dropped call is a worse outcome than trusting the body over the header."""
        body = json.dumps({"data": {"event_type": "call.hangup", "payload": {}}}).encode()
        event = await parse_telnyx_webhook(_request(body, "text/plain"))

        assert event.wire_format is WireFormat.CALL_CONTROL
        assert event.status is CallStatus.COMPLETED


class TestHangupCause:
    @pytest.mark.parametrize(
        ("cause", "expected"),
        [
            ("NORMAL_CLEARING", CallStatus.COMPLETED),
            ("USER_BUSY", CallStatus.BUSY),
            ("NO_ANSWER", CallStatus.NO_ANSWER),
            ("CALL_REJECTED", CallStatus.CANCELED),
            ("SOMETHING_NOBODY_MAPPED", CallStatus.FAILED),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_cause_decides_the_stored_status(
        self,
        cause: str,
        expected: CallStatus,
    ) -> None:
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.hangup",
                    "payload": {"call_control_id": "v3:abc", "hangup_cause": cause},
                },
            },
        ).encode()
        event = await parse_telnyx_webhook(_request(body, "application/json"))

        assert event.resolved_status() is expected

    @pytest.mark.asyncio
    async def test_a_cause_on_a_non_hangup_event_changes_nothing(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.answered",
                    "payload": {"call_control_id": "v3:abc", "hangup_cause": "USER_BUSY"},
                },
            },
        ).encode()
        event = await parse_telnyx_webhook(_request(body, "application/json"))

        assert event.resolved_status() is CallStatus.IN_PROGRESS


class TestSipResponseCode:
    @pytest.mark.asyncio
    async def test_a_successful_sip_code_does_not_fail_the_call(self) -> None:
        """A completed TeXML call carries SipResponseCode=200.

        Reading that as a hangup cause made it an unmapped abnormal value, and
        every healthy call was stored as FAILED.
        """
        body = b"CallSid=v3%3Aabc&CallStatus=completed&SipResponseCode=200"
        event = await parse_telnyx_webhook(
            _request(body, "application/x-www-form-urlencoded"),
        )

        assert event.hangup_cause == ""
        assert event.resolved_status() is CallStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_an_explicit_hangup_cause_still_decides(self) -> None:
        body = b"CallSid=v3%3Aabc&CallStatus=completed&HangupCause=USER_BUSY&SipResponseCode=486"
        event = await parse_telnyx_webhook(
            _request(body, "application/x-www-form-urlencoded"),
        )

        assert event.resolved_status() is CallStatus.BUSY


class TestMisdeclaredBodies:
    """The body's shape decides, not the header. It can lie in either direction."""

    @pytest.mark.asyncio
    async def test_a_form_body_labelled_json_still_parses(self) -> None:
        body = b"CallSid=v3%3Aabc&From=%2B1&To=%2B2&CallStatus=ringing"
        event = await parse_telnyx_webhook(_request(body, "application/json"))

        assert event.wire_format is WireFormat.TEXML
        assert event.call_id == "v3:abc"


class TestDurationEdges:
    @pytest.mark.asyncio
    async def test_a_zero_duration_is_preserved(self) -> None:
        """A call that ended before it was answered really did last no time."""
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.hangup",
                    "payload": {"call_control_id": "v3:abc", "duration_seconds": 0},
                },
            },
        ).encode()
        event = await parse_telnyx_webhook(_request(body, "application/json"))

        assert event.duration_seconds == 0

    @pytest.mark.asyncio
    async def test_an_overflowing_duration_does_not_raise(self) -> None:
        """`int(float("1e309"))` is an OverflowError, and a 500 on the webhook."""
        body = b"CallSid=v3%3Aabc&CallStatus=completed&CallDuration=1e309"
        event = await parse_telnyx_webhook(
            _request(body, "application/x-www-form-urlencoded"),
        )

        assert event.duration_seconds is None


class TestUnknownEvents:
    @pytest.mark.asyncio
    async def test_an_unmapped_event_leaves_the_status_alone(self) -> None:
        """Machine detection and friends must not rewrite the call's status."""
        body = json.dumps(
            {
                "data": {
                    "event_type": "call.machine.detection.ended",
                    "payload": {"call_control_id": "v3:abc"},
                },
            },
        ).encode()
        event = await parse_telnyx_webhook(_request(body, "application/json"))

        assert event.status is None
        assert event.resolved_status() is None

    @pytest.mark.asyncio
    async def test_a_malformed_json_body_does_not_raise(self) -> None:
        event = await parse_telnyx_webhook(_request(b"{not json", "application/json"))

        assert event.call_id == ""
        assert event.status is None
