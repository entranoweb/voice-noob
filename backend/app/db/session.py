"""Database session management with async SQLAlchemy."""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

logger = logging.getLogger(__name__)

# Create async engine
engine = create_async_engine(
    str(settings.DATABASE_URL),
    echo=False,  # Disable SQL query logging (too verbose even in debug mode)
    future=True,
    pool_pre_ping=True,
    pool_size=50,  # Production: increased for 100+ concurrent voice agents
    max_overflow=50,  # Production: total max 100 connections for scalability
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
        except Exception:
            await session.rollback()
            logger.exception("Database session error")
            raise
        finally:
            await session.close()
