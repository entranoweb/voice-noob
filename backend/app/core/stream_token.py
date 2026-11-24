"""Short-lived signed tokens that admit a carrier's media stream to an agent.

The media-stream websockets used to accept any connection: anyone who learned an
agent's UUID could open ``/ws/telephony/{provider}/{agent_id}`` and hold a
conversation with that agent, tools included. The carrier cannot present a user
session, so the proof has to come from the one thing this process did
authenticate — the signed webhook that answered the call.

That webhook mints a token here and hands it to the carrier inside the answer
document. The carrier brings it back when it opens the stream, and the socket
admits nobody without it.

What the token binds, and why each part is there:

``provider``
    A token minted for a Telnyx call does not open the Twilio socket.
``agent_id``
    One agent. A token for agent A is refused on agent B's path.
``call_id``
    The identifier the answering webhook knew the call by. On Telnyx it travels
    in the stream URL beside the token; on Twilio it is the ``callSid`` the start
    frame announces. Binding it means the token cannot be moved to another call's
    record (see DECISIONS §33: the identifier on its own is a write primitive).
``expiry``
    The carrier connects within a second or two of receiving the document. Two
    minutes is generous for that and short for anything else.

What it does not do: it is not single-use. A token is valid until it expires, so
whoever holds one inside that window can connect with it. It travels only between
this process and the carrier, over TLS, so that is the accepted residual risk;
closing it would need shared state that a failed Redis would then turn into
dropped calls.

Only the standard library is used, deliberately: this is on the path of every
call and must not acquire a dependency that can move underneath it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import StrEnum

from app.core.config import settings

# How long the carrier has to open the stream after the webhook answered.
DEFAULT_TTL_SECONDS = 120

# The close code a media-stream socket uses when the token is refused. Distinct
# from 4003 (agent inactive) and 4004 (agent unknown) so a log line or a
# preflight can tell an authentication refusal from a routing one.
STREAM_TOKEN_REJECTED = 4401

_VERSION = "v1"
# Domain separation: the application's signing key also signs login tokens. A
# derived key means a stream token can never be mistaken for, or replayed as,
# anything else that key signs.
_KEY_CONTEXT = b"synthiq-voice/stream-token/v1"
_PARTS = 2


class StreamTokenVerdict(StrEnum):
    """Why a token was admitted or refused. The value is what gets logged."""

    OK = "ok"
    MISSING = "missing"
    MALFORMED = "malformed"
    EXPIRED = "expired"
    BAD_SIGNATURE = "bad_signature"


@dataclass(frozen=True)
class StreamTokenResult:
    verdict: StreamTokenVerdict

    @property
    def ok(self) -> bool:
        return self.verdict is StreamTokenVerdict.OK


def _key() -> bytes:
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()


def _signature(provider: str, agent_id: str, call_id: str, expires_at: int) -> str:
    # Length-prefixed fields: a plain join would let ("ab", "c") and ("a", "bc")
    # sign to the same bytes whenever a separator can appear inside a field, and
    # a carrier's call identifier is not ours to constrain.
    fields = (_VERSION, provider, agent_id, call_id, str(expires_at))
    message = b"".join(
        len(encoded).to_bytes(4, "big") + encoded
        for encoded in (field.encode("utf-8") for field in fields)
    )
    digest = hmac.new(_key(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def mint_stream_token(
    *,
    provider: str,
    agent_id: str,
    call_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """A token admitting one carrier stream to one agent for one call.

    The result is URL-safe and XML-safe as it stands: digits, a dot, and
    unpadded base64url. It needs no escaping in a query string or an attribute.
    """
    issued = time.time() if now is None else now
    expires_at = int(issued) + ttl_seconds
    return f"{expires_at}.{_signature(provider, agent_id, call_id, expires_at)}"


def verify_stream_token(
    token: str | None,
    *,
    provider: str,
    agent_id: str,
    call_id: str,
    now: float | None = None,
) -> StreamTokenResult:
    """Whether ``token`` admits this provider, agent and call right now.

    The signature is checked before the expiry is reported, so an attacker
    probing with forged tokens learns nothing about what a valid expiry looks
    like, and the comparison is constant-time.
    """
    if not token:
        return StreamTokenResult(StreamTokenVerdict.MISSING)

    parts = token.split(".")
    if len(parts) != _PARTS or not parts[0].isascii() or not parts[0].isdigit():
        return StreamTokenResult(StreamTokenVerdict.MALFORMED)

    expires_at = int(parts[0])
    expected = _signature(provider, agent_id, call_id, expires_at)
    if not hmac.compare_digest(parts[1].encode("utf-8"), expected.encode("utf-8")):
        return StreamTokenResult(StreamTokenVerdict.BAD_SIGNATURE)

    current = time.time() if now is None else now
    if current >= expires_at:
        return StreamTokenResult(StreamTokenVerdict.EXPIRED)

    return StreamTokenResult(StreamTokenVerdict.OK)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "STREAM_TOKEN_REJECTED",
    "StreamTokenResult",
    "StreamTokenVerdict",
    "mint_stream_token",
    "verify_stream_token",
]
