"""The one way an outbound call is placed.

`/calls` and the campaign worker both dial through `place_outbound_call`, so
neither can reach the carrier without the same three things:

1. the workspace and from-number gates (the agent is the owner's and is in that
   workspace; the from-number is an active number registered there, on this
   carrier: that registration is what proved the account holds it),
2. a `CallRecord` written BEFORE the dial, so the answer webhook, the status
   callbacks and the media stream can all find the workspace the call belongs
   to (no record, and they refuse: a call billed and then silent),
3. the record's id in the answer URL (and so, for Twilio, in the status
   callback URL derived from it), so an early callback finds the record by id
   before the carrier's own call id has been stored.

`provider_call_id` is not nullable (and this change adds no migration), so
until the carrier answers the dial the row holds `pending:<record id>`. It can
never equal a carrier id, so a callback that carries a real id cannot match it
by accident. After the dial it is replaced by the carrier's id; if the dial
raises, the row is marked failed and ended, never left looking live.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import select, update

from app.core.auth import user_id_to_uuid
from app.core.ownership import agent_is_in_workspace, number_variants
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.phone_number import PhoneNumber, PhoneNumberStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.services.telephony.base import CallInfo

logger = structlog.get_logger()

PENDING_PREFIX = "pending:"


class OutboundRefusedError(Exception):
    """A gate said no; nothing was dialled and nothing was written."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Dialler(Protocol):
    async def initiate_call(
        self,
        to_number: str,
        from_number: str,
        webhook_url: str,
        agent_id: str | None = None,
    ) -> CallInfo: ...


async def place_outbound_call(
    db: AsyncSession,
    *,
    provider: str,
    service: _Dialler,
    agent: Agent,
    user_uuid: uuid.UUID,
    workspace_id: uuid.UUID,
    from_number: str,
    to_number: str,
    answer_url: Callable[[str], str],
) -> tuple[CallRecord, CallInfo]:
    """Gate, record, dial, then store the carrier's id. See the module docstring.

    `answer_url` takes the record id and returns the URL the carrier fetches
    when the call is answered.
    """
    if user_id_to_uuid(agent.user_id) != user_uuid:
        raise OutboundRefusedError(403, "Agent does not belong to this account")
    if not await agent_is_in_workspace(db, agent.id, workspace_id):
        raise OutboundRefusedError(403, "Agent is not in this workspace")
    from_record = (
        (
            await db.execute(
                select(PhoneNumber).where(
                    PhoneNumber.phone_number.in_(number_variants(from_number)),
                    PhoneNumber.status == PhoneNumberStatus.ACTIVE.value,
                    PhoneNumber.workspace_id == workspace_id,
                    PhoneNumber.user_id == user_uuid,
                    PhoneNumber.provider == provider,
                )
            )
        )
        .scalars()
        .first()
    )
    if from_record is None:
        raise OutboundRefusedError(
            422, "from_number is not an active number registered in this workspace"
        )

    record_id = uuid.uuid4()
    record = CallRecord(
        id=record_id,
        user_id=user_uuid,
        workspace_id=workspace_id,
        provider=provider,
        provider_call_id=f"{PENDING_PREFIX}{record_id}",
        agent_id=agent.id,
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.INITIATED.value,
        from_number=from_number,
        to_number=to_number,
    )
    db.add(record)
    await db.commit()

    try:
        call_info = await service.initiate_call(
            to_number=to_number,
            from_number=from_number,
            webhook_url=answer_url(str(record_id)),
            agent_id=str(agent.id),
        )
    except Exception:
        # The carrier never took the call, so the row must not look live.
        record.status = CallStatus.FAILED.value
        record.ended_at = datetime.now(UTC)
        await db.commit()
        logger.warning("outbound_dial_failed", record_id=str(record_id), provider=provider)
        raise

    # Only while still pending: an answer or status callback that arrived first
    # has already stored the id the carrier itself used, and that one stands.
    await adopt_provider_call_id(db, record, call_info.call_id)
    return record, call_info


async def adopt_provider_call_id(db: AsyncSession, record: CallRecord, call_id: str) -> bool:
    """Store the carrier's id on a record that is still `pending:`, atomically.

    The conditional UPDATE means whichever of the post-dial write and an early
    webhook gets there first wins, and the other leaves it alone. Commits, and
    refreshes `record` so the caller sees what is stored.
    """
    if not call_id:
        return False
    result = await db.execute(
        update(CallRecord)
        .where(
            CallRecord.id == record.id,
            CallRecord.provider_call_id.startswith(PENDING_PREFIX),
        )
        .values(provider_call_id=call_id)
    )
    await db.commit()
    await db.refresh(record)
    return bool(result.rowcount)  # type: ignore[attr-defined]
