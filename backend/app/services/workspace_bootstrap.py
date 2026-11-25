"""A workspace for the first account of a fresh install.

``synthiq-caller upload`` needs a workspace id, and on a new install nobody had
one until somebody made it by hand. This makes one, once.

The rule is deliberately narrow: only an install with no workspaces at all gets
one, and it goes to the first account (lowest id). An install that already has
any workspace is somebody's, and is left alone, so running this on every start
is safe. It is not a per-user default; that belongs with invitations and members.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select, text

from app.core.audit import AuditAction, record_audit
from app.models.user import User
from app.models.workspace import Workspace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

DEFAULT_WORKSPACE_NAME = "Default workspace"
# Arbitrary constant naming the lock; shared by every process on the database.
_ADVISORY_LOCK_KEY = 0x5E71_0A01

# Serialises starts inside one process (and is all SQLite needs); the advisory
# lock does the same across processes on PostgreSQL.
_lock = asyncio.Lock()


async def ensure_default_workspace(db: AsyncSession) -> Workspace | None:
    """Create the install's first workspace if it has none. Returns it when created.

    Idempotent and safe to run concurrently: whoever holds the lock counts and
    creates; everyone after sees the workspace and does nothing.
    """
    async with _lock:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            # Released when the transaction ends, so it is held until the commit below.
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})

        if ((await db.execute(select(func.count(Workspace.id)))).scalar() or 0) > 0:
            await db.rollback()
            return None

        first = (
            await db.execute(select(User).order_by(User.id).limit(1))
        ).scalar_one_or_none()
        if first is None:
            await db.rollback()
            return None

        workspace = Workspace(user_id=first.id, name=DEFAULT_WORKSPACE_NAME, is_default=True)
        db.add(workspace)
        await db.flush()
        await record_audit(
            db,
            AuditAction.WORKSPACE_BOOTSTRAP,
            workspace_id=workspace.id,
            target_type="workspace",
            target_id=str(workspace.id),
            details={"owner_user_id": first.id, "reason": "install had no workspace"},
        )
        await db.commit()
        logger.info("default_workspace_created", workspace_id=str(workspace.id), user_id=first.id)
        return workspace
