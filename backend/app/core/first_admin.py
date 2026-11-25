"""The first administrator account, and the refusal to run on a shipped password.

The application creates an administrator the first time it starts on an empty
database, from ADMIN_EMAIL and ADMIN_PASSWORD. The repository has shipped
several defaults for that password. A production instance must never hold a
working superuser login that anyone can read out of the source.

Outside DEBUG this module therefore:

- refuses to create the first administrator from a shipped password;
- refuses to serve while any administrator's stored password is a shipped one,
  with one way out that needs no database access: set ADMIN_EMAIL to that
  account and ADMIN_PASSWORD to a new value, and the stored password is
  replaced at the next start.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from app.core.audit import AuditAction, record_audit
from app.core.config import (
    SHIPPED_ADMIN_PASSWORDS,
    InsecureDefaultCredentialsError,
    is_shipped_admin_password,
    settings,
)
from app.models.user import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


def _stored_password_is_shipped(hashed_password: str) -> bool:
    from app.api.auth import verify_password

    return any(verify_password(candidate, hashed_password) for candidate in SHIPPED_ADMIN_PASSWORDS)


async def ensure_first_admin(db: AsyncSession) -> None:
    """Create the first administrator if there are no users; refuse shipped passwords."""
    from app.api.auth import get_password_hash

    production = not settings.DEBUG
    configured_is_shipped = is_shipped_admin_password(settings.ADMIN_PASSWORD)

    user_count = (await db.execute(select(func.count(User.id)))).scalar() or 0

    if user_count == 0:
        if production and configured_is_shipped:
            message = (
                "Refusing to create the first administrator: ADMIN_PASSWORD is unset "
                "or still a default shipped in the repository. Set ADMIN_EMAIL and "
                "ADMIN_PASSWORD to values of your own. For a throwaway local run "
                "only, DEBUG=true skips this check."
            )
            raise InsecureDefaultCredentialsError(message)
        admin_user = User(
            email=settings.ADMIN_EMAIL,
            hashed_password=get_password_hash(settings.ADMIN_PASSWORD),
            full_name=settings.ADMIN_NAME,
            is_active=True,
            is_superuser=True,
        )
        db.add(admin_user)
        await db.flush()
        await record_audit(
            db,
            AuditAction.ADMIN_BOOTSTRAP,
            target_type="user",
            target_id=str(admin_user.id),
            details={"reason": "first start with no users"},
        )
        await db.commit()
        logger.info("Created default admin user", name=settings.ADMIN_NAME)
        return

    if not production:
        return

    admins = (
        (
            await db.execute(
                select(User).where(User.is_superuser.is_(True), User.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    for admin in admins:
        if not _stored_password_is_shipped(admin.hashed_password):
            continue
        if admin.email == settings.ADMIN_EMAIL and not configured_is_shipped:
            admin.hashed_password = get_password_hash(settings.ADMIN_PASSWORD)
            await record_audit(
                db,
                AuditAction.ADMIN_PASSWORD_REPLACED,
                target_type="user",
                target_id=str(admin.id),
                details={"reason": "stored password was a shipped default"},
            )
            await db.commit()
            logger.warning("admin_default_password_replaced", user_id=admin.id)
            continue
        message = (
            f"Refusing to start: administrator account id {admin.id} still has a "
            "default password shipped in the repository. Set ADMIN_EMAIL to that "
            "account's address and ADMIN_PASSWORD to a new value, then start "
            "again; the stored password is replaced at start-up."
        )
        raise InsecureDefaultCredentialsError(message)
