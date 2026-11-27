"""Database session management with async SQLAlchemy."""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.log_redaction import describe_exception

logger = logging.getLogger(__name__)

# Create async engine
engine = create_async_engine(
    str(settings.DATABASE_URL),
    echo=False,  # Disable SQL query logging (too verbose even in debug mode)
    future=True,
    pool_pre_ping=True,
    # Per worker process; see DB_POOL_SIZE in app/core/config.py for the budget.
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=900,  # Recycle every 15 min for better connection health
    pool_timeout=5,  # Fail fast if pool exhausted (prevents cascading timeouts)
    pool_use_lifo=True,  # Use LIFO for better connection reuse (keeps hot connections)
)

# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as exc:
            await session.rollback()
            # A traceback would carry the statement's bound parameters.
            logger.error("Database session error: %s", describe_exception(exc))  # noqa: TRY400
            raise
        finally:
            await session.close()
