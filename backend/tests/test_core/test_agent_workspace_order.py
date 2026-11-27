"""Which workspace an agent acts in, when it is linked to more than one.

The workspace decides whose API keys and integrations a request uses and which
account a call is billed to, so the choice must not depend on the order Postgres
happens to return rows in. A query with no ORDER BY returns rows in physical
order, and `UPDATE` writes a new row version at a later position. So a link
that was inserted first and then touched sits AFTER one inserted later: the
unordered query answers with the other workspace, while the ordered one still
answers the default.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text, update

from app.api.auth import get_password_hash
from app.core.ownership import agent_workspace_id
from app.models.agent import Agent
from app.models.user import User
from app.models.workspace import AgentWorkspace, Workspace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

APP = Path(__file__).resolve().parents[2] / "app"
# The phone path is owned by another change and is checked there.
EXCLUDED = ("api/telephony.py", "api/telephony_ws.py", "services/telephony/")


async def _owner_with_agent(session: AsyncSession, email: str) -> tuple[User, Agent]:
    user = User(
        email=email,
        hashed_password=get_password_hash("correct-horse-battery"),
        full_name="Owner",
        is_active=True,
        is_superuser=False,
    )
    session.add(user)
    await session.commit()
    agent = Agent(user_id=user.id, name="Agent", system_prompt="x", pricing_tier="premium")
    session.add(agent)
    await session.commit()
    return user, agent


async def _workspace(session: AsyncSession, user: User, name: str) -> Workspace:
    workspace = Workspace(user_id=user.id, name=name)
    session.add(workspace)
    await session.commit()
    return workspace


async def _push_to_the_end_of_the_table(session: AsyncSession, link: AgentWorkspace) -> None:
    """Rewrite the row so Postgres stores its newest version after the others."""
    await session.execute(
        update(AgentWorkspace)
        .where(AgentWorkspace.id == link.id)
        .values(updated_at=datetime.now(UTC))
    )
    await session.commit()


async def _unordered_first(session: AsyncSession, agent: Agent) -> object:
    row = await session.execute(
        text("SELECT workspace_id FROM agent_workspaces WHERE agent_id = :a LIMIT 1"),
        {"a": agent.id},
    )
    return row.scalar_one()


@pytest.mark.asyncio
async def test_default_link_wins_even_when_physically_last(test_session: AsyncSession) -> None:
    user, agent = await _owner_with_agent(test_session, "a@example.test")
    default_ws = await _workspace(test_session, user, "default")
    other_ws = await _workspace(test_session, user, "other")
    default_link = AgentWorkspace(agent_id=agent.id, workspace_id=default_ws.id, is_default=True)
    test_session.add(default_link)
    await test_session.commit()
    test_session.add(AgentWorkspace(agent_id=agent.id, workspace_id=other_ws.id, is_default=False))
    await test_session.commit()
    await _push_to_the_end_of_the_table(test_session, default_link)

    # Proof the setup bites: a bare LIMIT 1 picks the wrong workspace.
    if test_session.bind.dialect.name == "postgresql":  # type: ignore[union-attr]
        assert await _unordered_first(test_session, agent) == other_ws.id

    assert await agent_workspace_id(test_session, agent) == default_ws.id


@pytest.mark.asyncio
async def test_oldest_link_wins_when_none_is_default(test_session: AsyncSession) -> None:
    user, agent = await _owner_with_agent(test_session, "b@example.test")
    old_ws = await _workspace(test_session, user, "old")
    new_ws = await _workspace(test_session, user, "new")
    now = datetime.now(UTC)
    old_link = AgentWorkspace(
        agent_id=agent.id, workspace_id=old_ws.id, created_at=now - timedelta(days=30)
    )
    test_session.add(old_link)
    await test_session.commit()
    test_session.add(AgentWorkspace(agent_id=agent.id, workspace_id=new_ws.id, created_at=now))
    await test_session.commit()
    await _push_to_the_end_of_the_table(test_session, old_link)

    assert await agent_workspace_id(test_session, agent) == old_ws.id


@pytest.mark.asyncio
async def test_a_link_to_someone_elses_workspace_is_never_chosen(
    test_session: AsyncSession,
) -> None:
    user, agent = await _owner_with_agent(test_session, "c@example.test")
    stranger, _ = await _owner_with_agent(test_session, "d@example.test")
    foreign_ws = await _workspace(test_session, stranger, "foreign")
    own_ws = await _workspace(test_session, user, "own")
    test_session.add(AgentWorkspace(agent_id=agent.id, workspace_id=foreign_ws.id, is_default=True))
    test_session.add(AgentWorkspace(agent_id=agent.id, workspace_id=own_ws.id))
    await test_session.commit()

    assert await agent_workspace_id(test_session, agent) == own_ws.id


@pytest.mark.asyncio
async def test_no_link_is_none(test_session: AsyncSession) -> None:
    _, agent = await _owner_with_agent(test_session, "e@example.test")

    assert await agent_workspace_id(test_session, agent) is None


def test_no_unordered_agent_workspace_lookup_outside_the_helper() -> None:
    """A new `select(AgentWorkspace...).limit(1)` must go through the helper."""
    offenders = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel == "core/ownership.py" or rel.startswith(EXCLUDED):
            continue
        source = path.read_text()
        for match in re.finditer(r"select\(\s*AgentWorkspace[^;]*?\.limit\(1\)", source, re.S):
            offenders.append(f"{rel}: {match.group(0)[:60]!r}")
    assert offenders == []
