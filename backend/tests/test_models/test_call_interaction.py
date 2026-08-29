"""Tests for CallInteraction model."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_interaction import CallInteraction


class TestCallInteractionModel:
    """Test CallInteraction model creation and validation."""

    @pytest.mark.asyncio
    async def test_create_call_interaction_success(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        sample_call_interaction_data: dict[str, Any],
        refresh_full: Any,
    ) -> None:
        """Test creating a call interaction with all fields."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        call = CallInteraction(contact_id=contact.id, **sample_call_interaction_data)
        test_session.add(call)
        await test_session.commit()
        await refresh_full(test_session, call)

        assert call.id is not None
        assert call.contact_id == contact.id
        assert call.call_started_at == sample_call_interaction_data["call_started_at"]
        assert call.call_ended_at == sample_call_interaction_data["call_ended_at"]
        assert call.duration_seconds == sample_call_interaction_data["duration_seconds"]
        assert call.agent_name == sample_call_interaction_data["agent_name"]
        assert call.agent_id == sample_call_interaction_data["agent_id"]
        assert call.outcome == sample_call_interaction_data["outcome"]
        assert call.transcript == sample_call_interaction_data["transcript"]
        assert call.ai_summary == sample_call_interaction_data["ai_summary"]
        assert call.sentiment_score == sample_call_interaction_data["sentiment_score"]
        assert call.action_items == sample_call_interaction_data["action_items"]
        assert call.notes == sample_call_interaction_data["notes"]
        assert call.created_at is not None
        assert call.updated_at is not None

    @pytest.mark.asyncio
    async def test_create_call_interaction_minimal_fields(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        refresh_full: Any,
    ) -> None:
        """Test creating a call interaction with minimal required fields."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        call_time = datetime.now(UTC)

        call = CallInteraction(
            contact_id=contact.id,
            call_started_at=call_time,
        )
        test_session.add(call)
        await test_session.commit()
        await refresh_full(test_session, call)

        assert call.id is not None
        assert call.contact_id == contact.id
        assert call.call_started_at == call_time
        assert call.call_ended_at is None
        assert call.duration_seconds is None
        assert call.agent_name is None
        assert call.agent_id is None
        assert call.outcome is None
        assert call.transcript is None
        assert call.ai_summary is None
        assert call.sentiment_score is None
        assert call.action_items is None
        assert call.notes is None

    @pytest.mark.asyncio
    async def test_call_interaction_foreign_key_constraint(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test that call interaction requires valid contact_id."""
        call = CallInteraction(
            contact_id=99999,  # Non-existent contact
            call_started_at=datetime.now(UTC),
        )
        test_session.add(call)

        with pytest.raises(IntegrityError):
            await test_session.commit()

    @pytest.mark.asyncio
    async def test_call_interaction_outcome_values(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test different call interaction outcome values."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        outcomes = ["answered", "voicemail", "no_answer", "callback_requested", "busy"]

        for i, outcome in enumerate(outcomes):
            call = CallInteraction(
                contact_id=contact.id,
                call_started_at=datetime.now(UTC) - timedelta(hours=i),
                outcome=outcome,
            )
            test_session.add(call)

        await test_session.commit()

        # Verify all calls were created with correct outcome
        result = await test_session.execute(select(CallInteraction))
        calls = result.scalars().all()
        call_outcomes = {c.outcome for c in calls}

        assert call_outcomes == set(outcomes)

    @pytest.mark.asyncio
    async def test_call_interaction_repr(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test call interaction string representation."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        call_time = datetime.now(UTC)

        call = CallInteraction(
            contact_id=contact.id,
            call_started_at=call_time,
            outcome="answered",
        )
        test_session.add(call)
        await test_session.commit()
        await test_session.refresh(call)

        repr_str = repr(call)
        assert "CallInteraction" in repr_str
        assert "answered" in repr_str

    @pytest.mark.asyncio
    async def test_call_interaction_with_transcript(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        refresh_full: Any,
    ) -> None:
        """Test call interaction with long transcript."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        long_transcript = "Agent: Hello. Customer: Hi. " * 500

        call = CallInteraction(
            contact_id=contact.id,
            call_started_at=datetime.now(UTC),
            transcript=long_transcript,
        )
        test_session.add(call)
        await test_session.commit()
        await refresh_full(test_session, call)

        assert call.transcript == long_transcript

    @pytest.mark.asyncio
    async def test_call_interaction_sentiment_score_range(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test call interaction with different sentiment scores."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        sentiment_scores = [-1.0, -0.5, 0.0, 0.5, 1.0]

        for i, score in enumerate(sentiment_scores):
            call = CallInteraction(
                contact_id=contact.id,
                call_started_at=datetime.now(UTC) - timedelta(hours=i),
                sentiment_score=score,
            )
            test_session.add(call)

        await test_session.commit()

        # Verify all scores were stored correctly
        result = await test_session.execute(select(CallInteraction))
        calls = result.scalars().all()
        stored_scores = {c.sentiment_score for c in calls}

        assert stored_scores == set(sentiment_scores)

    @pytest.mark.asyncio
    async def test_call_interaction_duration_calculation(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test call interaction with calculated duration."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        start_time = datetime.now(UTC)
        end_time = start_time + timedelta(minutes=5, seconds=30)
        duration = int((end_time - start_time).total_seconds())

        call = CallInteraction(
            contact_id=contact.id,
            call_started_at=start_time,
            call_ended_at=end_time,
            duration_seconds=duration,
        )
        test_session.add(call)
        await test_session.commit()
        await test_session.refresh(call)

        assert call.duration_seconds == 330  # 5 minutes 30 seconds

    @pytest.mark.asyncio
    async def test_query_calls_by_contact(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test querying call interactions by contact_id."""
        user = await create_test_user()
        contact1 = await create_test_contact(user_id=user.id, phone_number="+1111111111")
        contact2 = await create_test_contact(user_id=user.id, phone_number="+2222222222")

        # Create calls for contact1
        for i in range(3):
            call = CallInteraction(
                contact_id=contact1.id,
                call_started_at=datetime.now(UTC) - timedelta(hours=i),
            )
            test_session.add(call)

        # Create calls for contact2
        for i in range(2):
            call = CallInteraction(
                contact_id=contact2.id,
                call_started_at=datetime.now(UTC) - timedelta(hours=i + 10),
            )
            test_session.add(call)

        await test_session.commit()

        # Query contact1's calls
        result = await test_session.execute(
            select(CallInteraction).where(CallInteraction.contact_id == contact1.id)
        )
        contact1_calls = result.scalars().all()

        assert len(contact1_calls) == 3

    @pytest.mark.asyncio
    async def test_query_calls_by_outcome(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test querying call interactions by outcome."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        # Create calls with different outcomes
        for i, outcome in enumerate(["answered", "voicemail", "no_answer"]):
            call = CallInteraction(
                contact_id=contact.id,
                call_started_at=datetime.now(UTC) - timedelta(hours=i),
                outcome=outcome,
            )
            test_session.add(call)

        await test_session.commit()

        # Query answered calls
        result = await test_session.execute(
            select(CallInteraction).where(CallInteraction.outcome == "answered")
        )
        answered_calls = result.scalars().all()

        assert len(answered_calls) == 1
        assert answered_calls[0].outcome == "answered"

    @pytest.mark.asyncio
    async def test_query_calls_by_agent(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test querying call interactions by agent_id."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        # Create calls with different agents
        for i in range(3):
            call = CallInteraction(
                contact_id=contact.id,
                call_started_at=datetime.now(UTC) - timedelta(hours=i),
                agent_id=f"agent-{i % 2}",  # Two different agents
            )
            test_session.add(call)

        await test_session.commit()

        # Query calls by specific agent
        result = await test_session.execute(
            select(CallInteraction).where(CallInteraction.agent_id == "agent-0")
        )
        agent_calls = result.scalars().all()

        assert len(agent_calls) == 2


class TestCallInteractionRelationships:
    """Test CallInteraction model relationships."""

    @pytest.mark.asyncio
    async def test_call_interaction_contact_relationship(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test call interaction to contact relationship."""
        user = await create_test_user()
        contact = await create_test_contact(
            user_id=user.id,
            first_name="Jane",
            phone_number="+1234567890",
        )

        call = CallInteraction(
            contact_id=contact.id,
            call_started_at=datetime.now(UTC),
        )
        test_session.add(call)
        await test_session.commit()
        await test_session.refresh(call)

        # Access contact through relationship
        assert call.contact is not None
        assert call.contact.id == contact.id
        assert call.contact.first_name == "Jane"
        assert call.contact.phone_number == "+1234567890"
