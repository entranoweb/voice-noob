"""Normalise the two wire formats a Telnyx number can deliver.

Telnyx has two voice products and they do not share a webhook shape:

* **Call Control** posts JSON — ``{"data": {"event_type": ..., "payload": {...}}}``
  — and expects the application to drive the call with API commands. Returning
  markup to it does nothing.
* **TeXML** posts ``application/x-www-form-urlencoded`` with Twilio-compatible
  parameter names (``CallSid``, ``From``, ``To``, ``CallStatus``) and expects a
  TeXML document back.

The inbound path in this codebase configures numbers as TeXML applications and
answers with a TeXML document, so what actually arrives on a real call is the
form-encoded shape. The handlers read ``request.json()``, which on that body
raises before any of the logic runs — the webhook returns 500, Telnyx gets no
document, and the call is dropped by the carrier. Nothing caught it because no
call had ever been placed.

Rather than pick one format and hope the account is configured to match, this
module accepts either and hands back one shape. Getting it wrong is a dropped
call, and which product a number is attached to is not something the webhook
handler can see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.models.call_record import CallStatus

if TYPE_CHECKING:
    from fastapi import Request


class WireFormat(StrEnum):
    """Which Telnyx product sent this webhook."""

    TEXML = "texml"
    CALL_CONTROL = "call_control"


# Call Control event types to the status they leave the call in. Events with no
# entry (``call.machine.detection.ended`` and friends) report their own type and
# leave the record's status alone.
_CALL_CONTROL_STATUS = {
    "call.initiated": CallStatus.INITIATED,
    "call.ringing": CallStatus.RINGING,
    "call.answered": CallStatus.IN_PROGRESS,
    "call.bridged": CallStatus.IN_PROGRESS,
    "call.hangup": CallStatus.COMPLETED,
}

# TeXML reuses Twilio's CallStatus vocabulary.
_TEXML_STATUS = {
    "queued": CallStatus.INITIATED,
    "initiated": CallStatus.INITIATED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.CANCELED,
    "cancelled": CallStatus.CANCELED,
}

# Hangup causes that mean something other than a normal end of call. Applied
# only on a hangup event, and only over a status of COMPLETED.
_HANGUP_CAUSE_STATUS = {
    "user_busy": CallStatus.BUSY,
    "no_answer": CallStatus.NO_ANSWER,
    "timeout": CallStatus.NO_ANSWER,
    "call_rejected": CallStatus.CANCELED,
    "originator_cancel": CallStatus.CANCELED,
}

_NORMAL_HANGUP_CAUSES = frozenset({"", "normal_clearing", "normal_release"})


@dataclass(frozen=True, slots=True)
class TelnyxCallEvent:
    """One Telnyx voice webhook, in a shape the handlers can act on."""

    wire_format: WireFormat
    call_id: str
    from_number: str = ""
    to_number: str = ""
    event_type: str = ""
    status: CallStatus | None = None
    hangup_cause: str = ""
    duration_seconds: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_hangup(self) -> bool:
        """Whether this event ends the call."""
        return self.status in {
            CallStatus.COMPLETED,
            CallStatus.BUSY,
            CallStatus.NO_ANSWER,
            CallStatus.FAILED,
            CallStatus.CANCELED,
        }

    @property
    def is_answered(self) -> bool:
        return self.status is CallStatus.IN_PROGRESS

    def resolved_status(self) -> CallStatus | None:
        """The status to store, with the hangup cause taken into account.

        A hangup carrying ``USER_BUSY`` is a busy call, not a completed one, and
        the distinction is the whole of a campaign's answer-rate reporting.
        """
        if not self.is_hangup:
            return self.status

        cause = self.hangup_cause.strip().lower()
        if cause in _NORMAL_HANGUP_CAUSES:
            return self.status
        mapped = _HANGUP_CAUSE_STATUS.get(cause)
        if mapped is not None:
            return mapped
        # An abnormal cause with no mapping is a failure, not a clean hangup.
        return CallStatus.FAILED if self.status is CallStatus.COMPLETED else self.status


async def parse_telnyx_webhook(request: Request) -> TelnyxCallEvent:
    """Read a Telnyx voice webhook in whichever format it arrived in.

    The body has already been read once by signature verification; Starlette
    caches it, so reading it again here is free and does not consume the stream.
    """
    body = await request.body()
    content_type = request.headers.get("content-type", "")

    if "json" in content_type.lower():
        return _from_call_control(body)

    # Anything else is treated as form-encoded. TeXML posts
    # application/x-www-form-urlencoded, but a body that parses as JSON is
    # accepted as Call Control regardless of what the header claimed, because a
    # misdeclared content type is a far cheaper failure to absorb than a dropped
    # call.
    stripped = body.lstrip()
    if stripped.startswith(b"{"):
        return _from_call_control(body)

    form = await request.form()
    return _from_texml({key: str(value) for key, value in form.items()})


def _from_call_control(body: bytes) -> TelnyxCallEvent:
    try:
        parsed = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}

    data = parsed.get("data", {}) if isinstance(parsed, dict) else {}
    payload = data.get("payload", {}) if isinstance(data, dict) else {}
    event_type = str(data.get("event_type", "")) if isinstance(data, dict) else ""

    duration = payload.get("duration_seconds") or payload.get("call_duration")

    return TelnyxCallEvent(
        wire_format=WireFormat.CALL_CONTROL,
        call_id=str(payload.get("call_control_id", "")),
        from_number=str(payload.get("from", "")),
        to_number=str(payload.get("to", "")),
        event_type=event_type,
        status=_CALL_CONTROL_STATUS.get(event_type),
        hangup_cause=str(payload.get("hangup_cause", "")),
        duration_seconds=_as_int(duration),
        raw=parsed if isinstance(parsed, dict) else {},
    )


def _from_texml(form: dict[str, str]) -> TelnyxCallEvent:
    call_status = form.get("CallStatus", "").strip().lower()
    return TelnyxCallEvent(
        wire_format=WireFormat.TEXML,
        call_id=form.get("CallSid", ""),
        from_number=form.get("From", ""),
        to_number=form.get("To", ""),
        event_type=call_status,
        status=_TEXML_STATUS.get(call_status),
        # `HangupCause` only. `SipResponseCode` is not a hangup cause: a normal
        # completed call carries `200` there, which `resolved_status` would read
        # as an unmapped abnormal cause and store as FAILED. Reading a field for
        # a meaning it does not have is how a healthy call gets reported as a
        # broken one.
        hangup_cause=form.get("HangupCause", ""),
        duration_seconds=_as_int(form.get("CallDuration")),
        raw=dict(form),
    )


def _as_int(value: Any) -> int | None:
    """Parse a count, treating anything unreadable as absent rather than zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


__all__ = ["TelnyxCallEvent", "WireFormat", "parse_telnyx_webhook"]
