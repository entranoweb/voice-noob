"""Whose object is this: the checks every by-id route shares.

An id in a path or a request body says which object the caller wants. It says
nothing about whether the object is theirs. Each route that takes an agent id,
a workspace id or a phone number from the client loads it through one of these
helpers, so "not yours" and "does not exist" get the same answer (404) and a
route cannot forget the owner filter.

404, not 403, on purpose: answering 403 for another customer's agent confirms
that the id is real.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlalchemy import or_, select

from app.core.auth import user_id_to_uuid
from app.models.agent import Agent
from app.models.phone_number import PhoneNumber, PhoneNumberStatus
from app.models.workspace import AgentWorkspace, Workspace

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

# What inbound routing can match against a dialled number: digits, with or
# without the leading plus. E.164 allows at most 15 digits; carrier object ids
# (a Twilio "PN…" sid, a 19-digit Telnyx id) fall outside this on purpose.
_DIALLABLE = re.compile(r"^\+?\d{7,15}$")


# Which of an agent's workspace links wins: default first, then oldest, then id
# for a total order.
AGENT_WORKSPACE_ORDER = (
    AgentWorkspace.is_default.desc(),
    AgentWorkspace.created_at,
    AgentWorkspace.id,
)


def parse_uuid(value: str | uuid.UUID, what: str) -> uuid.UUID:
    """A UUID from client input, or a 404 for `what` when it is not one."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found"
        ) from exc


async def get_owned_agent(db: AsyncSession, agent_id: str | uuid.UUID, user: User) -> Agent:
    """The agent with this id if `user` owns it; 404 otherwise."""
    agent_uuid = parse_uuid(agent_id, "Agent")
    result = await db.execute(select(Agent).where(Agent.id == agent_uuid, Agent.user_id == user.id))
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


async def get_owned_workspace(
    db: AsyncSession, workspace_id: str | uuid.UUID, user: User
) -> Workspace:
    """The workspace with this id if `user` owns it; 404 otherwise."""
    workspace_uuid = parse_uuid(workspace_id, "Workspace")
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_uuid, Workspace.user_id == user.id)
    )
    workspace = result.scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


async def owned_agent_workspace_id(db: AsyncSession, agent: Agent, user: User) -> uuid.UUID | None:
    """The workspace an owned agent acts in, when that workspace is also the user's.

    The link table is written by the owner, but a row is only trusted when the
    workspace on the other end belongs to the same user. An agent can be in
    several workspaces; the pick is then the default one, otherwise the lowest
    id, so it is at least the same every time. It is for the pages that need "a"
    workspace; call routing never uses it (the number's workspace decides).

    An agent can be linked to several workspaces. The answer is the default link
    first, then the oldest, then the lowest id, so the same workspace (and so the
    same API key and bill) is chosen every time.
    """
    result = await db.execute(
        select(AgentWorkspace.workspace_id)
        .join(Workspace, Workspace.id == AgentWorkspace.workspace_id)
        .where(AgentWorkspace.agent_id == agent.id, Workspace.user_id == user.id)
        .order_by(*AGENT_WORKSPACE_ORDER)
        .limit(1)
    )
    return result.scalar_one_or_none()


def is_diallable(value: str | None) -> bool:
    """Whether this string is something a dialled number could equal."""
    return value is not None and _DIALLABLE.match(value.strip()) is not None


def number_variants(number: str) -> tuple[str, str]:
    """The two spellings of one number: with and without the leading plus."""
    bare = number.strip().lstrip("+")
    return (bare, f"+{bare}")


async def ensure_phone_reference_is_owned(
    db: AsyncSession,
    reference: str | None,
    user: User,
    agent_workspace_id: uuid.UUID | None = None,
) -> None:
    """Refuse an agent's `phone_number_id` that is not a live number of this account.

    The field is free text and inbound routing reads it, so without this check
    any signed-in user could write another customer's public phone number into
    their own agent and have that customer's calls resolve to it.

    A diallable reference (digits, with or without the plus) is matched on the
    registered number itself and nothing else. `provider_id` is free text the
    registrant typed and is never proven, so it must not make a different
    number claimable: a reference is accepted only when a live (not released)
    record of the user holds that number, which registration proved.

    A non-diallable reference is an opaque carrier id or a record id:

    - It matches a record owned by somebody else: refused.
    - It matches only released records of the user: refused.
    - It matches nothing anywhere (what the agent pages send from the carrier's
      own listing): accepted. It can never equal a dialled number, so it
      cannot route anybody's call.

    When `agent_workspace_id` is given (the agent already belongs to a
    workspace), a record in a different workspace is refused with 422: the
    carrier proof is per workspace, so routing across workspaces defeats it.
    """
    if reference is None or not reference.strip():
        return
    reference = reference.strip()
    user_uuid = user_id_to_uuid(user.id)
    live = PhoneNumber.status != PhoneNumberStatus.RELEASED.value

    refused = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="That phone number is not registered to your account",
    )

    if is_diallable(reference):
        result = await db.execute(
            select(PhoneNumber).where(
                PhoneNumber.phone_number.in_(number_variants(reference)), live
            )
        )
        records = list(result.scalars().all())
        if not records or any(record.user_id != user_uuid for record in records):
            raise refused
    else:
        conditions = [PhoneNumber.provider_id == reference]
        with contextlib.suppress(ValueError):
            conditions.append(PhoneNumber.id == uuid.UUID(reference))
        result = await db.execute(select(PhoneNumber).where(or_(*conditions)))
        records = list(result.scalars().all())
        if any(record.user_id != user_uuid for record in records):
            raise refused
        if records and not any(
            record.status != PhoneNumberStatus.RELEASED.value for record in records
        ):
            raise refused

    if agent_workspace_id is not None and any(
        record.workspace_id is not None and record.workspace_id != agent_workspace_id
        for record in records
        if record.status != PhoneNumberStatus.RELEASED.value
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That phone number belongs to a different workspace than this agent.",
        )


def reference_condition(reference: str) -> ColumnElement[bool]:
    """Which records an agent's `phone_number_id` value refers to.

    A diallable value is the number itself (either spelling); anything else is a
    carrier id or a record id.
    """
    if is_diallable(reference):
        return PhoneNumber.phone_number.in_(number_variants(reference))
    conditions = [PhoneNumber.provider_id == reference]
    with contextlib.suppress(ValueError):
        conditions.append(PhoneNumber.id == uuid.UUID(reference))
    return or_(*conditions)


async def agent_number_conflicts(
    db: AsyncSession, agent: Agent, workspace_ids: list[uuid.UUID] | set[uuid.UUID]
) -> bool:
    """Whether the agent would be cut off from a live number that names it.

    Both columns count: a live record that has this agent as `assigned_agent_id`,
    or a live record that the agent's own `phone_number_id` refers to. Such a
    number only routes while the agent is in the number's workspace, so a set of
    workspaces that does not contain that workspace is a conflict. Records with
    no workspace are not (they route nowhere, whatever the agent does).
    """
    live = PhoneNumber.status != PhoneNumberStatus.RELEASED.value
    conditions = [PhoneNumber.assigned_agent_id == agent.id]
    reference = (agent.phone_number_id or "").strip()
    if reference:
        conditions.append(reference_condition(reference))
    records = (await db.execute(select(PhoneNumber).where(or_(*conditions), live))).scalars().all()
    return any(
        record.workspace_id is not None and record.workspace_id not in workspace_ids
        for record in records
    )


async def ensure_agent_number_fits_workspaces(
    db: AsyncSession, agent: Agent, workspace_ids: list[uuid.UUID] | set[uuid.UUID]
) -> None:
    """Refuse (422) linking an agent into workspaces its numbers are not in.

    This is a clear message at the write; routing does not rely on it (the read
    path in `number_routing` checks the same fact on every call).
    """
    if workspace_ids and await agent_number_conflicts(db, agent, workspace_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "That agent is used by a phone number registered to a different workspace. "
                "Move the number to this workspace or unassign the agent first."
            ),
        )


async def agent_workspace_set(db: AsyncSession, agent_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(
        select(AgentWorkspace.workspace_id).where(AgentWorkspace.agent_id == agent_id)
    )
    return set(rows.scalars().all())


async def agents_dialling_number_outside(
    db: AsyncSession, user: User, number: str, workspace_id: uuid.UUID
) -> list[Agent]:
    """Agents of `user` whose `phone_number_id` is `number` and that are not in `workspace_id`.

    Agents in no workspace are not counted: they are not reached from any
    workspace's webhook, and the link step governs where they may go.
    """
    agents = (
        (
            await db.execute(
                select(Agent).where(
                    Agent.user_id == user.id,
                    Agent.phone_number_id.in_(number_variants(number)),
                )
            )
        )
        .scalars()
        .all()
    )
    outside: list[Agent] = []
    for agent in agents:
        linked = set(
            (
                await db.execute(
                    select(AgentWorkspace.workspace_id).where(AgentWorkspace.agent_id == agent.id)
                )
            )
            .scalars()
            .all()
        )
        if linked and workspace_id not in linked:
            outside.append(agent)
    return outside


async def agent_is_in_workspace(
    db: AsyncSession, agent_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(AgentWorkspace.agent_id).where(
            AgentWorkspace.agent_id == agent_id, AgentWorkspace.workspace_id == workspace_id
        )
    )
    return result.first() is not None
