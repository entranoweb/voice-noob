"""Erasure, retention and the record of each data-protection run.

One place decides which rows belong to an account, because the owner is keyed
two ways in this schema: agents, workspaces, contacts and test runs carry the
integer user id; call records, phone numbers, campaigns, settings and
integrations carry a UUID derived from it. Comparing the wrong pair raises on
PostgreSQL, which is how export and erasure were broken.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import ColumnElement, delete, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditAction, record_audit
from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.models.agent import Agent
from app.models.appointment import Appointment
from app.models.call_evaluation import CallEvaluation
from app.models.call_interaction import CallInteraction
from app.models.call_record import CallRecord
from app.models.campaign import Campaign, CampaignContact
from app.models.compliance_run import (
    KIND_RETENTION,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TRIGGER_SCHEDULE,
    ComplianceRun,
)
from app.models.contact import Contact
from app.models.phone_number import PhoneNumber
from app.models.privacy_settings import ConsentRecord, PrivacySettings
from app.models.test_scenario import TestRun, TestScenario
from app.models.user import User
from app.models.user_integration import UserIntegration
from app.models.user_settings import UserSettings
from app.models.workspace import AgentWorkspace, Workspace

logger = structlog.get_logger()

# The shortest retention period an account may set. A period of 0 or less would
# delete every call the account has; anything under a month is a slip, not a policy.
MIN_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
# In a run's counts: this sweep counted what it would delete and deleted nothing.
DRY_RUN = "dry_run"
# In an erasure's counts: recordings the carrier still holds, which erasure
# here cannot reach.
RECORDINGS_LEFT = "recordings_left_at_carrier"
RETENTION_LOCK_KEY = "compliance:retention:lock"
RETENTION_LOCK_SECONDS = 3600
# How often the worker wakes to see whether a sweep is due.
RETENTION_POLL_SECONDS = 900


# =============================================================================
# Which rows belong to an account
# =============================================================================


async def owned_workspace_ids(db: AsyncSession, user_id: int) -> list[uuid.UUID]:
    result = await db.execute(select(Workspace.id).where(Workspace.user_id == user_id))
    return list(result.scalars().all())


async def owned_agent_ids(db: AsyncSession, user_id: int) -> list[uuid.UUID]:
    result = await db.execute(select(Agent.id).where(Agent.user_id == user_id))
    return list(result.scalars().all())


def _any_of(*conditions: ColumnElement[bool] | None) -> ColumnElement[bool]:
    present = [condition for condition in conditions if condition is not None]
    return or_(*present) if present else false()


def call_record_owner_filter(
    user_id: int, agent_ids: list[uuid.UUID], workspace_ids: list[uuid.UUID]
) -> ColumnElement[bool]:
    """Call records that belong to the account, however they were linked."""
    return _any_of(
        CallRecord.user_id == user_id_to_uuid(user_id),
        CallRecord.agent_id.in_(agent_ids) if agent_ids else None,
        CallRecord.workspace_id.in_(workspace_ids) if workspace_ids else None,
    )


def contact_owner_filter(user_id: int, workspace_ids: list[uuid.UUID]) -> ColumnElement[bool]:
    return _any_of(
        Contact.user_id == user_id,
        Contact.workspace_id.in_(workspace_ids) if workspace_ids else None,
    )


def _rows(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


# =============================================================================
# Run records
# =============================================================================


async def start_run(
    db: AsyncSession, kind: str, *, trigger: str, user_id: int | None
) -> ComplianceRun:
    run = ComplianceRun(kind=kind, trigger=trigger, user_id=user_id, status=STATUS_RUNNING)
    db.add(run)
    await db.flush()
    return run


def finish_run(run: ComplianceRun, counts: dict[str, int] | None = None) -> None:
    run.status = STATUS_SUCCEEDED
    run.finished_at = datetime.now(UTC)
    run.counts = dict(counts) if counts is not None else None


async def record_failed_run(
    db: AsyncSession, kind: str, *, trigger: str, user_id: int | None, error: BaseException
) -> None:
    """Record a run that raised. The caller has already rolled its work back."""
    db.add(
        ComplianceRun(
            kind=kind,
            trigger=trigger,
            user_id=user_id,
            status=STATUS_FAILED,
            finished_at=datetime.now(UTC),
            error=type(error).__name__,
        )
    )
    await db.commit()


async def latest_run(
    db: AsyncSession,
    kind: str,
    *,
    user_id: int | None = None,
    trigger: str | None = None,
) -> ComplianceRun | None:
    """The most recent finished run of a kind, optionally for one account."""
    query = (
        select(ComplianceRun)
        .where(ComplianceRun.kind == kind, ComplianceRun.status != STATUS_RUNNING)
        .order_by(ComplianceRun.started_at.desc())
        .limit(1)
    )
    if user_id is not None:
        query = query.where(ComplianceRun.user_id == user_id)
    if trigger is not None:
        query = query.where(ComplianceRun.trigger == trigger)
    result = await db.execute(query)
    return result.scalar_one_or_none()


# =============================================================================
# Erasure
# =============================================================================


async def carrier_recordings(db: AsyncSession, user_id: int) -> list[dict[str, str]]:
    """The account's calls whose recording is held by a carrier, by the carrier's own id.

    Erasure here deletes the row and with it the link to the recording. The
    recording itself is in the carrier's account and stays there until it is
    deleted there. These are the references to do that with.
    """
    workspace_ids = await owned_workspace_ids(db, user_id)
    agent_ids = await owned_agent_ids(db, user_id)
    result = await db.execute(
        select(CallRecord.provider, CallRecord.provider_call_id)
        .where(call_record_owner_filter(user_id, agent_ids, workspace_ids))
        .where(CallRecord.recording_url.is_not(None))
        .where(CallRecord.recording_url != "")
    )
    return [
        {"provider": str(provider), "provider_call_id": str(call_id)}
        for provider, call_id in result.all()
    ]


async def erase_account_data(
    db: AsyncSession, user_id: int, not_erased: list[dict[str, str]] | None = None
) -> dict[str, int]:
    """Delete everything the account holds. Does not commit; does not delete the login.

    Children are deleted before parents and by explicit statement, so the result
    does not depend on which ON DELETE rules a given database carries.

    What cannot be deleted from here is counted, not passed over: recordings a
    carrier holds are listed into ``not_erased`` (when given) and counted under
    ``recordings_left_at_carrier``, so that the run never reads as complete
    while they exist.
    """
    user_uuid = user_id_to_uuid(user_id)
    workspace_ids = await owned_workspace_ids(db, user_id)
    agent_ids = await owned_agent_ids(db, user_id)
    counts: dict[str, int] = {}
    left = await carrier_recordings(db, user_id)
    if not_erased is not None:
        not_erased.extend(left)

    call_filter = call_record_owner_filter(user_id, agent_ids, workspace_ids)
    owned_calls = select(CallRecord.id).where(call_filter)
    contact_filter = contact_owner_filter(user_id, workspace_ids)
    owned_contacts = select(Contact.id).where(contact_filter)
    owned_campaigns = select(Campaign.id).where(Campaign.user_id == user_uuid)

    counts["call_evaluations"] = _rows(
        await db.execute(
            delete(CallEvaluation).where(
                _any_of(
                    CallEvaluation.call_id.in_(owned_calls),
                    CallEvaluation.workspace_id.in_(workspace_ids) if workspace_ids else None,
                    CallEvaluation.agent_id.in_(agent_ids) if agent_ids else None,
                )
            )
        )
    )
    counts["campaign_contacts"] = _rows(
        await db.execute(
            delete(CampaignContact).where(
                _any_of(
                    CampaignContact.campaign_id.in_(owned_campaigns),
                    CampaignContact.contact_id.in_(owned_contacts),
                )
            )
        )
    )
    counts["campaigns"] = _rows(
        await db.execute(delete(Campaign).where(Campaign.user_id == user_uuid))
    )
    counts["test_runs"] = _rows(
        await db.execute(
            delete(TestRun).where(
                _any_of(
                    TestRun.user_id == user_id,
                    TestRun.agent_id.in_(agent_ids) if agent_ids else None,
                )
            )
        )
    )
    counts["test_scenarios"] = _rows(
        await db.execute(delete(TestScenario).where(TestScenario.user_id == user_id))
    )
    counts["call_interactions"] = _rows(
        await db.execute(
            delete(CallInteraction).where(
                _any_of(
                    CallInteraction.contact_id.in_(owned_contacts),
                    CallInteraction.workspace_id.in_(workspace_ids) if workspace_ids else None,
                )
            )
        )
    )
    counts["appointments"] = _rows(
        await db.execute(
            delete(Appointment).where(
                _any_of(
                    Appointment.contact_id.in_(owned_contacts),
                    Appointment.workspace_id.in_(workspace_ids) if workspace_ids else None,
                )
            )
        )
    )
    counts["call_records"] = _rows(await db.execute(delete(CallRecord).where(call_filter)))
    counts["phone_numbers"] = _rows(
        await db.execute(delete(PhoneNumber).where(PhoneNumber.user_id == user_uuid))
    )
    counts["contacts"] = _rows(await db.execute(delete(Contact).where(contact_filter)))
    counts["agent_workspaces"] = _rows(
        await db.execute(
            delete(AgentWorkspace).where(
                _any_of(
                    AgentWorkspace.agent_id.in_(agent_ids) if agent_ids else None,
                    AgentWorkspace.workspace_id.in_(workspace_ids) if workspace_ids else None,
                )
            )
        )
    )
    counts["agents"] = _rows(await db.execute(delete(Agent).where(Agent.user_id == user_id)))
    counts["integrations"] = _rows(
        await db.execute(delete(UserIntegration).where(UserIntegration.user_id == user_uuid))
    )
    counts["user_settings"] = _rows(
        await db.execute(delete(UserSettings).where(UserSettings.user_id == user_uuid))
    )
    counts["workspaces"] = _rows(
        await db.execute(delete(Workspace).where(Workspace.user_id == user_id))
    )
    counts["consent_records"] = _rows(
        await db.execute(delete(ConsentRecord).where(ConsentRecord.user_id == user_id))
    )
    counts["privacy_settings"] = _rows(
        await db.execute(delete(PrivacySettings).where(PrivacySettings.user_id == user_id))
    )
    counts[RECORDINGS_LEFT] = len(left)
    return counts


# =============================================================================
# Retention
# =============================================================================


async def cleanup_expired_data(
    db: AsyncSession, *, user_id: int | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Delete call data older than each account's retention period.

    Covers every account, or one when ``user_id`` is given. An account with no
    privacy settings has no retention period, and nothing of its is deleted:
    there is no default period to delete by. An account whose period is below
    ``MIN_RETENTION_DAYS`` is skipped and logged, never swept. Consent records
    are kept: they are the evidence that consent was given or withdrawn.

    With ``dry_run`` nothing is deleted; the counts are of what would be. Does
    not commit.
    """
    now = datetime.now(UTC)
    counts = {"call_interactions": 0, "call_evaluations": 0, "call_records": 0, "accounts": 0}

    async def remove(model: Any, *conditions: ColumnElement[bool]) -> int:
        if dry_run:
            query = select(func.count()).select_from(model)
            for condition in conditions:
                query = query.where(condition)
            return int((await db.execute(query)).scalar_one())
        statement = delete(model)
        for condition in conditions:
            statement = statement.where(condition)
        return _rows(await db.execute(statement))

    users_query = select(User.id)
    if user_id is not None:
        users_query = users_query.where(User.id == user_id)
    account_ids = list((await db.execute(users_query)).scalars().all())

    settings_result = await db.execute(select(PrivacySettings))
    retention_by_user = {
        row.user_id: row.data_retention_days for row in settings_result.scalars().all()
    }

    for account_id in account_ids:
        days = retention_by_user.get(account_id)
        if days is None:
            continue  # no period was ever set for this account
        if days < MIN_RETENTION_DAYS:
            counts["skipped_invalid_period"] = counts.get("skipped_invalid_period", 0) + 1
            logger.error(
                "retention_period_below_minimum_account_skipped",
                user_id=account_id,
                days=days,
                minimum=MIN_RETENTION_DAYS,
            )
            continue
        cutoff = now - timedelta(days=days)
        workspace_ids = await owned_workspace_ids(db, account_id)
        agent_ids = await owned_agent_ids(db, account_id)
        counts["accounts"] += 1

        owned_contacts = select(Contact.id).where(contact_owner_filter(account_id, workspace_ids))
        counts["call_interactions"] += await remove(
            CallInteraction,
            _any_of(
                CallInteraction.contact_id.in_(owned_contacts),
                CallInteraction.workspace_id.in_(workspace_ids) if workspace_ids else None,
            ),
            CallInteraction.call_started_at < cutoff,
        )

        old_calls = (
            select(CallRecord.id)
            .where(call_record_owner_filter(account_id, agent_ids, workspace_ids))
            .where(CallRecord.created_at < cutoff)
        )
        counts["call_evaluations"] += await remove(
            CallEvaluation, CallEvaluation.call_id.in_(old_calls)
        )
        counts["call_records"] += await remove(
            CallRecord,
            call_record_owner_filter(account_id, agent_ids, workspace_ids),
            CallRecord.created_at < cutoff,
        )

    return counts


async def run_retention(
    db: AsyncSession, *, trigger: str, user_id: int | None = None, dry_run: bool = False
) -> ComplianceRun:
    """Run a retention sweep and record how it ended. Commits.

    What would be deleted is counted and logged before anything is. With
    ``dry_run`` the sweep stops there, and its run is recorded as a dry run.
    """
    try:
        run = await start_run(db, KIND_RETENTION, trigger=trigger, user_id=user_id)
        would = await cleanup_expired_data(db, user_id=user_id, dry_run=True)
        logger.warning(
            "retention_sweep_would_delete", trigger=trigger, dry_run=dry_run, would_delete=would
        )
        if dry_run:
            counts = {**would, DRY_RUN: 1}
        else:
            counts = await cleanup_expired_data(db, user_id=user_id)
        finish_run(run, counts)
        await record_audit(
            db,
            AuditAction.RETENTION_SWEEP,
            actor_user_id=user_id,
            target_type="account" if user_id is not None else "all_accounts",
            target_id=str(user_id) if user_id is not None else None,
            details={"trigger": trigger, "dry_run": dry_run, "deleted": counts},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        await record_failed_run(db, KIND_RETENTION, trigger=trigger, user_id=user_id, error=exc)
        logger.exception("retention_sweep_failed", trigger=trigger)
        raise
    logger.warning("retention_sweep_completed", trigger=trigger, deleted=counts)
    return run


async def sweep_if_due(db: AsyncSession, redis: Any) -> ComplianceRun | None:
    """Run the scheduled sweep when one is due and no other process holds it.

    Returns the run, or None when nothing was done. A Redis lock keeps several
    workers from sweeping at once; the last recorded run decides whether a
    sweep is due, so a restart does not trigger an extra one.

    The first sweep a database has ever had deletes nothing: it counts what it
    would delete, logs and records that, and leaves the deleting to the sweep
    one interval later. Turning the sweep on, or upgrading into it, therefore
    never deletes within minutes of a start.
    """
    interval = timedelta(hours=max(1, settings.RETENTION_SWEEP_INTERVAL_HOURS))
    last = await latest_run(db, KIND_RETENTION, trigger=TRIGGER_SCHEDULE)
    rehearsed = last is not None and last.status == STATUS_SUCCEEDED
    if last is not None and last.status == STATUS_SUCCEEDED:
        started = last.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if datetime.now(UTC) - started < interval:
            return None

    token = uuid.uuid4().hex
    acquired = await redis.set(RETENTION_LOCK_KEY, token, nx=True, ex=RETENTION_LOCK_SECONDS)
    if not acquired:
        return None
    try:
        return await run_retention(db, trigger=TRIGGER_SCHEDULE, dry_run=not rehearsed)
    finally:
        with contextlib.suppress(Exception):
            if await redis.get(RETENTION_LOCK_KEY) == token:
                await redis.delete(RETENTION_LOCK_KEY)


class RetentionWorker:
    """Background task that runs the retention sweep on its interval."""

    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_loop(self) -> None:
        from app.db.redis import get_redis
        from app.db.session import AsyncSessionLocal

        while self.running:
            try:
                redis = await get_redis()
                async with AsyncSessionLocal() as db:
                    await sweep_if_due(db, redis)
            except Exception:
                logger.exception("retention_worker_tick_failed")
            await asyncio.sleep(RETENTION_POLL_SECONDS)


_worker: RetentionWorker | None = None


async def start_retention_worker() -> None:
    global _worker
    if _worker is None:
        _worker = RetentionWorker()
    await _worker.start()


async def stop_retention_worker() -> None:
    global _worker
    if _worker is not None:
        await _worker.stop()
        _worker = None
