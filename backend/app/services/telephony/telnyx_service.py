"""Telnyx telephony service implementation."""

import httpx
import structlog
import telnyx

from app.core.log_redaction import log_exception, redact_phone
from app.services.telephony.base import (
    CallDirection,
    CallInfo,
    CallStatus,
    PhoneNumber,
    TelephonyProvider,
)

logger = structlog.get_logger()


TEXML_APP_NAME = "voice-noob-inbound"


def _call_id_from_response(body: dict[str, object]) -> str:
    """The call id from a create-call response.

    Documented: a flat ``call_sid``. Also accepted, in case Telnyx wraps it as
    its other endpoints do: the same under ``data``, or ``sid`` /
    ``call_control_id``. Empty when none is present, never invented.
    """
    inner = body.get("data")
    for source in (body, inner if isinstance(inner, dict) else {}):
        for key in ("call_sid", "sid", "call_control_id"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


class TelnyxConfigurationError(Exception):
    """The Telnyx account is missing something an outbound call needs."""


class TelnyxService(TelephonyProvider):
    """Telnyx telephony service for voice calls and phone number management."""

    def __init__(self, api_key: str, public_key: str | None = None):
        """Initialize Telnyx client.

        Args:
            api_key: Telnyx API Key
            public_key: Telnyx Public Key (for webhook verification)
        """
        self.api_key = api_key
        self.public_key = public_key
        telnyx.api_key = api_key  # type: ignore[attr-defined]
        self.logger = logger.bind(provider="telnyx")
        self._http_client: httpx.AsyncClient | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client for TeXML API calls."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url="https://api.telnyx.com/v2",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._http_client

    async def initiate_call(
        self,
        to_number: str,
        from_number: str,
        webhook_url: str,
        agent_id: str | None = None,
    ) -> CallInfo:
        """Initiate an outbound call via Telnyx TeXML.

        Args:
            to_number: Destination phone number (E.164 format)
            from_number: Source phone number (E.164 format)
            webhook_url: URL for TeXML instructions when call connects
            agent_id: Optional agent ID for context

        Returns:
            CallInfo with call details
        """
        self.logger.info(
            "initiating_call",
            to=redact_phone(to_number),
            from_=redact_phone(from_number),
            webhook_url=webhook_url,
            agent_id=agent_id,
        )

        client = await self._get_http_client()

        # Telnyx "Create a connection-scoped TeXML call": POST
        # /texml/calls/{texml_application_id}. Field names are case-sensitive
        # PascalCase; `Url` overrides the application's XML request URL.
        # The StatusCallback* fields are NOT listed on this operation (they are
        # on the account-scoped one), so Telnyx may ignore them; they are the
        # answer URL's twin and carry the same record_id when honoured.
        payload = {
            "To": to_number,
            "From": from_number,
            "Url": webhook_url,
            "StatusCallback": webhook_url.replace("/answer", "/status"),
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": "initiated ringing answered completed",
        }

        application_id = await self._get_texml_application_id()
        response = await client.post(f"/texml/calls/{application_id}", json=payload)
        response.raise_for_status()
        call_id = _call_id_from_response(response.json())

        self.logger.info("call_initiated", call_sid=call_id)

        return CallInfo(
            call_id=call_id,
            call_control_id=call_id,
            from_number=from_number,
            to_number=to_number,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.INITIATED,
            agent_id=agent_id,
        )

    async def initiate_call_via_call_control(
        self,
        to_number: str,
        from_number: str,
        connection_id: str,
        webhook_url: str,
        agent_id: str | None = None,
    ) -> CallInfo:
        """Initiate an outbound call via Telnyx Call Control API.

        Args:
            to_number: Destination phone number (E.164 format)
            from_number: Source phone number (E.164 format)
            connection_id: Telnyx connection ID
            webhook_url: URL for call event webhooks
            agent_id: Optional agent ID for context

        Returns:
            CallInfo with call details
        """
        self.logger.info(
            "initiating_call_via_call_control",
            to=redact_phone(to_number),
            from_=redact_phone(from_number),
            connection_id=connection_id,
        )

        call = telnyx.Call.create(  # type: ignore[attr-defined]
            connection_id=connection_id,
            to=to_number,
            from_=from_number,
            webhook_url=webhook_url,
        )

        self.logger.info("call_initiated", call_control_id=call.call_control_id)

        return CallInfo(
            call_id=call.call_control_id,
            call_control_id=call.call_control_id,
            from_number=from_number,
            to_number=to_number,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.INITIATED,
            agent_id=agent_id,
        )

    async def hangup_call(self, call_id: str) -> bool:
        """Hang up an active Telnyx call.

        Args:
            call_id: Telnyx Call Control ID

        Returns:
            True if successful
        """
        self.logger.info("hanging_up_call", call_control_id=call_id)

        try:
            client = await self._get_http_client()
            response = await client.post(f"/calls/{call_id}/actions/hangup", json={})
            response.raise_for_status()
            return True
        except Exception:
            log_exception(self.logger, "hangup_failed", None, call_control_id=call_id)
            return False

    async def answer_call(self, call_control_id: str, webhook_url: str | None = None) -> bool:
        """Answer an incoming call.

        Args:
            call_control_id: Telnyx Call Control ID
            webhook_url: Optional webhook URL for call events

        Returns:
            True if successful
        """
        self.logger.info("answering_call", call_control_id=call_control_id)

        try:
            client = await self._get_http_client()
            payload: dict[str, str] = {}
            if webhook_url:
                payload["webhook_url"] = webhook_url

            response = await client.post(
                f"/calls/{call_control_id}/actions/answer",
                json=payload,
            )
            response.raise_for_status()
            return True
        except Exception:
            log_exception(self.logger, "answer_failed", None, call_control_id=call_control_id)
            return False

    async def stream_audio(
        self,
        call_control_id: str,
        stream_url: str,
        stream_track: str = "both_tracks",
    ) -> bool:
        """Start streaming audio to/from a WebSocket.

        Args:
            call_control_id: Telnyx Call Control ID
            stream_url: WebSocket URL for audio streaming
            stream_track: Which tracks to stream (inbound_track, outbound_track, both_tracks)

        Returns:
            True if successful
        """
        self.logger.info(
            "starting_stream",
            call_control_id=call_control_id,
            stream_url=stream_url,
        )

        try:
            client = await self._get_http_client()
            response = await client.post(
                f"/calls/{call_control_id}/actions/streaming_start",
                json={
                    "stream_url": stream_url,
                    "stream_track": stream_track,
                },
            )
            response.raise_for_status()
            return True
        except Exception as e:
            log_exception(self.logger, "stream_start_failed", e, call_control_id=call_control_id)
            return False

    async def list_phone_numbers(self) -> list[PhoneNumber]:
        """List all Telnyx phone numbers.

        Returns:
            List of PhoneNumber objects
        """
        self.logger.info("listing_phone_numbers")

        numbers = []
        client = await self._get_http_client()

        response = await client.get("/phone_numbers")
        response.raise_for_status()
        data = response.json()

        for number in data.get("data", []):
            numbers.append(
                PhoneNumber(
                    id=number.get("id", ""),
                    phone_number=number.get("phone_number", ""),
                    friendly_name=number.get("connection_name"),
                    provider="telnyx",
                    capabilities={
                        "voice": True,
                        "sms": number.get("messaging_profile_id") is not None,
                    },
                )
            )

        self.logger.info("phone_numbers_listed", count=len(numbers))
        return numbers

    async def search_phone_numbers(
        self,
        country: str = "US",
        area_code: str | None = None,
        contains: str | None = None,
        limit: int = 10,
    ) -> list[PhoneNumber]:
        """Search for available Telnyx phone numbers.

        Args:
            country: Country code (e.g., "US")
            area_code: Area code filter (NPA)
            contains: Pattern to match
            limit: Maximum results

        Returns:
            List of available PhoneNumber objects
        """
        self.logger.info(
            "searching_phone_numbers",
            country=country,
            area_code=area_code,
            contains=contains,
        )

        client = await self._get_http_client()

        params: dict[str, str | int | bool] = {
            "filter[country_code]": country,
            "filter[features]": "voice",
            "filter[limit]": limit,
        }
        if area_code:
            params["filter[national_destination_code]"] = area_code
        if contains:
            params["filter[phone_number][contains]"] = contains

        response = await client.get("/available_phone_numbers", params=params)
        response.raise_for_status()
        data = response.json()

        numbers = []
        for number in data.get("data", []):
            numbers.append(
                PhoneNumber(
                    id="",  # Not purchased yet
                    phone_number=number.get("phone_number", ""),
                    friendly_name=number.get("region_information", [{}])[0].get("region_name"),
                    provider="telnyx",
                    capabilities={
                        "voice": "voice" in number.get("features", []),
                        "sms": "sms" in number.get("features", []),
                    },
                )
            )

        self.logger.info("phone_numbers_found", count=len(numbers))
        return numbers

    async def purchase_phone_number(self, phone_number: str) -> PhoneNumber:
        """Purchase a Telnyx phone number.

        Args:
            phone_number: Phone number to purchase (E.164 format)

        Returns:
            Purchased PhoneNumber object
        """
        self.logger.info("purchasing_phone_number", phone_number=redact_phone(phone_number))

        client = await self._get_http_client()

        # First, create a number order
        response = await client.post(
            "/number_orders",
            json={
                "phone_numbers": [{"phone_number": phone_number}],
            },
        )
        response.raise_for_status()
        order_data = response.json()

        # Get the phone number ID from the order
        phone_numbers = order_data.get("data", {}).get("phone_numbers", [])
        if not phone_numbers:
            raise ValueError("No phone number returned from order")

        number_data = phone_numbers[0]

        self.logger.info("phone_number_purchased", id=number_data.get("id"))

        return PhoneNumber(
            id=number_data.get("id", ""),
            phone_number=number_data.get("phone_number", phone_number),
            friendly_name=None,
            provider="telnyx",
            capabilities={"voice": True, "sms": True},
        )

    async def release_phone_number(self, phone_number_id: str) -> bool:
        """Release a Telnyx phone number.

        Args:
            phone_number_id: Phone number ID to release

        Returns:
            True if successful
        """
        self.logger.info("releasing_phone_number", id=phone_number_id)

        try:
            client = await self._get_http_client()
            response = await client.delete(f"/phone_numbers/{phone_number_id}")
            response.raise_for_status()
            return True
        except Exception as e:
            log_exception(self.logger, "release_failed", e, id=phone_number_id)
            return False

    async def configure_phone_number(
        self,
        phone_number_id: str,
        connection_id: str | None = None,
        texml_application_id: str | None = None,
    ) -> bool:
        """Configure a phone number with connection or TeXML application.

        Args:
            phone_number_id: Phone number ID
            connection_id: Telnyx connection ID for Call Control
            texml_application_id: Telnyx TeXML Application ID

        Returns:
            True if successful
        """
        self.logger.info(
            "configuring_phone_number",
            id=phone_number_id,
            connection_id=connection_id,
            texml_application_id=texml_application_id,
        )

        try:
            client = await self._get_http_client()
            payload: dict[str, str] = {}

            if connection_id:
                payload["connection_id"] = connection_id
            if texml_application_id:
                payload["texml_application_id"] = texml_application_id

            response = await client.patch(
                f"/phone_numbers/{phone_number_id}",
                json=payload,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            log_exception(self.logger, "configure_failed", e, id=phone_number_id)
            return False

    async def configure_phone_number_webhook(
        self,
        phone_number_id: str,
        voice_url: str,
        status_callback_url: str | None = None,
    ) -> bool:
        """Configure webhook URLs for a phone number via TeXML application.

        For Telnyx, we need to create/get a TeXML application with the webhook URL,
        then assign it to the phone number.

        Args:
            phone_number_id: Phone number ID
            voice_url: URL for incoming call webhooks
            status_callback_url: URL for status callbacks (optional)

        Returns:
            True if successful
        """
        self.logger.info(
            "configuring_phone_number_webhook",
            id=phone_number_id,
            voice_url=voice_url,
        )

        try:
            # Get or create TeXML application with our webhook URL
            texml_app_id = await self._get_or_create_texml_application(
                voice_url, status_callback_url
            )

            if not texml_app_id:
                self.logger.error("failed_to_get_texml_app")
                return False

            # Assign the TeXML application to the phone number
            return await self.configure_phone_number(
                phone_number_id=phone_number_id,
                texml_application_id=texml_app_id,
            )
        except Exception as e:
            log_exception(self.logger, "webhook_config_failed", e, id=phone_number_id)
            return False

    async def _get_or_create_texml_application(
        self,
        voice_url: str,
        status_callback_url: str | None = None,
    ) -> str | None:
        """Get or create a TeXML application with the specified webhook URL.

        Args:
            voice_url: URL for incoming call webhooks
            status_callback_url: URL for status callbacks (optional)

        Returns:
            TeXML application ID or None if failed
        """
        client = await self._get_http_client()
        app_name = TEXML_APP_NAME

        try:
            # List existing TeXML applications
            response = await client.get("/texml_applications")
            response.raise_for_status()
            data = response.json()

            # Look for existing app with our name
            for app in data.get("data", []):
                if app.get("friendly_name") == app_name:
                    app_id = app.get("id")
                    # Update the webhook URL in case it changed
                    update_payload: dict[str, str] = {"voice_url": voice_url}
                    if status_callback_url:
                        update_payload["status_callback_url"] = status_callback_url

                    await client.patch(f"/texml_applications/{app_id}", json=update_payload)
                    self.logger.info("updated_texml_app", id=app_id)
                    return str(app_id)

            # Create new TeXML application
            create_payload: dict[str, str] = {
                "friendly_name": app_name,
                "voice_url": voice_url,
                "voice_method": "POST",
            }
            if status_callback_url:
                create_payload["status_callback_url"] = status_callback_url
                create_payload["status_callback_method"] = "POST"

            response = await client.post("/texml_applications", json=create_payload)
            response.raise_for_status()
            new_data = response.json()
            app_id = new_data.get("data", {}).get("id")
            self.logger.info("created_texml_app", id=app_id)
            return str(app_id) if app_id else None

        except Exception as e:
            log_exception(self.logger, "texml_app_error", e)
            return None

    async def _get_texml_application_id(self) -> str:
        """The id of the TeXML application the purchase flow attaches numbers to.

        Telnyx's outbound TeXML call is scoped to a TeXML application, not to a
        credential connection. Nothing is created here: an application made
        without our voice URL would answer calls with nothing.

        Raises:
            TelnyxConfigurationError: when the account has no such application.
        """
        client = await self._get_http_client()
        response = await client.get("/texml_applications")
        response.raise_for_status()
        for app in response.json().get("data", []):
            if app.get("friendly_name") == TEXML_APP_NAME and app.get("id"):
                return str(app["id"])
        raise TelnyxConfigurationError(
            f"No Telnyx TeXML application named '{TEXML_APP_NAME}' exists on this account. "
            "Buy or assign a number through Synthiq first (that creates it), or create a "
            "TeXML application with that name in Telnyx Mission Control."
        )

    def generate_answer_response(self, websocket_url: str, agent_id: str | None = None) -> str:  # noqa: ARG002
        """Generate TeXML response to answer a call and stream to WebSocket.

        Three attributes here are load-bearing and none of them are optional:

        ``bidirectionalMode="rtp"``
            Telnyx defaults this to ``mp3``. On the default, audio the agent
            sends back is interpreted as an MP3 stream, and this bridge sends
            base64 G.711 µ-law — so the caller hears nothing while the logs show
            audio being written. Only ``rtp`` carries raw codec frames back.

        ``bidirectionalCodec="PCMU"``
            µ-law, matching the ``g711_ulaw`` format the realtime session is
            configured for in both directions. It is also Telnyx's default, but
            a default that silences a call if it ever moves is worth stating.

        ``bidirectionalSamplingRate="8000"``
            8 kHz, matching PCMU on the PSTN.

        ``<Hangup/>`` closes the leg the moment the stream ends. Telnyx documents
        ``<Connect>`` as running the *next* instruction once the connected service
        stops, so anything between Connect and Hangup is time the caller spends
        listening to silence after the conversation is over. An earlier version of
        this carried a ``<Pause length="40"/>`` there, copied from a reference
        implementation as a guard against Connect returning early; it guards
        nothing Connect does not already guarantee, and it ended every call with
        forty seconds of dead air.

        Args:
            websocket_url: WebSocket URL for media streaming
            agent_id: Optional agent ID for context

        Returns:
            TeXML response string
        """
        # Build TeXML with proper XML escaping for & in URLs
        escaped_ws_url = websocket_url.replace("&", "&amp;")

        texml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{escaped_ws_url}" bidirectionalMode="rtp" bidirectionalCodec="PCMU" bidirectionalSamplingRate="8000" />
    </Connect>
    <Hangup/>
</Response>"""

        return texml

    def generate_gather_response(
        self,
        message: str,
        action_url: str,
        num_digits: int = 1,
        timeout: int = 5,
    ) -> str:
        """Generate TeXML response to gather DTMF input.

        Args:
            message: Message to speak before gathering
            action_url: URL to send gathered digits to
            num_digits: Number of digits to gather
            timeout: Timeout in seconds

        Returns:
            TeXML response string
        """
        escaped_action_url = action_url.replace("&", "&amp;")

        texml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather numDigits="{num_digits}" action="{escaped_action_url}" method="POST" timeout="{timeout}">
        <Say>{message}</Say>
    </Gather>
</Response>"""

        return texml

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
