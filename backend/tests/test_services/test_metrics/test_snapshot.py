"""Tests for the CRM snapshot that feeds deterministic task completion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from app.services.qa.metrics.accuracy.task_completion import TaskCompletion
from app.services.qa.metrics.context import build_context
from app.services.qa.metrics.snapshot import capture_crm_state

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
class TestCaptureCrmState:
    async def test_empty_crm_snapshots_empty_tables(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        user = await create_test_user()
        state = await capture_crm_state(test_session, user.id)
        assert state == {"contacts": [], "appointments": []}

    async def test_captures_a_contact(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        user = await create_test_user()
        await create_test_contact(user_id=user.id, first_name="Jane", status="new")

        state = await capture_crm_state(test_session, user.id)

        assert len(state["contacts"]) == 1
        assert state["contacts"][0]["first_name"] == "Jane"
        assert state["contacts"][0]["status"] == "new"

    async def test_captures_an_appointment(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        create_test_appointment: Any,
    ) -> None:
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)
        await create_test_appointment(contact_id=contact.id, status="scheduled")

        state = await capture_crm_state(test_session, user.id)

        assert len(state["appointments"]) == 1
        assert state["appointments"][0]["status"] == "scheduled"

    async def test_does_not_leak_another_users_rows(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """A test must not see another tenant's data, and a shared row changing
        underneath a run would make the same scenario pass or fail at random."""
        mine = await create_test_user(email="mine@example.com")
        theirs = await create_test_user(email="theirs@example.com")
        await create_test_contact(user_id=theirs.id, first_name="NotMine")

        state = await capture_crm_state(test_session, mine.id)

        assert state["contacts"] == []

    async def test_omits_ids_and_timestamps(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """They differ on every run, so including them would fail every
        comparison for reasons no scenario cares about."""
        user = await create_test_user()
        await create_test_contact(user_id=user.id)

        record = (await capture_crm_state(test_session, user.id))["contacts"][0]

        assert "id" not in record
        assert "created_at" not in record


@pytest.mark.asyncio
class TestSnapshotFeedsTaskCompletion:
    """The snapshot and the metric have to actually fit together."""

    async def test_a_booked_appointment_passes_the_metric(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        create_test_appointment: Any,
    ) -> None:
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)
        await create_test_appointment(contact_id=contact.id, status="scheduled")

        context = build_context(
            run_id="r",
            success_criteria={"expected_db_state": {"appointments": [{"status": "scheduled"}]}},
            final_db_state=await capture_crm_state(test_session, user.id),
        )

        assert TaskCompletion().compute(context).passed is True

    async def test_no_appointment_fails_the_metric(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """The flagship failure: the agent talked well and booked nothing."""
        user = await create_test_user()
        await create_test_contact(user_id=user.id)

        context = build_context(
            run_id="r",
            success_criteria={"expected_db_state": {"appointments": [{"status": "scheduled"}]}},
            final_db_state=await capture_crm_state(test_session, user.id),
        )

        score = TaskCompletion().compute(context)
        assert score.passed is False
        assert score.detail["diff"]["appointments"]["missing"] == [{"status": "scheduled"}]

    async def test_the_wrong_appointment_time_fails(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        create_test_appointment: Any,
    ) -> None:
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)
        wrong_day = datetime.now(UTC) + timedelta(days=9)
        await create_test_appointment(contact_id=contact.id, scheduled_at=wrong_day)

        expected_day = str(datetime.now(UTC) + timedelta(days=1))
        context = build_context(
            run_id="r",
            success_criteria={
                "expected_db_state": {"appointments": [{"scheduled_at": expected_day}]},
            },
            final_db_state=await capture_crm_state(test_session, user.id),
        )

        assert TaskCompletion().compute(context).passed is False
