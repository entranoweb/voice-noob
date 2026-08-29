"""Tests for the inline-check endpoint.

This is the seam every external harness comes through - the promptfoo provider,
a CI script, someone's own runner - so what matters is that it needs nothing
persisted first and that it reports unmeasurable runs honestly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.contact import Contact
from app.models.test_scenario import TestScenario

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User

WHEN = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0).isoformat()


@pytest.fixture(autouse=True)
def qa_enabled() -> Any:
    """QA is off and keyless in the test environment; these tests are about the
    endpoint's behaviour once it is on."""
    with patch("app.api.testing.settings") as mock_settings:
        mock_settings.QA_ENABLED = True
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        yield mock_settings


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


@pytest.mark.asyncio
class TestInlineCheck:
    async def test_requires_an_agent_that_exists(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        client, _user = authenticated_test_client

        response = await client.post(
            "/api/v1/testing/check",
            json={"agent_id": str(uuid.uuid4()), "says": ["hello"]},
        )

        assert response.status_code == 404

    async def test_rejects_a_malformed_agent_id(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        client, _user = authenticated_test_client

        response = await client.post(
            "/api/v1/testing/check",
            json={"agent_id": "not-a-uuid", "says": ["hello"]},
        )

        assert response.status_code == 400

    async def test_requires_at_least_one_caller_turn(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """An empty conversation cannot produce a result worth reporting."""
        client, _user = authenticated_test_client

        response = await client.post(
            "/api/v1/testing/check",
            json={"agent_id": str(uuid.uuid4()), "says": []},
        )

        assert response.status_code == 422

    async def test_needs_authentication(
        self,
        test_client: AsyncClient,
    ) -> None:
        """Auth is checked before anything else the endpoint does."""
        response = await test_client.post(
            "/api/v1/testing/check",
            json={"agent_id": str(uuid.uuid4()), "says": ["hello"]},
        )
        assert response.status_code in {401, 403}


@pytest.mark.asyncio
class TestInlineCheckRun:
    """The happy path, driven through HTTP with a scripted model."""

    async def _create_agent(self, user: User, session: Any) -> Agent:
        agent = Agent(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Booking agent",
            system_prompt="You book appointments.",
            pricing_tier="balanced",
            enabled_tools=["crm"],
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent

    async def test_books_and_reports_the_state_it_produced(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        client, user = authenticated_test_client
        agent = await self._create_agent(user, test_session)

        scripted = _ScriptedClient(
            [_tool_use("create_contact", first_name="Jane", phone_number="5551234567")],
            [_tool_use("book_appointment", contact_phone="5551234567", scheduled_at=WHEN)],
            [_text("Booked for tomorrow.")],
        )

        # Patch where the name is bound, not where it is defined: test_runner
        # imported it directly, so patching the source module rebinds nothing.
        with patch(
            "app.services.qa.test_runner.get_anthropic_client",
            return_value=scripted,
        ):
            response = await client.post(
                "/api/v1/testing/check",
                json={
                    "agent_id": str(agent.id),
                    "says": ["I'm Jane on 5551234567, book me for tomorrow"],
                    "invokes": ["book_appointment"],
                    "leaves": {"appointments": [{"status": "scheduled"}]},
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["passed"] is True
        assert body["outcome"] == "passed"
        assert [c["name"] for c in body["tool_calls"]] == [
            "create_contact",
            "book_appointment",
        ]
        assert body["final_state"]["appointments"][0]["status"] == "scheduled"

    async def test_a_talkative_agent_that_books_nothing_fails_with_an_explanation(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        client, user = authenticated_test_client
        agent = await self._create_agent(user, test_session)

        scripted = _ScriptedClient([_text("Absolutely, you're all set for tomorrow!")])

        # Patch where the name is bound, not where it is defined: test_runner
        # imported it directly, so patching the source module rebinds nothing.
        with patch(
            "app.services.qa.test_runner.get_anthropic_client",
            return_value=scripted,
        ):
            response = await client.post(
                "/api/v1/testing/check",
                json={
                    "agent_id": str(agent.id),
                    "says": ["book me for tomorrow"],
                    "invokes": ["book_appointment"],
                    "leaves": {"appointments": [{"status": "scheduled"}]},
                },
            )

        body = response.json()
        assert body["passed"] is False
        assert "task_completion failed" in body["explanation"]
        metrics = {m["metric"]: m for m in body["metrics"]}
        assert metrics["task_completion"]["passed"] is False
        assert metrics["state_restored"]["passed"] is True

    async def test_nothing_is_persisted_by_a_check(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        """The property that lets an external harness call this in a loop."""
        client, user = authenticated_test_client
        agent = await self._create_agent(user, test_session)

        scripted = _ScriptedClient(
            [_tool_use("create_contact", first_name="Jane", phone_number="5551234567")],
            [_text("Thanks.")],
        )

        # Patch where the name is bound, not where it is defined: test_runner
        # imported it directly, so patching the source module rebinds nothing.
        with patch(
            "app.services.qa.test_runner.get_anthropic_client",
            return_value=scripted,
        ):
            await client.post(
                "/api/v1/testing/check",
                json={"agent_id": str(agent.id), "says": ["hello"]},
            )

        assert (await test_session.execute(select(TestScenario))).scalars().all() == []
        assert (await test_session.execute(select(Contact))).scalars().all() == []
