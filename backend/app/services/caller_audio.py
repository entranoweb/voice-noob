"""Database-side handling of caller audio: delete it with its owner, and expire it.

Every deletion removes the file first and the row after. A file that cannot be
removed is reported to the caller, never skipped: whoever asked for deletion
must be able to say it did not fully happen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import ColumnElement, delete, select

from app.models.caller_audio import CallerAudio
from app.models.workspace import Workspace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.audio_store import AudioStore

logger = structlog.get_logger()


async def delete_audio(
    db: AsyncSession, store: AudioStore, *conditions: ColumnElement[bool]
) -> tuple[int, list[str]]:
    """Delete the matching audio. Returns (rows deleted, ids of rows whose file would not go).

    A row whose file could not be removed is kept, so that the file is not
    orphaned and a later attempt (or the next sweep) can find it. Does not commit.
    """
    rows = (await db.execute(select(CallerAudio).where(*conditions))).scalars().all()
    gone: list[Any] = []
    failed: list[str] = []
    for row in rows:
        if store.delete(row.storage_key):
            gone.append(row.id)
        else:
            failed.append(str(row.id))
    if gone:
        await db.execute(delete(CallerAudio).where(CallerAudio.id.in_(gone)))
    return len(gone), failed


async def expire_audio(
    db: AsyncSession,
    store: AudioStore,
    *,
    workspace_ids: list[Any] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete audio older than its workspace's ``audio_retention_days``.

    Age is counted from when the audio was stored. Audio younger than its
    workspace's period is never touched, whatever another workspace sets. With
    ``dry_run`` nothing is deleted and ``expired`` is what would be. Does not commit.
    """
    now = now or datetime.now(UTC)
    query = select(CallerAudio.id, CallerAudio.created_at, Workspace.audio_retention_days).join(
        Workspace, Workspace.id == CallerAudio.workspace_id
    )
    if workspace_ids is not None:
        query = query.where(CallerAudio.workspace_id.in_(workspace_ids))
    due: list[Any] = []
    for audio_id, created, days in (await db.execute(query)).all():
        stored = created if created.tzinfo is not None else created.replace(tzinfo=UTC)
        if stored < now - timedelta(days=days):
            due.append(audio_id)
    counts = {"expired": len(due), "failed": 0}
    if dry_run or not due:
        return counts
    _, failed = await delete_audio(db, store, CallerAudio.id.in_(due))
    counts["expired"] = len(due) - len(failed)
    counts["failed"] = len(failed)
    if failed:
        logger.error("caller_audio_expiry_incomplete", failed=len(failed))
    return counts
