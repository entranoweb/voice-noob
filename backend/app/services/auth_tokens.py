"""Secrets, hashing and refresh-token rotation.

Every secret this application hands out (refresh tokens, reset links, personal
API tokens) is 32 random bytes from ``secrets`` and is stored as its SHA-256.
A fast hash is right here: the input has 256 bits of entropy, so there is
nothing to brute-force, and a salted slow hash would only make every
authenticated request slower. A database leak yields hashes that cannot be
replayed.

Comparison: tokens are looked up by their hash (an indexed equality on a value
the attacker cannot predict, so the lookup time says nothing about the secret),
and the stored hash is then compared again with ``hmac.compare_digest``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy import select, update

from app.core.audit import AuditAction, record_audit
from app.core.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

TOKEN_BYTES = 32


class RefreshRejected(Exception):  # noqa: N818 - reads better than ...Error at the call site
    """The refresh token cannot be used. The caller answers 401 and says no more."""


def new_secret() -> str:
    """A URL-safe random secret of 32 bytes (43 characters)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hashes_match(left: str, right: str) -> bool:
    """Constant-time comparison of two hex digests."""
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


def as_utc(value: datetime) -> datetime:
    """A datetime read back from the database, as an aware UTC value.

    SQLite returns naive datetimes for ``DateTime(timezone=True)``; PostgreSQL
    returns aware ones. Everything is written in UTC.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


async def issue_refresh_token(
    db: AsyncSession,
    user_id: int,
    *,
    family_id: uuid.UUID | None = None,
    family_expires_at: datetime | None = None,
) -> str:
    """Add a refresh token to the session and return it. The caller commits.

    Without ``family_id`` this starts a new family (a sign-in). Rotation passes
    the family on, and the absolute limit with it, so rotating can never
    extend a sign-in past ``REFRESH_TOKEN_MAX_AGE_DAYS``.
    """
    now = _now()
    family_end = family_expires_at or now + timedelta(days=settings.REFRESH_TOKEN_MAX_AGE_DAYS)
    expires = min(now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), family_end)
    raw = new_secret()
    db.add(
        RefreshToken(
            family_id=family_id or uuid.uuid4(),
            user_id=user_id,
            token_hash=hash_token(raw),
            created_at=now,
            expires_at=expires,
            family_expires_at=family_end,
        )
    )
    return raw


async def revoke_family(db: AsyncSession, family_id: uuid.UUID) -> int:
    """Revoke every live token of a sign-in. Returns how many were still live."""
    result = cast(
        "CursorResult[tuple[()]]",
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        ),
    )
    return int(result.rowcount)


async def revoke_user_sessions(
    db: AsyncSession, user: User, *, revoke_access_tokens: bool = True
) -> None:
    """End every session of ``user``: refresh tokens now, access tokens by version.

    Access tokens are stateless, so they cannot be listed and struck out. They
    carry the user's ``token_version``; bumping it makes every one issued
    before this moment fail the check in ``get_current_user``. A caller that has
    already made the user inactive may pass ``revoke_access_tokens=False``: those
    tokens are refused while the account is off, and switching it back on is
    meant to restore the access token the person still holds.
    """
    if revoke_access_tokens:
        user.token_version += 1
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def _reuse_detected(
    db: AsyncSession, family_id: uuid.UUID, user_id: int, request: Request | None, why: str
) -> None:
    """A retired token came back: revoke the family, say so, and keep the record."""
    await revoke_family(db, family_id)
    logger.warning(
        "refresh_token_reuse_suspected_theft",
        user_id=user_id,
        family_id=str(family_id),
        why=why,
    )
    await record_audit(
        db,
        AuditAction.REFRESH_REUSE_DETECTED,
        actor_user_id=user_id,
        target_type="refresh_token_family",
        target_id=str(family_id),
        success=False,
        details={"why": why},
        request=request,
        commit=True,
    )


async def rotate_refresh_token(
    db: AsyncSession, presented: str, *, request: Request | None = None
) -> tuple[User, str]:
    """Trade a refresh token for the next one in its family.

    Returns the user and the new refresh token (committed). Raises
    :class:`RefreshRejected` for anything that is not a live token of an active
    user. A token that was already used is a theft signal: the whole family is
    revoked before the refusal.
    """
    presented_hash = hash_token(presented)
    row = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == presented_hash))
    ).scalar_one_or_none()
    if row is None or not hashes_match(row.token_hash, presented_hash):
        raise RefreshRejected

    if row.revoked_at is not None:
        raise RefreshRejected
    if row.used_at is not None:
        await _reuse_detected(db, row.family_id, row.user_id, request, "token already used")
        raise RefreshRejected

    now = _now()
    if as_utc(row.expires_at) <= now or as_utc(row.family_expires_at) <= now:
        raise RefreshRejected

    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        await revoke_family(db, row.family_id)
        await db.commit()
        raise RefreshRejected

    # Claim the token. Two requests presenting the same token at the same moment
    # both read it as unused; only one UPDATE can match, and the other is a replay.
    claimed = cast(
        "CursorResult[tuple[()]]",
        await db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.id == row.id,
                RefreshToken.used_at.is_(None),
                RefreshToken.revoked_at.is_(None),
            )
            .values(used_at=now)
        ),
    )
    if claimed.rowcount != 1:
        # Read before the rollback expires the row.
        family_id, user_id = row.family_id, row.user_id
        await db.rollback()
        await _reuse_detected(db, family_id, user_id, request, "token used twice at once")
        raise RefreshRejected

    successor = await issue_refresh_token(
        db,
        user.id,
        family_id=row.family_id,
        family_expires_at=as_utc(row.family_expires_at),
    )
    await db.commit()
    return user, successor
