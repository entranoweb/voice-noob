"""Which agent a dialled number reaches, and in which workspace.

This is the one place that decides, and it is the invariant the write paths
(phone numbers, agents, workspaces) only try to keep tidy. A call routes when,
and only when, all of these hold:

1. A `PhoneNumber` record exists for the dialled number (either spelling), it is
   not released, and exactly one account holds it.
2. Its status is `active`. Pending and suspended numbers route nowhere.
3. It has a `workspace_id`. The workspace is what the carrier proof was made
   against and whose credentials sign the webhook.
4. If the caller names the carrier the webhook came from, the record is on it.
5. The agent is the user's own and has an `agent_workspaces` row for THAT
   workspace.

Precedence among agents that pass (4): the record's `assigned_agent_id` first,
then agents whose `phone_number_id` names the number, oldest first. An agent
that fails (4) is skipped, not an error, so a stale assignment cannot hide a
valid agent that names the number.

Anything else routes nowhere. The caller hears "no agent is configured" and the
log carries a warning naming the reason (never the number).
"""

from __future__ import annotations

import uuid  # noqa: TC003 - dataclass field annotation
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from app.core.auth import user_id_to_uuid
from app.models.agent import Agent
from app.models.phone_number import PhoneNumber, PhoneNumberStatus
from app.models.workspace import AgentWorkspace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


@dataclass(frozen=True)
class RouteDecision:
    """The outcome: an agent and the workspace it is reached in, or a reason."""

    agent: Agent | None
    workspace_id: uuid.UUID | None
    reason: str


@dataclass(frozen=True)
class StoppedAgent:
    """An agent that names a number and does not get its calls under the rule."""

    agent_id: uuid.UUID
    reason: str


def _candidates(phone_number: str) -> tuple[str, str, str]:
    bare = phone_number.strip().lstrip("+")
    return (phone_number, bare, f"+{bare}")


async def _in_workspace(db: AsyncSession, agent_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
    row = await db.execute(
        select(AgentWorkspace.agent_id).where(
            AgentWorkspace.agent_id == agent_id, AgentWorkspace.workspace_id == workspace_id
        )
    )
    return row.first() is not None


async def resolve_number_route(  # noqa: PLR0911 - one early exit per reason
    phone_number: str, db: AsyncSession, *, provider: str | None = None
) -> RouteDecision:
    """Apply the rule in the module docstring to one dialled number.

    A webhook passes the carrier it came from: a number proven on one carrier
    must not route a call announced by the other, whose key would then be
    checked against a workspace that holds no account there.
    """
    candidates = _candidates(phone_number)
    records = list(
        (
            await db.execute(
                select(PhoneNumber).where(
                    PhoneNumber.phone_number.in_(candidates),
                    PhoneNumber.status != PhoneNumberStatus.RELEASED.value,
                )
            )
        )
        .scalars()
        .all()
    )
    if not records:
        return _refuse("no_registered_number")
    if len(records) > 1:
        return _refuse("number_held_by_several_records")
    record = records[0]
    if provider is not None and record.provider != provider:
        return _refuse("number_is_on_another_carrier")
    if record.status != PhoneNumberStatus.ACTIVE.value:
        return _refuse("number_not_active")
    if record.workspace_id is None:
        return _refuse("number_has_no_workspace")

    ordered: list[Agent] = []
    if record.assigned_agent_id is not None:
        assigned = await db.get(Agent, record.assigned_agent_id)
        if assigned is not None:
            ordered.append(assigned)
    named = (
        (
            await db.execute(
                select(Agent)
                .where(Agent.phone_number_id.in_(candidates))
                .order_by(Agent.created_at, Agent.id)
            )
        )
        .scalars()
        .all()
    )
    ordered.extend(agent for agent in named if agent not in ordered)
    if not ordered:
        return _refuse("no_agent_for_number")

    for agent in ordered:
        if user_id_to_uuid(agent.user_id) != record.user_id:
            continue
        if await _in_workspace(db, agent.id, record.workspace_id):
            return RouteDecision(agent=agent, workspace_id=record.workspace_id, reason="ok")
    return _refuse("no_agent_in_numbers_workspace")


def _refuse(reason: str) -> RouteDecision:
    logger.warning("inbound_number_routes_nowhere", reason=reason)
    return RouteDecision(agent=None, workspace_id=None, reason=reason)


async def find_agents_that_stop_routing(db: AsyncSession) -> list[StoppedAgent]:
    """Agents that name a number (either column) and would not get its calls.

    Read-only, no carrier call. Run before this rule ships to see its impact.
    """
    from app.models.phone_number import PhoneNumber as Record

    stopped: dict[uuid.UUID, str] = {}
    agents = (
        (
            await db.execute(
                select(Agent)
                .where(Agent.phone_number_id.is_not(None), Agent.phone_number_id != "")
                .order_by(Agent.created_at, Agent.id)
            )
        )
        .scalars()
        .all()
    )
    for agent in agents:
        decision = await resolve_number_route(agent.phone_number_id or "", db)
        if decision.agent is None or decision.agent.id != agent.id:
            # Not routed to this agent. When another agent gets the calls (the
            # assigned one), this agent's field is simply ignored.
            stopped[agent.id] = decision.reason if decision.agent is None else "outranked"
    assigned = (
        await db.execute(
            select(Record).where(
                Record.assigned_agent_id.is_not(None),
                Record.status != PhoneNumberStatus.RELEASED.value,
            )
        )
    ).scalars()
    for record in assigned:
        decision = await resolve_number_route(record.phone_number, db)
        if record.assigned_agent_id is not None and (
            decision.agent is None or decision.agent.id != record.assigned_agent_id
        ):
            stopped.setdefault(
                record.assigned_agent_id,
                decision.reason if decision.agent is None else "outranked",
            )
    return [StoppedAgent(agent_id=a, reason=r) for a, r in stopped.items()]
