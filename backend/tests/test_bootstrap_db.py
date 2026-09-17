"""Exercise the real database entry points on disposable PostgreSQL databases."""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.tenant import Tenant
from app.models.user import Identity, User

BACKEND = Path(__file__).resolve().parents[1]
INDEX_NAMES = (
    "uq_identities_email_lower_not_null",
    "uq_identities_phone_normalized_not_null",
    "uq_channel_configs_dingtalk_app_configured",
    "ix_chat_messages_subagent_dispatch_pending",
    "ix_chat_messages_leader_dispatch_pending",
    "ix_chat_messages_turn_inbox_pending_fifo",
)
pytestmark = pytest.mark.bootstrap


async def _execute(url, sql):
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql) if isinstance(sql, str) else sql)
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


def execute(url, sql):
    return asyncio.run(_execute(url, sql))


@pytest.fixture
def empty_database():
    admin_url = make_url(os.environ["DATABASE_URL"])
    name = f"test_bootstrap_{uuid.uuid4().hex}"
    execute(admin_url, f'CREATE DATABASE "{name}"')
    try:
        yield admin_url.set(database=name).render_as_string(hide_password=False)
    finally:
        execute(admin_url, f'DROP DATABASE "{name}" WITH (FORCE)')


def _command(kind):
    if kind == "entrypoint":
        return ["/bin/bash", str(BACKEND / "entrypoint.sh")]
    if kind == "alembic":
        return [sys.executable, "-m", "alembic", "upgrade", "heads"]
    return [sys.executable, "-m", "app.scripts.bootstrap_db"]


def _environment(url):
    return {
        **os.environ,
        "DATABASE_URL": url,
        "PROCESS_ROLE": "bootstrap",
        "START_COMMAND": "true",
        "AGENT_DATA_DIR": "/tmp/bootstrap-test-agents",
    }


def run_bootstrap(url, kind="module", command=None, success=True):
    result = subprocess.run(
        command or _command(kind), cwd=BACKEND, env=_environment(url),
        capture_output=True, text=True, timeout=60, check=False,
    )
    if success:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
    return result


def assert_schema_ready(url):
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    heads = set(ScriptDirectory.from_config(config).get_heads())
    assert {row[0] for row in execute(url, "SELECT version_num FROM alembic_version")} == heads
    tables = {row[0] for row in execute(url, "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
    assert {
        "published_pages", "published_page_access", "published_page_visitors",
        "published_page_anonymous_visitors", "cli_tool_binary_versions",
        "channel_type_defaults", "speech_recognition_configs", "workspace_edit_locks",
        "workspace_file_revisions", "tools", "tenants", "projects",
        "openapi_applications", "openapi_user_bindings", "openapi_credentials",
    } <= tables
    for name in INDEX_NAMES:
        assert execute(url, f"SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('{name}')") == [(True,)]
    assert execute(url, "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")[0][0] == 9


@pytest.mark.parametrize("kind", ["module", "entrypoint", "alembic"])
def test_fresh_entry_points_and_repeat_preserve_data(empty_database, kind):
    run_bootstrap(empty_database, kind)
    assert_schema_ready(empty_database)
    execute(empty_database, Tenant.__table__.insert().values(id=uuid.uuid4(), name="Kept", slug="kept"))
    # Also exercise standalone -> actual entrypoint, the previously broken pair.
    run_bootstrap(empty_database, "entrypoint")
    assert execute(empty_database, "SELECT name FROM tenants WHERE slug='kept'") == [("Kept",)]
    assert_schema_ready(empty_database)


def test_unversioned_database_is_not_stamped_or_modified(empty_database):
    execute(empty_database, "CREATE TABLE legacy_data (value text)")
    execute(empty_database, "INSERT INTO legacy_data VALUES ('keep')")
    result = run_bootstrap(empty_database, success=False)
    assert "tables but no Alembic revision" in result.stderr
    assert execute(empty_database, "SELECT * FROM legacy_data") == [("keep",)]
    assert execute(empty_database, "SELECT to_regclass('alembic_version')") == [(None,)]


def test_failed_fresh_setup_rolls_back_before_retry(empty_database):
    failure = (
        "from app.core import database_guards\n"
        "def fail(connection):\n"
        "    raise RuntimeError('injected guard failure')\n"
        "database_guards.ensure_current_database_guards = fail\n"
        "from app.scripts.bootstrap_db import main\n"
        "main()\n"
    )
    result = run_bootstrap(empty_database, command=[sys.executable, "-c", failure], success=False)
    assert "injected guard failure" in result.stderr
    assert execute(empty_database, "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()") == []
    run_bootstrap(empty_database)
    assert_schema_ready(empty_database)


def test_concurrent_bootstrap_serializes_schema_changes(empty_database):
    processes = [
        subprocess.Popen(
            _command(kind), cwd=BACKEND, env=_environment(empty_database),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for kind in ("module", "alembic")
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert_schema_ready(empty_database)


def _restore_previous_bootstrap_gap(url):
    run_bootstrap(url, command=[
        sys.executable, "-m", "alembic", "downgrade", "company_mcp_group_backfill",
    ])
    # The immediately preceding bootstrap has the same tables, but omits these
    # six indexes. Recreate exactly that known defect, not an arbitrary old schema.
    for name in INDEX_NAMES:
        execute(url, f'DROP INDEX "{name}"')


@pytest.mark.parametrize("revision", ["openapi_applications_v1", "repair_bootstrap_indexes"])
def test_upgrade_each_integration_parent_preserves_data(empty_database, revision):
    run_bootstrap(empty_database)
    run_bootstrap(empty_database, command=[
        sys.executable, "-m", "alembic", "downgrade", "company_mcp_group_backfill",
    ])
    run_bootstrap(empty_database, command=[
        sys.executable, "-m", "alembic", "upgrade", revision,
    ])
    execute(empty_database, Tenant.__table__.insert().values(
        id=uuid.uuid4(), name="Integration parent", slug="integration-parent",
    ))
    run_bootstrap(empty_database)
    assert_schema_ready(empty_database)
    assert execute(empty_database, "SELECT name FROM tenants WHERE slug='integration-parent'") == [
        ("Integration parent",),
    ]


def test_repairs_previous_stamped_installation(empty_database):
    run_bootstrap(empty_database)
    _restore_previous_bootstrap_gap(empty_database)
    run_bootstrap(empty_database)
    assert_schema_ready(empty_database)


def test_repair_rejects_conflicts_and_recovers_invalid_index(empty_database):
    run_bootstrap(empty_database)
    _restore_previous_bootstrap_gap(empty_database)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    execute(empty_database, (
        "INSERT INTO identities (id,email,is_active,is_platform_admin,email_verified) VALUES "
        f"('{first}','Audit@example.test',true,false,false),"
        f"('{second}',' audit@example.test ',true,false,false)"
    ))
    run_bootstrap(empty_database, success=False)
    assert execute(empty_database, "SELECT count(*) FROM identities") == [(2,)]
    assert execute(empty_database, "SELECT version_num FROM alembic_version") == [("company_mcp_group_backfill",)]
    # Explicitly correct the disposable fixture, then prove retry repairs the
    # invalid index left by CREATE UNIQUE INDEX CONCURRENTLY's failed build.
    execute(empty_database, f"UPDATE identities SET email='separate@example.test' WHERE id='{second}'")
    run_bootstrap(empty_database)
    assert_schema_ready(empty_database)
    assert execute(empty_database, "SELECT count(*) FROM identities") == [(2,)]


@pytest.mark.asyncio
async def test_fresh_database_rejects_normalized_identity_and_channel_duplicates(empty_database):
    run_bootstrap(empty_database)
    engine = create_async_engine(empty_database)
    try:
        async with AsyncSession(engine) as db:
            db.add(Identity(email="Audit@example.test", phone="+86 138-0000-0000"))
            await db.flush()
            for duplicate in (
                Identity(email=" audit@example.test "),
                Identity(phone="8613800000000"),
            ):
                with pytest.raises(IntegrityError):
                    async with db.begin_nested():
                        db.add(duplicate)
                        await db.flush()
            tenant = Tenant(name="Bootstrap", slug=f"bootstrap-{uuid.uuid4().hex}")
            db.add(tenant)
            await db.flush()
            owner = User(tenant_id=tenant.id, display_name="Owner")
            db.add(owner)
            await db.flush()
            agents = [Agent(name=f"Agent {n}", tenant_id=tenant.id, creator_id=owner.id) for n in range(2)]
            db.add_all(agents)
            await db.flush()
            db.add(ChannelConfig(agent_id=agents[0].id, channel_type="dingtalk", app_id="same-app", is_configured=True))
            await db.flush()
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    db.add(ChannelConfig(agent_id=agents[1].id, channel_type="dingtalk", app_id="same-app", is_configured=True))
                    await db.flush()
            # An unconfigured second entry is explicitly allowed by the predicate.
            db.add(ChannelConfig(agent_id=agents[1].id, channel_type="dingtalk", app_id="same-app", is_configured=False))
            await db.flush()
    finally:
        await engine.dispose()
