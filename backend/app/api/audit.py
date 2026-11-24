"""Read access to the audit trail. There is no route that changes it."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.db.session import get_db
from app.models.audit_event import AuditEvent
from app.models.workspace import Workspace

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


class AuditEventResponse(BaseModel):
    """One audited action."""

    id: str
    created_at: datetime
    actor_user_id: int | None
    workspace_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    success: bool
    ip_address: str | None
    details: dict[str, Any] | None


class AuditEventListResponse(BaseModel):
    """A page of audited actions, newest first."""

    events: list[AuditEventResponse]
    total: int
    limit: int
    offset: int


@router.get("/events", response_model=AuditEventListResponse)
async def list_audit_events(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = Query(None, max_length=64),
) -> AuditEventListResponse:
    """The caller's own audit trail.

    An event is the caller's when they performed it or when it concerns one of
    their workspaces. Events of other accounts are never returned.
    """
    owned_workspaces = select(Workspace.id).where(Workspace.user_id == current_user.id)
    scope = or_(
        AuditEvent.actor_user_id == current_user.id,
        AuditEvent.workspace_id.in_(owned_workspaces),
    )
    conditions = [scope]
    if action:
        conditions.append(AuditEvent.action == action)

    total = (await db.execute(select(func.count(AuditEvent.id)).where(*conditions))).scalar() or 0
    result = await db.execute(
        select(AuditEvent)
        .where(*conditions)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .limit(limit)
        .offset(offset)
    )
    events = [
        AuditEventResponse(
            id=str(row.id),
            created_at=row.created_at,
            actor_user_id=row.actor_user_id,
            workspace_id=str(row.workspace_id) if row.workspace_id else None,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            success=row.success,
            ip_address=row.ip_address,
            details=row.details,
        )
        for row in result.scalars().all()
    ]
    return AuditEventListResponse(events=events, total=total, limit=limit, offset=offset)
