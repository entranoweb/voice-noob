"""Property-based tests for data persistence.

**Feature: synthiq-voice-platform, Property 1: Data Persistence Round-Trip**
**Validates: Requirements 2.3**

This module tests that data written to PostgreSQL persists correctly
and can be retrieved with identical values.
"""

import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import Base
from app.models.contact import Contact
from app.models.user import User

# Strategy for generating valid email addresses
email_strategy = st.emails()

# Strategy for generating valid names (non-empty, reasonable length)
name_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Zs")),
    min_size=1,
    max_size=100,
).filter(lambda x: x.strip() != "")

# Strategy for generating valid phone numbers
phone_strategy = st.from_regex(r"\+1[0-9]{10}", fullmatch=True)


async def create_test_db_session() -> tuple[AsyncSession, str]:
    """Create a fresh test database session for each hypothesis example."""
    test_db_fd, test_db_path = tempfile.mkstemp(suffix=".db")
    os.close(test_db_fd)
    test_db_url = f"sqlite+aiosqlite:///{test_db_path}"

    engine = create_async_engine(
        test_db_url,
        echo=False,
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    session = async_session()
    return session, test_db_path


async def cleanup_test_db(session: AsyncSession, db_path: str) -> None:
    """Clean up test database after each hypothesis example."""
    await session.close()
    try:
        path = Path(db_path)
        if path.exists():
            path.unlink()
    except Exception:  # noqa: S110
        pass  # Intentionally silent - test cleanup should not fail tests


@pytest.mark.asyncio
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    email=email_strategy,
    full_name=name_strategy,
)
async def test_user_data_persistence_round_trip(
    email: str,
    full_name: str,
) -> None:
    """
    **Feature: synthiq-voice-platform, Property 1: Data Persistence Round-Trip**
    **Validates: Requirements 2.3**

    Property: For any valid user data written to the database,
    querying that data SHALL return identical values.
    """
    session, db_path = await create_test_db_session()

    try:
        # Create user with generated data
        user = User(
            email=email,
            hashed_password="test_hash_password_123",  # noqa: S106
            full_name=full_name.strip(),
            is_active=True,
            is_superuser=False,
        )

        session.add(user)
        await session.commit()
        await session.refresh(user)

        user_id = user.id

        # Clear session cache to force database read
        session.expire_all()

        # Query the user back from database
        result = await session.execute(select(User).where(User.id == user_id))
        retrieved_user = result.scalar_one_or_none()

        # Verify round-trip: data retrieved matches data written
        assert retrieved_user is not None, "User should exist in database"
        assert retrieved_user.email == email, "Email should match"
        assert retrieved_user.full_name == full_name.strip(), "Full name should match"
        assert retrieved_user.is_active is True, "is_active should match"
        assert retrieved_user.is_superuser is False, "is_superuser should match"

    finally:
        await cleanup_test_db(session, db_path)


@pytest.mark.asyncio
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    first_name=name_strategy,
    last_name=name_strategy,
    email=email_strategy,
    phone=phone_strategy,
)
async def test_contact_data_persistence_round_trip(
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
) -> None:
    """
    **Feature: synthiq-voice-platform, Property 1: Data Persistence Round-Trip**
    **Validates: Requirements 2.3**

    Property: For any valid contact data written to the database,
    querying that data SHALL return identical values.
    """
    session, db_path = await create_test_db_session()

    try:
        # Create a user first (contacts require a user_id)
        user = User(
            email=f"owner_{email}",
            hashed_password="test_hash_password_123",  # noqa: S106
            full_name="Test Owner",
            is_active=True,
            is_superuser=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        # Create contact with generated data
        contact = Contact(
            user_id=user.id,
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email,
            phone_number=phone,
            status="new",
        )

        session.add(contact)
        await session.commit()
        await session.refresh(contact)

        contact_id = contact.id

        # Clear session cache to force database read
        session.expire_all()

        # Query the contact back from database
        result = await session.execute(select(Contact).where(Contact.id == contact_id))
        retrieved_contact = result.scalar_one_or_none()

        # Verify round-trip: data retrieved matches data written
        assert retrieved_contact is not None, "Contact should exist in database"
        assert retrieved_contact.first_name == first_name.strip(), "First name should match"
        assert retrieved_contact.last_name == last_name.strip(), "Last name should match"
        assert retrieved_contact.email == email, "Email should match"
        assert retrieved_contact.phone_number == phone, "Phone number should match"
        assert retrieved_contact.status == "new", "Status should match"

    finally:
        await cleanup_test_db(session, db_path)
