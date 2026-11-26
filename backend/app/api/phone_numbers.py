"""Phone numbers API routes."""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditAction, record_audit
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.limiter import limiter
from app.core.ownership import (
    agent_is_in_workspace,
    agents_dialling_number_outside,
    get_owned_agent,
    number_variants,
    parse_uuid,
)
from app.db.session import get_db
from app.models.agent import Agent
from app.models.phone_number import PhoneNumber, PhoneNumberStatus
from app.models.workspace import Workspace
from app.services.telephony.number_ownership import (
    CarrierLookupFactory,
    get_carrier_lookup_factory,
    normalise_number,
    verify_number_on_workspace_account,
)

router = APIRouter(prefix="/api/v1/phone-numbers", tags=["phone-numbers"])
logger = structlog.get_logger()


async def validate_workspace_access(workspace_id: str, user_id: int, db: AsyncSession) -> uuid.UUID:
    """Validate that user has access to the workspace.

    Args:
        workspace_id: Workspace ID string
        user_id: User ID (integer)
        db: Database session

    Returns:
        Workspace UUID if valid

    Raises:
        HTTPException: If workspace not found or user doesn't have access
    """
    workspace_uuid = parse_uuid(workspace_id, "Workspace")

    # Check if user owns the workspace
    workspace_result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_uuid,
            Workspace.user_id == user_id,
        )
    )
    workspace = workspace_result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return workspace_uuid


# =============================================================================
# Pydantic Models
# =============================================================================


class PhoneNumberResponse(BaseModel):
    """Phone number response."""

    id: str
    phone_number: str
    friendly_name: str | None
    provider: str
    provider_id: str
    workspace_id: str | None
    workspace_name: str | None = None
    assigned_agent_id: str | None
    assigned_agent_name: str | None = None
    can_receive_calls: bool
    can_make_calls: bool
    can_receive_sms: bool
    can_send_sms: bool
    status: str
    notes: str | None
    purchased_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PhoneNumberListResponse(BaseModel):
    """Paginated phone numbers response."""

    phone_numbers: list[PhoneNumberResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class CreatePhoneNumberRequest(BaseModel):
    """Request to register a phone number."""

    phone_number: str
    friendly_name: str | None = None
    provider: str = "telnyx"
    provider_id: str
    workspace_id: str | None = None
    can_receive_calls: bool = True
    can_make_calls: bool = True
    can_receive_sms: bool = False
    can_send_sms: bool = False
    notes: str | None = None


class UpdatePhoneNumberRequest(BaseModel):
    """Request to update a phone number."""

    friendly_name: str | None = None
    workspace_id: str | None = None
    assigned_agent_id: str | None = None
    status: str | None = None
    notes: str | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is not None and value not in {item.value for item in PhoneNumberStatus}:
            allowed = ", ".join(item.value for item in PhoneNumberStatus)
            raise ValueError(f"status must be one of: {allowed}")
        return value


# =============================================================================
# Phone Number Endpoints
# =============================================================================


@router.get("", response_model=PhoneNumberListResponse)
async def list_phone_numbers(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
    status: str | None = Query(default=None, description="Filter by status"),
) -> PhoneNumberListResponse:
    """List phone numbers for the current user.

    Args:
        current_user: Authenticated user
        db: Database session
        page: Page number (1-indexed)
        page_size: Number of records per page
        workspace_id: Optional filter by workspace ID
        status: Optional filter by status

    Returns:
        Paginated list of phone numbers
    """
    log = logger.bind(user_id=current_user.id)
    log.info("listing_phone_numbers", page=page, page_size=page_size)

    # Build query
    user_uuid = user_id_to_uuid(current_user.id)
    query = select(PhoneNumber).where(PhoneNumber.user_id == user_uuid)

    # Apply filters
    if workspace_id:
        query = query.where(PhoneNumber.workspace_id == parse_uuid(workspace_id, "Workspace"))
    if status:
        query = query.where(PhoneNumber.status == status)

    # Get total count
    count_query = select(PhoneNumber.id).where(PhoneNumber.user_id == user_uuid)
    if workspace_id:
        count_query = count_query.where(
            PhoneNumber.workspace_id == parse_uuid(workspace_id, "Workspace")
        )
    if status:
        count_query = count_query.where(PhoneNumber.status == status)

    count_result = await db.execute(count_query)
    total = len(count_result.all())

    # Apply pagination and ordering
    offset = (page - 1) * page_size
    query = query.order_by(desc(PhoneNumber.created_at)).offset(offset).limit(page_size)

    result = await db.execute(query)
    records = result.scalars().all()

    # Build response with workspace and agent names
    phone_numbers = []
    for record in records:
        workspace_name = None
        agent_name = None

        if record.workspace:
            workspace_name = record.workspace.name
        if record.assigned_agent:
            agent_name = record.assigned_agent.name

        phone_numbers.append(
            PhoneNumberResponse(
                id=str(record.id),
                phone_number=record.phone_number,
                friendly_name=record.friendly_name,
                provider=record.provider,
                provider_id=record.provider_id,
                workspace_id=str(record.workspace_id) if record.workspace_id else None,
                workspace_name=workspace_name,
                assigned_agent_id=str(record.assigned_agent_id)
                if record.assigned_agent_id
                else None,
                assigned_agent_name=agent_name,
                can_receive_calls=record.can_receive_calls,
                can_make_calls=record.can_make_calls,
                can_receive_sms=record.can_receive_sms,
                can_send_sms=record.can_send_sms,
                status=record.status,
                notes=record.notes,
                purchased_at=record.purchased_at,
                created_at=record.created_at,
            )
        )

    total_pages = (total + page_size - 1) // page_size

    return PhoneNumberListResponse(
        phone_numbers=phone_numbers,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{phone_number_id}", response_model=PhoneNumberResponse)
async def get_phone_number(
    phone_number_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PhoneNumberResponse:
    """Get a specific phone number.

    Args:
        phone_number_id: Phone number ID
        current_user: Authenticated user
        db: Database session

    Returns:
        Phone number details
    """
    log = logger.bind(user_id=current_user.id, phone_number_id=phone_number_id)
    log.info("getting_phone_number")

    user_uuid = user_id_to_uuid(current_user.id)
    result = await db.execute(
        select(PhoneNumber).where(
            PhoneNumber.id == parse_uuid(phone_number_id, "Phone number"),
            PhoneNumber.user_id == user_uuid,
        )
    )
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="Phone number not found")

    workspace_name = None
    agent_name = None

    if record.workspace:
        workspace_name = record.workspace.name
    if record.assigned_agent:
        agent_name = record.assigned_agent.name

    return PhoneNumberResponse(
        id=str(record.id),
        phone_number=record.phone_number,
        friendly_name=record.friendly_name,
        provider=record.provider,
        provider_id=record.provider_id,
        workspace_id=str(record.workspace_id) if record.workspace_id else None,
        workspace_name=workspace_name,
        assigned_agent_id=str(record.assigned_agent_id) if record.assigned_agent_id else None,
        assigned_agent_name=agent_name,
        can_receive_calls=record.can_receive_calls,
        can_make_calls=record.can_make_calls,
        can_receive_sms=record.can_receive_sms,
        can_send_sms=record.can_send_sms,
        status=record.status,
        notes=record.notes,
        purchased_at=record.purchased_at,
        created_at=record.created_at,
    )


@router.post("", response_model=PhoneNumberResponse, status_code=201)
@limiter.limit("10/minute")  # Rate limit phone number creation
async def create_phone_number(
    create_request: CreatePhoneNumberRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    lookup_factory: CarrierLookupFactory = Depends(get_carrier_lookup_factory),
) -> PhoneNumberResponse:
    """Register a new phone number.

    The number must be on the carrier account whose credentials the workspace
    holds: it is looked up there first, and refused (422) if it is not, or
    (503) if the carrier could not be asked. Only then is it registered.

    Args:
        create_request: Phone number details
        request: HTTP request (for rate limiting)
        current_user: Authenticated user
        db: Database session

    Returns:
        Created phone number
    """
    log = logger.bind(user_id=current_user.id)
    log.info("creating_phone_number", phone_number=create_request.phone_number)

    user_uuid = user_id_to_uuid(current_user.id)

    # Validate workspace access if workspace_id is provided
    workspace_uuid: uuid.UUID | None = None
    if create_request.workspace_id:
        workspace_uuid = await validate_workspace_access(
            create_request.workspace_id, current_user.id, db
        )

    # Proof first, so someone who does not hold the number learns nothing about
    # whether another account has registered it (the 409 below).
    await verify_number_on_workspace_account(
        db,
        user_id=user_uuid,
        workspace_id=workspace_uuid,
        provider=create_request.provider,
        number=create_request.phone_number,
        factory=lookup_factory,
    )

    # One number, one owner. Inbound routing looks a dialled number up across
    # every account, so a second account registering the same number would
    # either take the first one's calls or kill its line.
    # Stored in one spelling (E.164 with the plus), already validated above.
    number = normalise_number(create_request.phone_number)
    taken = await db.execute(
        select(PhoneNumber.id).where(
            PhoneNumber.phone_number.in_(number_variants(number)),
            PhoneNumber.status != PhoneNumberStatus.RELEASED.value,
        )
    )
    if taken.first() is not None:
        raise HTTPException(status_code=409, detail="That phone number is already registered")

    phone_number = PhoneNumber(
        user_id=user_uuid,
        phone_number=number,
        friendly_name=create_request.friendly_name,
        provider=create_request.provider,
        provider_id=create_request.provider_id,
        workspace_id=workspace_uuid,
        can_receive_calls=create_request.can_receive_calls,
        can_make_calls=create_request.can_make_calls,
        can_receive_sms=create_request.can_receive_sms,
        can_send_sms=create_request.can_send_sms,
        notes=create_request.notes,
    )

    db.add(phone_number)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Two registrations of one number racing each other: the index decides.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="That phone number is already registered"
        ) from exc
    await db.refresh(phone_number)

    return PhoneNumberResponse(
        id=str(phone_number.id),
        phone_number=phone_number.phone_number,
        friendly_name=phone_number.friendly_name,
        provider=phone_number.provider,
        provider_id=phone_number.provider_id,
        workspace_id=str(phone_number.workspace_id) if phone_number.workspace_id else None,
        workspace_name=None,
        assigned_agent_id=None,
        assigned_agent_name=None,
        can_receive_calls=phone_number.can_receive_calls,
        can_make_calls=phone_number.can_make_calls,
        can_receive_sms=phone_number.can_receive_sms,
        can_send_sms=phone_number.can_send_sms,
        status=phone_number.status,
        notes=phone_number.notes,
        purchased_at=phone_number.purchased_at,
        created_at=phone_number.created_at,
    )


@router.put("/{phone_number_id}", response_model=PhoneNumberResponse)
@limiter.limit("30/minute")  # Rate limit phone number updates
async def update_phone_number(  # noqa: PLR0912, PLR0915
    phone_number_id: str,
    update_request: UpdatePhoneNumberRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    lookup_factory: CarrierLookupFactory = Depends(get_carrier_lookup_factory),
) -> PhoneNumberResponse:
    """Update a phone number.

    Moving a number into a different workspace, or bringing a released one back,
    or putting one back to active is a number entering a workspace, so it is
    verified against that workspace's carrier account like a new registration.
    Assigning it to another agent is not (the number's place in its workspace
    was proven when it got there), but the agent must be in the same workspace.

    Args:
        phone_number_id: Phone number ID
        update_request: Update details
        request: HTTP request (for rate limiting)
        current_user: Authenticated user
        db: Database session

    Returns:
        Updated phone number
    """
    log = logger.bind(user_id=current_user.id, phone_number_id=phone_number_id)
    log.info("updating_phone_number")

    user_uuid = user_id_to_uuid(current_user.id)
    result = await db.execute(
        select(PhoneNumber).where(
            PhoneNumber.id == parse_uuid(phone_number_id, "Phone number"),
            PhoneNumber.user_id == user_uuid,
        )
    )
    phone_number = result.scalar_one_or_none()

    if not phone_number:
        raise HTTPException(status_code=404, detail="Phone number not found")

    # Where the number will sit after this update, and whether it is entering
    # that place (a different workspace, or back from released).
    target_workspace = phone_number.workspace_id
    if update_request.workspace_id is not None:
        target_workspace = (
            await validate_workspace_access(update_request.workspace_id, current_user.id, db)
            if update_request.workspace_id
            else None
        )
    # A number coming back into service is entering a workspace again: from
    # released to anything live, or from any other state to active. The account
    # may have lost it meanwhile.
    new_status = update_request.status
    reviving = new_status is not None and (
        (
            phone_number.status == PhoneNumberStatus.RELEASED.value
            and new_status != PhoneNumberStatus.RELEASED.value
        )
        or (
            new_status == PhoneNumberStatus.ACTIVE.value
            and phone_number.status != PhoneNumberStatus.ACTIVE.value
        )
    )
    moving = target_workspace != phone_number.workspace_id
    if moving and target_workspace is not None:
        # An agent in another workspace that dials this number would keep
        # receiving its calls from there, past the proof the move just asked for.
        stranded = await agents_dialling_number_outside(
            db, current_user, phone_number.phone_number, target_workspace
        )
        if stranded:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Agent "
                    + ", ".join(f"'{a.name}'" for a in stranded)
                    + " is in another workspace and uses this phone number. "
                    "Clear its phone number before moving the number."
                ),
            )
    if moving or reviving:
        await verify_number_on_workspace_account(
            db,
            user_id=user_uuid,
            workspace_id=target_workspace,
            provider=phone_number.provider,
            number=phone_number.phone_number,
            factory=lookup_factory,
        )

    # Update fields
    if update_request.friendly_name is not None:
        phone_number.friendly_name = update_request.friendly_name
    if update_request.workspace_id is not None:
        # Validate workspace access before updating
        if update_request.workspace_id:
            workspace_uuid = await validate_workspace_access(
                update_request.workspace_id, current_user.id, db
            )
            phone_number.workspace_id = workspace_uuid
        else:
            phone_number.workspace_id = None
    if (
        moving
        and phone_number.assigned_agent_id is not None
        and update_request.assigned_agent_id is None
        and not (
            target_workspace is not None
            and await agent_is_in_workspace(db, phone_number.assigned_agent_id, target_workspace)
        )
    ):
        # The assigned agent is not in the number's new workspace: calls must
        # not keep routing to it. Say so rather than do it silently.
        log.info(
            "phone_number_unassigned_on_workspace_move",
            agent_id=str(phone_number.assigned_agent_id),
        )
        update_request.assigned_agent_id = ""
    if update_request.assigned_agent_id is not None:
        previous_agent = phone_number.assigned_agent_id
        if update_request.assigned_agent_id:
            # The agent must be the caller's own: assigning routes this number's
            # calls to it, and the response names it.
            agent = await get_owned_agent(db, update_request.assigned_agent_id, current_user)
            # Carrier proof is per workspace; an agent of another workspace
            # would route this number's calls across that boundary.
            if target_workspace is None or not await agent_is_in_workspace(
                db, agent.id, target_workspace
            ):
                raise HTTPException(
                    status_code=422,
                    detail="That agent belongs to a different workspace than this phone number.",
                )
            phone_number.assigned_agent_id = agent.id
        else:
            phone_number.assigned_agent_id = None
        if phone_number.assigned_agent_id != previous_agent:
            await record_audit(
                db,
                AuditAction.PHONE_NUMBER_ASSIGN,
                actor_user_id=current_user.id,
                workspace_id=phone_number.workspace_id,
                target_type="phone_number",
                target_id=str(phone_number.id),
                details={
                    "from_agent": str(previous_agent) if previous_agent else None,
                    "to_agent": str(phone_number.assigned_agent_id)
                    if phone_number.assigned_agent_id
                    else None,
                },
                request=request,
            )
    if update_request.status is not None:
        phone_number.status = update_request.status
    if update_request.notes is not None:
        phone_number.notes = update_request.notes

    try:
        await db.commit()
    except IntegrityError as exc:
        # Bringing a released number back when another account now holds it.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="That phone number is already registered"
        ) from exc
    await db.refresh(phone_number)

    workspace_name = None
    agent_name = None

    if phone_number.workspace:
        workspace_name = phone_number.workspace.name
    if phone_number.assigned_agent:
        agent_name = phone_number.assigned_agent.name

    return PhoneNumberResponse(
        id=str(phone_number.id),
        phone_number=phone_number.phone_number,
        friendly_name=phone_number.friendly_name,
        provider=phone_number.provider,
        provider_id=phone_number.provider_id,
        workspace_id=str(phone_number.workspace_id) if phone_number.workspace_id else None,
        workspace_name=workspace_name,
        assigned_agent_id=str(phone_number.assigned_agent_id)
        if phone_number.assigned_agent_id
        else None,
        assigned_agent_name=agent_name,
        can_receive_calls=phone_number.can_receive_calls,
        can_make_calls=phone_number.can_make_calls,
        can_receive_sms=phone_number.can_receive_sms,
        can_send_sms=phone_number.can_send_sms,
        status=phone_number.status,
        notes=phone_number.notes,
        purchased_at=phone_number.purchased_at,
        created_at=phone_number.created_at,
    )


@router.delete("/{phone_number_id}", status_code=204)
@limiter.limit("10/minute")  # Rate limit phone number deletion
async def delete_phone_number(
    phone_number_id: str,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a phone number.

    Args:
        phone_number_id: Phone number ID
        current_user: Authenticated user
        db: Database session
    """
    log = logger.bind(user_id=current_user.id, phone_number_id=phone_number_id)
    log.info("deleting_phone_number")

    user_uuid = user_id_to_uuid(current_user.id)
    result = await db.execute(
        select(PhoneNumber).where(
            PhoneNumber.id == parse_uuid(phone_number_id, "Phone number"),
            PhoneNumber.user_id == user_uuid,
        )
    )
    phone_number = result.scalar_one_or_none()

    if not phone_number:
        raise HTTPException(status_code=404, detail="Phone number not found")

    # Agents that name this number in their own field would otherwise keep a
    # dangling reference; routing ignores it without a record, but clear it.
    for agent in (
        (
            await db.execute(
                select(Agent).where(
                    Agent.user_id == current_user.id,
                    Agent.phone_number_id.in_(number_variants(phone_number.phone_number)),
                )
            )
        )
        .scalars()
        .all()
    ):
        agent.phone_number_id = None
    await db.delete(phone_number)
    await db.commit()
