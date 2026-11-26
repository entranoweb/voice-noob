"""Request size limits: an oversize body or field is refused with a message naming
the limit, and a value exactly at the limit is accepted.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.core.limits import (
    DESCRIPTION_MAX_CHARS,
    JSON_MAX_CHARS,
    MAX_JSON_BODY_BYTES,
    MAX_TURNS,
    NOTES_MAX_CHARS,
    PROMPT_MAX_CHARS,
    TRANSCRIPT_MAX_CHARS,
    TURN_TEXT_MAX_CHARS,
)
from app.models.agent import Agent
from app.models.call_record import CallRecord
from app.models.contact import Contact
from app.models.workspace import Workspace
from app.services.caller_results import MAX_CALL_PAYLOAD_BYTES
from tests.test_api.test_caller_runs import _bundle, _call, _post

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User

pytestmark = pytest.mark.asyncio


def _detail(response: Any) -> str:
    return json.dumps(response.json()["detail"])


async def _agent(user: User, session: Any) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Limits agent",
        system_prompt="You answer the phone.",
        pricing_tier="balanced",
        enabled_tools=[],
        enable_transcript=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


class TestRealtimeTranscript:
    async def test_a_transcript_over_the_limit_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User], test_session: Any
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent(user, test_session)

        response = await client.post(
            f"/api/v1/realtime/transcript/{agent.id}",
            json={"session_id": "s", "transcript": "x" * (TRANSCRIPT_MAX_CHARS + 1)},
        )

        assert response.status_code == 422
        assert f"at most {TRANSCRIPT_MAX_CHARS} characters" in _detail(response)

    async def test_a_transcript_at_the_limit_is_stored_whole(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent(user, test_session)
        transcript = "x" * TRANSCRIPT_MAX_CHARS

        response = await client.post(
            f"/api/v1/realtime/transcript/{agent.id}",
            json={"session_id": "s", "transcript": transcript, "duration_seconds": 5},
        )

        assert response.status_code == 200, response.text
        await test_session.rollback()
        from sqlalchemy import select

        record = (await test_session.execute(select(CallRecord))).scalar_one()
        assert record.transcript == transcript

    async def test_a_negative_or_absurd_duration_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            f"/api/v1/realtime/transcript/{uuid.uuid4()}",
            json={"session_id": "s", "transcript": "hi", "duration_seconds": 10**9},
        )
        assert response.status_code == 422


class TestBodyCap:
    async def test_a_body_over_the_cap_is_refused_413_before_validation(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/crm/contacts",
            content=b"x" * (MAX_JSON_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert str(MAX_JSON_BODY_BYTES) in response.json()["detail"]


class TestFreeText:
    async def test_contact_notes_over_the_limit_are_refused(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/crm/contacts",
            json={
                "first_name": "Jo",
                "phone_number": "+15550001111",
                "notes": "n" * (NOTES_MAX_CHARS + 1),
            },
        )
        assert response.status_code == 422
        assert f"at most {NOTES_MAX_CHARS} characters" in _detail(response)

    async def test_contact_notes_at_the_limit_are_accepted(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/crm/contacts",
            json={
                "first_name": "Jo",
                "phone_number": "+15550001111",
                "notes": "n" * NOTES_MAX_CHARS,
            },
        )
        assert response.status_code == 201, response.text


class TestScenarioAndCheck:
    async def test_an_inline_check_with_too_many_turns_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/testing/check",
            json={"agent_id": str(uuid.uuid4()), "says": ["hi"] * (MAX_TURNS + 1)},
        )
        assert response.status_code == 422
        assert f"at most {MAX_TURNS} items" in _detail(response)

    async def test_an_inline_check_turn_over_the_limit_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/testing/check",
            json={"agent_id": str(uuid.uuid4()), "says": ["x" * (TURN_TEXT_MAX_CHARS + 1)]},
        )
        assert response.status_code == 422
        assert f"at most {TURN_TEXT_MAX_CHARS} characters" in _detail(response)

    async def test_an_inline_check_json_over_the_limit_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/testing/check",
            json={
                "agent_id": str(uuid.uuid4()),
                "says": ["hi"],
                "given": {"rows": "x" * JSON_MAX_CHARS},
            },
        )
        assert response.status_code == 422
        assert f"at most {JSON_MAX_CHARS} characters as JSON" in _detail(response)

    async def test_an_inline_check_at_the_limits_passes_validation(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/testing/check",
            json={
                "agent_id": str(uuid.uuid4()),
                "says": ["x" * TURN_TEXT_MAX_CHARS] * MAX_TURNS,
            },
        )
        # Validation and the body cap passed; the next thing checked is whether QA is enabled.
        assert response.status_code not in (413, 422), response.text

    async def test_a_scenario_description_over_the_limit_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client
        response = await client.post(
            "/api/v1/testing/scenarios",
            json={
                "name": "s",
                "description": "d" * (DESCRIPTION_MAX_CHARS + 1),
                "category": "booking",
                "difficulty": "easy",
                "caller_persona": {},
                "conversation_flow": [],
                "expected_behaviors": [],
                "success_criteria": {},
            },
        )
        assert response.status_code == 422
        assert f"at most {DESCRIPTION_MAX_CHARS} characters" in _detail(response)


class TestResavingAValueAlreadyOverTheCap:
    """A row saved before a cap existed keeps saving; a changed value is capped.

    The stored value is written straight to the database, as it would be if it
    pre-dates the cap, and is never cut.
    """

    @pytest.mark.parametrize(
        ("field", "cap"),
        [
            ("system_prompt", PROMPT_MAX_CHARS),
            ("description", DESCRIPTION_MAX_CHARS),
            ("initial_greeting", TURN_TEXT_MAX_CHARS),
        ],
    )
    async def test_an_agent_text_field_sent_back_unchanged_is_accepted_and_kept(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
        field: str,
        cap: int,
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent(user, test_session)
        stored = "s" * (cap + 500)
        setattr(agent, field, stored)
        await test_session.commit()

        same = await client.put(f"/api/v1/agents/{agent.id}", json={field: stored})
        assert same.status_code == 200, same.text

        await test_session.refresh(agent)
        assert getattr(agent, field) == stored  # never cut

    @pytest.mark.parametrize(
        ("field", "cap"),
        [
            ("system_prompt", PROMPT_MAX_CHARS),
            ("description", DESCRIPTION_MAX_CHARS),
            ("initial_greeting", TURN_TEXT_MAX_CHARS),
        ],
    )
    async def test_changing_it_to_another_oversized_value_is_refused(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
        field: str,
        cap: int,
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent(user, test_session)
        stored = "s" * (cap + 500)
        setattr(agent, field, stored)
        await test_session.commit()

        changed = await client.put(f"/api/v1/agents/{agent.id}", json={field: "t" * (cap + 500)})

        assert changed.status_code == 422
        assert f"at most {cap} characters" in _detail(changed)
        await test_session.refresh(agent)
        assert getattr(agent, field) == stored

    async def test_a_value_at_the_cap_is_still_accepted_as_a_change(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent(user, test_session)
        response = await client.put(
            f"/api/v1/agents/{agent.id}", json={"system_prompt": "p" * PROMPT_MAX_CHARS}
        )
        assert response.status_code == 200, response.text

    async def test_embed_settings_sent_back_unchanged_are_accepted_a_bigger_one_is_not(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent(user, test_session)
        stored = {"note": "e" * (JSON_MAX_CHARS + 500)}
        agent.embed_settings = stored
        await test_session.commit()

        same = await client.patch(
            f"/api/v1/agents/{agent.id}/embed", json={"embed_settings": stored}
        )
        assert same.status_code == 200, same.text

        bigger = await client.patch(
            f"/api/v1/agents/{agent.id}/embed",
            json={"embed_settings": {"note": "f" * (JSON_MAX_CHARS + 900)}},
        )
        assert bigger.status_code == 422
        await test_session.refresh(agent)
        assert agent.embed_settings == stored

    async def test_workspace_settings_sent_back_unchanged_are_accepted(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        stored = {"note": "w" * (JSON_MAX_CHARS + 500)}
        workspace = await test_session.get(Workspace, test_workspace.id)
        assert workspace is not None
        workspace.settings = stored
        await test_session.commit()

        same = await client.put(
            f"/api/v1/workspaces/{test_workspace.id}", json={"settings": stored}
        )
        assert same.status_code == 200, same.text

        changed = await client.put(
            f"/api/v1/workspaces/{test_workspace.id}",
            json={"settings": {"note": "x" * (JSON_MAX_CHARS + 500)}},
        )
        assert changed.status_code == 422
        await test_session.refresh(workspace)
        assert workspace.settings == stored

    async def test_contact_notes_sent_back_unchanged_are_accepted(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        client, user = authenticated_test_client
        stored = "n" * (NOTES_MAX_CHARS + 300)
        contact = Contact(
            user_id=user.id, first_name="Jo", phone_number="+15550002222", notes=stored
        )
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)

        # The request model strips and re-checks nothing about length on its own.
        same = await client.put(f"/api/v1/crm/contacts/{contact.id}", json={"notes": stored})
        assert same.status_code == 200, same.text

        changed = await client.put(
            f"/api/v1/crm/contacts/{contact.id}", json={"notes": "m" * (NOTES_MAX_CHARS + 300)}
        )
        assert changed.status_code == 422
        assert f"at most {NOTES_MAX_CHARS} characters" in _detail(changed)


class TestCancelAppendsToNotesNearTheCap:
    """The cancel tool appends a reason to notes. It is not a request field, so it
    is not capped: it must neither fail nor cut what is stored."""

    async def test_cancelling_with_notes_at_the_cap_keeps_them_whole(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: Any,
    ) -> None:
        from datetime import UTC, datetime

        from app.models.appointment import Appointment
        from app.services.tools.crm_tools import CRMTools

        _, user = authenticated_test_client
        contact = Contact(user_id=user.id, first_name="Jo", phone_number="+15550003333")
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)
        notes = "n" * NOTES_MAX_CHARS
        appointment = Appointment(
            contact_id=contact.id,
            scheduled_at=datetime(2030, 1, 1, 10, 0, tzinfo=UTC),
            status="scheduled",
            notes=notes,
        )
        test_session.add(appointment)
        await test_session.commit()
        await test_session.refresh(appointment)

        result = await CRMTools(test_session, user.id).cancel_appointment(
            appointment.id, reason="r" * 500
        )

        assert result["success"] is True, result
        await test_session.refresh(appointment, ["notes"])
        assert appointment.notes is not None
        assert appointment.notes.startswith(notes)
        assert appointment.notes.endswith("Cancellation reason: " + "r" * 500)
        assert len(appointment.notes) > NOTES_MAX_CHARS


class TestCallerUpload:
    async def test_a_body_over_the_bundle_cap_is_still_413(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The upload keeps its own cap; the general body cap does not apply to it."""
        client, _ = authenticated_test_client
        monkeypatch.setattr("app.api.caller_runs.MAX_BUNDLE_BYTES", 200)
        response = await _post(client, test_workspace.id, _bundle())
        assert response.status_code == 413

    async def test_an_upload_larger_than_the_general_cap_is_not_refused_by_it(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        bundle = _bundle()
        bundle["note"] = "x" * (MAX_JSON_BODY_BYTES + 10)

        response = await _post(client, test_workspace.id, bundle)

        assert response.status_code == 201, response.text

    async def test_one_call_over_its_payload_cap_is_refused_422(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Workspace,
    ) -> None:
        client, _ = authenticated_test_client
        bundle = _bundle()
        bundle["runs"][0]["payload"] = _call(
            "big", "passed", payload_extra={"blob": "x" * MAX_CALL_PAYLOAD_BYTES}
        )

        response = await _post(client, test_workspace.id, bundle)

        assert response.status_code == 422
        assert "the most one call may hold" in _detail(response)

    async def test_a_realistic_call_is_nowhere_near_the_call_cap(self) -> None:
        sizes = len(json.dumps(_call("existing-patient", "passed")))
        assert sizes < MAX_CALL_PAYLOAD_BYTES // 100
