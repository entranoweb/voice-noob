"""Call history API routes."""

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import CurrentUser, user_id_to_uuid
from app.db.session import get_db
from app.models.call_record import CallRecord
from app.services.qa.call_metrics import metrics_for_call

router = APIRouter(prefix="/api/v1/calls", tags=["calls"])
logger = structlog.get_logger()


# =============================================================================
# Pydantic Models
# =============================================================================


class CallRecordResponse(BaseModel):
    """Call record response."""

    id: str
    provider: str
    provider_call_id: str
    agent_id: str | None
    agent_name: str | None = None
    contact_id: int | None
    contact_name: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    direction: str
    status: str
    from_number: str
    to_number: str
    duration_seconds: int
    recording_url: str | None
    transcript: str | None
    started_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None

    model_config = {"from_attributes": True}


class MetricScoreResponse(BaseModel):
    """One metric's result for one call.

    ``value`` of ``null`` means the metric could not be measured for this call —
    no audio, or no ground truth to compare against. It is deliberately distinct
    from ``0.0``, which means measured and bad. A client that renders the two the
    same way is reporting a failure that did not happen.
    """

    metric: str
    version: str
    category: str
    kind: str
    value: float | None
    passed: bool | None
    unit: str | None
    detail: dict[str, Any]


class CallMetricsResponse(BaseModel):
    """Every metric computed for one real call."""

    call_id: str
    outcome: str
    trustworthy: bool
    has_audio: bool
    scores: list[MetricScoreResponse]
    invalid_reasons: list[str]


class CallRecordListResponse(BaseModel):
    """Paginated call records response."""

    calls: list[CallRecordResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


# =============================================================================
# Call History Endpoints
# =============================================================================


@router.get("", response_model=CallRecordListResponse)
async def list_calls(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    agent_id: str | None = Query(default=None, description="Filter by agent ID"),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
    direction: str | None = Query(
        default=None, description="Filter by direction: inbound or outbound"
    ),
    status: str | None = Query(default=None, description="Filter by status"),
) -> CallRecordListResponse:
    """List call records for the current user.

    Args:
        current_user: Authenticated user
        db: Database session
        page: Page number (1-indexed)
        page_size: Number of records per page
        agent_id: Optional filter by agent ID
        direction: Optional filter by direction
        status: Optional filter by status

    Returns:
        Paginated list of call records
    """
    log = logger.bind(user_id=current_user.id)
    log.info("listing_calls", page=page, page_size=page_size)

    # Build query with eager loading to prevent N+1 queries
    user_uuid = user_id_to_uuid(current_user.id)
    query = (
        select(CallRecord)
        .where(CallRecord.user_id == user_uuid)
        .options(
            selectinload(CallRecord.agent),
            selectinload(CallRecord.contact),
            selectinload(CallRecord.workspace),
        )
    )

    # Apply filters
    if agent_id:
        query = query.where(CallRecord.agent_id == uuid.UUID(agent_id))
    if workspace_id:
        query = query.where(CallRecord.workspace_id == uuid.UUID(workspace_id))
    if direction:
        query = query.where(CallRecord.direction == direction)
    if status:
        query = query.where(CallRecord.status == status)

    # Get total count
    count_query = select(CallRecord.id).where(CallRecord.user_id == user_uuid)
    if agent_id:
        count_query = count_query.where(CallRecord.agent_id == uuid.UUID(agent_id))
    if workspace_id:
        count_query = count_query.where(CallRecord.workspace_id == uuid.UUID(workspace_id))
    if direction:
        count_query = count_query.where(CallRecord.direction == direction)
    if status:
        count_query = count_query.where(CallRecord.status == status)

    count_result = await db.execute(count_query)
    total = len(count_result.all())

    # Apply pagination and ordering
    offset = (page - 1) * page_size
    query = query.order_by(desc(CallRecord.started_at)).offset(offset).limit(page_size)

    result = await db.execute(query)
    records = result.scalars().all()

    # Build response with agent, contact, and workspace names
    calls = []
    for record in records:
        agent_name = None
        contact_name = None
        workspace_name = None

        if record.agent:
            agent_name = record.agent.name
        if record.contact:
            contact_name = f"{record.contact.first_name} {record.contact.last_name or ''}".strip()
        if record.workspace:
            workspace_name = record.workspace.name

        calls.append(
            CallRecordResponse(
                id=str(record.id),
                provider=record.provider,
                provider_call_id=record.provider_call_id,
                agent_id=str(record.agent_id) if record.agent_id else None,
                agent_name=agent_name,
                contact_id=record.contact_id,
                contact_name=contact_name,
                workspace_id=str(record.workspace_id) if record.workspace_id else None,
                workspace_name=workspace_name,
                direction=record.direction,
                status=record.status,
                from_number=record.from_number,
                to_number=record.to_number,
                duration_seconds=record.duration_seconds,
                recording_url=record.recording_url,
                transcript=record.transcript,
                started_at=record.started_at,
                answered_at=record.answered_at,
                ended_at=record.ended_at,
            )
        )

    total_pages = (total + page_size - 1) // page_size

    return CallRecordListResponse(
        calls=calls,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{call_id}", response_model=CallRecordResponse)
async def get_call(
    call_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CallRecordResponse:
    """Get a specific call record.

    Args:
        call_id: Call record ID
        current_user: Authenticated user
        db: Database session

    Returns:
        Call record details
    """
    log = logger.bind(user_id=current_user.id, call_id=call_id)
    log.info("getting_call")

    user_uuid = user_id_to_uuid(current_user.id)
    result = await db.execute(
        select(CallRecord).where(
            CallRecord.id == uuid.UUID(call_id),
            CallRecord.user_id == user_uuid,
        )
    )
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="Call record not found")

    agent_name = None
    contact_name = None
    workspace_name = None

    if record.agent:
        agent_name = record.agent.name
    if record.contact:
        contact_name = f"{record.contact.first_name} {record.contact.last_name or ''}".strip()
    if record.workspace:
        workspace_name = record.workspace.name

    return CallRecordResponse(
        id=str(record.id),
        provider=record.provider,
        provider_call_id=record.provider_call_id,
        agent_id=str(record.agent_id) if record.agent_id else None,
        agent_name=agent_name,
        contact_id=record.contact_id,
        contact_name=contact_name,
        workspace_id=str(record.workspace_id) if record.workspace_id else None,
        workspace_name=workspace_name,
        direction=record.direction,
        status=record.status,
        from_number=record.from_number,
        to_number=record.to_number,
        duration_seconds=record.duration_seconds,
        recording_url=record.recording_url,
        transcript=record.transcript,
        started_at=record.started_at,
        answered_at=record.answered_at,
        ended_at=record.ended_at,
    )


@router.get("/{call_id}/metrics", response_model=CallMetricsResponse)
async def get_call_metrics(
    call_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CallMetricsResponse:
    """Compute the metric suite for one real call.

    Only some metrics can say anything about a call that was not a simulation:
    latency and interruptions are measured off the live bridge, while word error
    rate needs to know what the caller meant to say and a human caller has no
    script. Those report ``value: null`` rather than a zero — see
    ``app.services.qa.call_metrics`` for why the distinction is load-bearing.
    """
    log = logger.bind(user_id=current_user.id, call_id=call_id)

    user_uuid = user_id_to_uuid(current_user.id)
    result = await db.execute(
        select(CallRecord).where(
            CallRecord.id == uuid.UUID(call_id),
            CallRecord.user_id == user_uuid,
        )
    )
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="Call record not found")

    results = metrics_for_call(record)
    log.info("call_metrics_computed", outcome=results.outcome.value)

    return CallMetricsResponse(
        call_id=str(record.id),
        outcome=results.outcome.value,
        trustworthy=results.trustworthy,
        has_audio=bool(record.turns),
        scores=[
            MetricScoreResponse(
                metric=score.metric,
                version=score.version,
                category=score.category.value,
                kind=score.kind.value,
                value=score.value,
                passed=score.passed,
                unit=score.unit,
                detail=score.detail,
            )
            for score in results.scores
        ],
        invalid_reasons=list(results.invalid_reasons),
    )


@router.get("/agent/{agent_id}/stats")
async def get_agent_call_stats(
    agent_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, int | float]:
    """Get call statistics for an agent.

    Args:
        agent_id: Agent ID
        current_user: Authenticated user
        db: Database session

    Returns:
        Call statistics for the agent
    """
    log = logger.bind(user_id=current_user.id, agent_id=agent_id)
    log.info("getting_agent_call_stats")

    user_uuid = user_id_to_uuid(current_user.id)
    # Get all calls for this agent
    result = await db.execute(
        select(CallRecord).where(
            CallRecord.agent_id == uuid.UUID(agent_id),
            CallRecord.user_id == user_uuid,
        )
    )
    records = result.scalars().all()

    total_calls = len(records)
    total_duration = sum(r.duration_seconds for r in records)
    completed_calls = sum(1 for r in records if r.status == "completed")
    inbound_calls = sum(1 for r in records if r.direction == "inbound")
    outbound_calls = sum(1 for r in records if r.direction == "outbound")

    avg_duration = total_duration / total_calls if total_calls > 0 else 0

    return {
        "total_calls": total_calls,
        "completed_calls": completed_calls,
        "inbound_calls": inbound_calls,
        "outbound_calls": outbound_calls,
        "total_duration_seconds": total_duration,
        "average_duration_seconds": round(avg_duration, 1),
    }
