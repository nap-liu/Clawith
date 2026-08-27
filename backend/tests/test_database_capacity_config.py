"""Database pool configuration coverage for supported test/runtime dialects."""

from app.config import Settings
from app.database import database_engine_options


def test_database_pool_defaults_remain_conservative() -> None:
    assert Settings.model_fields["DATABASE_POOL_SIZE"].default == 20
    assert Settings.model_fields["DATABASE_MAX_OVERFLOW"].default == 10

    settings = Settings.model_construct(
        DATABASE_URL="postgresql+asyncpg://user:pass@db.example/test_clawith",
        DEBUG=False,
    )
    options = database_engine_options(settings)

    assert options["pool_size"] == 20
    assert options["max_overflow"] == 10


def test_postgres_engine_options_use_configured_pool_budget() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://user:pass@db.example/test_clawith",
        DATABASE_POOL_SIZE=41,
        DATABASE_MAX_OVERFLOW=17,
        DATABASE_POOL_TIMEOUT_SECONDS=7.5,
        DATABASE_POOL_RECYCLE_SECONDS=900,
        DATABASE_POOL_PRE_PING=False,
        DATABASE_POOL_USE_LIFO=False,
    )

    options = database_engine_options(settings)

    assert options == {
        "echo": False,
        "pool_pre_ping": False,
        "pool_recycle": 900,
        "pool_size": 41,
        "max_overflow": 17,
        "pool_timeout": 7.5,
        "pool_use_lifo": False,
    }


def test_sqlite_engine_options_omit_queue_pool_only_arguments() -> None:
    settings = Settings(DATABASE_URL="sqlite+aiosqlite:///:memory:")

    options = database_engine_options(settings)

    assert options == {
        "echo": False,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
