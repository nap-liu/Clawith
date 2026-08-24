from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import projects as projects_api
from app.database import Base
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.project import ProjectAgentCreate, ProjectAgentUpdate, ProjectMemberCreate
from app.services import agent_context, agent_memory, project_agent_service
from app.services.agent_context import build_agent_context
from app.services.agent_runtime_workspace import AgentRuntimeWorkspace, bind_agent_runtime_workspace
from app.services.project_agent_service import (
    create_project_agent,
    deactivate_project_agent,
    get_project_agent,
    list_project_agents,
    promote_project_agent,
    restore_project_agent,
    serialize_project_agent,
    update_project_agent,
)
from app.services.project_service import add_member
from app.services.storage import LocalStorageBackend, normalize_storage_key

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


async def _owner_and_project(db) -> tuple[User, Project]:
    tenant = Tenant(name="Project Agent", slug=f"project-agent-{uuid.uuid4().hex[:8]}")
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
        name="Dedicated Agents",
        description="",
        goal="",
        success_criteria=[],
        visibility="private",
        status="planning",
        settings={"git": {"mode": "managed"}},
    )
    db.add(project)
    await db.flush()
    return owner, project


async def _project_in_same_tenant(db, owner: User, *, name: str) -> Project:
    project = Project(
        tenant_id=owner.tenant_id,
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
    return project


async def _user_in_same_tenant(db, owner: User, *, display_name: str) -> User:
    identity = Identity(
        username=f"member-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex}@local.test",
    )
    db.add(identity)
    await db.flush()
    user = User(
        identity_id=identity.id,
        tenant_id=owner.tenant_id,
        display_name=display_name,
        role="member",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


@pytest.fixture
def project_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(project_agent_service, "_project_root", lambda _project: repo)

    async def fake_commit(_project, message, paths, **_kwargs):
        return {"commit": "a" * 40, "message": message, "paths": paths}

    monkeypatch.setattr(project_agent_service, "commit_project_changes", fake_commit)
    return repo


async def test_project_agent_source_error_uses_formal_digital_employee_copy(db):
    owner, project = await _owner_and_project(db)

    with pytest.raises(HTTPException) as error:
        await create_project_agent(
            db,
            project,
            owner,
            ProjectAgentCreate(
                source_agent_id=uuid.uuid4(),
                name="Unavailable source",
            ),
        )

    assert error.value.status_code == 422
    assert error.value.detail == "源数字员工不可用"


async def test_project_agent_blank_copy_update_and_read_are_project_owned(
    db,
    project_repo,
    monkeypatch: pytest.MonkeyPatch,
):
    owner, project = await _owner_and_project(db)
    source = Agent(
        name="Shared Architect",
        role_description="Architecture",
        creator_id=owner.id,
        tenant_id=project.tenant_id,
        scope="standard",
        status="idle",
        access_mode="company",
        agent_type="native",
    )
    db.add(source)
    await db.flush()

    async def seed_from_source(
        project_root,
        agent_id,
        *,
        source_agent_id=None,
        overwrite=False,
        default_soul="",
        default_memory="",
    ):
        del overwrite, default_soul, default_memory
        layout = project_agent_service.project_agent_workspace(project_root, agent_id)
        layout.workspace.mkdir(parents=True)
        layout.soul.write_text(f"source:{source_agent_id}\n", encoding="utf-8")
        layout.memory.write_text("source memory\n", encoding="utf-8")

    monkeypatch.setattr(project_agent_service, "create_project_agent_workspace", seed_from_source)
    agent, member = await create_project_agent(
        db,
        project,
        owner,
        ProjectAgentCreate(
            source_agent_id=source.id,
            name="Project Architect",
            soul="# Fixed soul\n",
            core_memory="# Fixed memory\n",
        ),
    )

    assert agent.scope == "project"
    assert agent.project_id == project.id
    assert agent.source_agent_id == source.id
    assert agent.agent_dir == f".agents/{agent.id}"
    assert member.agent_id == agent.id
    assert member.is_enabled is True
    assert (project_repo / agent.agent_dir / "soul.md").read_text() == "# Fixed soul\n"
    assert (project_repo / agent.agent_dir / "memory.md").read_text() == "# Fixed memory\n"
    assert (project_repo / agent.agent_dir / "workspace").is_dir()

    await update_project_agent(
        db,
        project,
        owner,
        agent,
        member,
        ProjectAgentUpdate(
            name="Project Principal",
            role_description="Own project architecture",
            soul="# Owner controlled\n",
            core_memory="# Core context\n",
        ),
    )
    await db.refresh(agent)
    await db.refresh(member)
    assert member.name_snapshot == "Project Principal"
    assert member.role_snapshot == "Own project architecture"
    serialized = await serialize_project_agent(project, agent, member)
    assert serialized["soul"] == "# Owner controlled\n"
    assert serialized["core_memory"] == "# Core context\n"

    records = await list_project_agents(db, project)
    assert [(row[0].id, row[1].id) for row in records] == [(agent.id, member.id)]
    loaded_agent, loaded_member = await get_project_agent(db, project, agent.id)
    assert loaded_agent.id == agent.id
    assert loaded_member.id == member.id


async def _utc_timezone(_agent_id: uuid.UUID) -> str:
    return "UTC"


async def _no_extension_prompts(_agent_id: uuid.UUID) -> list[str]:
    return []


async def test_blank_project_agent_gets_professional_project_identity_in_runtime_context(
    db,
    project_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    owner, project = await _owner_and_project(db)
    project.name = "企业客户续费治理"
    project.goal = "基于健康度信号识别续费风险并推动干预闭环。"
    project.success_criteria = [
        "所有高风险客户均有风险证据与责任人",
        "续费预测误差低于 10%",
    ]
    agent, _member = await create_project_agent(
        db,
        project,
        owner,
        ProjectAgentCreate(
            name="客户成功数据分析师",
            role_description="负责健康度建模、风险分层和干预效果复盘。",
        ),
    )
    layout = project_agent_service.project_agent_workspace(project_repo, agent.id)
    soul = layout.soul.read_text(encoding="utf-8")
    memory = layout.memory.read_text(encoding="utf-8")
    assert "客户成功数据分析师" in soul
    assert "健康度建模、风险分层和干预效果复盘" in soul
    assert "企业客户续费治理" in memory
    assert "续费预测误差低于 10%" in memory

    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_context, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_memory, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_context, "_collect_extension_prompts", _no_extension_prompts)
    monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone", _utc_timezone)
    runtime = AgentRuntimeWorkspace(
        agent_id=agent.id,
        local_root=layout.root,
        storage_prefix=normalize_storage_key(layout.root.relative_to(tmp_path).as_posix()),
        project_id=project.id,
        project_repo_root=project_repo,
    )
    with bind_agent_runtime_workspace(runtime):
        static_prompt, dynamic_prompt = await build_agent_context(
            agent.id,
            agent.name,
            agent.role_description or "",
        )
    runtime_context = f"{static_prompt}\n{dynamic_prompt}"
    assert "客户成功数据分析师" in runtime_context
    assert "健康度建模、风险分层和干预效果复盘" in runtime_context
    assert "基于健康度信号识别续费风险并推动干预闭环" in runtime_context
    assert "续费预测误差低于 10%" in runtime_context


async def test_whitespace_project_agent_identity_keeps_generated_professional_defaults(
    db,
    project_repo: Path,
):
    owner, project = await _owner_and_project(db)
    project.name = "供应链缺货治理"
    project.goal = "降低关键物料缺货风险。"
    agent, _member = await create_project_agent(
        db,
        project,
        owner,
        ProjectAgentCreate(
            name="供应计划师",
            role_description="负责需求预测、补货参数与缺货风险判断。",
            soul="   ",
            core_memory="\n\t",
        ),
    )

    layout = project_agent_service.project_agent_workspace(project_repo, agent.id)
    assert "供应计划师" in layout.soul.read_text(encoding="utf-8")
    assert "需求预测、补货参数与缺货风险判断" in layout.soul.read_text(encoding="utf-8")
    assert "供应链缺货治理" in layout.memory.read_text(encoding="utf-8")


async def test_project_agent_lifecycle_retains_assets_and_promotion_copies_identity(
    db,
    project_repo,
    monkeypatch: pytest.MonkeyPatch,
):
    owner, project = await _owner_and_project(db)
    agent, member = await create_project_agent(
        db,
        project,
        owner,
        ProjectAgentCreate(name="Project QA", role_description="Validate", soul="# QA\n"),
    )
    tenant = await db.get(Tenant, project.tenant_id)
    assert agent.primary_model_id == tenant.default_model_id
    layout = project_agent_service.project_agent_workspace(project_repo, agent.id)

    async def fake_deactivate_member(_db, _project, target_member, **_kwargs):
        target_member.is_enabled = False
        return []

    async def fake_restore_member(_db, _project, target_member, **_kwargs):
        target_member.is_enabled = True
        return []

    monkeypatch.setattr(project_agent_service, "deactivate_project_member", fake_deactivate_member)
    monkeypatch.setattr(project_agent_service, "restore_project_member", fake_restore_member)

    assert await deactivate_project_agent(db, project, owner, agent, member, reason="rotation") == []
    assert member.is_enabled is False
    assert agent.status == "stopped"
    assert layout.soul.is_file()

    await restore_project_agent(db, project, owner, agent, member, reason="return")
    assert member.is_enabled is True
    assert agent.status == "idle"

    copied = {}

    async def fake_promote(project_root, project_agent_id, target_agent_id, *, overwrite=False):
        copied.update(
            project_root=project_root,
            project_agent_id=project_agent_id,
            target_agent_id=target_agent_id,
            overwrite=overwrite,
        )

    monkeypatch.setattr(project_agent_service, "promote_project_agent_workspace", fake_promote)
    promoted = await promote_project_agent(db, project, owner, agent, name="Independent QA")

    assert promoted.scope == "standard"
    assert promoted.project_id is None
    assert promoted.agent_dir is None
    assert promoted.source_agent_id == agent.id
    assert promoted.name == "Independent QA"
    assert copied == {
        "project_root": project_repo,
        "project_agent_id": agent.id,
        "target_agent_id": promoted.id,
        "overwrite": True,
    }


def test_identity_writes_are_atomic_and_reject_symlinks(tmp_path: Path):
    target = tmp_path / "soul.md"
    assert project_agent_service._write_text_if_changed(target, "first") is True
    assert project_agent_service._write_text_if_changed(target, "first") is False
    assert target.read_text(encoding="utf-8") == "first"

    linked = tmp_path / "memory.md"
    linked.symlink_to(target)
    with pytest.raises(HTTPException, match="项目数字员工身份文件不能是符号链接"):
        project_agent_service._write_text_if_changed(linked, "blocked")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".agents/agent-id/soul.md", True),
        ("/.agents/agent-id/memory.md", True),
        (".agents/agent-id/workspace/../soul.md", True),
        (r".agents\agent-id\memory.md", True),
        (".agents/agent-id/workspace/soul.md", False),
        ("docs/soul.md", False),
    ],
)
def test_project_agent_identity_path_guard_normalizes_paths(path: str, expected: bool):
    assert projects_api._is_project_agent_identity_path(path) is expected


async def test_project_owned_agent_cannot_join_another_project(db):
    owner, project_a = await _owner_and_project(db)
    project_b = await _project_in_same_tenant(db, owner, name="Project B")
    project_agent = Agent(
        name="Project A Specialist",
        role_description="Only belongs to project A",
        creator_id=owner.id,
        tenant_id=project_a.tenant_id,
        project_id=project_a.id,
        scope="project",
        status="idle",
        access_mode="private",
        agent_type="native",
    )
    db.add(project_agent)
    await db.flush()

    with pytest.raises(HTTPException) as exc_info:
        await add_member(
            db,
            project_b,
            ProjectMemberCreate(agent_id=project_agent.id),
            actor_user_id=owner.id,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "项目专用数字员工不能加入其他项目"


async def test_standard_agent_can_still_join_multiple_projects(db):
    owner, project_a = await _owner_and_project(db)
    project_b = await _project_in_same_tenant(db, owner, name="Project B")
    standard_agent = Agent(
        name="Shared Specialist",
        role_description="Reusable standard Agent",
        creator_id=owner.id,
        tenant_id=project_a.tenant_id,
        scope="standard",
        status="idle",
        access_mode="private",
        agent_type="native",
    )
    db.add(standard_agent)
    await db.flush()

    member_a = await add_member(
        db,
        project_a,
        ProjectMemberCreate(agent_id=standard_agent.id),
        actor_user_id=owner.id,
    )
    member_b = await add_member(
        db,
        project_b,
        ProjectMemberCreate(agent_id=standard_agent.id),
        actor_user_id=owner.id,
    )

    assert member_a.project_id == project_a.id
    assert member_b.project_id == project_b.id
    assert member_a.agent_id == member_b.agent_id == standard_agent.id


async def test_only_project_owner_can_change_project_agent_identity(db):
    owner, project = await _owner_and_project(db)
    member = await _user_in_same_tenant(db, owner, display_name="Project member")

    with pytest.raises(HTTPException) as update_exc:
        await projects_api.patch_project_owned_agent(
            project.id,
            uuid.uuid4(),
            ProjectAgentUpdate(soul="# Not allowed\n", core_memory="# Not allowed\n"),
            current_user=member,
            db=db,
        )

    with pytest.raises(HTTPException) as soul_exc:
        await projects_api._guard_project_agent_identity_paths(
            db,
            member,
            project,
            ".agents/project-agent/soul.md",
        )
    with pytest.raises(HTTPException) as memory_exc:
        await projects_api._guard_project_agent_identity_paths(
            db,
            member,
            project,
            ".agents/project-agent/memory.md",
        )

    assert update_exc.value.status_code == 404
    assert soul_exc.value.status_code == 404
    assert memory_exc.value.status_code == 404
    await projects_api._guard_project_agent_identity_paths(
        db,
        owner,
        project,
        ".agents/project-agent/soul.md",
        ".agents/project-agent/memory.md",
    )
    await projects_api._guard_project_agent_identity_paths(
        db,
        member,
        project,
        ".agents/project-agent/workspace/notes.md",
    )
