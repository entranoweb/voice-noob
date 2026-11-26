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
from app.models.phone_number import PhoneNumber
from app.models.workspace import AgentWorkspace, Workspace

if TYPE_CHECKING:
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
    workspace on the other end belongs to the same user.

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
    db: AsyncSession, reference: str | None, user: User
) -> None:
    """Refuse an agent's `phone_number_id` that points at someone else's number.

    The field is free text and inbound routing reads it, so without this check
    any signed-in user could write another customer's public phone number into
    their own agent and have that customer's calls resolve to it.

    Three cases:

    - It matches a phone number record (by id, carrier id or the number
      itself) owned by somebody else: refused.
    - It is a diallable number and the user holds no record for it: refused.
      A number has to be registered to its owner before an agent can claim it.
    - It is an opaque carrier id with no record anywhere (what the agent pages
      send from the carrier's own listing): accepted. It can never equal a
      dialled number, so it cannot route anybody's call.
    """
    if reference is None or not reference.strip():
        return
    reference = reference.strip()
    user_uuid = user_id_to_uuid(user.id)

    conditions = [PhoneNumber.provider_id == reference]
    if is_diallable(reference):
        conditions.append(PhoneNumber.phone_number.in_(number_variants(reference)))
    with contextlib.suppress(ValueError):
        conditions.append(PhoneNumber.id == uuid.UUID(reference))

    result = await db.execute(select(PhoneNumber.user_id).where(or_(*conditions)))
    owners = set(result.scalars().all())

    refused = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="That phone number is not registered to your account",
    )
    if owners - {user_uuid}:
        raise refused
    if is_diallable(reference) and user_uuid not in owners:
        raise refused
