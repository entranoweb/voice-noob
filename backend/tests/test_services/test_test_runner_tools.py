"""End-to-end: a simulated run executes the agent's real tools.

This is the behaviour the whole metric layer rests on. A simulator that mocks
tool responses can only tell you the model said the right words; running the
tool for real and then reading the database tells you whether the appointment
exists. These tests assert the second thing.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.contact import Contact
from app.models.test_scenario import TestRunStatus, TestScenario
from app.services.qa.test_runner import TestRunner

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

WHEN = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0).isoformat()


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use(name: str, **arguments: Any) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use",
        id=f"toolu_{uuid.uuid4().hex[:8]}",
        name=name,
        input=arguments,
    )


class _ScriptedClient:
    """An Anthropic client that replays a fixed list of responses.

    Scripted rather than recorded because the point under test is the harness's
    tool loop, not the model's choices.
    """

    def __init__(self, *responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        # Snapshot the messages: the runner keeps appending to the same list,
        # so storing the reference would show every request the final history.
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        blocks = self._responses.pop(0) if self._responses else [_text("Anything else?")]
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop)


async def _scenario(db: AsyncSession, **overrides: Any) -> TestScenario:
    scenario = TestScenario(
        name="Books an appointment",
        description="Caller asks for a slot tomorrow",
        category="booking",
        difficulty="easy",
        caller_persona={"name": "Jane", "phone": "5551234567"},
        conversation_flow=[
            {"speaker": "user", "message": "Hi, can I book for tomorrow? I'm Jane, 5551234567."},
        ],
        expected_behaviors=["collects the caller's details", "books the slot"],
        success_criteria={
            "must_invoke_tools": ["book_appointment"],
            "expected_db_state": {"appointments": [{"status": "scheduled"}]},
        },
        is_active=True,
        is_built_in=False,
        **overrides,
    )
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)
    return scenario


@pytest.mark.asyncio
class TestToolsExecuteForReal:
    async def test_the_agent_books_and_the_database_proves_it(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(test_session)

        client = _ScriptedClient(
            [
                _text("Happy to help."),
                _tool_use("create_contact", first_name="Jane", phone_number="5551234567"),
            ],
            [_tool_use("book_appointment", contact_phone="5551234567", scheduled_at=WHEN)],
            [_text("You're booked for tomorrow.")],
        )

        runner = TestRunner(test_session)
        runner._client = client
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 90}),
        ):
            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        # The appointment was real while the run was happening - the metric saw
        # it in the database, not in a transcript - and is gone afterwards,
        # because the run rolled itself back.
        metrics = test_run.criteria_results["metrics"]
        assert metrics["task_completion"]["passed"] is True
        assert (await test_session.execute(select(Appointment))).scalars().all() == []
        assert metrics["state_restored"]["passed"] is True

        assert test_run.status == TestRunStatus.PASSED.value
        assert test_run.passed is True
        assert [call["name"] for call in test_run.actual_tool_calls] == [
            "create_contact",
            "book_appointment",
        ]

    async def test_a_talkative_agent_that_books_nothing_fails(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """The flagship regression: the transcript reads perfectly and the
        booking never happened. A judge scoring the transcript passes this run;
        the database does not."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(test_session)

        client = _ScriptedClient([_text("Absolutely, you're all set for tomorrow!")])

        runner = TestRunner(test_session)
        runner._client = client
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 95}),
        ):
            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        assert (await test_session.execute(select(Appointment))).scalars().all() == []
        assert test_run.passed is False
        assert test_run.status == TestRunStatus.FAILED.value

        metrics = test_run.criteria_results["metrics"]
        assert metrics["task_completion"]["passed"] is False
        assert metrics["expected_tools_invoked"]["passed"] is False

    async def test_a_malformed_tool_call_is_not_executed(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Missing required arguments is a model failure. Recording it as one
        keeps it distinguishable from a downstream outage."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(test_session)

        client = _ScriptedClient(
            [_tool_use("create_contact", first_name="Jane")],  # no phone_number
            [_text("Sorry, what number can I reach you on?")],
        )

        runner = TestRunner(test_session)
        runner._client = client
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 40}),
        ):
            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        assert test_run.actual_tool_calls[0]["outcome"] == "invalid_args"
        assert test_run.criteria_results["metrics"]["tool_call_validity"]["value"] == 0.0

    async def test_the_model_sees_the_tool_result(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Without the result fed back, the agent cannot confirm what it just
        did and every multi-step scenario stalls."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(test_session)

        client = _ScriptedClient(
            [_tool_use("create_contact", first_name="Jane", phone_number="5551234567")],
            [_text("Thanks Jane.")],
        )

        runner = TestRunner(test_session)
        runner._client = client
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 70}),
        ):
            await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        follow_up = client.requests[1]["messages"]
        results = [
            block
            for message in follow_up
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert len(results) == 1
        assert results[0]["is_error"] is False
        # Tool results ride in a user turn, which is where the Messages API
        # expects them.
        assert follow_up[-1]["role"] == "user"

    async def test_tools_are_offered_to_the_model(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(test_session)

        client = _ScriptedClient([_text("Hello!")])
        runner = TestRunner(test_session)
        runner._client = client
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 70}),
        ):
            await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        offered = {tool["name"] for tool in client.requests[0]["tools"]}
        assert "book_appointment" in offered


@pytest.mark.asyncio
class TestIsolation:
    """A run must not leave the caller's CRM different from how it found it."""

    async def test_a_seeded_fixture_is_visible_to_the_agent(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """The caller claims to be someone. The fixture is what makes that true
        for the length of the run."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(
            test_session,
            fixture={"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]},
        )

        client = _ScriptedClient(
            [_tool_use("search_customer", query="5551234567")],
            [_text("Welcome back, Jane.")],
        )
        runner = TestRunner(test_session)
        runner._client = client
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 80}),
        ):
            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        found = test_run.actual_tool_calls[0]["result"]
        assert "Jane" in json.dumps(found)

    async def test_the_fixture_does_not_outlive_the_run(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(
            test_session,
            fixture={"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]},
        )

        runner = TestRunner(test_session)
        runner._client = _ScriptedClient([_text("Hello!")])
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 50}),
        ):
            await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        assert (await test_session.execute(select(Contact))).scalars().all() == []

    async def test_the_ledger_is_recorded_on_the_run(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """The audit trail is the deliverable, not a side effect."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(
            test_session,
            fixture={"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]},
        )

        runner = TestRunner(test_session)
        runner._client = _ScriptedClient([_text("Hello!")])
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 50}),
        ):
            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        restored = test_run.criteria_results["metrics"]["state_restored"]
        assert restored["passed"] is True
        assert restored["detail"]["seeded_count"] == 1

    async def test_opting_out_leaves_the_writes_in_place(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Isolation is the default, not the only option - but an unscoped run
        reports state_restored as unmeasurable rather than clean."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])
        scenario = await _scenario(test_session)

        client = _ScriptedClient(
            [_tool_use("create_contact", first_name="Jane", phone_number="5551234567")],
            [_text("Thanks Jane.")],
        )
        runner = TestRunner(test_session)
        runner._client = client
        with patch.object(
            runner,
            "_evaluate_conversation",
            new=AsyncMock(return_value={"overall_score": 70}),
        ):
            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
                isolated=False,
            )

        assert len((await test_session.execute(select(Contact))).scalars().all()) == 1
        assert test_run.criteria_results["metrics"]["state_restored"]["value"] is None
