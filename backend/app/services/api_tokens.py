"""Personal API tokens: create, check, revoke.

A token is ``sqt_`` + 32 random bytes. It is shown once, when it is made. The
database keeps its SHA-256 (the lookup key) and its first twelve characters (so
a person can tell tokens apart in a list; eight random characters are no help
to anybody trying to guess the rest).

The only check that runs on every request is :func:`authenticate_api_token`. It
answers ``None`` for every kind of failure, so the caller cannot report which
check a guess failed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from app.core.auth import API_TOKEN_PREFIX
from app.models.api_token import SCOPE_CALLER_UPLOAD, ApiToken
from app.models.user import User
from app.services.auth_tokens import as_utc, hash_token, hashes_match, new_secret

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

PREFIX_LENGTH = len(API_TOKEN_PREFIX) + 8
# A real token is 47 characters. Anything longer is not one; refusing it early
# keeps an oversized header out of the hash and the query.
MAX_TOKEN_LENGTH = 128
# `last_used_at` is for a person scanning a list; a write per upload is not needed.
LAST_USED_RESOLUTION = timedelta(minutes=1)
MAX_ACTIVE_TOKENS_PER_USER = 20


def new_api_token() -> str:
    return API_TOKEN_PREFIX + new_secret()


async def create_api_token(
    db: AsyncSession, user: User, *, name: str, expires_in_days: int | None
) -> tuple[ApiToken, str]:
    """Add a token to the session (the caller commits). Returns the row and the secret."""
    secret = new_api_token()
    row = ApiToken(
        user_id=user.id,
        name=name,
        token_prefix=secret[:PREFIX_LENGTH],
        token_hash=hash_token(secret),
        scope=SCOPE_CALLER_UPLOAD,
        expires_at=(datetime.now(UTC) + timedelta(days=expires_in_days))
        if expires_in_days
        else None,
    )
    db.add(row)
    return row, secret


async def authenticate_api_token(
    db: AsyncSession, token: str, required_scopes: Sequence[str]
) -> User | None:
    """The user a token acts for, when the token may be used for ``required_scopes``.

    A route that asks for no scope never accepts a personal token, which is what
    keeps the token out of everything except the routes that named it.
    """
    if not required_scopes or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
        return None

    digest = hash_token(token)
    row = (
        await db.execute(select(ApiToken).where(ApiToken.token_hash == digest))
    ).scalar_one_or_none()
    if row is None or not hashes_match(row.token_hash, digest):
        return None

    now = datetime.now(UTC)
    expired = row.expires_at is not None and as_utc(row.expires_at) <= now
    wrong_scope = any(scope != row.scope for scope in required_scopes)
    if row.revoked_at is not None or expired or wrong_scope:
        return None

    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        return None

    if row.last_used_at is None or now - as_utc(row.last_used_at) > LAST_USED_RESOLUTION:
        await db.execute(update(ApiToken).where(ApiToken.id == row.id).values(last_used_at=now))
        await db.commit()
    return user
