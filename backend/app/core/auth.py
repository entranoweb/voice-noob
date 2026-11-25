"""Authentication dependencies and utilities."""

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.api_token import SCOPE_CALLER_UPLOAD
from app.models.user import User

security = HTTPBearer()

# Every personal API token starts with this, so it is told apart from a session
# token before anything is decoded.
API_TOKEN_PREFIX = "sqt_"  # noqa: S105 - a prefix, not a credential


def user_id_to_uuid(user_id: int) -> uuid.UUID:
    """Convert integer user ID to a deterministic UUID.

    Some models (Agent, UserSettings) use UUID for user_id instead of int.
    This function generates a consistent UUID from the integer user ID
    using a namespace-based approach.
    """
    # Use a fixed namespace UUID for this application
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # UUID namespace DNS
    return uuid.uuid5(namespace, f"user:{user_id}")


# What a personal API token may be used for. A route opts in with
# ``Security(get_current_user, scopes=[SCOPE_CALLER_UPLOAD])``; every other
# route, including every route written after this one, refuses it.

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def user_from_access_token(token: str, db: AsyncSession) -> User | None:
    """The active user a signed access token belongs to, or None.

    Refused when the signature, algorithm or expiry is wrong, when the user is
    gone or disabled, and when the token was issued under an older
    ``token_version`` (the user's sessions were ended since). Tokens issued
    before ``token_version`` existed carry no claim and count as version 0,
    which is every existing user's value.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = int(payload.get("sub") or 0)
        version = int(payload.get("ver", 0))
    except (jwt.PyJWTError, TypeError, ValueError):
        # Expired, tampered, malformed or signed with another algorithm.
        return None
    if not user_id:
        return None

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return None

    # A disabled account has no access, whatever its token says. Access tokens
    # carry no revocation list, so this lookup is what makes disabling take
    # effect on the next request rather than when the token expires; the
    # version check does the same for a password reset.
    if not user.is_active or user.token_version != version:
        return None
    return user


async def get_current_user(
    security_scopes: SecurityScopes,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """The signed-in user: a session access token, or a scoped personal API token.

    A session access token is accepted everywhere. A personal API token
    (``sqt_...``) is accepted only on a route that names its scope in
    ``security_scopes``, and only when the token grants that scope.
    """
    token = credentials.credentials

    if token.startswith(API_TOKEN_PREFIX):
        # Imported here: the token service needs this module for ``user_id_to_uuid``.
        from app.services.api_tokens import authenticate_api_token

        user = await authenticate_api_token(db, token, security_scopes.scopes)
    else:
        user = await user_from_access_token(token, db)

    if user is None:
        raise _CREDENTIALS_ERROR
    return user


async def get_current_superuser(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """The signed-in user, who must be a platform administrator."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return current_user


async def get_user_id_from_uuid(user_uuid: uuid.UUID, db: AsyncSession) -> int | None:
    """Look up the integer user ID from a generated UUID.

    Since user_id_to_uuid is a one-way hash, we need to scan users and compare.
    This is O(n) but typically small for most applications.

    Args:
        user_uuid: The UUID generated via user_id_to_uuid
        db: Database session

    Returns:
        The integer user ID, or None if not found
    """
    result = await db.execute(select(User))
    users = result.scalars().all()

    for user in users:
        if user_id_to_uuid(user.id) == user_uuid:
            return user.id

    return None


# Type alias for dependency injection
CurrentUser = Annotated[User, Depends(get_current_user)]
# The one dependency that also accepts a personal API token, and only one scoped
# for caller uploads. Used by exactly two routes (upload a run, upload its audio).
CallerUploader = Annotated[User, Security(get_current_user, scopes=[SCOPE_CALLER_UPLOAD])]
SuperUser = Annotated[User, Depends(get_current_superuser)]
