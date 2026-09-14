"""Alembic environment configuration for async SQLAlchemy."""

import asyncio
from logging.config import fileConfig

from alembic import command, context
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.core.database_guards import ensure_current_database_guards
from app.database import Base
from app.models import registry  # noqa: F401

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _is_head_upgrade() -> bool:
    # CLI upgrades and our standalone command opt into current-schema bootstrap.
    # Keep historical targets, downgrade, stamp and autogenerate unchanged.
    cli_command = getattr(config.cmd_opts, "cmd", (None,))[0]
    return config.attributes.get("bootstrap_current_schema", False) or (
        cli_command is command.upgrade
        and getattr(config.cmd_opts, "revision", None) in {"head", "heads"}
    )


def do_run_migrations(connection):
    bootstrap = _is_head_upgrade()
    if bootstrap:
        # Session lock survives Alembic's concurrent-index autocommit blocks.
        connection.execute(text("SELECT pg_advisory_lock(724019381)"))
        connection.commit()
    try:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            if bootstrap:
                tables = set(inspect(connection).get_table_names()) - {"alembic_version"}
                versions = context.get_context().get_current_heads()
                if tables and not versions:
                    raise RuntimeError(
                        "Database has tables but no Alembic revision; refusing to replay "
                        "historical migrations or stamp unknown data. Use a verified "
                        "legacy migration baseline, or a new empty database."
                    )
                if not tables and not versions:
                    # Create and stamp atomically; current models already include
                    # historical columns, so replaying their ADD statements is wrong.
                    Base.metadata.create_all(connection)
                    ensure_current_database_guards(connection)
                    context.get_context().stamp(ScriptDirectory.from_config(config), "heads")
                    return
            context.run_migrations()
            if bootstrap:
                ensure_current_database_guards(connection)
    finally:
        if bootstrap:
            connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(724019381)"))
            connection.commit()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # asyncpg does not consume libpq PGOPTIONS. Bound both bootstrap locks
        # and migration DDL on this connection, before acquiring advisory locks.
        connect_args={"server_settings": {"lock_timeout": "5s", "statement_timeout": "60s"}},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
