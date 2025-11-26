"""Authentication dependencies and utilities."""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User

security = HTTPBearer()


def user_id_to_uuid(user_id: int) -> uuid.UUID:
    """Convert integer user ID to a deterministic UUID.

    Some models (Agent, UserSettings) use UUID for user_id instead of int.
    This function generates a consistent UUID from the integer user ID
    using a namespace-based approach.
    """
    # Use a fixed namespace UUID for this application
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # UUID namespace DNS
    return uuid.uuid5(namespace, f"user:{user_id}")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Get current authenticated user from JWT token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        token = credentials.credentials
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError as exc:
        raise credentials_exception from exc

    # Fetch user from database
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception

    return user


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
