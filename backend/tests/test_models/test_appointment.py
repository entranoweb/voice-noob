"""Tests for Appointment model."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment


class TestAppointmentModel:
    """Test Appointment model creation and validation."""

    @pytest.mark.asyncio
    async def test_create_appointment_success(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        sample_appointment_data: dict[str, Any],
        refresh_full: Any,
    ) -> None:
        """Test creating an appointment with all fields."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        appointment = Appointment(contact_id=contact.id, **sample_appointment_data)
        test_session.add(appointment)
        await test_session.commit()
        await refresh_full(test_session, appointment)

        assert appointment.id is not None
        assert appointment.contact_id == contact.id
        assert appointment.scheduled_at == sample_appointment_data["scheduled_at"]
        assert appointment.duration_minutes == sample_appointment_data["duration_minutes"]
        assert appointment.status == sample_appointment_data["status"]
        assert appointment.service_type == sample_appointment_data["service_type"]
        assert appointment.notes == sample_appointment_data["notes"]
        assert appointment.created_by_agent == sample_appointment_data["created_by_agent"]
        assert appointment.created_at is not None
        assert appointment.updated_at is not None

    @pytest.mark.asyncio
    async def test_create_appointment_minimal_fields(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        refresh_full: Any,
    ) -> None:
        """Test creating an appointment with minimal required fields."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        scheduled_time = datetime.now(UTC) + timedelta(days=1)

        appointment = Appointment(
            contact_id=contact.id,
            scheduled_at=scheduled_time,
        )
        test_session.add(appointment)
        await test_session.commit()
        await refresh_full(test_session, appointment)

        assert appointment.id is not None
        assert appointment.contact_id == contact.id
        assert appointment.scheduled_at == scheduled_time
        assert appointment.duration_minutes == 30  # Default value
        assert appointment.status == "scheduled"  # Default value
        assert appointment.service_type is None
        assert appointment.notes is None
        assert appointment.created_by_agent is None

    @pytest.mark.asyncio
    async def test_appointment_foreign_key_constraint(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test that appointment requires valid contact_id."""
        appointment = Appointment(
            contact_id=99999,  # Non-existent contact
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )
        test_session.add(appointment)

        with pytest.raises(IntegrityError):
            await test_session.commit()

    @pytest.mark.asyncio
    async def test_appointment_default_values(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test appointment default values."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        appointment = Appointment(
            contact_id=contact.id,
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )
        test_session.add(appointment)
        await test_session.commit()
        await test_session.refresh(appointment)

        assert appointment.duration_minutes == 30
        assert appointment.status == "scheduled"

    @pytest.mark.asyncio
    async def test_appointment_status_values(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test different appointment status values."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        statuses = ["scheduled", "completed", "cancelled", "no_show"]

        for i, status in enumerate(statuses):
            appointment = Appointment(
                contact_id=contact.id,
                scheduled_at=datetime.now(UTC) + timedelta(days=i + 1),
                status=status,
            )
            test_session.add(appointment)

        await test_session.commit()

        # Verify all appointments were created with correct status
        result = await test_session.execute(select(Appointment))
        appointments = result.scalars().all()
        appointment_statuses = {a.status for a in appointments}

        assert appointment_statuses == set(statuses)

    @pytest.mark.asyncio
    async def test_appointment_repr(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test appointment string representation."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        scheduled_time = datetime.now(UTC) + timedelta(days=1)

        appointment = Appointment(
            contact_id=contact.id,
            scheduled_at=scheduled_time,
            status="scheduled",
        )
        test_session.add(appointment)
        await test_session.commit()
        await test_session.refresh(appointment)

        repr_str = repr(appointment)
        assert "Appointment" in repr_str
        assert "scheduled" in repr_str

    @pytest.mark.asyncio
    async def test_appointment_with_service_type(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test appointment with service type."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        appointment = Appointment(
            contact_id=contact.id,
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
            service_type="consultation",
        )
        test_session.add(appointment)
        await test_session.commit()
        await test_session.refresh(appointment)

        assert appointment.service_type == "consultation"

    @pytest.mark.asyncio
    async def test_appointment_with_custom_duration(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test appointment with custom duration."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        appointment = Appointment(
            contact_id=contact.id,
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
            duration_minutes=60,
        )
        test_session.add(appointment)
        await test_session.commit()
        await test_session.refresh(appointment)

        assert appointment.duration_minutes == 60

    @pytest.mark.asyncio
    async def test_query_appointments_by_contact(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test querying appointments by contact_id."""
        user = await create_test_user()
        contact1 = await create_test_contact(user_id=user.id, phone_number="+1111111111")
        contact2 = await create_test_contact(user_id=user.id, phone_number="+2222222222")

        # Create appointments for contact1
        for i in range(3):
            appointment = Appointment(
                contact_id=contact1.id,
                scheduled_at=datetime.now(UTC) + timedelta(days=i + 1),
            )
            test_session.add(appointment)

        # Create appointments for contact2
        for i in range(2):
            appointment = Appointment(
                contact_id=contact2.id,
                scheduled_at=datetime.now(UTC) + timedelta(days=i + 10),
            )
            test_session.add(appointment)

        await test_session.commit()

        # Query contact1's appointments
        result = await test_session.execute(
            select(Appointment).where(Appointment.contact_id == contact1.id)
        )
        contact1_appointments = result.scalars().all()

        assert len(contact1_appointments) == 3

    @pytest.mark.asyncio
    async def test_query_appointments_by_status(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test querying appointments by status."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        # Create appointments with different statuses
        for i, status in enumerate(["scheduled", "completed", "cancelled"]):
            appointment = Appointment(
                contact_id=contact.id,
                scheduled_at=datetime.now(UTC) + timedelta(days=i + 1),
                status=status,
            )
            test_session.add(appointment)

        await test_session.commit()

        # Query scheduled appointments
        result = await test_session.execute(
            select(Appointment).where(Appointment.status == "scheduled")
        )
        scheduled_appointments = result.scalars().all()

        assert len(scheduled_appointments) == 1
        assert scheduled_appointments[0].status == "scheduled"

    @pytest.mark.asyncio
    async def test_query_appointments_by_date_range(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test querying appointments by date range."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        # Create appointments at different times
        today = datetime.now(UTC)
        dates = [
            today + timedelta(days=1),
            today + timedelta(days=5),
            today + timedelta(days=10),
        ]

        for date in dates:
            appointment = Appointment(
                contact_id=contact.id,
                scheduled_at=date,
            )
            test_session.add(appointment)

        await test_session.commit()

        # Query appointments in next 7 days
        start_date = today
        end_date = today + timedelta(days=7)

        result = await test_session.execute(
            select(Appointment).where(
                Appointment.scheduled_at >= start_date,
                Appointment.scheduled_at <= end_date,
            )
        )
        appointments_in_range = result.scalars().all()

        assert len(appointments_in_range) == 2


class TestAppointmentRelationships:
    """Test Appointment model relationships."""

    @pytest.mark.asyncio
    async def test_appointment_contact_relationship(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Test appointment to contact relationship."""
        user = await create_test_user()
        contact = await create_test_contact(
            user_id=user.id,
            first_name="John",
            last_name="Doe",
        )

        appointment = Appointment(
            contact_id=contact.id,
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )
        test_session.add(appointment)
        await test_session.commit()
        await test_session.refresh(appointment)

        # Access contact through relationship
        assert appointment.contact is not None
        assert appointment.contact.id == contact.id
        assert appointment.contact.first_name == "John"
        assert appointment.contact.last_name == "Doe"
