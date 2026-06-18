"""provision_agent service: native creation side-effects + scope validation.

Covers the B1 extraction of the agent-creation flow out of the REST route
``create_agent`` into ``app.services.agent_provisioning.provision_agent``.

Runs against the real Postgres ``clawith_test`` DB (no sqlite). In the test
container Docker is unavailable, so ``agent_manager.start_container`` is a safe
no-op; the native no-template path exercised here is clean.
"""

from __future__ import annotations
import uuid
import pytest
from sqlalchemy import select
from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None) -> User:
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


async def test_provision_agent_native_side_effects():
    from app.services.agent_provisioning import AgentProvisionInput, provision_agent
    from app.models.participant import Participant
    creator = await _seed_user(tenant_id=(await _seed_tenant()).id)
    async with async_session() as db:
        # reload creator in this session
        creator = (await db.execute(select(User).where(User.id == creator.id))).scalar_one()
        agent, raw_key = await provision_agent(
            db, creator=creator, tenant_id=creator.tenant_id,
            data=AgentProvisionInput(name="Scout", role_description="researcher"),
        )
        agent_id = agent.id
        assert raw_key is None
        assert agent.creator_id == creator.id
        assert agent.tenant_id == creator.tenant_id
        assert agent.access_mode == "company"
    async with async_session() as db:
        p = (await db.execute(select(Participant).where(
            Participant.type == "agent", Participant.ref_id == agent_id))).scalar_one_or_none()
        assert p is not None and p.display_name == "Scout"


async def test_provision_agent_rejects_bad_scope_type():
    from app.services.agent_provisioning import AgentProvisionInput, provision_agent
    creator = await _seed_user(tenant_id=(await _seed_tenant()).id)
    async with async_session() as db:
        creator = (await db.execute(select(User).where(User.id == creator.id))).scalar_one()
        with pytest.raises(ValueError):
            await provision_agent(db, creator=creator, tenant_id=creator.tenant_id,
                                  data=AgentProvisionInput(name="X", permission_scope_type="bogus"))
