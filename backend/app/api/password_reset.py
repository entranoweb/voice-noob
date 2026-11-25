"""Password reset by an emailed, single-use link.

Two things this must not do: tell a stranger which addresses have accounts, and
leave a way back in for whoever had the old password (or a disabled account).
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_password_hash
from app.core.audit import AuditAction, record_audit
from app.core.config import settings
from app.core.limiter import limiter
from app.core.public_url import frontend_base_url
from app.db.session import get_db
from app.models.api_token import ApiToken
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.services.auth_tokens import (
    as_utc,
    hash_token,
    hashes_match,
    new_secret,
    revoke_user_sessions,
)
from app.services.mail import Mail, Mailer, get_mailer

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
logger = structlog.get_logger()

MIN_PASSWORD_LENGTH = 8
# The same words whether or not the address has an account.
REQUESTED_DETAIL = "If that address has an account, a reset link is on its way."
INVALID_LINK_DETAIL = "This reset link is invalid or has expired."


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class DetailResponse(BaseModel):
    detail: str


def _reset_mail(to: str, link: str) -> Mail:
    minutes = settings.PASSWORD_RESET_EXPIRE_MINUTES
    return Mail(
        to=to,
        subject="Reset your Synthiq Voice password",
        text=(
            "Someone asked to reset the password for this account.\n\n"
            f"Open this link within {minutes} minutes to choose a new one:\n{link}\n\n"
            "It works once. If it was not you, ignore this message; nothing has changed."
        ),
    )


@router.post(
    "/forgot-password", response_model=DetailResponse, status_code=status.HTTP_202_ACCEPTED
)
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,  # named `request` for slowapi; also used for the link's origin
    payload: ForgotPasswordRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    mailer: Mailer = Depends(get_mailer),
) -> DetailResponse:
    """Email a reset link if the address has an active account.

    The answer is the same either way, and the work done is the same shape: the
    account lookup, a fresh secret and its hash, one audit row. The mail itself
    goes out after the response, so sending does not show in the timing.
    """
    now = datetime.now(UTC)
    user = (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    raw = new_secret()
    digest = hash_token(raw)

    issued = False
    if user is not None and user.is_active:
        recent = (
            await db.execute(
                select(func.count(PasswordResetToken.id)).where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.created_at > now - timedelta(hours=1),
                )
            )
        ).scalar() or 0
        if recent < settings.PASSWORD_RESET_MAX_PER_HOUR:
            # A newer link replaces every older one.
            await db.execute(
                update(PasswordResetToken)
                .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
                .values(used_at=now)
            )
            db.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=digest,
                    created_at=now,
                    expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
                )
            )
            issued = True

    # The address of an account that does not exist is not written down, and
    # `account_found` is the only thing the row says about the lookup.
    await record_audit(
        db,
        AuditAction.PASSWORD_RESET_REQUESTED,
        actor_user_id=user.id if user else None,
        target_type="user" if user else None,
        target_id=str(user.id) if user else None,
        details={"account_found": user is not None, "link_sent": issued},
        request=request,
    )
    await db.commit()

    if issued and user is not None:
        link = f"{frontend_base_url(request)}/reset-password?token={raw}"
        background.add_task(mailer.send, _reset_mail(user.email, link))
    return DetailResponse(detail=REQUESTED_DETAIL)


def _invalid_link() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_LINK_DETAIL)


@router.post("/reset-password", response_model=DetailResponse)
@limiter.limit("10/minute")
async def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> DetailResponse:
    """Set a new password with a link's token, once.

    Every refusal is the same 400, so the response does not say whether a token
    never existed, was used, or expired. The password is set, every session and
    personal API token of the account ends, and nobody is signed in by it.
    """
    digest = hash_token(payload.token)
    row = (
        await db.execute(select(PasswordResetToken).where(PasswordResetToken.token_hash == digest))
    ).scalar_one_or_none()
    if row is None or not hashes_match(row.token_hash, digest):
        raise _invalid_link()

    now = datetime.now(UTC)
    if row.used_at is not None or as_utc(row.expires_at) <= now:
        raise _invalid_link()

    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    # A disabled account cannot reset its way back in: the link dies with the access.
    if user is None or not user.is_active:
        raise _invalid_link()

    # Claim the link. Two requests with the same link both read it unused; only
    # one UPDATE matches.
    claimed = cast(
        "CursorResult[tuple[()]]",
        await db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.id == row.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=now)
        ),
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise _invalid_link()

    user.hashed_password = get_password_hash(payload.new_password)
    await revoke_user_sessions(db, user)
    await db.execute(
        update(ApiToken)
        .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await record_audit(
        db,
        AuditAction.PASSWORD_RESET_COMPLETED,
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )
    await db.commit()
    logger.info("password_reset_completed", user_id=user.id)
    return DetailResponse(detail="Password updated. Sign in with the new one.")


# Silence "imported but unused" for the one name the module re-exports for tests.
__all__ = ["router"]
