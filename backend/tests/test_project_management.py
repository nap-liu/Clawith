from __future__ import annotations

import subprocess
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.agent  # noqa: F401
import app.models.chat_session  # noqa: F401
import app.models.llm  # noqa: F401
import app.models.org  # noqa: F401
import app.models.participant  # noqa: F401
import app.models.project  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.user  # noqa: F401
from app.database import Base
from app.models.agent import Agent
from app.models.project import ProjectAccessGrant, ProjectRun
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.project import ProjectCapabilityCreate, ProjectCreate, ProjectMemberCreate
from app.services.project_git_service import (
    commit_project_changes,
    create_branch,
    initialize_project_repo,
    list_project_files,
    restore_as_new_commit,
    write_project_file,
)
from app.services.project_service import create_project, freeze_run_members, require_project
from app.services.recipient_resolver import RecipientResolutionError, resolve_agent_recipient

TABLES = [
    "llm_models",
    "identities",
    "tenants",
    "users",
    "agent_templates",
    "agents",
    "agent_agent_relationships",
    "participants",
    "project_templates",
    "projects",
    "project_access_grants",
    "project_member_snapshots",
    "project_capability_bindings",
    "project_work_items",
    "project_runs",
    "project_run_member_snapshots",
    "project_events",
    "chat_sessions",
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
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _tenant(db, name: str) -> Tenant:
    tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}")
    db.add(tenant)
    await db.flush()
    return tenant


async def _user(db, tenant: Tenant, name: str) -> User:
    identity = Identity(username=f"{name}-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex}@local.test")
    db.add(identity)
    await db.flush()
    user = User(identity_id=identity.id, tenant_id=tenant.id, display_name=name, role="member", is_active=True)
    db.add(user)
    await db.flush()
    return user


async def test_private_project_and_explicit_share_are_tenant_safe(db, monkeypatch):
    async def fake_git(_project):
        return {"mode": "managed", "head": "a" * 40, "default_branch": "main"}

    monkeypatch.setattr("app.services.project_git_service.initialize_project_repo", fake_git)
    tenant = await _tenant(db, "One")
    other_tenant = await _tenant(db, "Two")
    owner = await _user(db, tenant, "Owner")
    viewer = await _user(db, tenant, "Viewer")
    outsider = await _user(db, other_tenant, "Outsider")

    project = await create_project(
        db,
        owner,
        ProjectCreate(name="Private", objective="Ship safely", shared_user_ids=[viewer.id], visibility="shared"),
    )
    assert project.status == "running"
    assert project.goal == "Ship safely"
    assert (await require_project(db, owner, project.id)).id == project.id
    assert (await require_project(db, viewer, project.id)).id == project.id
    with pytest.raises(HTTPException) as denied_edit:
        await require_project(db, viewer, project.id, edit=True)
    assert denied_edit.value.status_code == 404
    with pytest.raises(HTTPException) as hidden:
        await require_project(db, outsider, project.id)
    assert hidden.value.status_code == 404

    grant = await db.get(ProjectAccessGrant, (await db.execute(ProjectAccessGrant.__table__.select())).first().id)
    grant.role = "edit"
    await db.flush()
    assert (await require_project(db, viewer, project.id, edit=True)).id == project.id


async def test_run_freezes_member_and_effective_capability_snapshots(db, monkeypatch):
    async def fake_git(_project):
        return {"mode": "managed", "head": "b" * 40, "default_branch": "main"}

    monkeypatch.setattr("app.services.project_git_service.initialize_project_repo", fake_git)
    tenant = await _tenant(db, "Snapshot")
    owner = await _user(db, tenant, "Owner")
    leader = Agent(
        name="Leader",
        role_description="Drive delivery",
        creator_id=owner.id,
        tenant_id=tenant.id,
        status="running",
        access_mode="private",
    )
    worker = Agent(
        name="Worker",
        role_description="Build",
        creator_id=owner.id,
        tenant_id=tenant.id,
        status="running",
        access_mode="private",
    )
    db.add_all([leader, worker])
    await db.flush()
    project = await create_project(
        db,
        owner,
        ProjectCreate(
            name="Snapshots",
            members=[
                ProjectMemberCreate(agent_id=leader.id, is_leader=True),
                ProjectMemberCreate(agent_id=worker.id),
            ],
            capabilities=[
                ProjectCapabilityCreate(
                    capability_type="tool",
                    capability_name="project-shell",
                    source="shared",
                    scope={"paths": ["workspace/**"]},
                )
            ],
        ),
    )
    run = ProjectRun(
        tenant_id=tenant.id,
        project_id=project.id,
        initiated_by_user_id=owner.id,
        status="queued",
        trigger_type="manual",
    )
    db.add(run)
    await db.flush()
    snapshots = await freeze_run_members(db, project, run)
    assert len(snapshots) == 2
    assert sum(snapshot.is_leader for snapshot in snapshots) == 1
    assert all(snapshot.capability_snapshot[0]["name"] == "project-shell" for snapshot in snapshots)

    resolved = await resolve_agent_recipient(db, leader.id, worker.id, project_id=project.id)
    assert resolved.source_agent.id == leader.id
    assert resolved.target_agent.id == worker.id
    assert resolved.relationship is None
    with pytest.raises(RecipientResolutionError) as not_globally_related:
        await resolve_agent_recipient(db, leader.id, worker.id)
    assert not_globally_related.value.code == "recipient_not_related"


async def test_managed_git_restore_creates_new_commit_without_rewriting(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_git_service.get_settings",
        lambda: SimpleNamespace(STORAGE_LOCAL_ROOT=str(tmp_path)),
    )
    project = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Git Project",
        description="Traceable",
        goal="Never rewrite history",
        success_criteria=["restore commit"],
        settings={"git": {"mode": "managed"}},
    )
    initial = await initialize_project_repo(project)
    repo = tmp_path / "_projects" / str(project.tenant_id) / str(project.id) / "repo"
    written = await write_project_file(project, "deliverables/result.md", "# Result\n\nTraceable.\n")
    assert written["commit"] != initial["head"]
    files = await list_project_files(project)
    assert next(item for item in files if item["path"] == "deliverables/result.md")["preview"].startswith("# Result")
    with pytest.raises(HTTPException) as traversal:
        await write_project_file(project, "../outside.md", "escaped")
    assert traversal.value.status_code == 422
    with pytest.raises(HTTPException) as git_metadata:
        await write_project_file(project, ".git/config", "escaped")
    assert git_metadata.value.status_code == 422

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    changed = await commit_project_changes(project, "Change README", ["README.md"])
    changed_head = changed["commit"]

    milestone = await commit_project_changes(project, "Delivery milestone", milestone=True)
    assert milestone["commit"] != changed_head
    assert milestone["changed"] is False

    restored = await restore_as_new_commit(project, initial["head"])
    assert restored["commit"] not in {initial["head"], changed_head, milestone["commit"]}
    assert (repo / "README.md").read_text(encoding="utf-8").startswith("# Git Project")
    branch = await create_branch(project, "review/restore", initial["head"])
    assert branch["status"] == "completed"
    assert (
        subprocess.check_output(["git", "-C", str(repo), "rev-parse", "review/restore"], text=True).strip()
        == initial["head"]
    )


def test_project_router_exposes_closed_loop_contract():
    from app.api.projects import router

    paths = {route.path for route in router.routes}
    assert {
        "/projects",
        "/projects/templates",
        "/projects/bootstrap-options",
        "/projects/{project_id}/members",
        "/projects/{project_id}/capabilities",
        "/projects/{project_id}/work-items",
        "/projects/{project_id}/runs",
        "/projects/{project_id}/events",
        "/projects/{project_id}/a2a",
        "/projects/{project_id}/settings",
        "/projects/{project_id}/files",
        "/projects/{project_id}/git/commit",
        "/projects/{project_id}/git/restore",
    } <= paths
