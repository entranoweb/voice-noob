"""The few Twilio REST calls and checks the phone leg needs, over plain httpx.

No Twilio package is imported: creating a call, ending it, listing its
recordings and checking a request signature are four small things, and keeping
them here keeps the caller's dependencies to two.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from xml.sax.saxutils import quoteattr

import httpx

API_BASE = "https://api.twilio.com"
STATUS_EVENTS = ("initiated", "ringing", "answered", "completed")
ENDED_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})


class MissingCredentials(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    account_sid: str
    # Signs Twilio's requests to us, and authenticates ours unless an API key is given.
    auth_token: str
    api_key_sid: str = ""
    api_key_secret: str = ""

    @staticmethod
    def from_env(env: Mapping[str, str]) -> Credentials:
        sid = env.get("TWILIO_ACCOUNT_SID", "").strip()
        token = env.get("TWILIO_AUTH_TOKEN", "").strip()
        key_sid = env.get("TWILIO_API_KEY_SID", "").strip()
        key_secret = env.get("TWILIO_API_KEY_SECRET", "").strip()
        missing = [
            name
            for name, value in (("TWILIO_ACCOUNT_SID", sid), ("TWILIO_AUTH_TOKEN", token))
            if not value
        ]
        if bool(key_sid) != bool(key_secret):
            missing.append("TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET (set both or neither)")
        if missing:
            raise MissingCredentials(
                "cannot place a call: missing " + ", ".join(missing) + " in the environment"
            )
        return Credentials(sid, token, key_sid, key_secret)

    def basic_auth(self) -> tuple[str, str]:
        if self.api_key_sid:
            return self.api_key_sid, self.api_key_secret
        return self.account_sid, self.auth_token


def stream_twiml(stream_url: str, parameters: Mapping[str, str] | None = None) -> str:
    """TwiML that connects the call to a bidirectional media stream and nothing else.

    Twilio drops any query string from a stream URL, so values travel as
    ``<Parameter>`` elements and arrive in the stream's ``start`` message.
    """
    inner = "".join(
        f"<Parameter name={quoteattr(name)} value={quoteattr(value)}/>"
        for name, value in (parameters or {}).items()
    )
    stream = f"<Stream url={quoteattr(stream_url)}>{inner}</Stream>"
    return f"<Response><Connect>{stream}</Connect></Response>"


def expected_signature(auth_token: str, url: str, params: Mapping[str, str] | None = None) -> str:
    """The ``X-Twilio-Signature`` Twilio would send for this request.

    HMAC-SHA1 over the full URL exactly as Twilio requested it (query string
    included), followed, for a form POST, by each parameter name and value in
    name order; base64 of the digest. For a GET there are no body parameters.
    """
    data = url + "".join(f"{name}{value}" for name, value in sorted((params or {}).items()))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(
    auth_token: str, url: str, signature: str, params: Mapping[str, str] | None = None
) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(expected_signature(auth_token, url, params), signature)


class CallControl(Protocol):
    """What the phone leg needs from a carrier's control API."""

    async def create_call(
        self, *, to: str, from_: str, twiml: str, status_callback: str, record: bool, timeout_s: int
    ) -> str:
        """Place the call and return the carrier's call id."""
        ...

    async def end_call(self, call_sid: str) -> None: ...


class TwilioRest:
    def __init__(
        self,
        credentials: Credentials,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = API_BASE,
    ) -> None:
        self._credentials = credentials
        self._client = client or httpx.AsyncClient(timeout=30)
        self._root = f"{base_url}/2010-04-01/Accounts/{credentials.account_sid}"

    async def _post(self, path: str, data: list[tuple[str, str]]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._root}{path}", data=dict_of_lists(data), auth=self._credentials.basic_auth()
        )
        if response.status_code >= 400:
            # The body names the cause (unverified number, geo permission, bad TwiML).
            raise RuntimeError(
                f"Twilio refused the request: {response.status_code} {response.text}"
            )
        payload: dict[str, Any] = response.json()
        return payload

    async def create_call(
        self, *, to: str, from_: str, twiml: str, status_callback: str, record: bool, timeout_s: int
    ) -> str:
        data: list[tuple[str, str]] = [
            ("To", to),
            ("From", from_),
            ("Twiml", twiml),
            ("Timeout", str(timeout_s)),
            ("StatusCallback", status_callback),
            # GET, so the same small server that takes the media stream can answer it.
            ("StatusCallbackMethod", "GET"),
        ]
        data.extend(("StatusCallbackEvent", event) for event in STATUS_EVENTS)
        if record:
            data.extend([("Record", "true"), ("RecordingChannels", "dual")])
        payload = await self._post("/Calls.json", data)
        return str(payload["sid"])

    async def end_call(self, call_sid: str) -> None:
        await self._post(f"/Calls/{call_sid}.json", [("Status", "completed")])

    async def list_recordings(self, call_sid: str) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._root}/Calls/{call_sid}/Recordings.json", auth=self._credentials.basic_auth()
        )
        response.raise_for_status()
        recordings: list[dict[str, Any]] = response.json().get("recordings", [])
        return recordings

    async def find_number(self, number: str) -> dict[str, Any] | None:
        """One of the account's own numbers, or None when it does not hold it.

        A read only: nothing is dialled and nothing is changed. A caller ID that
        was verified rather than bought is not one of the account's numbers and
        is not found here.
        """
        response = await self._client.get(
            f"{self._root}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": number},
            auth=self._credentials.basic_auth(),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Twilio refused the request: {response.status_code}")
        held: list[dict[str, Any]] = response.json().get("incoming_phone_numbers", [])
        return held[0] if held else None

    async def aclose(self) -> None:
        await self._client.aclose()


def dict_of_lists(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Form data that keeps repeated names (``StatusCallbackEvent`` is sent four times)."""
    grouped: dict[str, list[str]] = {}
    for name, value in pairs:
        grouped.setdefault(name, []).append(value)
    return grouped
