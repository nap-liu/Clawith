"""Real PostgreSQL/Alembic checks for the one-time offline ACL conversion."""

import asyncio
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.permissions import get_agent_access_level_for_user_id
from app.models.agent import Agent, AgentPermission
from app.models.tenant import Tenant
from app.models.user import Identity, User
from tests.test_bootstrap_db import empty_database, execute, run_bootstrap  # noqa: F401


def alembic(url, *arguments, success=True):
    return run_bootstrap(
        url, command=[sys.executable, "-m", "alembic", *arguments], success=success,
    )


def snapshot(url):
    return {
        "revision": execute(url, "SELECT version_num FROM alembic_version"),
        "agents": execute(url, "SELECT id, access_mode, company_access_level FROM agents ORDER BY id"),
        "grants": execute(url, "SELECT id, agent_id, scope_type, scope_id, access_level FROM agent_permissions ORDER BY id"),
        "audit": execute(url, "SELECT id, action, details::text FROM audit_logs ORDER BY id"),
    }


async def access_levels(url, user_id, agent_ids):
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as db:
            return [
                await get_agent_access_level_for_user_id(db, user_id, await db.get(Agent, agent_id))
                for agent_id in agent_ids
            ]
    finally:
        await engine.dispose()


def test_offline_conversion_refuses_online_upgrade_and_never_restores_old_rosters(empty_database):  # noqa: F811
    url = empty_database
    # Fresh bootstrap remains automatic; an empty schema has no ACL decisions
    # and can be taken to the parent to construct the previous representation.
    run_bootstrap(url)
    alembic(url, "downgrade", "merge_media_model_runtime")
    tenant_id, owner_id, user_id, replacement_id = (uuid.uuid4() for _ in range(4))
    execute(url, Tenant.__table__.insert().values(id=tenant_id, name="ACL migration", slug="acl-migration"))
    for uid in (owner_id, user_id, replacement_id):
        identity_id = uuid.uuid4()
        execute(url, Identity.__table__.insert().values(id=identity_id, username=str(uid), password_hash="test"))
        execute(url, User.__table__.insert().values(
            id=uid, identity_id=identity_id, tenant_id=tenant_id, display_name=str(uid), role="member",
        ))
    agent_ids = [uuid.uuid4() for _ in range(3)]
    for agent_id, mode in zip(agent_ids, ("company", "custom", "private"), strict=True):
        execute(url, Agent.__table__.insert().values(
            id=agent_id, tenant_id=tenant_id, creator_id=owner_id, name=mode,
            access_mode=mode, company_access_level="use",
        ))
        execute(url, AgentPermission.__table__.insert().values(
            agent_id=agent_id, scope_type="company", access_level="manage",
        ))
        execute(url, AgentPermission.__table__.insert().values(
            agent_id=agent_id, scope_type="user", scope_id=user_id, access_level="manage",
        ))

    before = snapshot(url)
    refused = alembic(url, "upgrade", "heads", success=False)
    assert "approved offline cutover" in refused.stderr
    assert snapshot(url) == before
    # Programmatic startup must fail closed too, rather than bypass the gate.
    refused = run_bootstrap(url, success=False)
    assert "approved offline cutover" in refused.stderr
    assert snapshot(url) == before

    alembic(url, "-x", "agent_permissions_cutover=offline", "upgrade", "heads")
    assert asyncio.run(access_levels(url, user_id, agent_ids)) == ["use", "manage", None]
    migrated = snapshot(url)
    assert len(migrated["audit"]) == 3
    assert len(migrated["grants"]) == 2
    run_bootstrap(url)
    assert snapshot(url) == migrated

    # Reproduce a legacy REST roster replacement with no new writer audit.
    # A downgrade must never put the revoked original user back.
    execute(url, AgentPermission.__table__.delete().where(AgentPermission.agent_id == agent_ids[1]))
    execute(url, AgentPermission.__table__.insert().values(
        agent_id=agent_ids[1], scope_type="user", scope_id=replacement_id, access_level="manage",
    ))
    latest = snapshot(url)
    refused = alembic(url, "downgrade", "merge_media_model_runtime", success=False)
    assert "cannot be downgraded safely" in refused.stderr
    assert snapshot(url) == latest
    assert asyncio.run(access_levels(url, user_id, [agent_ids[1]])) == [None]
    assert asyncio.run(access_levels(url, replacement_id, [agent_ids[1]])) == ["manage"]


async def test_cutover_fails_boundedly_while_an_old_writer_holds_the_agent_table(empty_database):  # noqa: F811
    url = empty_database
    await asyncio.to_thread(run_bootstrap, url)
    await asyncio.to_thread(alembic, url, "downgrade", "merge_media_model_runtime")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(select(Agent.id).with_for_update())
            refused = await asyncio.to_thread(
                alembic, url, "-x", "agent_permissions_cutover=offline", "upgrade", "heads",
                success=False,
            )
            assert "lock timeout" in refused.stderr
            revision = await asyncio.to_thread(execute, url, "SELECT version_num FROM alembic_version")
            assert revision == [("merge_media_model_runtime",)]
    finally:
        await engine.dispose()
