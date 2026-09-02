from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.participant import Participant
from app.models.project import Project, ProjectMemberSnapshot
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services import project_agent_template_service as template_service
from app.services.project_agent_template_service import (
    export_project_agents_for_template,
    instantiate_project_agents_from_template,
)
from app.services.project_agent_workspace import create_project_agent_workspace, project_agent_workspace

TABLES = [
    "llm_models",
    "identities",
    "tenants",
    "users",
    "agent_templates",
    "project_templates",
    "projects",
    "agents",
    "participants",
    "project_member_snapshots",
    "project_events",
]


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Base.metadata.tables[name] for name in TABLES],
            )
        )
        await connection.execute(text("PRAGMA ignore_check_constraints = ON"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _project_context(db, *, name: str) -> tuple[User, Project, LLMModel]:
    tenant = Tenant(name=name, slug=f"template-{uuid.uuid4().hex[:10]}")
    db.add(tenant)
    await db.flush()
    model = LLMModel(
        tenant_id=tenant.id,
        provider="openai",
        model="project-default",
        api_key_encrypted="test",
        label="Project default",
        enabled=True,
    )
    db.add(model)
    await db.flush()
    tenant.default_model_id = model.id
    identity = Identity(username=f"owner-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@local.test")
    db.add(identity)
    await db.flush()
    owner = User(
        identity_id=identity.id,
        tenant_id=tenant.id,
        display_name="Owner",
        role="platform_admin",
        is_active=True,
    )
    db.add(owner)
    await db.flush()
    project = Project(
        tenant_id=tenant.id,
        owner_user_id=owner.id,
        name=name,
        description="",
        goal="",
        success_criteria=[],
        visibility="private",
        status="planning",
        settings={"git": {"mode": "managed"}},
    )
    db.add(project)
    await db.flush()
    return owner, project, model


async def test_project_template_round_trip_creates_fresh_project_agents(
    db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    owner, source_project, _model = await _project_context(db, name="Source")
    target_project = Project(
        tenant_id=source_project.tenant_id,
        owner_user_id=owner.id,
        name="Target",
        description="",
        goal="",
        success_criteria=[],
        visibility="private",
        status="planning",
        settings={"git": {"mode": "managed"}},
    )
    db.add(target_project)
    await db.flush()
    roots = {source_project.id: tmp_path / "source", target_project.id: tmp_path / "target"}
    for root in roots.values():
        root.mkdir()
    monkeypatch.setattr(template_service, "_project_root", lambda project: roots[project.id])

    async def fake_commit(_project, _message, _paths, **_kwargs):
        return {"commit": "b" * 40}

    monkeypatch.setattr(template_service, "commit_project_changes", fake_commit)
    source_agent_id = uuid.uuid4()
    source_agent = Agent(
        id=source_agent_id,
        name="Project Researcher",
        role_description="Research",
        creator_id=owner.id,
        tenant_id=source_project.tenant_id,
        scope="project",
        project_id=source_project.id,
        agent_dir=Agent.project_agent_dir(source_agent_id),
        agent_type="native",
        status="idle",
        access_mode="private",
    )
    source_agent.daily_memory_load_days = 0
    db.add(source_agent)
    await db.flush()
    db.add(Participant(type="agent", ref_id=source_agent.id, display_name=source_agent.name))
    db.add(
        ProjectMemberSnapshot(
            tenant_id=source_project.tenant_id,
            project_id=source_project.id,
            agent_id=source_agent.id,
            name_snapshot=source_agent.name,
            role_snapshot=source_agent.role_description,
            is_leader=True,
            config_snapshot={"temperature": 0},
        )
    )
    await db.flush()
    await create_project_agent_workspace(roots[source_project.id], source_agent.id)
    source_layout = project_agent_workspace(roots[source_project.id], source_agent.id)
    source_layout.soul.write_text(f"owner of {source_project.id}\n", encoding="utf-8")
    source_layout.memory.write_text("durable product context\n", encoding="utf-8")
    (source_layout.workspace / "brief.md").write_text("safe brief\n", encoding="utf-8")

    definition_agents = await export_project_agents_for_template(db, source_project)
    assert definition_agents[0]["runtime"]["daily_memory_load_days"] == 0
    assert definition_agents[0]["member_config"]["temperature"] == 0
    assert str(source_project.id) not in str(definition_agents)
    created = await instantiate_project_agents_from_template(db, target_project, owner, definition_agents)

    assert len(created) == 1
    created_agent, member = created[0]
    assert created_agent.id != source_agent.id
    assert created_agent.scope == "project"
    assert created_agent.project_id == target_project.id
    assert created_agent.primary_model_id is not None
    assert created_agent.daily_memory_load_days == 0
    assert member.is_leader is True
    assert member.config_snapshot["temperature"] == 0
    target_layout = project_agent_workspace(roots[target_project.id], created_agent.id)
    assert target_layout.memory.read_text(encoding="utf-8") == "durable product context\n"
    assert (target_layout.workspace / "brief.md").read_text(encoding="utf-8") == "safe brief\n"
