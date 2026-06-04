import uuid
import pytest
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User, Identity

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_agent_has_webhook_queue_max_default_1000():
    async with async_session() as db:
        tenant = Tenant(name="T", slug=f"t_{uuid.uuid4().hex[:6]}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant.id)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native", tenant_id=tenant.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        assert agent.webhook_queue_max == 1000
