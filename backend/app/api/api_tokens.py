"""Personal API tokens: create, list, revoke.

These routes take a signed-in session (``CurrentUser``), never a personal token,
so a leaked token cannot make more tokens or hide itself by revoking others.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditAction, record_audit
from app.core.auth import CurrentUser
from app.db.session import get_db
from app.models.api_token import ApiToken
from app.services.api_tokens import MAX_ACTIVE_TOKENS_PER_USER, create_api_token

router = APIRouter(prefix="/api/v1/auth/api-tokens", tags=["auth"])

MAX_EXPIRY_DAYS = 3650


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=MAX_EXPIRY_DAYS)


class ApiTokenSummary(BaseModel):
    id: uuid.UUID
    name: str
    token_prefix: str
    scope: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class ApiTokenCreated(ApiTokenSummary):
    # The only time the secret is in any response.
    token: str


@router.post("", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: ApiTokenCreate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApiTokenCreated:
    """Make a token. The response is the only place it is ever shown."""
    active = (
        await db.execute(
            select(func.count(ApiToken.id)).where(
                ApiToken.user_id == current_user.id, ApiToken.revoked_at.is_(None)
            )
        )
    ).scalar() or 0
    if active >= MAX_ACTIVE_TOKENS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_ACTIVE_TOKENS_PER_USER} active tokens. Revoke one first.",
        )

    row, secret = await create_api_token(
        db, current_user, name=payload.name.strip(), expires_in_days=payload.expires_in_days
    )
    await db.flush()
    await record_audit(
        db,
        AuditAction.API_TOKEN_CREATE,
        actor_user_id=current_user.id,
        target_type="api_token",
        target_id=str(row.id),
        details={"name": row.name, "prefix": row.token_prefix, "scope": row.scope},
        request=request,
    )
    await db.commit()
    await db.refresh(row)
    return ApiTokenCreated(**ApiTokenSummary.model_validate(row).model_dump(), token=secret)


@router.get("", response_model=list[ApiTokenSummary])
async def list_tokens(
    current_user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ApiToken]:
    result = await db.execute(
        select(ApiToken).where(ApiToken.user_id == current_user.id).order_by(ApiToken.created_at)
    )
    return list(result.scalars().all())


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Revoke one of your own tokens. Somebody else's is a 404, not a 403."""
    row = (
        await db.execute(
            select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await record_audit(
            db,
            AuditAction.API_TOKEN_REVOKE,
            actor_user_id=current_user.id,
            target_type="api_token",
            target_id=str(row.id),
            details={"name": row.name, "prefix": row.token_prefix},
            request=request,
        )
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
