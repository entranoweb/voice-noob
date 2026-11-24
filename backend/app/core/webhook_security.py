"""Webhook signature validation for Twilio and Telnyx.

A webhook that cannot be verified is refused. That used to depend on ``DEBUG``:
with no key configured, a debug instance accepted every webhook unverified and a
production one rejected every webhook, and neither said so. ``DEBUG`` controls
logging and reloading; it is not a statement about who may post calls to this
instance. The only way to accept an unsigned webhook now is the explicit
``ALLOW_UNSIGNED_WEBHOOKS`` setting, which is off by default and announces itself
when it is on.
"""

import hashlib
import hmac
from functools import wraps
from typing import Any

import structlog
from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.public_url import public_request_url

logger = structlog.get_logger()


def unsigned_webhooks_allowed() -> bool:
    """Whether the operator has explicitly opted out of signature verification."""
    return bool(settings.ALLOW_UNSIGNED_WEBHOOKS)


def warn_if_unsigned_webhooks_allowed() -> bool:
    """Say so, loudly. Called at import and by the application at startup."""
    if not unsigned_webhooks_allowed():
        return False
    logger.critical(
        "unsigned_webhooks_allowed",
        detail=(
            "ALLOW_UNSIGNED_WEBHOOKS is true: carrier webhooks are accepted "
            "without a verified signature, so anyone who can reach this host "
            "can post a call to it. Never set this on an instance a real "
            "number points at."
        ),
    )
    return True


# This module is imported when the telephony routes are, which is at startup.
warn_if_unsigned_webhooks_allowed()


def validate_twilio_signature(
    signature: str,
    url: str,
    params: dict[str, Any],
    auth_token: str,
) -> bool:
    """Validate Twilio webhook signature.

    Twilio signs webhooks using HMAC-SHA1 with the account auth token.
    The signature is passed in the X-Twilio-Signature header.

    Args:
        signature: The X-Twilio-Signature header value
        url: The full URL of the webhook endpoint
        params: The POST parameters from the request
        auth_token: The Twilio auth token

    Returns:
        True if signature is valid, False otherwise
    """
    if not signature or not auth_token:
        return False

    # Sort params and concatenate to URL
    sorted_params = sorted(params.items())
    data = url + "".join(f"{k}{v}" for k, v in sorted_params)

    # Create HMAC-SHA1 signature
    expected_sig = hmac.new(
        auth_token.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha1,
    ).digest()

    import base64

    expected_sig_b64 = base64.b64encode(expected_sig).decode("utf-8")

    return hmac.compare_digest(signature, expected_sig_b64)


def validate_telnyx_signature(
    signature: str,
    timestamp: str,
    payload: bytes,
    public_key: str | None = None,
) -> bool:
    """Validate Telnyx webhook signature.

    Telnyx uses ed25519 signatures for webhook validation.
    Headers: telnyx-signature-ed25519, telnyx-timestamp

    Args:
        signature: The telnyx-signature-ed25519 header value
        timestamp: The telnyx-timestamp header value
        payload: The raw request body
        public_key: The Telnyx public key (optional, uses settings if not provided)

    Returns:
        True if signature is valid, False otherwise
    """
    if not signature or not timestamp:
        return False

    # Use provided key or fall back to settings
    key = public_key or settings.TELNYX_PUBLIC_KEY
    if not key:
        # Nothing to verify against, so nothing is verified: refuse. The explicit
        # override is honoured by the callers below, not here, so this function
        # only ever returns True for a signature it actually checked.
        logger.warning("telnyx_public_key_not_configured")
        return False

    try:
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        # Decode the public key
        public_key_bytes = base64.b64decode(key)
        ed25519_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)

        # Create the signed payload (timestamp + payload)
        signed_payload = f"{timestamp}|".encode() + payload

        # Decode and verify signature
        signature_bytes = base64.b64decode(signature)
        ed25519_key.verify(signature_bytes, signed_payload)

        return True
    except Exception as e:
        logger.warning("telnyx_signature_validation_failed", error=str(e))
        return False


async def get_twilio_webhook_params(request: Request) -> dict[str, str]:
    """Extract form parameters from Twilio webhook request."""
    form_data = await request.form()
    return {key: str(value) for key, value in form_data.items()}


async def verify_twilio_webhook(request: Request, workspace_auth_token: str | None = None) -> bool:
    """Verify Twilio webhook signature from request.

    Twilio signs with the auth token of the account the number belongs to. In a
    deployment where each workspace brings its own Twilio account, that is the
    workspace's token, not this instance's global one, so the caller passes the
    token of the workspace that owns what the webhook is about (the number it
    was sent to, the agent it names, the call it reports on).

    The caller resolves that from the target and never from the ``AccountSid``
    the request claims. A request is only as trustworthy as the secret it is
    checked against: picking the secret from a field the sender controls would
    let one tenant sign, with their own token, a webhook aimed at another
    tenant's number.

    The global token remains a fallback, so a deployment that runs every number
    on one platform account keeps working.

    Args:
        request: FastAPI request object
        workspace_auth_token: Twilio auth token of the workspace the webhook's
            target belongs to, when it has one

    Returns:
        True if the signature is valid, or if ALLOW_UNSIGNED_WEBHOOKS is set

    Raises:
        HTTPException: If the signature is missing, invalid or cannot be checked
    """
    if unsigned_webhooks_allowed():
        logger.critical("twilio_webhook_accepted_unverified")
        return True

    candidates: list[tuple[str, str]] = []
    if workspace_auth_token:
        candidates.append(("workspace", workspace_auth_token))
    if settings.TWILIO_AUTH_TOKEN and workspace_auth_token != settings.TWILIO_AUTH_TOKEN:
        candidates.append(("global", settings.TWILIO_AUTH_TOKEN))
    if not candidates:
        # Refused, not a server error: an instance with no token cannot tell a
        # genuine webhook from a forged one, and the answer to both is no.
        logger.error("twilio_auth_token_not_configured")
        raise HTTPException(status_code=403, detail="Twilio webhook verification is not configured")

    # Get signature from header
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("missing_twilio_signature")
        raise HTTPException(status_code=403, detail="Missing Twilio signature")

    # Twilio signs the URL it posted to, which behind a proxy is not the URL this
    # process sees.
    url = public_request_url(request)
    params = await get_twilio_webhook_params(request)

    # Validate signature
    for source, auth_token in candidates:
        if validate_twilio_signature(signature, url, params, auth_token):
            logger.debug("twilio_signature_verified", credential=source)
            return True

    logger.warning("invalid_twilio_signature", url=url, tried=[source for source, _ in candidates])
    raise HTTPException(status_code=403, detail="Invalid Twilio signature")


async def verify_telnyx_webhook(request: Request, workspace_public_key: str | None = None) -> bool:
    """Verify Telnyx webhook signature from request.

    Telnyx signs with the key pair of the account the number belongs to. Where
    each workspace brings its own Telnyx account, that is the workspace's public
    key, not this instance's global one, so the caller passes the key of the
    workspace that owns what the webhook is about (the number it was sent to,
    the agent it names, the call it reports on).

    As with Twilio, the caller resolves that from the target and never from
    anything the request says about who sent it. A webhook genuinely signed by
    one tenant's Telnyx account (a call they placed to another tenant's number,
    say) is then checked against the other tenant's key and refused.

    The global key remains a fallback, so a deployment that runs every number on
    one platform account keeps working. A deployment where tenants bring their
    own accounts should leave ``TELNYX_PUBLIC_KEY`` unset: while it is set, a
    webhook signed by the platform account is accepted for any workspace.

    Args:
        request: FastAPI request object
        workspace_public_key: Telnyx public key of the workspace the webhook's
            target belongs to, when it has one

    Returns:
        True if the signature is valid, or if ALLOW_UNSIGNED_WEBHOOKS is set

    Raises:
        HTTPException: If the signature is missing, invalid or cannot be checked
    """
    if unsigned_webhooks_allowed():
        logger.critical("telnyx_webhook_accepted_unverified")
        return True

    candidates: list[tuple[str, str]] = []
    if workspace_public_key:
        candidates.append(("workspace", workspace_public_key))
    if settings.TELNYX_PUBLIC_KEY and workspace_public_key != settings.TELNYX_PUBLIC_KEY:
        candidates.append(("global", settings.TELNYX_PUBLIC_KEY))
    if not candidates:
        # Refused, not a server error: with no key there is no telling a genuine
        # webhook from a forged one, and the answer to both is no.
        logger.error("telnyx_public_key_not_configured")
        raise HTTPException(status_code=403, detail="Telnyx webhook verification is not configured")

    # Get signature headers
    signature = request.headers.get("telnyx-signature-ed25519", "")
    timestamp = request.headers.get("telnyx-timestamp", "")

    if not signature or not timestamp:
        logger.warning("missing_telnyx_signature")
        raise HTTPException(status_code=403, detail="Missing Telnyx signature")

    # Get raw body
    body = await request.body()

    # Validate signature
    for source, public_key in candidates:
        if validate_telnyx_signature(signature, timestamp, body, public_key):
            logger.debug("telnyx_signature_verified", credential=source)
            return True

    logger.warning("invalid_telnyx_signature", tried=[source for source, _ in candidates])
    raise HTTPException(status_code=403, detail="Invalid Telnyx signature")


def require_twilio_signature(func: Any) -> Any:
    """Decorator to require valid Twilio signature on webhook endpoints."""

    @wraps(func)
    async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
        await verify_twilio_webhook(request)
        return await func(request, *args, **kwargs)

    return wrapper


def require_telnyx_signature(func: Any) -> Any:
    """Decorator to require valid Telnyx signature on webhook endpoints."""

    @wraps(func)
    async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
        await verify_telnyx_webhook(request)
        return await func(request, *args, **kwargs)

    return wrapper
