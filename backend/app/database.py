"""Database connection and session management."""

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings, get_settings

settings = get_settings()


def database_engine_options(config: Settings) -> dict[str, Any]:
    """Build dialect-safe engine and queue-pool options from settings."""

    options: dict[str, Any] = {
        "echo": config.DEBUG,
        "pool_pre_ping": config.DATABASE_POOL_PRE_PING,
        "pool_recycle": config.DATABASE_POOL_RECYCLE_SECONDS,
    }
    if make_url(config.DATABASE_URL).get_backend_name() != "sqlite":
        options.update(
            pool_size=config.DATABASE_POOL_SIZE,
            max_overflow=config.DATABASE_MAX_OVERFLOW,
            pool_timeout=config.DATABASE_POOL_TIMEOUT_SECONDS,
            pool_use_lifo=config.DATABASE_POOL_USE_LIFO,
        )
    return options


engine = create_async_engine(settings.DATABASE_URL, **database_engine_options(settings))

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
