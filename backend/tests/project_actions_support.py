"""API-level acceptance tests for the AI-native project closed loop.

These tests intentionally use the real SQLAlchemy models and managed Git
implementation behind a small FastAPI app.  They verify observable API
contracts and durable state, rather than mocking the project service itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models.agent  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.chat_compaction  # noqa: F401
import app.models.chat_session  # noqa: F401
import app.models.channel_config  # noqa: F401
import app.models.dingtalk_provisioning  # noqa: F401
import app.models.gateway_message  # noqa: F401
import app.models.llm  # noqa: F401
import app.models.mcp_server  # noqa: F401
import app.models.org  # noqa: F401
import app.models.participant  # noqa: F401
import app.models.notification  # noqa: F401
import app.models.project  # noqa: F401
import app.models.published_page  # noqa: F401
import app.models.skill  # noqa: F401
import app.models.subagent_run  # noqa: F401
import app.models.task  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.tool  # noqa: F401
import app.models.user  # noqa: F401
from app.api import files as files_api
from app.api import mcp_servers as mcp_servers_api
from app.api import projects as projects_api
from app.core.security import get_current_user
from app.database import Base, get_db
from app.models.activity_log import DailyTokenUsage
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.gateway_message import GatewayMessage
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer
from app.models.org import OrgDepartment, OrgMember
from app.models.project import (
    Project,
    ProjectAccessGrant,
    ProjectCapabilityBinding,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRun,
    ProjectTemplate,
    ProjectWorkItem,
)
from app.models.skill import Skill, SkillFile, SkillInstall
from app.models.subagent_run import SubagentRun
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services.project_git_service import project_repo_path

pytestmark = pytest.mark.asyncio

TABLES = [
    "llm_models",
    "identities",
    "tenants",
    "users",
    "identity_providers",
    "org_departments",
    "org_members",
    "directory_group_edges",
    "directory_account_groups",
    "agent_templates",
    "agents",
    "agent_permissions",
    "agent_activity_logs",
    "daily_token_usage",
    "audit_logs",
    "approval_requests",
    "channel_configs",
    "dingtalk_channel_provisioning_sessions",
    "gateway_messages",
    "published_pages",
    "notifications",
    "tasks",
    "task_logs",
    "mcp_servers",
    "mcp_server_overrides",
    "tools",
    "agent_tools",
    "agent_agent_relationships",
    "participants",
    "skills",
    "skill_files",
    "skill_installs",
    "project_templates",
    "projects",
    "project_repository_operations",
    "project_access_grants",
    "project_member_snapshots",
    "project_capability_bindings",
    "project_work_items",
    "project_runs",
    "project_run_member_snapshots",
    "project_events",
    "chat_sessions",
    "chat_messages",
    "chat_compactions",
    "subagent_runs",
    "workspace_file_revisions",
]


@dataclass
class ProjectApiEnv:
    client: AsyncClient
    db: AsyncSession
    session_factory: Any
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    viewer_id: uuid.UUID
    org_admin_id: uuid.UUID
    source_leader_id: uuid.UUID
    source_worker_id: uuid.UUID
    source_reviewer_id: uuid.UUID
    leader_id: uuid.UUID
    worker_id: uuid.UUID
    reviewer_id: uuid.UUID
    owner: User
    viewer: User
    org_admin: User
    leader: Agent
    worker: Agent
    reviewer: Agent
    active_user: dict[str, uuid.UUID]
    storage_root: Path

    def authenticate_as(self, user_id: uuid.UUID) -> None:
        self.active_user["value"] = user_id


async def _user(db: AsyncSession, tenant: Tenant, name: str) -> User:
    suffix = uuid.uuid4().hex[:8]
    identity = Identity(
        username=f"{name.lower()}-{suffix}",
        email=f"{name.lower()}-{suffix}@project.test",
        password_hash="test-only",
    )
    db.add(identity)
    await db.flush()
    user = User(
        identity_id=identity.id,
        tenant_id=tenant.id,
        display_name=name,
        role="member",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def _agent(db: AsyncSession, tenant: Tenant, owner: User, name: str, role: str) -> Agent:
    agent = Agent(
        name=name,
        role_description=role,
        creator_id=owner.id,
        tenant_id=tenant.id,
        status="running",
        access_mode="private",
        autonomy_policy={"write": "confirm"},
        max_tool_rounds=17,
    )
    db.add(agent)
    await db.flush()
    return agent


@pytest.fixture
async def project_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ProjectApiEnv]:
    database_url = os.environ.get("PROJECT_TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    engine = create_async_engine(database_url)
    table_names = list(TABLES)
    async with engine.begin() as connection:
        if os.environ.get("PROJECT_TEST_DATABASE_URL"):
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Base.metadata.tables[name] for name in table_names],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    # Durable subagent helpers intentionally open their own transaction. Point
    # that runtime at the same isolated test database instead of the process
    # default database configured for production.
    monkeypatch.setattr("app.services.subagent_runtime.async_session", session_factory)
    monkeypatch.setattr(
        "app.services.project_group_turn_lifecycle.async_session",
        session_factory,
    )
    monkeypatch.setattr("app.services.project_runtime_tools.async_session", session_factory)
    monkeypatch.setattr("app.api.websocket.async_session", session_factory)
    monkeypatch.setattr("app.services.channel_llm.async_session", session_factory)

    tenant = Tenant(name="Project API", slug=f"project-api-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    tenant_model = LLMModel(
        tenant_id=tenant.id,
        provider="openai",
        model="project-tenant-default",
        api_key_encrypted="test-only",
        label="Project tenant default",
        enabled=True,
        context_window=128000,
    )
    session.add(tenant_model)
    await session.flush()
    tenant.default_model_id = tenant_model.id
    owner = await _user(session, tenant, "Owner")
    viewer = await _user(session, tenant, "Viewer")
    org_admin = await _user(session, tenant, "Org Admin")
    org_admin.role = "org_admin"
    leader = await _agent(session, tenant, owner, "Leader", "Drive outcomes")
    worker = await _agent(session, tenant, owner, "Worker", "Build deliverables")
    reviewer = await _agent(session, tenant, owner, "Reviewer", "Review evidence")
    session.add(
        Tool(
            name="send_message_to_parent",
            display_name="Message Parent Agent",
            description="Return an interim result to the parent session",
            type="builtin",
            category="subagent",
            parameters_schema={"type": "object", "properties": {"message": {"type": "string"}}},
            enabled=True,
            is_default=True,
            source="builtin",
        )
    )
    await session.commit()

    tenant_id = tenant.id
    owner_id = owner.id
    viewer_id = viewer.id
    org_admin_id = org_admin.id
    leader_id = leader.id
    worker_id = worker.id
    reviewer_id = reviewer.id
    active_user = {"value": owner_id}

    async def override_db() -> AsyncIterator[AsyncSession]:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    async def override_user() -> User:
        # Production auth loads a user on every request. Resolve by immutable
        # ID so an expected authorization rollback cannot leave the fixture
        # holding an expired ORM identity.
        result = await session.execute(select(User).where(User.id == active_user["value"]))
        return result.scalar_one()

    async def skip_external_delivery(_run_id: uuid.UUID) -> None:
        # The endpoint's durable queue/run/event behavior is in scope.  The
        # external agent transport is independently covered by bridge tests.
        return None

    async def skip_external_audit(**_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "app.services.project_git_service.get_settings",
        lambda: SimpleNamespace(STORAGE_LOCAL_ROOT=str(tmp_path)),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime_workspace.get_settings",
        lambda: SimpleNamespace(STORAGE_LOCAL_ROOT=str(tmp_path), AGENT_DATA_DIR=str(tmp_path)),
    )
    monkeypatch.setattr(projects_api, "deliver_project_a2a", skip_external_delivery)
    monkeypatch.setattr(mcp_servers_api, "write_audit_log", skip_external_audit)

    test_app = FastAPI()
    test_app.include_router(projects_api.router, prefix="/api")
    test_app.include_router(files_api.router, prefix="/api")
    test_app.include_router(mcp_servers_api.router, prefix="/api")
    test_app.dependency_overrides[get_db] = override_db
    test_app.dependency_overrides[get_current_user] = override_user

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://project.test") as client:
        yield ProjectApiEnv(
            client=client,
            db=session,
            session_factory=session_factory,
            tenant_id=tenant_id,
            owner_id=owner_id,
            viewer_id=viewer_id,
            org_admin_id=org_admin_id,
            source_leader_id=leader_id,
            source_worker_id=worker_id,
            source_reviewer_id=reviewer_id,
            leader_id=leader_id,
            worker_id=worker_id,
            reviewer_id=reviewer_id,
            owner=owner,
            viewer=viewer,
            org_admin=org_admin,
            leader=leader,
            worker=worker,
            reviewer=reviewer,
            active_user=active_user,
            storage_root=tmp_path,
        )

    await session.close()
    await engine.dispose()


async def _create_project(
    env: ProjectApiEnv,
    *,
    name: str = "Closed loop",
    leader_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    response = await env.client.post(
        "/api/projects",
        json={
            "name": name,
            "description": "API acceptance project",
            "goal": "Deliver a traceable result",
            "success_criteria": ["Evidence is committed", "Review is recorded"],
            "members": [
                {"agent_id": str(leader_id or env.source_leader_id), "is_leader": True},
                {"agent_id": str(env.source_worker_id)},
                {"agent_id": str(env.source_reviewer_id)},
            ],
            "capabilities": [
                {
                    "capability_type": "tool",
                    "capability_name": "project-shell",
                    "source": "shared",
                    "scope": {"paths": ["workspace/**"]},
                },
                {
                    "capability_type": "tool",
                    "capability_name": "worker-private-tool",
                    "source": "inherited",
                    "inherited_from_agent_id": str(env.source_worker_id),
                    "scope": {"paths": ["deliverables/**"]},
                },
            ],
            "settings": {
                "git": {"repository_mode": "managed", "branch_policy": "work_item"},
                "runtime": {"mode": "balanced", "monthly_budget": 300},
                "policies": {"approval": "risk", "max_parallel_runs": 3},
            },
        },
    )
    assert response.status_code == 201, response.text
    project = response.json()
    member_ids_by_name = {
        member["agent_name"]: uuid.UUID(member["agent_id"])
        for member in project["members"]
    }
    env.leader_id = member_ids_by_name.get("Leader", env.leader_id)
    env.worker_id = member_ids_by_name.get("Worker", env.worker_id)
    env.reviewer_id = member_ids_by_name.get("Reviewer", env.reviewer_id)
    return project


async def _create_work_item(
    env: ProjectApiEnv,
    project_id: str | uuid.UUID,
    *,
    title: str,
    assignee_agent_id: uuid.UUID | None = None,
    dependency_ids: list[str] | None = None,
) -> dict[str, Any]:
    response = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": title,
            "assignee_agent_id": str(assignee_agent_id) if assignee_agent_id else None,
            "dependency_ids": dependency_ids or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _mark_project_running(env: ProjectApiEnv, project_id: str | uuid.UUID) -> None:
    project = await env.db.get(Project, uuid.UUID(str(project_id)))
    assert project is not None
    project.status = "running"
    await env.db.commit()


async def _exercise_project_skill_assets_followup(
    env: ProjectApiEnv,
    *,
    source_project_id: uuid.UUID,
    project_agent_id: uuid.UUID,
    copied_manifest: Path,
    manifest_content: str,
    skill_binding: dict[str, Any],
    source_project: Project,
    metadata: dict[str, Any],
    folder: str,
    library_skill_id: uuid.UUID,
    library_binding: dict[str, Any],
    library_folder: str,
    library_root: Path,
    selected_template_body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env.authenticate_as(env.owner_id)
    deactivated = await env.client.post(f"/api/projects/{source_project_id}/agents/{project_agent_id}/deactivate")
    assert deactivated.status_code == 200, deactivated.text
    assert copied_manifest.read_text(encoding="utf-8") == manifest_content
    retained_binding = await env.db.get(ProjectCapabilityBinding, uuid.UUID(skill_binding["id"]))
    assert retained_binding is not None
    retained_capabilities = (await env.client.get(f"/api/projects/{source_project_id}/capabilities")).json()
    assert next(item for item in retained_capabilities if item["id"] == skill_binding["id"])[
        "availability"
    ] == "available"

    restored_member = await env.client.post(
        f"/api/projects/{source_project_id}/agents/{project_agent_id}/restore"
    )
    assert restored_member.status_code == 200, restored_member.text
    second_agent_response = await env.client.post(
        f"/api/projects/{source_project_id}/agents",
        json={"source_agent_id": str(env.source_worker_id), "name": "Second release owner"},
    )
    assert second_agent_response.status_code == 201, second_agent_response.text
    second_agent_id = uuid.UUID(second_agent_response.json()["id"])
    from app.services.project_agent_workspace import project_agent_workspace
    second_layout = project_agent_workspace(
        project_repo_path(source_project.tenant_id, source_project.id),
        second_agent_id,
    )
    second_source_binding = (
        await env.db.execute(
            select(ProjectCapabilityBinding).where(
                ProjectCapabilityBinding.project_id == source_project_id,
                ProjectCapabilityBinding.capability_type == "skill",
                ProjectCapabilityBinding.inherited_from_agent_id == second_agent_id,
                ProjectCapabilityBinding.capability_name == "Release Check",
            )
        )
    ).scalar_one()
    assert second_source_binding.config["skill_asset"]["asset_id"] == metadata["asset_id"]
    assert os.stat(copied_manifest).st_ino == os.stat(
        second_layout.root / "skills" / folder / "SKILL.md"
    ).st_ino

    second_library_response = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "skill",
            "capability_id": str(library_skill_id),
            "source": "inherited",
            "inherited_from_agent_id": str(second_agent_id),
        },
    )
    assert second_library_response.status_code == 201, second_library_response.text
    second_library_binding = second_library_response.json()
    first_library_row = await env.db.get(ProjectCapabilityBinding, uuid.UUID(library_binding["id"]))
    second_library_row = await env.db.get(ProjectCapabilityBinding, uuid.UUID(second_library_binding["id"]))
    assert first_library_row is not None and second_library_row is not None
    assert (
        first_library_row.config["skill_asset"]["asset_id"]
        == second_library_row.config["skill_asset"]["asset_id"]
    )
    second_library_root = second_layout.root / "skills" / library_folder
    assert os.stat(library_root / "SKILL.md").st_ino == os.stat(second_library_root / "SKILL.md").st_ino

    disabled_response = await env.client.patch(
        f"/api/projects/{source_project_id}/capabilities/{second_library_binding['id']}",
        json={"is_enabled": False},
    )
    assert disabled_response.status_code == 200, disabled_response.text
    assert disabled_response.json()["is_enabled"] is False
    assert disabled_response.json()["availability"] == "available"
    assert not second_library_root.exists()
    source_library_after_disable = await env.db.get(Skill, library_skill_id)
    assert source_library_after_disable is not None
    assert source_library_after_disable.status == "published"
    assert len(
        list(
            (
                await env.db.execute(select(SkillFile).where(SkillFile.skill_id == library_skill_id))
            ).scalars()
        )
    ) == 2
    enabled_response = await env.client.patch(
        f"/api/projects/{source_project_id}/capabilities/{second_library_binding['id']}",
        json={"is_enabled": True},
    )
    assert enabled_response.status_code == 200, enabled_response.text
    assert second_library_root.is_dir()

    library_skill = await env.db.get(Skill, library_skill_id)
    assert library_skill is not None
    library_skill.version = 6
    library_manifest_row = (
        await env.db.execute(
            select(SkillFile).where(
                SkillFile.skill_id == library_skill_id,
                SkillFile.path == "SKILL.md",
            )
        )
    ).scalar_one()
    refreshed_library_content = (
        "---\nname: Library Check\nversion: 6\ndescription: Updated library check\n---\n"
    )
    library_manifest_row.content = refreshed_library_content
    await env.db.commit()
    refresh_response = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities/{library_binding['id']}/refresh"
    )
    assert refresh_response.status_code == 200, refresh_response.text
    assert refresh_response.json()["changed"] is True
    assert refresh_response.json()["affected_member_count"] == 2
    assert refresh_response.json()["capability"]["version"] == "6"
    assert (library_root / "SKILL.md").read_text(encoding="utf-8") == refreshed_library_content
    assert (second_library_root / "SKILL.md").read_text(encoding="utf-8") == refreshed_library_content
    assert os.stat(library_root / "SKILL.md").st_ino == os.stat(second_library_root / "SKILL.md").st_ino

    impact_response = await env.client.get(
        f"/api/projects/{source_project_id}/capabilities/{library_binding['id']}/delete-impact"
    )
    assert impact_response.status_code == 200, impact_response.text
    assert impact_response.json()["affected_member_count"] == 2
    rejected_delete = await env.client.delete(
        f"/api/projects/{source_project_id}/capabilities/{library_binding['id']}"
    )
    assert rejected_delete.status_code == 409
    assert rejected_delete.json()["detail"]["affected_member_count"] == 2
    confirmed_delete = await env.client.delete(
        f"/api/projects/{source_project_id}/capabilities/{library_binding['id']}?confirm=true"
    )
    assert confirmed_delete.status_code == 200, confirmed_delete.text
    assert confirmed_delete.json()["affected_member_count"] == 2
    remaining_library_bindings = list(
        (
            await env.db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == source_project_id,
                    ProjectCapabilityBinding.capability_id == library_skill_id,
                )
            )
        ).scalars()
    )
    assert remaining_library_bindings == []
    assert not library_root.exists()
    assert not second_library_root.exists()

    workspace_manifest_content = (
        "---\nname: Release Check\nversion: 4\ndescription: Edited in the project Agent Skill page\n---\n"
    )
    workspace_write = await env.client.put(
        f"/api/agents/{project_agent_id}/files/content",
        params={"path": f"skills/{folder}/SKILL.md"},
        json={"content": workspace_manifest_content},
    )
    assert workspace_write.status_code == 200, workspace_write.text
    assert copied_manifest.read_text(encoding="utf-8") == workspace_manifest_content
    workspace_capabilities = (await env.client.get(f"/api/projects/{source_project_id}/capabilities")).json()
    edited_skill = next(item for item in workspace_capabilities if item["id"] == skill_binding["id"])
    assert edited_skill["version"] == "4"
    assert edited_skill["description"] == "Edited in the project Agent Skill page"
    edited_manifest = (await env.client.get(f"/api/projects/{source_project_id}/template-manifest")).json()
    edited_manifest_skill = next(
        item for item in edited_manifest["skills"] if item["binding_id"] == skill_binding["id"]
    )
    assert edited_manifest_skill["version"] == "4"
    assert {"path", "sha256", "asset_id"}.isdisjoint(edited_manifest_skill)

    workspace_delete = await env.client.delete(
        f"/api/agents/{project_agent_id}/files/content",
        params={"path": f"skills/{folder}/SKILL.md"},
    )
    assert workspace_delete.status_code == 200, workspace_delete.text
    assert workspace_delete.json()["project_skill_deleted"] is True
    assert workspace_delete.json()["affected_member_count"] == 2
    remaining_source_skill_bindings = list(
        (
            await env.db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == source_project_id,
                    ProjectCapabilityBinding.capability_type == "skill",
                    ProjectCapabilityBinding.capability_name == "Release Check",
                )
            )
        ).scalars()
    )
    assert remaining_source_skill_bindings == []
    assert not copied_manifest.parent.exists()
    assert not (second_layout.root / "skills" / folder).exists()
    deletion_events = (await env.client.get(f"/api/projects/{source_project_id}/events?limit=200")).json()
    file_delete_event = next(
        event
        for event in reversed(deletion_events)
        if event["event_type"] == "capability.deleted"
        and event["event_metadata"].get("source") == "agent_files"
    )
    assert file_delete_event["event_metadata"]["affected_member_count"] == 2

    def fail_template_skill_copy(*_args, **_kwargs):
        raise OSError("simulated template Skill copy failure")

    monkeypatch.setattr(
        "app.services.project_skill_assets._write_files",
        fail_template_skill_copy,
    )
    failed_project_name = f"Failed Skill restore {uuid.uuid4().hex[:8]}"
    with pytest.raises(OSError, match="simulated template Skill copy failure"):
        await env.client.post(
            "/api/projects/from-template",
            json={"template_id": selected_template_body["id"], "name": failed_project_name},
        )
    failed_project = (
        await env.db.execute(select(Project).where(Project.name == failed_project_name))
    ).scalar_one_or_none()
    assert failed_project is None


__all__ = [name for name in globals() if not name.startswith("__")]
