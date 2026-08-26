"""Tests for Contact model."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact


class TestContactModel:
    """Test Contact model creation and validation."""

    @pytest.mark.asyncio
    async def test_create_contact_success(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        sample_contact_data: dict[str, Any],
    ) -> None:
        """Test creating a contact with all fields."""
        user = await create_test_user()

        contact = Contact(user_id=user.id, **sample_contact_data)
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)

        assert contact.id is not None
        assert contact.user_id == user.id
        assert contact.first_name == sample_contact_data["first_name"]
        assert contact.last_name == sample_contact_data["last_name"]
        assert contact.email == sample_contact_data["email"]
        assert contact.phone_number == sample_contact_data["phone_number"]
        assert contact.company_name == sample_contact_data["company_name"]
        assert contact.status == sample_contact_data["status"]
        assert contact.tags == sample_contact_data["tags"]
        assert contact.notes == sample_contact_data["notes"]
        assert contact.created_at is not None
        assert contact.updated_at is not None

    @pytest.mark.asyncio
    async def test_create_contact_minimal_fields(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test creating a contact with minimal required fields."""
        user = await create_test_user()

        contact = Contact(
            user_id=user.id,
            first_name="Jane",
            phone_number="+9876543210",
        )
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)

        assert contact.id is not None
        assert contact.first_name == "Jane"
        assert contact.phone_number == "+9876543210"
        assert contact.last_name is None
        assert contact.email is None
        assert contact.company_name is None
        assert contact.status == "new"  # Default value
        assert contact.tags is None
        assert contact.notes is None

    @pytest.mark.asyncio
    async def test_contact_foreign_key_constraint(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test that contact requires valid user_id."""
        contact = Contact(
            user_id=99999,  # Non-existent user
            first_name="Test",
            phone_number="+1234567890",
        )
        test_session.add(contact)

        with pytest.raises(IntegrityError):
            await test_session.commit()

    @pytest.mark.asyncio
    async def test_contact_default_status(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test that contact status defaults to 'new'."""
        user = await create_test_user()

        contact = Contact(
            user_id=user.id,
            first_name="Default",
            phone_number="+1111111111",
        )
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)

        assert contact.status == "new"

    @pytest.mark.asyncio
    async def test_contact_status_values(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test different contact status values."""
        user = await create_test_user()

        statuses = ["new", "contacted", "qualified", "converted", "lost"]

        for status in statuses:
            contact = Contact(
                user_id=user.id,
                first_name=f"Contact_{status}",
                phone_number=f"+{statuses.index(status)}000000000",
                status=status,
            )
            test_session.add(contact)

        await test_session.commit()

        # Verify all contacts were created with correct status
        result = await test_session.execute(select(Contact))
        contacts = result.scalars().all()
        contact_statuses = {c.status for c in contacts}

        assert contact_statuses == set(statuses)

    @pytest.mark.asyncio
    async def test_contact_repr(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test contact string representation."""
        user = await create_test_user()

        contact = Contact(
            user_id=user.id,
            first_name="John",
            last_name="Doe",
            phone_number="+1234567890",
        )
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)

        repr_str = repr(contact)
        assert "John" in repr_str
        assert "Doe" in repr_str
        assert "+1234567890" in repr_str

    @pytest.mark.asyncio
    async def test_contact_with_tags(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test contact with tags."""
        user = await create_test_user()

        contact = Contact(
            user_id=user.id,
            first_name="Tagged",
            phone_number="+1111111111",
            tags="lead,important,vip",
        )
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)

        assert contact.tags == "lead,important,vip"

    @pytest.mark.asyncio
    async def test_contact_with_notes(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test contact with long notes."""
        user = await create_test_user()

        long_notes = "This is a very long note. " * 100

        contact = Contact(
            user_id=user.id,
            first_name="Noted",
            phone_number="+2222222222",
            notes=long_notes,
        )
        test_session.add(contact)
        await test_session.commit()
        await test_session.refresh(contact)

        assert contact.notes == long_notes

    @pytest.mark.asyncio
    async def test_query_contacts_by_user(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test querying contacts by user_id."""
        user1 = await create_test_user(email="user1@example.com")
        user2 = await create_test_user(email="user2@example.com")

        # Create contacts for user1
        for i in range(3):
            contact = Contact(
                user_id=user1.id,
                first_name=f"User1_Contact{i}",
                phone_number=f"+100000000{i}",
            )
            test_session.add(contact)

        # Create contacts for user2
        for i in range(2):
            contact = Contact(
                user_id=user2.id,
                first_name=f"User2_Contact{i}",
                phone_number=f"+200000000{i}",
            )
            test_session.add(contact)

        await test_session.commit()

        # Query user1's contacts
        result = await test_session.execute(select(Contact).where(Contact.user_id == user1.id))
        user1_contacts = result.scalars().all()

        assert len(user1_contacts) == 3

    @pytest.mark.asyncio
    async def test_query_contacts_by_status(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test querying contacts by status."""
        user = await create_test_user()

        # Create contacts with different statuses
        for status in ["new", "contacted", "qualified"]:
            contact = Contact(
                user_id=user.id,
                first_name=f"Contact_{status}",
                phone_number=f"+{['new', 'contacted', 'qualified'].index(status)}00000000",
                status=status,
            )
            test_session.add(contact)

        await test_session.commit()

        # Query new contacts
        result = await test_session.execute(select(Contact).where(Contact.status == "new"))
        new_contacts = result.scalars().all()

        assert len(new_contacts) == 1
        assert new_contacts[0].status == "new"


class TestContactRelationships:
    """Test Contact model relationships."""

    @pytest.mark.asyncio
    async def test_contact_appointments_relationship(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        create_test_appointment: Any,
    ) -> None:
        """Test contact to appointments relationship."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        # Create appointments for contact
        appointment1 = await create_test_appointment(contact_id=contact.id)
        appointment2 = await create_test_appointment(contact_id=contact.id)

        # Relationships are lazy: refresh() alone reloads columns but not
        # collections, so the first access would attempt lazy IO and raise
        # MissingGreenlet under asyncio. Name the relationship to load it here.
        await test_session.refresh(contact, attribute_names=["appointments"])

        # Access appointments through relationship
        assert len(contact.appointments) == 2
        appointment_ids = {a.id for a in contact.appointments}
        assert appointment1.id in appointment_ids
        assert appointment2.id in appointment_ids

    @pytest.mark.asyncio
    async def test_contact_call_interactions_relationship(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
        create_test_call_interaction: Any,
    ) -> None:
        """Test contact to call interactions relationship."""
        user = await create_test_user()
        contact = await create_test_contact(user_id=user.id)

        # Create call interactions for contact
        call1 = await create_test_call_interaction(contact_id=contact.id)
        call2 = await create_test_call_interaction(contact_id=contact.id)

        # Relationships are lazy: refresh() alone reloads columns but not
        # collections, so the first access would attempt lazy IO and raise
        # MissingGreenlet under asyncio. Name the relationship to load it here.
        await test_session.refresh(contact, attribute_names=["call_interactions"])

        # Access call interactions through relationship
        assert len(contact.call_interactions) == 2
        call_ids = {c.id for c in contact.call_interactions}
        assert call1.id in call_ids
        assert call2.id in call_ids
