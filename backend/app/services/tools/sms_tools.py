"""SMS integration tools for voice agents (Twilio and Telnyx)."""

from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

import httpx
import structlog

from app.core.log_redaction import log_exception
from app.core.safe_errors import (
    INVALID_NUMBER,
    is_unavailable_status,
    tool_failure,
    upstream_failure,
)
from app.services.tools.path_ids import encode_path_id, invalid_id_result

logger = structlog.get_logger()

# Provider error codes, matched on the code only (the provider's text is never passed on).
# Bad destination number:
#   Twilio 21211 invalid 'To' number, 21612 cannot route to this number, 21614 'To' is not a
#   valid mobile number (https://www.twilio.com/docs/api/errors, one page per code).
#   Telnyx 40310 invalid 'to', 40012 invalid messaging destination number
#   (https://developers.telnyx.com/docs/messaging/messages/error-codes).
# Temporary, so "unavailable" and not a number problem:
#   Telnyx 40008 undeliverable, the carrier did not accept it (documented as temporary).
# An unknown code falls through to the generic refusal.
_TWILIO_BAD_NUMBER_CODES = frozenset({"21211", "21612", "21614"})
_TELNYX_BAD_NUMBER_CODES = frozenset({"40310", "40012"})
_TELNYX_TEMPORARY_CODES = frozenset({"40008"})
_BAD_NUMBER_MESSAGE = "That phone number could not be reached. Please check the number."


def _provider_codes(response: httpx.Response) -> set[str]:
    """Error codes from a provider reply, without keeping any of its text."""
    try:
        body = response.json()
    except ValueError:
        return set()
    if not isinstance(body, dict):
        return set()
    entries = [body, *(e for e in body.get("errors", []) if isinstance(e, dict))]
    return {str(e["code"]) for e in entries if e.get("code") is not None}


def _send_rejection(
    provider: str,
    response: httpx.Response,
    bad_number_codes: frozenset[str],
    temporary_codes: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Speakable reason for a refused send: bad number, service down, or refused.

    The provider's message can quote the number back, so it is not passed on.
    """
    codes = _provider_codes(response)
    if codes & temporary_codes:
        return upstream_failure("text messaging service", 503)
    if not is_unavailable_status(response.status_code) and codes & bad_number_codes:
        logger.warning(
            "sms_send_rejected",
            provider=provider,
            status_code=response.status_code,
            reason="number",
        )
        return {"success": False, "error": _BAD_NUMBER_MESSAGE, "error_code": INVALID_NUMBER}
    return upstream_failure(
        "text messaging service",
        response.status_code,
        message="The text message could not be sent.",
    )


ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


class TwilioSMSTools:
    """Twilio SMS API integration tools.

    Provides tools for:
    - Sending SMS messages
    - Getting message status
    """

    BASE_URL = "https://api.twilio.com/2010-04-01"

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        """Initialize Twilio SMS tools.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            from_number: Twilio phone number to send from (E.164 format)
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.BASE_URL}/Accounts/{self.account_sid}",
                auth=(self.account_sid, self.auth_token),
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        """Get OpenAI function calling tool definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "twilio_send_sms",
                    "description": "Send an SMS message to a phone number",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {
                                "type": "string",
                                "description": "Recipient phone number (E.164 format, e.g., +14155551234)",
                            },
                            "body": {
                                "type": "string",
                                "description": "Message content (max 1600 characters)",
                            },
                        },
                        "required": ["to", "body"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "twilio_get_message_status",
                    "description": "Get the delivery status of a sent SMS message",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message_sid": {
                                "type": "string",
                                "description": "The Twilio message SID (starts with SM)",
                            },
                        },
                        "required": ["message_sid"],
                    },
                },
            },
        ]

    async def send_sms(self, to: str, body: str) -> dict[str, Any]:
        """Send an SMS message."""
        try:
            # Validate body length
            max_length = 1600
            if len(body) > max_length:
                return {
                    "success": False,
                    "error": f"Message too long. Max {max_length} characters, got {len(body)}",
                }

            response = await self.client.post(
                "/Messages.json",
                data={
                    "To": to,
                    "From": self.from_number,
                    "Body": body,
                },
            )

            if response.status_code != HTTPStatus.CREATED:
                return _send_rejection("twilio", response, _TWILIO_BAD_NUMBER_CODES)

            data = response.json()
            return {
                "success": True,
                "message_sid": data["sid"],
                "to": data["to"],
                "from": data["from"],
                "status": data["status"],
                "message": f"SMS sent successfully to {to}",
            }

        except Exception as e:
            log_exception(logger, "twilio_send_sms_error", e)
            return tool_failure(e, "text messaging service")

    async def get_message_status(self, message_sid: str) -> dict[str, Any]:
        """Get the status of a sent message."""
        safe_message_sid = encode_path_id(message_sid)
        if safe_message_sid is None:
            return invalid_id_result("message_sid")
        try:
            response = await self.client.get(f"/Messages/{safe_message_sid}.json")

            if response.status_code != HTTPStatus.OK:
                return upstream_failure(
                    "text messaging service", response.status_code, not_found="Message not found"
                )

            data = response.json()
            return {
                "success": True,
                "message_sid": data["sid"],
                "to": data["to"],
                "from": data["from"],
                "status": data["status"],
                "date_sent": data.get("date_sent"),
                "error_code": data.get("error_code"),
                "error_message": data.get("error_message"),
            }

        except Exception as e:
            log_exception(logger, "twilio_get_message_status_error", e)
            return tool_failure(e, "text messaging service")

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a Twilio SMS tool by name."""
        tool_map: dict[str, ToolHandler] = {
            "twilio_send_sms": self.send_sms,
            "twilio_get_message_status": self.get_message_status,
        }

        handler = tool_map.get(tool_name)
        if not handler:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}

        result: dict[str, Any] = await handler(**arguments)
        return result


class TelnyxSMSTools:
    """Telnyx SMS API integration tools.

    Provides tools for:
    - Sending SMS messages
    - Getting message status
    """

    BASE_URL = "https://api.telnyx.com/v2"

    def __init__(
        self,
        api_key: str,
        from_number: str,
        messaging_profile_id: str | None = None,
    ) -> None:
        """Initialize Telnyx SMS tools.

        Args:
            api_key: Telnyx API Key
            from_number: Telnyx phone number to send from (E.164 format)
            messaging_profile_id: Optional messaging profile ID for routing
        """
        self.api_key = api_key
        self.from_number = from_number
        self.messaging_profile_id = messaging_profile_id
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        """Get OpenAI function calling tool definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "telnyx_send_sms",
                    "description": "Send an SMS message to a phone number via Telnyx",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {
                                "type": "string",
                                "description": "Recipient phone number (E.164 format, e.g., +14155551234)",
                            },
                            "body": {
                                "type": "string",
                                "description": "Message content",
                            },
                        },
                        "required": ["to", "body"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "telnyx_get_message_status",
                    "description": "Get the delivery status of a sent SMS message via Telnyx",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message_id": {
                                "type": "string",
                                "description": "The Telnyx message ID (UUID)",
                            },
                        },
                        "required": ["message_id"],
                    },
                },
            },
        ]

    async def send_sms(self, to: str, body: str) -> dict[str, Any]:
        """Send an SMS message."""
        try:
            payload: dict[str, Any] = {
                "to": to,
                "from": self.from_number,
                "text": body,
                "type": "SMS",
            }

            if self.messaging_profile_id:
                payload["messaging_profile_id"] = self.messaging_profile_id

            response = await self.client.post("/messages", json=payload)

            if response.status_code != HTTPStatus.OK:
                return _send_rejection(
                    "telnyx", response, _TELNYX_BAD_NUMBER_CODES, _TELNYX_TEMPORARY_CODES
                )

            data = response.json()["data"]
            return {
                "success": True,
                "message_id": data["id"],
                "to": data["to"][0]["phone_number"],
                "from": data["from"]["phone_number"],
                "status": data.get("to", [{}])[0].get("status"),
                "message": f"SMS sent successfully to {to}",
            }

        except Exception as e:
            log_exception(logger, "telnyx_send_sms_error", e)
            return tool_failure(e, "text messaging service")

    async def get_message_status(self, message_id: str) -> dict[str, Any]:
        """Get the status of a sent message."""
        safe_message_id = encode_path_id(message_id)
        if safe_message_id is None:
            return invalid_id_result("message_id")
        try:
            response = await self.client.get(f"/messages/{safe_message_id}")

            if response.status_code != HTTPStatus.OK:
                return upstream_failure(
                    "text messaging service", response.status_code, not_found="Message not found"
                )

            data = response.json()["data"]
            to_info = data.get("to", [{}])[0] if data.get("to") else {}

            return {
                "success": True,
                "message_id": data["id"],
                "to": to_info.get("phone_number"),
                "from": data.get("from", {}).get("phone_number"),
                "status": to_info.get("status"),
                "completed_at": data.get("completed_at"),
                "errors": data.get("errors"),
            }

        except Exception as e:
            log_exception(logger, "telnyx_get_message_status_error", e)
            return tool_failure(e, "text messaging service")

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a Telnyx SMS tool by name."""
        tool_map: dict[str, ToolHandler] = {
            "telnyx_send_sms": self.send_sms,
            "telnyx_get_message_status": self.get_message_status,
        }

        handler = tool_map.get(tool_name)
        if not handler:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}

        result: dict[str, Any] = await handler(**arguments)
        return result
