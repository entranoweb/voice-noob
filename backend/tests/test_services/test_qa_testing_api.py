"""Tests for the pytest-facing scenario API.

Two things under test: that an inline scenario runs without persisting
anything, and that a failure explains itself well enough to act on. The second
is the point of the module - a bare `assert False` about a voice agent is
worthless at 2am.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.contact import Contact
from app.models.test_scenario import TestScenario
from app.services.qa.metrics.runner import RunOutcome
from app.services.qa.mutations import Mutation
from app.services.qa.test_runner import TestRunner
from app.services.qa.testing import RunResult, ScenarioSpec, checker

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
    def __init__(self, *responses: list[Any]) -> None:
        self._responses = list(responses)
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **_: Any) -> Any:
        blocks = self._responses.pop(0) if self._responses else [_text("Anything else?")]
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop)


def _checker(session: AsyncSession, *responses: list[Any]) -> Any:
    runner = TestRunner(session)
    runner._client = _ScriptedClient(*responses)
    return checker(runner)


class TestScenarioSpec:
    def test_builds_a_transient_scenario(self) -> None:
        """A scenario is an argument, not a row. Nothing should need saving."""
        scenario = ScenarioSpec(says=["hello"], invokes=["book_appointment"]).to_scenario()

        assert isinstance(scenario, TestScenario)
        assert scenario.conversation_flow == [{"speaker": "user", "message": "hello"}]
        assert scenario.success_criteria["must_invoke_tools"] == ["book_appointment"]

    def test_expected_state_lands_where_the_metric_reads_it(self) -> None:
        spec = ScenarioSpec(says=["hi"], leaves={"appointments": [{"status": "scheduled"}]})
        criteria = spec.to_scenario().success_criteria
        assert criteria["expected_db_state"] == {"appointments": [{"status": "scheduled"}]}

    def test_omits_criteria_that_were_not_declared(self) -> None:
        """An absent expectation must not become an empty one that always passes."""
        assert ScenarioSpec(says=["hi"]).to_scenario().success_criteria == {}

    def test_fixture_is_carried_through(self) -> None:
        given = {"contacts": [{"first_name": "Jane", "phone_number": "555"}]}
        assert ScenarioSpec(says=["hi"], given=given).to_scenario().fixture == given


@pytest.mark.asyncio
class TestCheck:
    async def test_a_passing_run_is_truthy(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        result = await _checker(
            test_session,
            [_tool_use("create_contact", first_name="Jane", phone_number="5551234567")],
            [_tool_use("book_appointment", contact_phone="5551234567", scheduled_at=WHEN)],
            [_text("Booked.")],
        ).check(
            agent=agent,
            user_id=user.id,
            says=["I'm Jane on 5551234567, book me for tomorrow"],
            invokes=["book_appointment"],
            leaves={"appointments": [{"status": "scheduled"}]},
        )

        assert result
        assert result.passed is True
        assert result.tools_invoked() == ["create_contact", "book_appointment"]

    async def test_nothing_is_persisted(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """No scenario row, no run row, and no leftover CRM state."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        await _checker(
            test_session,
            [_tool_use("create_contact", first_name="Jane", phone_number="5551234567")],
            [_text("Thanks.")],
        ).check(agent=agent, user_id=user.id, says=["hello"])

        assert (await test_session.execute(select(TestScenario))).scalars().all() == []
        assert (await test_session.execute(select(Contact))).scalars().all() == []

    async def test_a_fixture_is_visible_to_the_agent(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        result = await _checker(
            test_session,
            [_tool_use("book_appointment", contact_phone="5551234567", scheduled_at=WHEN)],
            [_text("Done.")],
        ).check(
            agent=agent,
            user_id=user.id,
            says=["book me for tomorrow"],
            given={"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]},
            leaves={"appointments": [{"status": "scheduled"}]},
        )

        assert result, result.explain()
        assert (await test_session.execute(select(Appointment))).scalars().all() == []

    async def test_the_judge_is_off_by_default(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """A CI run should not pay for a model call to narrate a verdict the
        deterministic assertions already reached."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        result = await _checker(test_session, [_text("Hello!")]).check(
            agent=agent,
            user_id=user.id,
            says=["hi"],
        )

        assert result.judgement == {}


@pytest.mark.asyncio
class TestFailureMessages:
    async def test_a_missing_booking_says_so(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """The flagship failure. The message has to name what was expected and
        what the agent actually did instead."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        result = await _checker(
            test_session,
            [_text("Absolutely, you're all set for tomorrow!")],
        ).check(
            agent=agent,
            user_id=user.id,
            says=["book me for tomorrow"],
            invokes=["book_appointment"],
            leaves={"appointments": [{"status": "scheduled"}]},
        )

        assert not result
        message = result.explain()
        assert "task_completion failed" in message
        assert "expected_tools_invoked failed" in message
        assert "tools invoked: none" in message
        assert "all set for tomorrow" in message

    async def test_the_repr_is_the_explanation(self) -> None:
        """pytest prints the repr of a falsey object, so that is where the
        explanation has to live for it to reach anyone."""
        result = RunResult(
            outcome=RunOutcome.FAILED,
            metrics=type(
                "M",
                (),
                {"scores": (), "invalid_reasons": ()},
            )(),  # type: ignore[arg-type]
            transcript=[],
            tool_calls=[],
            final_state={},
            ledger=None,
        )
        assert repr(result) == result.explain()

    async def test_an_unmeasurable_run_is_falsey_but_named_an_error(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """A run that could not be measured must not report success - and must
        not be mistaken for the agent failing either."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        result = await _checker(test_session, [_text("Hello!")]).check(
            agent=agent,
            user_id=user.id,
            says=["hi"],
        )

        assert not result
        assert result.errored is True
        assert "not an agent failure" in result.explain()


@pytest.mark.asyncio
class TestCompare:
    """A/B one configuration against another, through the real runner."""

    async def test_runs_every_variant_the_requested_number_of_times(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        comparison = await _checker(test_session).compare(
            agent=agent,
            user_id=user.id,
            spec=ScenarioSpec(says=["hello"]),
            variants={"terse": {"system_prompt": "Be brief."}},
            repeats=3,
        )

        assert len(comparison.base.runs) == 3
        assert len(comparison.variants) == 1
        assert len(comparison.variants[0].runs) == 3
        assert comparison.variants[0].name == "terse"

    async def test_the_variant_actually_reaches_the_model(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Without this the comparison would run the same prompt twice and
        report the difference between two samples of the same thing."""
        user = await create_test_user()
        agent = await create_test_agent(
            user_id=user.id,
            enabled_tools=["crm"],
            system_prompt="Original prompt.",
        )

        runner = TestRunner(test_session)
        client = _ScriptedClient()
        runner._client = client
        seen: list[str] = []
        original_create = client.messages.create

        async def recording_create(**kwargs: Any) -> Any:
            seen.append(str(kwargs.get("system")))
            return await original_create(**kwargs)

        client.messages.create = recording_create  # type: ignore[method-assign]

        await checker(runner).compare(
            agent=agent,
            user_id=user.id,
            spec=ScenarioSpec(says=["hello"]),
            variants=[Mutation(name="terse", overrides={"system_prompt": "Be brief."})],
            repeats=1,
        )

        assert "Original prompt." in seen
        assert "Be brief." in seen

    async def test_a_tiny_sample_is_reported_as_inconclusive(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Two runs cannot separate two configurations, and the comparison has
        to say so rather than point at whichever number is higher."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        comparison = await _checker(test_session).compare(
            agent=agent,
            user_id=user.id,
            spec=ScenarioSpec(says=["hello"]),
            variants={"terse": {"system_prompt": "Be brief."}},
            repeats=2,
        )

        assert comparison.winner() is None
        assert "inconclusive" in comparison.explain()

    async def test_nothing_leaks_between_variants(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Every run rolls back, so a variant never starts from state an
        earlier one left behind - which would bias whichever ran second."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, enabled_tools=["crm"])

        runner = TestRunner(test_session)
        runner._client = _ScriptedClient(
            [_tool_use("create_contact", first_name="Jane", phone_number="5551234567")],
            [_text("Done.")],
        )

        await checker(runner).compare(
            agent=agent,
            user_id=user.id,
            spec=ScenarioSpec(says=["hello"]),
            variants={"terse": {"system_prompt": "Be brief."}},
            repeats=2,
        )

        assert (await test_session.execute(select(Contact))).scalars().all() == []
