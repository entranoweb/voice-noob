"""Database-side handling of caller audio: delete it with its owner, and expire it.

Every deletion removes the file first and the row after. A file that cannot be
removed is reported to the caller, never skipped: whoever asked for deletion
must be able to say it did not fully happen.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import ColumnElement, delete, select, text, update

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
    query = (
        select(CallerAudio.id, CallerAudio.created_at, Workspace.audio_retention_days)
        .join(Workspace, Workspace.id == CallerAudio.workspace_id)
        .where(CallerAudio.expired_at.is_(None))
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
    rows = (await db.execute(select(CallerAudio).where(CallerAudio.id.in_(due)))).scalars().all()
    failed = 0
    done: list[Any] = []
    for row in rows:
        # Deleting a file that is already gone succeeds, so two workers that
        # race here do no harm. The row is kept, marked expired.
        if store.delete(row.storage_key):
            done.append(row.id)
        else:
            failed += 1
    if done:
        await db.execute(update(CallerAudio).where(CallerAudio.id.in_(done)).values(expired_at=now))
    counts["expired"] = len(done)
    counts["failed"] = failed
    if failed:
        logger.error("caller_audio_expiry_incomplete", failed=failed)
    return counts


# One number for every worker process: whoever holds it runs the pass.
_ADVISORY_LOCK_KEY = 7_302_025_001


async def expire_audio_once(
    session_factory: Any, store: AudioStore | None = None
) -> dict[str, int] | None:
    """One expiry pass in its own transaction. None when another process is running it.

    On PostgreSQL a transaction-level advisory lock keeps several workers from
    all doing the pass; the deletes are idempotent anyway, so on other databases
    (tests) it simply runs.
    """
    from app.services.audio_store import get_audio_store

    async with session_factory() as db:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            got = (
                await db.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
                )
            ).scalar()
            if not got:
                return None
        counts = await expire_audio(db, store or get_audio_store())
        await db.commit()
    if counts["expired"] or counts["failed"]:
        logger.warning("caller_audio_expired", **counts)
    return counts


class AudioExpiryWorker:
    """Deletes expired call audio at start-up and then every interval. Needs no operator."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        from app.core.config import settings
        from app.db.session import AsyncSessionLocal

        while True:
            try:
                await expire_audio_once(AsyncSessionLocal)
            except Exception:
                logger.exception("caller_audio_expiry_failed")
            await asyncio.sleep(max(1, settings.CALLER_AUDIO_EXPIRY_INTERVAL_MINUTES) * 60)


_worker: AudioExpiryWorker | None = None


async def start_audio_expiry_worker() -> None:
    global _worker
    if _worker is None:
        _worker = AudioExpiryWorker()
    await _worker.start()


async def stop_audio_expiry_worker() -> None:
    global _worker
    if _worker is not None:
        await _worker.stop()
        _worker = None
