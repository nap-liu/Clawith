"""PostgreSQL invariants for project-native Agent derivatives."""

import uuid

import pytest
from sqlalchemy import delete, null, select, text, update
from sqlalchemy.exc import IntegrityError

from app.database import async_session, engine
from app.models import registry  # noqa: F401
from app.models.agent import Agent
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.user import User


async def test_project_agent_scope_defaults_constraints_and_lifecycle():
    await engine.dispose()
    try:
        async with async_session() as db:
            tenant = Tenant(name="Project model", slug=f"project-model-{uuid.uuid4().hex}")
            db.add(tenant)
            await db.flush()
            owner = User(tenant_id=tenant.id, display_name="Owner")
            db.add(owner)
            await db.flush()
            project = Project(tenant_id=tenant.id, owner_user_id=owner.id, name="Project")
            source = Agent(tenant_id=tenant.id, creator_id=owner.id, name="Source")
            db.add_all([project, source])
            await db.flush()
            assert (source.scope, source.project_id, source.source_agent_id, source.agent_dir) == (
                "standard", None, None, None,
            )
            server_scope = await db.scalar(update(Agent).where(Agent.id == source.id).values(
                scope=text("DEFAULT"),
            ).returning(Agent.scope))
            assert server_scope == "standard"

            for fields in (
                {"scope": null()},
                {"scope": "invalid"},
                {"scope": "standard", "project_id": project.id},
                {"scope": "project", "agent_dir": ".agents/missing-project"},
                {"scope": "project", "project_id": project.id},
            ):
                with pytest.raises(IntegrityError):
                    async with db.begin_nested():
                        db.add(Agent(tenant_id=tenant.id, creator_id=owner.id, name="Invalid", **fields))
                        await db.flush()

            derivative = Agent(
                tenant_id=tenant.id, creator_id=owner.id, name="Derivative", scope="project",
                project_id=project.id, source_agent_id=source.id, agent_dir=".agents/derivative",
            )
            db.add(derivative)
            await db.flush()
            await db.execute(delete(Agent).where(Agent.id == source.id))
            await db.refresh(derivative)
            assert derivative.source_agent_id is None
            await db.execute(delete(Project).where(Project.id == project.id))
            assert await db.scalar(select(Agent.id).where(Agent.id == derivative.id)) is None
    finally:
        await engine.dispose()


def test_project_agent_directory_is_derived_from_agent_id():
    agent_id = uuid.uuid4()
    assert Agent.project_agent_dir(agent_id) == f".agents/{agent_id}"
