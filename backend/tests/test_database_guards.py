"""PostgreSQL behavior checks for create_all bootstrap database guards."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database_guards import (
    CURRENT_DATABASE_GUARDS,
    ensure_current_database_guards,
)
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.org import AgentRelationship
from app.models.tenant import Tenant
from app.models.user import User


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_current_guards_exist_and_reject_cross_tenant_relationship():
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT trigger_name FROM information_schema.triggers "
                "WHERE trigger_schema = current_schema()"
            )
        )
        installed = set(rows.scalars())

    expected = {guard.trigger for guard in CURRENT_DATABASE_GUARDS}
    assert expected <= installed

    # The repair step is safe to run again on every bootstrap role restart.
    async with engine.begin() as connection:
        await connection.run_sync(ensure_current_database_guards)

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        first_tenant = Tenant(name="Guard A", slug=f"guard-a-{suffix}")
        second_tenant = Tenant(name="Guard B", slug=f"guard-b-{suffix}")
        db.add_all([first_tenant, second_tenant])
        await db.flush()
        owner = User(
            tenant_id=first_tenant.id,
            display_name="Owner",
            role="member",
            is_active=True,
        )
        other_tenant_user = User(
            tenant_id=second_tenant.id,
            display_name="Other Tenant",
            role="member",
            is_active=True,
        )
        db.add_all([owner, other_tenant_user])
        await db.flush()
        agent = Agent(
            tenant_id=first_tenant.id,
            creator_id=owner.id,
            name="Guarded Agent",
            status="idle",
        )
        db.add(agent)
        await db.commit()

        with pytest.raises(DBAPIError, match="agent_relationship tenant mismatch"):
            async with db.begin_nested():
                db.add(
                    AgentRelationship(
                        agent_id=agent.id,
                        user_id=other_tenant_user.id,
                    )
                )
                await db.flush()
