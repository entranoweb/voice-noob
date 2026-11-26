"""Telephony API routes for Twilio and Telnyx integration.

This module provides:
- Webhook endpoints for inbound calls (Twilio/Telnyx)
- Phone number management (list, search, buy, release)
- Outbound call initiation
- Call status callbacks
- WebSocket endpoint for telephony media streaming
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.settings import get_user_api_keys
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings
from app.core.limiter import limiter
from app.core.log_redaction import redact_phone
from app.core.ownership import agent_is_in_workspace, number_variants, parse_uuid
from app.core.public_url import public_base_url, public_ws_base_url
from app.core.stream_token import mint_stream_token
from app.core.webhook_security import verify_telnyx_webhook, verify_twilio_webhook
from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.campaign import Campaign, CampaignContact, CampaignContactStatus

# Aliased: `PhoneNumber` in this module already means the provider DTO from
# services.telephony.base, which is a different thing from the table.
from app.models.phone_number import PhoneNumber as PhoneNumberRecord
from app.models.phone_number import PhoneNumberStatus
from app.services.qa import trigger_qa_evaluation
from app.services.telephony.number_routing import resolve_number_route
from app.services.telephony.telnyx_events import parse_telnyx_webhook
from app.services.telephony.telnyx_service import TelnyxService
from app.services.telephony.twilio_service import TwilioService

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.services.telephony.base import PhoneNumber

router = APIRouter(prefix="/api/v1/telephony", tags=["telephony"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])

logger = structlog.get_logger()


# =============================================================================
# Pydantic Models
# =============================================================================


class PhoneNumberResponse(BaseModel):
    """Phone number response."""

    id: str
    phone_number: str
    friendly_name: str | None = None
    provider: str
    capabilities: dict[str, bool] | None = None
    assigned_agent_id: str | None = None


class SearchPhoneNumbersRequest(BaseModel):
    """Request to search for phone numbers."""

    provider: str  # "twilio" or "telnyx"
    country: str = "US"
    area_code: str | None = None
    contains: str | None = None
    limit: int = 10


class PurchasePhoneNumberRequest(BaseModel):
    """Request to purchase a phone number."""

    provider: str  # "twilio" or "telnyx"
    phone_number: str


class InitiateCallRequest(BaseModel):
    """Request to initiate an outbound call."""

    to_number: str
    from_number: str
    agent_id: str


class CallResponse(BaseModel):
    """Call response."""

    call_id: str
    call_control_id: str | None = None
    from_number: str
    to_number: str
    direction: str
    status: str
    agent_id: str | None = None


# =============================================================================
# Helper Functions
# =============================================================================


async def get_twilio_service(
    user_id: int, db: AsyncSession, workspace_id: uuid.UUID | None = None
) -> TwilioService | None:
    """Get Twilio service for a user.

    Args:
        user_id: User ID (int)
        db: Database session
        workspace_id: Workspace UUID (required for workspace-specific API keys)
    """
    user_uuid = user_id_to_uuid(user_id)
    user_settings = await get_user_api_keys(user_uuid, db, workspace_id=workspace_id)

    if (
        not user_settings
        or not user_settings.twilio_account_sid
        or not user_settings.twilio_auth_token
    ):
        return None

    return TwilioService(
        account_sid=user_settings.twilio_account_sid,
        auth_token=user_settings.twilio_auth_token,
    )


async def twilio_auth_token_for_workspace(
    agent: Agent | None, workspace_id: uuid.UUID | None, db: AsyncSession
) -> str | None:
    """The Twilio auth token of the workspace a number routed in (not the agent's pick)."""
    if agent is None or workspace_id is None:
        return None
    user_settings = await get_user_api_keys(
        user_id_to_uuid(agent.user_id), db, workspace_id=workspace_id
    )
    return (user_settings.twilio_auth_token or None) if user_settings else None


async def telnyx_public_key_for_workspace(
    agent: Agent | None, workspace_id: uuid.UUID | None, db: AsyncSession
) -> str | None:
    """The Telnyx public key of the workspace a number routed in (not the agent's pick)."""
    if agent is None or workspace_id is None:
        return None
    user_settings = await get_user_api_keys(
        user_id_to_uuid(agent.user_id), db, workspace_id=workspace_id
    )
    return (user_settings.telnyx_public_key or None) if user_settings else None


async def _call_record_for(
    db: AsyncSession,
    provider: str,
    provider_call_id: str,
    *,
    record_id: str | None = None,
    agent_id: str | None = None,
) -> CallRecord | None:
    """The call record a webhook reports on, found by provider and call id.

    The record is what the answering webhook wrote, so its workspace is the one
    whose credentials sign everything else about the call. Newest first if the
    carrier ever reuses an id. `record_id` (put in the answer URL by `/calls`)
    is the fallback for an outbound call whose carrier-side id is not the one
    stored; it has to belong to the same provider and the named agent.
    """
    if provider_call_id:
        found = (
            (
                await db.execute(
                    select(CallRecord)
                    .where(
                        CallRecord.provider == provider,
                        CallRecord.provider_call_id == provider_call_id,
                    )
                    .order_by(CallRecord.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if found is not None:
            return found
    if record_id and agent_id:
        try:
            record_uuid, agent_uuid = uuid.UUID(record_id), uuid.UUID(agent_id)
        except ValueError:
            return None
        return (
            (
                await db.execute(
                    select(CallRecord).where(
                        CallRecord.id == record_uuid,
                        CallRecord.provider == provider,
                        CallRecord.agent_id == agent_uuid,
                    )
                )
            )
            .scalars()
            .first()
        )
    return None


async def twilio_auth_token_for_call(record: CallRecord | None, db: AsyncSession) -> str | None:
    """The Twilio token of the workspace the call record says the call is in."""
    if record is None:
        return None
    owner = await _agent_by_id(record.agent_id, db)
    return await twilio_auth_token_for_workspace(owner, record.workspace_id, db)


async def telnyx_public_key_for_call(record: CallRecord | None, db: AsyncSession) -> str | None:
    """The Telnyx public key of the workspace the call record says the call is in."""
    if record is None:
        return None
    owner = await _agent_by_id(record.agent_id, db)
    return await telnyx_public_key_for_workspace(owner, record.workspace_id, db)


async def _agent_by_id(agent_id: str | uuid.UUID | None, db: AsyncSession) -> Agent | None:
    try:
        agent_uuid = agent_id if isinstance(agent_id, uuid.UUID) else uuid.UUID(str(agent_id))
    except (ValueError, TypeError):
        return None
    result = await db.execute(select(Agent).where(Agent.id == agent_uuid))
    return result.scalar_one_or_none()


async def get_telnyx_service(
    user_id: int, db: AsyncSession, workspace_id: uuid.UUID | None = None
) -> TelnyxService | None:
    """Get Telnyx service for a user.

    Args:
        user_id: User ID (int)
        db: Database session
        workspace_id: Workspace UUID (required for workspace-specific API keys)
    """
    user_uuid = user_id_to_uuid(user_id)
    user_settings = await get_user_api_keys(user_uuid, db, workspace_id=workspace_id)

    if not user_settings or not user_settings.telnyx_api_key:
        return None

    return TelnyxService(
        api_key=user_settings.telnyx_api_key,
        public_key=user_settings.telnyx_public_key,
    )


async def get_agent_by_phone_number(phone_number: str, db: AsyncSession) -> Agent | None:
    """Find the agent a call to this number should reach.

    The rule lives in ``app.services.telephony.number_routing``: a registered,
    active number with a workspace, and an agent in that workspace (the
    assigned agent first, then agents whose ``phone_number_id`` names it).
    Anything else reaches nobody.
    """
    return (await resolve_number_route(phone_number, db)).agent


def build_telnyx_stream_url(
    request: Request,
    agent_id: str | None,
    *,
    call_id: str = "",
    direction: str = "inbound",
) -> str:
    """The wss:// URL a Telnyx media stream should connect back to.

    The call identifier travels in the query string because the media stream and
    the webhook do not agree on one, and the direction travels with it because
    inbound and outbound calls share this endpoint — a trace that labels every
    outbound call inbound is worse than one with no direction at all.

    On the identifier: The webhook knows the call by the identifier
    Telnyx signed the request with; the stream announces a ``call_control_id`` in
    its start frame. On a TeXML application those are not guaranteed to be the
    same string, and the bridge needs to find the row the webhook created in
    order to attach a transcript to it. Passing it explicitly removes the guess.

    On the host: ``PUBLIC_URL`` wins when it is set, because it is already the
    source of truth for the webhook URLs registered against a purchased number
    (``configure_phone_number_webhook``) and the carrier has to reach both at the
    same host. ``request.base_url`` is derived from the request line and the Host
    header, so behind a proxy that does not forward them it resolves to the
    internal address the app is bound to — ``wss://internal:8000/...`` is handed
    to Telnyx, the stream never connects, and the caller hears silence while
    every log line reads healthy. The scheme is forced to ``wss`` either way; it
    is the host that goes wrong.

    On the token: the socket this URL points at admits nobody without one
    (``app.core.stream_token``). It is minted here, by the webhook that has
    just verified the carrier's signature, and is bound to this agent and to
    this ``call_id`` — so it cannot be carried to another agent's socket, nor
    presented beside a different call's identifier to reach that call's row.
    """
    ws_url = public_ws_base_url(request)
    token = mint_stream_token(provider="telnyx", agent_id=str(agent_id), call_id=call_id)
    query = f"direction={quote(direction, safe='')}&token={quote(token, safe='')}"
    if call_id:
        query = f"call_id={quote(call_id, safe='')}&{query}"
    return f"{ws_url}/ws/telephony/telnyx/{agent_id}?{query}"


def build_twilio_stream_url(request: Request, agent_id: str | None) -> str:
    """The wss:// URL a Twilio media stream should connect back to.

    Same host rule as the Telnyx builder, for the same reason. It carries no
    query string on purpose: Twilio drops query parameters from a ``<Stream>``
    URL and delivers custom values as ``<Parameter>`` elements in the start
    frame instead, so that is where this provider's stream token travels
    (``TwilioService.generate_answer_response``).
    """
    return f"{public_ws_base_url(request)}/ws/telephony/twilio/{agent_id}"


def build_answer_webhook_url(
    request: Request, provider: str, agent_id: str, record_id: str | None = None
) -> str:
    """The URL a carrier fetches when an outbound call is answered.

    The carrier calls this from outside, so it needs the public host exactly
    as the stream URL does. Built from ``request.base_url`` it named the
    address the app is bound to whenever a proxy sat in front, and an
    outbound call was answered into nothing.
    """
    url = (
        f"{public_base_url(request)}/webhooks/{provider}/answer?agent_id={quote(agent_id, safe='')}"
    )
    if record_id:
        # Names the call record `/calls` wrote, so the answer webhook can find
        # it even where the carrier's own id for the call differs from the one
        # stored. It is a lookup key only: the request is still verified with
        # that record's workspace key, so naming another record gets nothing.
        url += f"&record_id={quote(record_id, safe='')}"
    return url


async def update_campaign_contact_from_call(
    call_record: CallRecord,
    call_status: str,
    duration_seconds: int,
    db: AsyncSession,
) -> None:
    """Update campaign contact status based on call outcome.

    Args:
        call_record: Call record that just completed
        call_status: Final call status (completed, busy, failed, no-answer, etc.)
        duration_seconds: Call duration in seconds
        db: Database session
    """
    # Only process outbound calls (campaigns make outbound calls)
    if call_record.direction != CallDirection.OUTBOUND.value:
        return

    # Find campaign contact that is currently being called to this number
    # from this campaign's agent (use selectinload to avoid N+1 queries)
    result = await db.execute(
        select(CampaignContact)
        .join(Campaign)
        .options(selectinload(CampaignContact.contact))
        .where(
            CampaignContact.status == CampaignContactStatus.CALLING.value,
            Campaign.agent_id == call_record.agent_id,
        )
    )
    campaign_contacts = result.scalars().all()

    if not campaign_contacts:
        return

    # Find the matching campaign contact by phone number
    for cc in campaign_contacts:
        # Contact is already loaded via selectinload
        contact: Contact | None = cc.contact

        if not contact:
            continue

        # Check if phone numbers match (normalize both)
        contact_phone = contact.phone_number.lstrip("+").replace("-", "").replace(" ", "")
        to_phone = call_record.to_number.lstrip("+").replace("-", "").replace(" ", "")

        if contact_phone != to_phone:
            continue

        # Found the matching campaign contact - update its status
        log = logger.bind(
            campaign_contact_id=str(cc.id),
            contact_id=contact.id,
            call_status=call_status,
        )

        # Map call status to campaign contact status
        status_map = {
            CallStatus.COMPLETED.value: CampaignContactStatus.COMPLETED.value,
            CallStatus.BUSY.value: CampaignContactStatus.BUSY.value,
            CallStatus.FAILED.value: CampaignContactStatus.FAILED.value,
            CallStatus.NO_ANSWER.value: CampaignContactStatus.NO_ANSWER.value,
            CallStatus.CANCELED.value: CampaignContactStatus.FAILED.value,
        }

        new_status = status_map.get(call_status, CampaignContactStatus.COMPLETED.value)
        cc.status = new_status
        cc.last_call_id = call_record.id
        cc.last_call_duration_seconds = duration_seconds
        cc.last_call_outcome = call_status

        # Get the campaign to update stats
        campaign_result = await db.execute(select(Campaign).where(Campaign.id == cc.campaign_id))
        campaign = campaign_result.scalar_one_or_none()

        if campaign:
            campaign.total_call_duration_seconds += duration_seconds

            if new_status == CampaignContactStatus.COMPLETED.value:
                campaign.contacts_completed += 1
            elif new_status in (
                CampaignContactStatus.FAILED.value,
                CampaignContactStatus.BUSY.value,
                CampaignContactStatus.NO_ANSWER.value,
            ):
                # Check if we should retry
                if cc.attempts < campaign.max_attempts_per_contact:
                    # Schedule retry
                    from datetime import timedelta

                    cc.status = CampaignContactStatus.PENDING.value
                    cc.next_attempt_at = datetime.now(UTC) + timedelta(
                        minutes=campaign.retry_delay_minutes
                    )
                    log.info(
                        "Scheduling retry",
                        next_attempt=cc.next_attempt_at.isoformat(),
                    )
                else:
                    campaign.contacts_failed += 1

        log.info("Campaign contact updated", new_status=new_status)
        break  # Only update one campaign contact per call


# =============================================================================
# Phone Number Management Endpoints
# =============================================================================


@router.get("/phone-numbers", response_model=list[PhoneNumberResponse])
async def list_phone_numbers(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    provider: str = Query("twilio", description="Provider: twilio or telnyx"),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> list[PhoneNumberResponse]:
    """List all phone numbers for the user's account.

    Args:
        provider: Telephony provider (twilio or telnyx)
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        List of phone numbers
    """
    log = logger.bind(user_id=current_user.id, provider=provider, workspace_id=workspace_id)
    log.info("listing_phone_numbers")

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    numbers: list[PhoneNumber] = []

    if provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            # Return empty list when credentials not configured (not an error)
            return []
        numbers = await twilio_service.list_phone_numbers()

    elif provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            # Return empty list when credentials not configured (not an error)
            return []
        numbers = await telnyx_service.list_phone_numbers()

    else:
        raise HTTPException(status_code=400, detail="Invalid provider. Use 'twilio' or 'telnyx'.")

    # Map to response model
    return [
        PhoneNumberResponse(
            id=n.id,
            phone_number=n.phone_number,
            friendly_name=n.friendly_name,
            provider=n.provider,
            capabilities=n.capabilities,
            assigned_agent_id=n.assigned_agent_id,
        )
        for n in numbers
    ]


@router.post("/phone-numbers/search", response_model=list[PhoneNumberResponse])
async def search_phone_numbers(
    request: SearchPhoneNumbersRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> list[PhoneNumberResponse]:
    """Search for available phone numbers to purchase.

    Args:
        request: Search parameters
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        List of available phone numbers
    """
    log = logger.bind(user_id=current_user.id, provider=request.provider, workspace_id=workspace_id)
    log.info("searching_phone_numbers", country=request.country, area_code=request.area_code)

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    numbers: list[PhoneNumber] = []

    if request.provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            raise HTTPException(
                status_code=400,
                detail="Twilio credentials not configured. Please add them in Settings.",
            )
        numbers = await twilio_service.search_phone_numbers(
            country=request.country,
            area_code=request.area_code,
            contains=request.contains,
            limit=request.limit,
        )

    elif request.provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            raise HTTPException(
                status_code=400,
                detail="Telnyx credentials not configured. Please add them in Settings.",
            )
        numbers = await telnyx_service.search_phone_numbers(
            country=request.country,
            area_code=request.area_code,
            contains=request.contains,
            limit=request.limit,
        )

    else:
        raise HTTPException(status_code=400, detail="Invalid provider. Use 'twilio' or 'telnyx'.")

    return [
        PhoneNumberResponse(
            id=n.id,
            phone_number=n.phone_number,
            friendly_name=n.friendly_name,
            provider=n.provider,
            capabilities=n.capabilities,
        )
        for n in numbers
    ]


async def _configure_webhook_for_provider(
    service: TwilioService | TelnyxService,
    number_id: str,
    provider: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Configure webhook for a purchased phone number."""
    public_url = settings.PUBLIC_URL
    if not public_url or not number_id:
        return

    voice_url = f"{public_url}/webhooks/{provider}/voice"
    webhook_success = await service.configure_phone_number_webhook(
        phone_number_id=number_id,
        voice_url=voice_url,
    )
    if webhook_success:
        log.info("webhook_configured", provider=provider, voice_url=voice_url)
    else:
        log.warning("webhook_config_failed", provider=provider, phone_number_id=number_id)


@router.post("/phone-numbers/purchase", response_model=PhoneNumberResponse)
@limiter.limit("5/minute")  # Strict rate limit for phone number purchases (costs money!)
async def purchase_phone_number(
    purchase_request: PurchasePhoneNumberRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> PhoneNumberResponse:
    """Purchase a phone number.

    Args:
        purchase_request: Purchase request with provider and phone number
        request: HTTP request (for rate limiting)
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Purchased phone number details
    """
    log = logger.bind(
        user_id=current_user.id, provider=purchase_request.provider, workspace_id=workspace_id
    )
    log.info("purchasing_phone_number", phone_number=redact_phone(purchase_request.phone_number))

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    number: PhoneNumber

    # Get public URL for webhook configuration
    if not settings.PUBLIC_URL:
        log.warning("PUBLIC_URL not configured, webhooks will not be set up automatically")

    if purchase_request.provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            raise HTTPException(
                status_code=400,
                detail="Twilio credentials not configured. Please add them in Settings.",
            )
        number = await twilio_service.purchase_phone_number(purchase_request.phone_number)
        await _configure_webhook_for_provider(twilio_service, number.id, "twilio", log)

    elif purchase_request.provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            raise HTTPException(
                status_code=400,
                detail="Telnyx credentials not configured. Please add them in Settings.",
            )
        number = await telnyx_service.purchase_phone_number(purchase_request.phone_number)
        await _configure_webhook_for_provider(telnyx_service, number.id, "telnyx", log)

    else:
        raise HTTPException(status_code=400, detail="Invalid provider. Use 'twilio' or 'telnyx'.")

    return PhoneNumberResponse(
        id=number.id,
        phone_number=number.phone_number,
        friendly_name=number.friendly_name,
        provider=number.provider,
        capabilities=number.capabilities,
    )


@router.delete("/phone-numbers/{phone_number_id}")
async def release_phone_number(
    phone_number_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    provider: str = Query(..., description="Provider: twilio or telnyx"),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> dict[str, str]:
    """Release a phone number.

    Args:
        phone_number_id: Phone number ID to release
        provider: Telephony provider
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Success message
    """
    log = logger.bind(user_id=current_user.id, provider=provider, workspace_id=workspace_id)
    log.info("releasing_phone_number", phone_number_id=phone_number_id)

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    success = False

    if provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            raise HTTPException(
                status_code=400,
                detail="Twilio credentials not configured.",
            )
        success = await twilio_service.release_phone_number(phone_number_id)

    elif provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            raise HTTPException(
                status_code=400,
                detail="Telnyx credentials not configured.",
            )
        success = await telnyx_service.release_phone_number(phone_number_id)

    else:
        raise HTTPException(status_code=400, detail="Invalid provider.")

    if not success:
        raise HTTPException(status_code=500, detail="Failed to release phone number.")

    return {"message": "Phone number released successfully"}


# =============================================================================
# Outbound Call Endpoints
# =============================================================================


@router.post("/calls", response_model=CallResponse)
@limiter.limit("30/minute")  # Rate limit outbound call initiation (costs money!)
async def initiate_call(
    call_request: InitiateCallRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> CallResponse:
    """Initiate an outbound call.

    Args:
        call_request: Call initiation request
        request: HTTP request (for rate limiting and building webhook URLs)
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Call details
    """
    log = logger.bind(
        user_id=current_user.id, agent_id=call_request.agent_id, workspace_id=workspace_id
    )
    log.info(
        "initiating_call",
        to=redact_phone(call_request.to_number),
        from_=redact_phone(call_request.from_number),
    )

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    # Load agent to get provider preference (verify user owns agent)
    user_uuid = user_id_to_uuid(current_user.id)
    result = await db.execute(
        select(Agent).where(
            Agent.id == parse_uuid(call_request.agent_id, "Agent"),
            Agent.user_id == current_user.id,  # Ensure user owns the agent (an int column)
        )
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Try Telnyx first
    telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
    twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)

    if not telnyx_service and not twilio_service:
        raise HTTPException(
            status_code=400,
            detail="No telephony provider configured. Please add Twilio or Telnyx credentials in Settings.",
        )

    provider = "telnyx" if telnyx_service else "twilio"

    # The workspace comes from the query string, so it is proven before any
    # carrier call (which costs money): the agent must be in it, and the number
    # the call is placed from must be a live, active number registered in it on
    # this carrier (that registration is what proved the account holds it).
    if not await agent_is_in_workspace(db, agent.id, workspace_uuid):
        raise HTTPException(status_code=403, detail="Agent is not in this workspace")
    from_record = (
        (
            await db.execute(
                select(PhoneNumberRecord).where(
                    PhoneNumberRecord.phone_number.in_(number_variants(call_request.from_number)),
                    PhoneNumberRecord.status == PhoneNumberStatus.ACTIVE.value,
                    PhoneNumberRecord.workspace_id == workspace_uuid,
                    PhoneNumberRecord.user_id == user_uuid,
                    PhoneNumberRecord.provider == provider,
                )
            )
        )
        .scalars()
        .first()
    )
    if from_record is None:
        raise HTTPException(
            status_code=422,
            detail="from_number is not an active number registered in this workspace",
        )

    # The call record gets its id now, so the answer URL can name it.
    record_id = uuid.uuid4()

    # The carrier fetches this from outside when the call is answered.
    webhook_url = build_answer_webhook_url(
        request, provider, call_request.agent_id, record_id=str(record_id)
    )

    if telnyx_service:
        call_info = await telnyx_service.initiate_call(
            to_number=call_request.to_number,
            from_number=call_request.from_number,
            webhook_url=webhook_url,
            agent_id=call_request.agent_id,
        )
    elif twilio_service:
        call_info = await twilio_service.initiate_call(
            to_number=call_request.to_number,
            from_number=call_request.from_number,
            webhook_url=webhook_url,
            agent_id=call_request.agent_id,
        )
    else:
        raise HTTPException(status_code=500, detail="Failed to initialize telephony service")

    log.info("call_initiated", call_id=call_info.call_id, provider=provider)

    # Create call record for outbound call (workspace_uuid already available from query param)
    call_record = CallRecord(
        id=record_id,
        user_id=user_id_to_uuid(current_user.id),
        workspace_id=workspace_uuid,
        provider=provider,
        provider_call_id=call_info.call_id,
        agent_id=parse_uuid(call_request.agent_id, "Agent"),
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.INITIATED.value,
        from_number=call_request.from_number,
        to_number=call_request.to_number,
    )
    db.add(call_record)
    await db.commit()
    log.info("call_record_created", record_id=str(call_record.id))

    return CallResponse(
        call_id=call_info.call_id,
        call_control_id=call_info.call_control_id,
        from_number=call_info.from_number,
        to_number=call_info.to_number,
        direction=call_info.direction.value,
        status=call_info.status.value,
        agent_id=call_info.agent_id,
    )


@router.post("/calls/{call_id}/hangup")
async def hangup_call(
    call_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    provider: str = Query(..., description="Provider: twilio or telnyx"),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> dict[str, str]:
    """Hang up an active call.

    Args:
        call_id: Call ID to hang up
        provider: Telephony provider
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Success message
    """
    log = logger.bind(
        user_id=current_user.id, call_id=call_id, provider=provider, workspace_id=workspace_id
    )
    log.info("hanging_up_call")

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    success = False

    if provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if twilio_service:
            success = await twilio_service.hangup_call(call_id)

    elif provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if telnyx_service:
            success = await telnyx_service.hangup_call(call_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to hang up call")

    return {"message": "Call ended successfully"}


# =============================================================================
# Twilio Webhook Endpoints
# =============================================================================


@webhook_router.post("/twilio/voice", response_class=HTMLResponse)
async def twilio_voice_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    call_sid: str = Form(default="", alias="CallSid"),
    from_number: str = Form(default="", alias="From"),
    to_number: str = Form(default="", alias="To"),
    call_status: str = Form(default="", alias="CallStatus"),
) -> Response:
    """Handle incoming Twilio voice calls.

    This webhook is called when a call comes in to a Twilio phone number.
    It returns TwiML to connect the call to our WebSocket for AI handling.
    """
    # The number decides whose Twilio account should have signed this: the
    # workspace that owns the agent the number routes to. Looked up before the
    # signature is checked (a read, nothing is written) and then verified.
    route = await resolve_number_route(to_number, db, provider="twilio")
    agent = route.agent
    await verify_twilio_webhook(
        request, await twilio_auth_token_for_workspace(agent, route.workspace_id, db)
    )

    log = logger.bind(
        webhook="twilio_voice",
        call_sid=call_sid,
        from_number=redact_phone(from_number),
        to_number=redact_phone(to_number),
        status=call_status,
    )
    log.info("twilio_incoming_call")

    agent_id = str(agent.id) if agent else None

    if not agent:
        log.warning("no_agent_for_number", to_number=redact_phone(to_number))
        # Return TwiML that says no agent is available
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, no agent is configured for this number. Goodbye.</Say>
    <Hangup/>
</Response>""",
            media_type="application/xml",
        )

    # Get workspace for the agent
    agent_workspace_id = route.workspace_id

    # Create call record for inbound call
    call_record = CallRecord(
        # call_records keys its owner by UUID while Agent carries the integer
        # user id. Passing the integer straight through raises on insert against
        # Postgres, so no inbound call has ever landed a row.
        user_id=user_id_to_uuid(agent.user_id),
        workspace_id=agent_workspace_id,
        provider="twilio",
        provider_call_id=call_sid,
        agent_id=agent.id,
        direction=CallDirection.INBOUND.value,
        status=CallStatus.RINGING.value,
        from_number=from_number,
        to_number=to_number,
    )
    db.add(call_record)
    await db.commit()
    log.info("call_record_created", record_id=str(call_record.id))

    # Build WebSocket URL for media streaming, and the token that admits this
    # call's stream to this agent. Bound to the CallSid Twilio signed, which is
    # the callSid its start frame will announce.
    stream_url = build_twilio_stream_url(request, agent_id)
    stream_token = mint_stream_token(provider="twilio", agent_id=str(agent_id), call_id=call_sid)

    # Generate TwiML to connect to our WebSocket
    twilio_service = TwilioService("", "")  # Just need TwiML generation
    twiml = twilio_service.generate_answer_response(stream_url, agent_id, stream_token=stream_token)

    log.info("twilio_twiml_generated", agent_id=agent_id)

    return Response(content=twiml, media_type="application/xml")


@webhook_router.post("/twilio/status")
async def twilio_status_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    call_sid: str = Form(default="", alias="CallSid"),
    call_status: str = Form(default="", alias="CallStatus"),
    call_duration: str = Form(default="0", alias="CallDuration"),
    from_number: str = Form(default="", alias="From"),
    to_number: str = Form(default="", alias="To"),
) -> dict[str, str]:
    """Handle Twilio call status callbacks.

    Called when call status changes (initiated, ringing, answered, completed).
    """
    # Find the call record first: the call it reports on decides whose Twilio
    # account should have signed this. Nothing is changed until that is verified.
    call_record = await _call_record_for(db, "twilio", call_sid)
    await verify_twilio_webhook(request, await twilio_auth_token_for_call(call_record, db))

    log = logger.bind(
        webhook="twilio_status",
        call_sid=call_sid,
        status=call_status,
        duration=call_duration,
    )
    log.info("twilio_status_update")

    if call_record:
        # Map Twilio status to our status
        status_map = {
            "initiated": CallStatus.INITIATED.value,
            "ringing": CallStatus.RINGING.value,
            "in-progress": CallStatus.IN_PROGRESS.value,
            "completed": CallStatus.COMPLETED.value,
            "busy": CallStatus.BUSY.value,
            "failed": CallStatus.FAILED.value,
            "no-answer": CallStatus.NO_ANSWER.value,
            "canceled": CallStatus.CANCELED.value,
        }

        call_record.status = status_map.get(call_status, call_status)

        # Update timestamps based on status
        if call_status == "in-progress" and not call_record.answered_at:
            call_record.answered_at = datetime.now(UTC)
        elif call_status in ("completed", "busy", "failed", "no-answer", "canceled"):
            call_record.ended_at = datetime.now(UTC)
            if call_duration:
                call_record.duration_seconds = int(call_duration)

            # Update campaign contact status if this was a campaign call
            await update_campaign_contact_from_call(
                call_record=call_record,
                call_status=call_record.status,
                duration_seconds=call_record.duration_seconds or 0,
                db=db,
            )

            # Trigger QA evaluation for completed calls
            if call_record.status == CallStatus.COMPLETED.value:
                background_tasks.add_task(trigger_qa_evaluation, call_record.id)
                log.info("qa_evaluation_triggered", call_id=str(call_record.id))

        await db.commit()
        log.info("call_record_updated", record_id=str(call_record.id), status=call_status)
    else:
        log.warning("call_record_not_found", call_sid=call_sid)

    return {"status": "received"}


@webhook_router.post("/twilio/answer", response_class=HTMLResponse)
async def twilio_answer_webhook(
    request: Request,
    agent_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle Twilio outbound call connection.

    Called when an outbound call is answered by the recipient.
    Returns TwiML to connect to our WebSocket.
    """
    # Twilio posts the CallSid with the answer request; the stream's start
    # frame announces the same one, and the token is bound to it. The call
    # record `/calls` wrote for that CallSid decides whose Twilio account
    # should have signed this: its workspace, never an arbitrary pick among the
    # agent's. A record for another agent is no record.
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    call_record = await _call_record_for(db, "twilio", call_sid)
    if call_record is not None and str(call_record.agent_id) != agent_id:
        call_record = None
    await verify_twilio_webhook(request, await twilio_auth_token_for_call(call_record, db))

    log = logger.bind(webhook="twilio_answer", agent_id=agent_id, call_sid=call_sid)
    log.info("twilio_outbound_answered")

    stream_url = build_twilio_stream_url(request, agent_id)
    stream_token = mint_stream_token(provider="twilio", agent_id=agent_id, call_id=call_sid)

    twilio_service = TwilioService("", "")
    twiml = twilio_service.generate_answer_response(stream_url, agent_id, stream_token=stream_token)

    return Response(content=twiml, media_type="application/xml")


# =============================================================================
# Telnyx Webhook Endpoints
# =============================================================================


@webhook_router.post("/telnyx/voice", response_class=HTMLResponse)
async def telnyx_voice_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle incoming Telnyx voice calls.

    This webhook is called when a call comes in to a Telnyx phone number.
    It returns TeXML to connect the call to our WebSocket for AI handling.
    """
    # Accepts both the TeXML (form-encoded) and Call Control (JSON) shapes. A
    # number attached to a TeXML application — which is what the purchase flow
    # configures, and the only mode in which returning a document does anything —
    # posts the form-encoded one.
    #
    # Read before it is verified, and only to learn which number it is for: the
    # number decides whose Telnyx account should have signed it. Nothing is
    # logged, stored or answered from it until the signature has been checked.
    event = await parse_telnyx_webhook(request)
    route = await resolve_number_route(event.to_number, db, provider="telnyx")
    agent = route.agent
    await verify_telnyx_webhook(
        request, await telnyx_public_key_for_workspace(agent, route.workspace_id, db)
    )

    log = logger.bind(
        webhook="telnyx_voice",
        call_control_id=event.call_id,
        from_number=redact_phone(event.from_number),
        to_number=redact_phone(event.to_number),
        event_type=event.event_type,
        wire_format=event.wire_format.value,
    )
    log.info("telnyx_incoming_call")

    agent_id = str(agent.id) if agent else None

    if not agent:
        log.warning("no_agent_for_number", to_number=redact_phone(event.to_number))
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, no agent is configured for this number. Goodbye.</Say>
    <Hangup/>
</Response>""",
            media_type="application/xml",
        )

    # Get workspace for the agent
    agent_workspace_id = route.workspace_id

    # Create call record for inbound call
    call_record = CallRecord(
        # call_records keys its owner by UUID while Agent carries the integer
        # user id. Passing the integer straight through raises on insert against
        # Postgres, so no inbound call has ever landed a row.
        user_id=user_id_to_uuid(agent.user_id),
        workspace_id=agent_workspace_id,
        provider="telnyx",
        provider_call_id=event.call_id,
        agent_id=agent.id,
        direction=CallDirection.INBOUND.value,
        status=CallStatus.RINGING.value,
        from_number=event.from_number,
        to_number=event.to_number,
    )
    db.add(call_record)
    await db.commit()
    log.info("call_record_created", record_id=str(call_record.id))

    stream_url = build_telnyx_stream_url(request, agent_id, call_id=event.call_id)

    telnyx_service = TelnyxService("")
    texml = telnyx_service.generate_answer_response(stream_url, agent_id)

    log.info("telnyx_texml_generated", agent_id=agent_id)

    return Response(content=texml, media_type="application/xml")


@webhook_router.post("/telnyx/answer", response_class=HTMLResponse)
async def telnyx_answer_webhook(
    request: Request,
    agent_id: str = Query(default=""),
    record_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle Telnyx outbound call connection.

    Called when an outbound call is answered by the recipient.
    Returns TeXML to connect to our WebSocket.
    """
    # The call record `/calls` wrote decides whose Telnyx account should have
    # signed this: its workspace, never an arbitrary pick among the agent's. The
    # carrier's id for the call is matched first; the record id carried in the
    # answer URL covers an id that is not the one stored.
    event = await parse_telnyx_webhook(request)
    call_record = await _call_record_for(
        db, "telnyx", event.call_id, record_id=record_id, agent_id=agent_id
    )
    if call_record is not None and str(call_record.agent_id) != agent_id:
        call_record = None
    await verify_telnyx_webhook(request, await telnyx_public_key_for_call(call_record, db))

    log = logger.bind(
        webhook="telnyx_answer",
        agent_id=agent_id,
        call_control_id=event.call_id,
        wire_format=event.wire_format.value,
    )
    log.info("telnyx_outbound_answered")

    # The stream is admitted against the id the call record is stored under.
    stream_call_id = call_record.provider_call_id if call_record else event.call_id
    stream_url = build_telnyx_stream_url(
        request, agent_id, call_id=stream_call_id, direction="outbound"
    )

    telnyx_service = TelnyxService("")
    texml = telnyx_service.generate_answer_response(stream_url, agent_id)

    return Response(content=texml, media_type="application/xml")


@webhook_router.post("/telnyx/status")
async def telnyx_status_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Handle Telnyx call status callbacks.

    Called when call events occur (call.initiated, call.answered, call.hangup, etc).
    """
    # Find the call record first: the call it reports on decides whose Telnyx
    # account should have signed this. Nothing is changed until that is verified.
    event = await parse_telnyx_webhook(request)
    call_record = await _call_record_for(db, "telnyx", event.call_id)
    await verify_telnyx_webhook(request, await telnyx_public_key_for_call(call_record, db))

    log = logger.bind(
        webhook="telnyx_status",
        event_type=event.event_type,
        call_control_id=event.call_id,
        wire_format=event.wire_format.value,
    )
    log.info("telnyx_status_update")

    if call_record:
        new_status = event.resolved_status()
        if new_status is not None:
            call_record.status = new_status.value

        if event.is_answered and not call_record.answered_at:
            call_record.answered_at = datetime.now(UTC)
        elif event.is_hangup:
            call_record.ended_at = datetime.now(UTC)
            # Prefer the carrier's own figure; fall back to our timestamps. The
            # carrier bills on its number, and a call answered before this
            # process restarted has no answered_at to subtract from.
            if event.duration_seconds is not None:
                call_record.duration_seconds = event.duration_seconds
            elif call_record.answered_at:
                duration = (call_record.ended_at - call_record.answered_at).total_seconds()
                call_record.duration_seconds = int(duration)

            # Update campaign contact status if this was a campaign call
            await update_campaign_contact_from_call(
                call_record=call_record,
                call_status=call_record.status,
                duration_seconds=call_record.duration_seconds or 0,
                db=db,
            )

            # Trigger QA evaluation for completed calls
            if call_record.status == CallStatus.COMPLETED.value:
                background_tasks.add_task(trigger_qa_evaluation, call_record.id)
                log.info("qa_evaluation_triggered", call_id=str(call_record.id))

        await db.commit()
        # Not `event=`: structlog reserves that key for the message itself, and
        # passing it raises inside the logger.
        log.info(
            "call_record_updated",
            record_id=str(call_record.id),
            telnyx_event=event.event_type,
        )
    else:
        log.warning("call_record_not_found", call_control_id=event.call_id)

    return {"status": "received"}
