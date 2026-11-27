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

import ast
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
    # The default link is the NEWER one, so only is_default ordering can pick it.
    now = datetime.now(UTC)
    default_link = AgentWorkspace(
        agent_id=agent.id, workspace_id=default_ws.id, is_default=True, created_at=now
    )
    test_session.add(default_link)
    await test_session.commit()
    test_session.add(
        AgentWorkspace(
            agent_id=agent.id,
            workspace_id=other_ws.id,
            is_default=False,
            created_at=now - timedelta(days=30),
        )
    )
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


# Files that may query agent_workspaces without the helper, and why.
ALLOWED_LOOKUPS = {
    # Known leftovers: an unordered `.limit(1)`. Pending PR #67, which takes the
    # workspace from the call record and deletes these lookups.
    "api/telephony.py",
    "api/telephony_ws.py",
    # Link management: exact (agent, workspace) pair, or whole lists, never a
    # "pick one" lookup.
    "api/workspaces.py",
}


def _queries_agent_workspaces(tree: ast.AST) -> list[int]:
    """Line numbers of select()/text() calls that reach the agent_workspaces table."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"select", "text"}:
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Name) and sub.id == "AgentWorkspace") or (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and "agent_workspaces" in sub.value
            ):
                lines.append(node.lineno)
                break
    return lines


def test_agent_workspace_lookups_go_through_the_helper() -> None:
    """Any select()/text() on agent_workspaces outside the helper fails unless listed."""
    offenders = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel == "core/ownership.py" or rel in ALLOWED_LOOKUPS:
            continue
        offenders += [f"{rel}:{n}" for n in _queries_agent_workspaces(ast.parse(path.read_text()))]
    assert offenders == []


def test_the_guard_sees_the_shapes_a_regex_would_miss() -> None:
    sources = [
        "q = select(AgentWorkspace.workspace_id)\nq = q.limit(1)",
        "(select(AgentWorkspace).where(x)\n.scalars().first())",
        "select(\n  AgentWorkspace\n)",
        "select(AgentWorkspace.workspace_id.label('w'))",
        "text('SELECT workspace_id FROM agent_workspaces')",
    ]
    for source in sources:
        assert _queries_agent_workspaces(ast.parse(source)), source
    assert not _queries_agent_workspaces(ast.parse("select(Agent)"))
