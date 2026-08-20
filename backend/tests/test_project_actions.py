"""API-level acceptance tests for the AI-native project closed loop.

These tests intentionally use the real SQLAlchemy models and managed Git
implementation behind a small FastAPI app.  They verify observable API
contracts and durable state, rather than mocking the project service itself.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models.agent  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.chat_compaction  # noqa: F401
import app.models.chat_session  # noqa: F401
import app.models.llm  # noqa: F401
import app.models.mcp_server  # noqa: F401
import app.models.org  # noqa: F401
import app.models.participant  # noqa: F401
import app.models.project  # noqa: F401
import app.models.subagent_run  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.tool  # noqa: F401
import app.models.user  # noqa: F401
from app.api import projects as projects_api
from app.core.security import get_current_user
from app.database import Base, get_db
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.project import Project, ProjectAccessGrant, ProjectEvent
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.user import Identity, User
from app.services.project_git_service import project_repo_path

pytestmark = pytest.mark.asyncio

TABLES = [
    "llm_models",
    "identities",
    "tenants",
    "users",
    "agent_templates",
    "agents",
    "tools",
    "agent_tools",
    "agent_agent_relationships",
    "participants",
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
]


@dataclass
class ProjectApiEnv:
    client: AsyncClient
    db: AsyncSession
    session_factory: Any
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    viewer_id: uuid.UUID
    leader_id: uuid.UUID
    worker_id: uuid.UUID
    reviewer_id: uuid.UUID
    owner: User
    viewer: User
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
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Base.metadata.tables[name] for name in TABLES],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    # Durable subagent helpers intentionally open their own transaction. Point
    # that runtime at the same isolated test database instead of the process
    # default database configured for production.
    monkeypatch.setattr("app.services.subagent_runtime.async_session", session_factory)
    monkeypatch.setattr("app.services.project_runtime_tools.async_session", session_factory)
    monkeypatch.setattr("app.api.websocket.async_session", session_factory)

    tenant = Tenant(name="Project API", slug=f"project-api-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    owner = await _user(session, tenant, "Owner")
    viewer = await _user(session, tenant, "Viewer")
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

    monkeypatch.setattr(
        "app.services.project_git_service.get_settings",
        lambda: SimpleNamespace(STORAGE_LOCAL_ROOT=str(tmp_path)),
    )
    monkeypatch.setattr(projects_api, "deliver_project_a2a", skip_external_delivery)

    test_app = FastAPI()
    test_app.include_router(projects_api.router, prefix="/api")
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
            leader_id=leader_id,
            worker_id=worker_id,
            reviewer_id=reviewer_id,
            owner=owner,
            viewer=viewer,
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
                {"agent_id": str(leader_id or env.leader_id), "is_leader": True},
                {"agent_id": str(env.worker_id)},
                {"agent_id": str(env.reviewer_id)},
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
                    "inherited_from_agent_id": str(env.worker_id),
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
    return response.json()


async def test_private_share_settings_and_audit_are_a_real_api_round_trip(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Private by default")
    project_id = project["id"]

    assert project["visibility"] == "private"
    assert project["shared_with"] == []
    assert project["status"] == "planning"

    env.authenticate_as(env.viewer_id)
    hidden = await env.client.get(f"/api/projects/{project_id}")
    assert hidden.status_code == 404

    env.authenticate_as(env.owner_id)
    settings_response = await env.client.get(f"/api/projects/{project_id}/settings")
    assert settings_response.status_code == 200, settings_response.text
    settings = settings_response.json()
    assert settings["policies"]["approval"] == "risk"
    assert settings["runtime"]["monthly_budget"] == 300

    saved = await env.client.patch(
        f"/api/projects/{project_id}/settings",
        json={
            "policies": {"approval": "all_writes", "max_parallel_runs": 2},
            "runtime": {"mode": "quality", "monthly_budget": 450},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["policies"] == {"approval": "all_writes", "max_parallel_runs": 2}
    assert saved.json()["runtime"]["monthly_budget"] == 450

    shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "shared", "shared_with_user_ids": [str(env.viewer_id)]},
    )
    assert shared.status_code == 200, shared.text
    assert shared.json()["visibility"] == "shared"
    assert [entry["user_id"] for entry in shared.json()["shared_with"]] == [str(env.viewer_id)]

    env.authenticate_as(env.viewer_id)
    visible = await env.client.get(f"/api/projects/{project_id}")
    assert visible.status_code == 200
    cannot_edit = await env.client.patch(f"/api/projects/{project_id}", json={"name": "Not allowed"})
    assert cannot_edit.status_code == 404

    grant = (
        await env.db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == uuid.UUID(project_id),
                ProjectAccessGrant.user_id == env.viewer_id,
            )
        )
    ).scalar_one()
    grant.role = "edit"
    await env.db.commit()
    normal_editor_update = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"description": "Editors may update ordinary project fields"},
    )
    assert normal_editor_update.status_code == 200
    editor_cannot_unshare = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "private"},
    )
    assert editor_cannot_unshare.status_code == 404
    editor_cannot_reshare = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"shared_with_user_ids": [str(env.viewer_id)]},
    )
    assert editor_cannot_reshare.status_code == 404

    env.authenticate_as(env.owner_id)
    empty_shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "shared", "shared_with_user_ids": []},
    )
    assert empty_shared.status_code == 422

    outsider_tenant = Tenant(name="Outsider", slug=f"outsider-{uuid.uuid4().hex[:8]}")
    env.db.add(outsider_tenant)
    await env.db.flush()
    outsider = await _user(env.db, outsider_tenant, "Outsider")
    await env.db.commit()
    cross_tenant_share = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "shared", "shared_with_user_ids": [str(outsider.id)]},
    )
    assert cross_tenant_share.status_code == 422

    private = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "private"},
    )
    assert private.status_code == 200
    assert private.json()["visibility"] == "private"
    assert private.json()["shared_with"] == []
    remaining_grants = (
        await env.db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == uuid.UUID(project_id)
            )
        )
    ).scalars().all()
    assert remaining_grants == []

    events_response = await env.client.get(f"/api/projects/{project_id}/events")
    assert events_response.status_code == 200
    event_types = {event["event_type"] for event in events_response.json()}
    assert {"project.created", "project.initialized", "project.settings.updated", "project.updated"} <= event_types


async def test_member_and_run_snapshots_are_isolated_and_a2a_bypasses_leader(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Snapshots and mesh")
    project_id = project["id"]
    worker_id = env.worker_id
    reviewer_id = env.reviewer_id
    source_before = {
        "name": env.worker.name,
        "role_description": env.worker.role_description,
        "autonomy_policy": dict(env.worker.autonomy_policy or {}),
        "max_tool_rounds": env.worker.max_tool_rounds,
    }

    members_response = await env.client.get(f"/api/projects/{project_id}/members")
    assert members_response.status_code == 200
    members = members_response.json()
    worker_snapshot = next(member for member in members if member["agent_id"] == str(worker_id))
    changed_snapshot = {
        **worker_snapshot["config_snapshot"],
        "autonomy_policy": {"write": "allow-in-project"},
        "project_instruction": "Only exists in this project",
    }
    patched_member = await env.client.patch(
        f"/api/projects/{project_id}/members/{worker_snapshot['id']}",
        json={"config_snapshot": changed_snapshot},
    )
    assert patched_member.status_code == 200, patched_member.text
    assert patched_member.json()["config_snapshot"]["project_instruction"] == "Only exists in this project"

    env.db.expire(env.worker)
    source_worker = await env.db.get(Agent, worker_id)
    assert source_worker is not None
    assert {
        "name": source_worker.name,
        "role_description": source_worker.role_description,
        "autonomy_policy": dict(source_worker.autonomy_policy or {}),
        "max_tool_rounds": source_worker.max_tool_rounds,
    } == source_before

    # Worker wakes Reviewer directly. Neither side is the project Leader and
    # no global Agent relationship is created or required.
    a2a_response = await env.client.post(
        f"/api/projects/{project_id}/a2a",
        json={
            "from_agent_id": str(worker_id),
            "to_agent_id": str(reviewer_id),
            "message": "Review the acceptance evidence directly",
            "mode": "review",
        },
    )
    assert a2a_response.status_code == 202, a2a_response.text
    assert a2a_response.json()["from_agent_id"] == str(worker_id)
    assert a2a_response.json()["to_agent_id"] == str(reviewer_id)

    # Execution runs are available after the explicit kickoff boundary. This
    # test focuses on immutable snapshots, so place the fixture in that state
    # without duplicating the kickoff acceptance test below.
    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()
    run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(worker_id), "trigger_type": "manual", "input": {"objective": "Build v1"}},
    )
    assert run_response.status_code == 201, run_response.text
    run_id = run_response.json()["id"]
    assert run_response.json()["output"]["subagent_session_id"]

    frozen_response = await env.client.get(f"/api/projects/{project_id}/runs/{run_id}/member-snapshots")
    assert frozen_response.status_code == 200
    frozen = frozen_response.json()
    assert len(frozen) == 3
    assert sum(item["is_leader"] for item in frozen) == 1
    worker_frozen = next(item for item in frozen if item["agent_id"] == str(worker_id))
    reviewer_frozen = next(item for item in frozen if item["agent_id"] == str(reviewer_id))
    assert {item["name"] for item in worker_frozen["capability_snapshot"]} == {
        "project-shell",
        "worker-private-tool",
    }
    assert {item["name"] for item in reviewer_frozen["capability_snapshot"]} == {"project-shell"}

    capabilities = (await env.client.get(f"/api/projects/{project_id}/capabilities")).json()
    shared_capability = next(item for item in capabilities if item["capability_name"] == "project-shell")
    disabled = await env.client.patch(
        f"/api/projects/{project_id}/capabilities/{shared_capability['id']}",
        json={"is_enabled": False},
    )
    assert disabled.status_code == 200
    frozen_again = (await env.client.get(f"/api/projects/{project_id}/runs/{run_id}/member-snapshots")).json()
    assert frozen_again == frozen

    events = (await env.client.get(f"/api/projects/{project_id}/events")).json()
    a2a_event = next(event for event in events if event["event_type"] == "a2a.queued")
    assert a2a_event["from_agent_id"] == str(worker_id)
    assert a2a_event["to_agent_id"] == str(reviewer_id)
    assert {"run.queued", "capability.updated", "member.snapshot.updated"} <= {
        event["event_type"] for event in events
    }


async def test_member_departure_is_audited_revocation_and_restore_starts_a_fresh_child(
    project_api: ProjectApiEnv,
):
    from app.api.websocket import WebSocketChatHandler
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectEvent, ProjectRun
    from app.models.subagent_run import SubagentRun
    from app.services.project_runtime_tools import load_project_runtime_scope
    from app.services.project_service import project_session_access_mode

    env = project_api
    project = await _create_project(env, name="Member lifecycle")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()

    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    leader = next(row for row in members if row["agent_id"] == str(env.leader_id))
    worker = next(row for row in members if row["agent_id"] == str(env.worker_id))

    leader_removal = await env.client.post(
        f"/api/projects/{project_id}/members/{leader['id']}/remove",
        json={"reason": "cannot remove current leader"},
    )
    assert leader_removal.status_code == 422

    first_run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.worker_id), "trigger_type": "manual", "input": {"objective": "First"}},
    )
    assert first_run_response.status_code == 201, first_run_response.text
    first_run_payload = first_run_response.json()
    first_child_id = uuid.UUID(first_run_payload["output"]["subagent_session_id"])
    first_project_run_id = uuid.UUID(first_run_payload["id"])
    first_child_session = await env.db.get(ChatSession, first_child_id)
    assert first_child_session is not None
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, first_child_session) == "edit"
    live_handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
    live_handler.project_session_access = "edit"
    live_handler.conv_id = str(first_child_id)
    live_handler.user_id = env.owner_id
    live_handler.read_only = False
    assert await live_handler._project_session_still_writable() is True

    removed = await env.client.post(
        f"/api/projects/{project_id}/members/{worker['id']}/remove",
        json={"reason": "staffing change"},
    )
    assert removed.status_code == 200, removed.text
    removed_payload = removed.json()
    assert removed_payload["id"] == worker["id"]
    assert removed_payload["is_enabled"] is False
    assert removed_payload["config_snapshot"]["membership"]["state"] == "departed"

    env.db.expire_all()
    first_child = await env.db.get(SubagentRun, first_child_id)
    first_project_run = await env.db.get(ProjectRun, first_project_run_id)
    first_child_session = await env.db.get(ChatSession, first_child_id)
    assert first_child is not None and first_child.status == "cancelled"
    assert first_project_run is not None and first_project_run.status == "cancelled"
    assert first_child_session is not None
    assert first_child_session.im_config["membership_revoked"] is True
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, first_child_session) == "read"
    assert await live_handler._project_session_still_writable() is False
    with pytest.raises(ValueError, match="active runtime member|permanently read-only"):
        await load_project_runtime_scope(
            env.db,
            session_id=first_child_id,
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
        )

    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    mention = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "@Worker continue", "mentions": [str(env.worker_id)]},
    )
    assert mention.status_code == 422
    assigned_run = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.worker_id), "trigger_type": "manual", "input": {"objective": "Blocked"}},
    )
    assert assigned_run.status_code == 422
    a2a = await env.client.post(
        f"/api/projects/{project_id}/a2a",
        json={
            "from_agent_id": str(env.leader_id),
            "to_agent_id": str(env.worker_id),
            "message": "Blocked",
            "mode": "notify",
        },
    )
    assert a2a.status_code == 422

    duplicate = await env.client.post(
        f"/api/projects/{project_id}/members",
        json={"agent_id": str(env.worker_id)},
    )
    assert duplicate.status_code == 409
    assert "restore" in duplicate.json()["detail"].lower()

    restored = await env.client.post(
        f"/api/projects/{project_id}/members/{worker['id']}/restore",
        json={"reason": "return to project"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == worker["id"]
    assert restored.json()["is_enabled"] is True
    assert restored.json()["config_snapshot"]["membership"]["generation"] == 2

    env.db.expire_all()
    first_child_session = await env.db.get(ChatSession, first_child_id)
    assert first_child_session is not None
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, first_child_session) == "read"
    assert await live_handler._project_session_still_writable() is False

    second_run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.worker_id), "trigger_type": "manual", "input": {"objective": "Fresh"}},
    )
    assert second_run_response.status_code == 201, second_run_response.text
    second_child_id = uuid.UUID(second_run_response.json()["output"]["subagent_session_id"])
    assert second_child_id != first_child_id
    second_child_session = await env.db.get(ChatSession, second_child_id)
    assert second_child_session is not None
    assert second_child_session.im_config["project_membership_generation"] == 2
    owner_user = await env.db.get(User, env.owner_id)
    assert owner_user is not None
    assert await project_session_access_mode(env.db, owner_user, second_child_session) == "edit"

    events = (
        await env.db.execute(
            select(ProjectEvent).where(ProjectEvent.project_id == project_id)
        )
    ).scalars().all()
    lifecycle_events = [row for row in events if row.event_type in {"member.departed", "member.restored"}]
    assert [row.event_type for row in lifecycle_events] == ["member.departed", "member.restored"]
    assert lifecycle_events[0].event_metadata["snapshot_retained"] is True
    assert lifecycle_events[1].event_metadata["old_sessions_remain_read_only"] is True


async def test_project_editor_can_remove_and_restore_participant_but_viewer_cannot(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.services.project_service import project_session_access_mode

    env = project_api
    project = await _create_project(env, name="Editor membership controls")
    project_id = project["id"]
    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()
    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    reviewer = next(row for row in members if row["agent_id"] == str(env.reviewer_id))
    reviewer_run = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.reviewer_id), "trigger_type": "manual", "input": {"objective": "Review"}},
    )
    assert reviewer_run.status_code == 201, reviewer_run.text
    reviewer_child_id = uuid.UUID(reviewer_run.json()["output"]["subagent_session_id"])
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert reviewer_session is not None

    grant = await env.client.post(
        f"/api/projects/{project_id}/access-grants",
        json={"user_id": str(env.viewer_id), "role": "view"},
    )
    assert grant.status_code == 201, grant.text
    viewer_user = await env.db.get(User, env.viewer_id)
    assert viewer_user is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "read"
    env.authenticate_as(env.viewer_id)
    forbidden = await env.client.post(
        f"/api/projects/{project_id}/members/{reviewer['id']}/remove",
        json={"reason": "viewer cannot"},
    )
    assert forbidden.status_code == 404

    env.authenticate_as(env.owner_id)
    grant_id = grant.json()["id"]
    grant_row = await env.db.get(ProjectAccessGrant, uuid.UUID(grant_id))
    assert grant_row is not None
    grant_row.role = "edit"
    await env.db.commit()
    viewer_user = await env.db.get(User, env.viewer_id)
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert viewer_user is not None and reviewer_session is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "edit"
    env.authenticate_as(env.viewer_id)
    removed = await env.client.post(
        f"/api/projects/{project_id}/members/{reviewer['id']}/remove",
        json={"reason": "editor staffing"},
    )
    assert removed.status_code == 200, removed.text
    viewer_user = await env.db.get(User, env.viewer_id)
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert viewer_user is not None and reviewer_session is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "read"
    restored = await env.client.post(
        f"/api/projects/{project_id}/members/{reviewer['id']}/restore",
        json={"reason": "editor restore"},
    )
    assert restored.status_code == 200, restored.text
    viewer_user = await env.db.get(User, env.viewer_id)
    reviewer_session = await env.db.get(ChatSession, reviewer_child_id)
    assert viewer_user is not None and reviewer_session is not None
    assert await project_session_access_mode(env.db, viewer_user, reviewer_session) == "read"


async def test_manual_run_requires_kickoff_and_dispatches_default_leader(project_api: ProjectApiEnv):
    from app.models.chat_session import ChatSession
    from app.models.project import Project, ProjectRun
    from app.models.subagent_run import SubagentRun

    env = project_api
    project = await _create_project(env, name="Manual execution contract")
    project_id = uuid.UUID(project["id"])

    before_kickoff = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"trigger_type": "manual", "input": {"objective": "Continue delivery"}},
    )
    assert before_kickoff.status_code == 409

    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()

    response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"trigger_type": "manual", "input": {"objective": "Continue delivery"}},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["agent_id"] == str(env.leader_id)
    assert payload["status"] in {"queued", "running"}
    assert payload["input"]["dispatch"]["task"] == "Continue delivery"
    assert payload["output"]["subagent_session_id"]

    run = await env.db.get(ProjectRun, uuid.UUID(payload["id"]))
    child = await env.db.get(SubagentRun, uuid.UUID(payload["output"]["subagent_session_id"]))
    assert child is not None
    child_session = await env.db.get(ChatSession, child.id)
    assert child_session is not None
    assert child_session.agent_id == env.leader_id
    assert run is not None and run.output["subagent_run_id"] == str(child.id)
    assert child.project_id == project_id


async def test_project_run_reconcile_persists_finished_terminal_state(project_api: ProjectApiEnv):
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Repair stale run")
    run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=uuid.UUID(project["id"]),
        initiated_by_user_id=env.owner_id,
        status="running",
        trigger_type="manual",
        finished_at=datetime.now(UTC),
        output={"result": "Already finished"},
    )
    env.db.add(run)
    await env.db.commit()

    response = await env.client.get(f"/api/projects/{project['id']}/runs")
    assert response.status_code == 200, response.text
    repaired_payload = next(item for item in response.json() if item["id"] == str(run.id))
    assert repaired_payload["status"] == "succeeded"

    # Verify the GET-owned reconciliation committed, rather than merely
    # changing the request session identity map.
    async with env.session_factory() as independent_db:
        persisted = await independent_db.get(ProjectRun, run.id)
        assert persisted is not None and persisted.status == "succeeded"


async def test_dispatch_does_not_regress_child_completed_project_run(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import Project, ProjectMemberSnapshot, ProjectRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Fast child completion")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    leader_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project_id,
                ProjectMemberSnapshot.agent_id == env.leader_id,
            )
        )
    ).scalar_one()
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(group["access_agent_id"]),
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Complete immediately",
        conversation_id=group["id"],
        message_meta={"kind": "project_run_request", "mentions": [str(env.leader_id)]},
    )
    run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=project_id,
        agent_id=env.leader_id,
        initiated_by_user_id=env.owner_id,
        status="queued",
        trigger_type="manual",
        input={
            "dispatch": {
                "group_session_id": group["id"],
                "project_member_id": str(leader_member.id),
                "turn_anchor_id": str(anchor.id),
                "task": "Complete immediately",
            }
        },
    )
    env.db.add_all([anchor, run])
    await env.db.commit()
    project_run_id = run.id
    child_id = uuid.uuid4()

    async def complete_before_dispatch_commit(**_kwargs):
        async with env.session_factory() as race_db:
            raced = await race_db.get(ProjectRun, project_run_id)
            assert raced is not None
            raced.status = "succeeded"
            raced.finished_at = datetime.now(UTC)
            raced.output = {"result": "Fast result"}
            await race_db.commit()
        return SimpleNamespace(id=child_id, status="completed"), True

    monkeypatch.setattr(subagent_runtime, "create_subagent", complete_before_dispatch_commit)
    result = await subagent_runtime.dispatch_project_run(project_run_id)
    assert result["status"] == "succeeded"
    env.db.expire_all()
    persisted = await env.db.get(ProjectRun, project_run_id)
    assert persisted is not None and persisted.status == "succeeded"
    assert persisted.finished_at is not None
    assert persisted.output["result"] == "Fast result"
    assert persisted.output["subagent_session_id"] == str(child_id)


async def test_project_a2a_delivery_returns_scoped_session_identifiers(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectRun
    from app.services import agent_tools, project_service

    env = project_api
    project = await _create_project(env, name="A2A session identity")
    queued = await env.client.post(
        f"/api/projects/{project['id']}/a2a",
        json={
            "from_agent_id": str(env.worker_id),
            "to_agent_id": str(env.reviewer_id),
            "message": "Return the exact project thread",
            "mode": "notify",
        },
    )
    assert queued.status_code == 202, queued.text
    queued_body = queued.json()

    async def scoped_sender(from_agent_id, args, **_kwargs):
        project_id = uuid.UUID(args["_project_id"])
        target_id = uuid.UUID(args["agent_id"])
        access_id = min(from_agent_id, target_id, key=str)
        peer_id = max(from_agent_id, target_id, key=str)
        async with env.session_factory() as delivery_db:
            delivery_db.add(
                ChatSession(
                    project_id=project_id,
                    agent_id=access_id,
                    peer_agent_id=peer_id,
                    source_channel="agent",
                    title="Worker ↔ Reviewer",
                    external_conv_id=f"project-a2a:{project_id}:{peer_id}",
                )
            )
            await delivery_db.commit()
        return "✅ Notification sent"

    monkeypatch.setattr(project_service, "async_session", env.session_factory)
    monkeypatch.setattr(agent_tools, "_send_message_to_agent", scoped_sender)
    await project_service.deliver_project_a2a(uuid.UUID(queued_body["run_id"]))

    env.db.expire_all()
    run = await env.db.get(ProjectRun, uuid.UUID(queued_body["run_id"]))
    assert run is not None and run.status == "succeeded"
    assert run.output["group_session_id"] == queued_body["group_session_id"]
    assert run.output["session_id"]
    assert run.output["session_agent_id"] == run.output["session_access_agent_id"]
    assert run.output["session_title"] == "Worker ↔ Reviewer"
    delivered = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.run_id == run.id,
                ProjectEvent.event_type == "a2a.delivered",
            )
        )
    ).scalar_one()
    assert delivered.event_metadata["session_id"] == run.output["session_id"]
    assert delivered.event_metadata["group_session_id"] == queued_body["group_session_id"]


async def test_project_group_routes_human_to_leader_and_reuses_durable_children(
    project_api: ProjectApiEnv,
):
    env = project_api
    project = await _create_project(env, name="Project Agent Group")
    project_id = project["id"]

    group_response = await env.client.get(f"/api/projects/{project_id}/group-session")
    assert group_response.status_code == 200, group_response.text
    group = group_response.json()
    assert group["source_channel"] == "project"
    assert group["max_mentions"] == 3

    passive = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Visible update only", "mentions": [], "attachments": []},
    )
    assert passive.status_code == 201, passive.text
    passive_body = passive.json()
    assert passive_body["awakened_agent_ids"] == [str(env.leader_id)]
    assert passive_body["default_leader_agent_id"] == str(env.leader_id)
    assert len(passive_body["subagent_runs"]) == 1
    assert passive_body["subagent_runs"][0]["agent_id"] == str(env.leader_id)
    assert passive_body["message"]["message_meta"]["visible_to_group"] is True
    assert (
        passive_body["message"]["message_meta"]["wake_policy"]
        == "default_leader_plus_structured_mentions"
    )

    mentioned = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Worker build and Reviewer check",
            "llm_content": "[brief.md extracted]\nBuild the evidence and review every acceptance item.",
            "mentions": [str(env.worker_id), str(env.reviewer_id), str(env.worker_id)],
            "attachments": [{"name": "brief.md", "path": "brief.md"}],
            "client_message_id": "mention-1",
        },
    )
    assert mentioned.status_code == 201, mentioned.text
    body = mentioned.json()
    assert body["awakened_agent_ids"] == [
        str(env.leader_id),
        str(env.worker_id),
        str(env.reviewer_id),
    ]
    assert len(body["subagent_runs"]) == 3
    assert all(row["project_run_id"] for row in body["subagent_runs"])
    assert body["message"]["display_content"] == "Worker build and Reviewer check"
    worker_child = next(row for row in body["subagent_runs"] if row["agent_id"] == str(env.worker_id))
    worker_input = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == worker_child["session_id"],
                ChatMessage.message_meta["project_run_id"].as_string()
                == worker_child["project_run_id"],
            )
        )
    ).scalar_one()
    assert worker_input.content.startswith("[brief.md extracted]")

    replay = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Worker build and Reviewer check",
            "mentions": [str(env.worker_id), str(env.reviewer_id)],
            "client_message_id": "mention-1",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["subagent_runs"] == body["subagent_runs"]

    reused = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Worker follow-up", "mentions": [str(env.worker_id)]},
    )
    assert reused.status_code == 201, reused.text
    reused_worker = next(
        row
        for row in reused.json()["subagent_runs"]
        if row["agent_id"] == str(env.worker_id)
    )
    assert reused_worker["session_id"] == worker_child["session_id"]
    assert reused_worker["project_run_id"] != worker_child["project_run_id"]

    leader_deduped = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Leader and Worker only once",
            "mentions": [str(env.leader_id), str(env.worker_id), str(env.leader_id)],
        },
    )
    assert leader_deduped.status_code == 201, leader_deduped.text
    assert leader_deduped.json()["awakened_agent_ids"] == [
        str(env.leader_id),
        str(env.worker_id),
    ]

    spoofed_agent = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Agent-visible update only",
            "sender_agent_id": str(env.worker_id),
            "mentions": [],
        },
    )
    assert spoofed_agent.status_code == 422

    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    assert stored_project is not None
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "policies": {
            **dict((stored_project.settings or {}).get("policies") or {}),
            "max_a2a_wakes": 12,
        },
    }
    await env.db.commit()
    expanded_group = await env.client.get(f"/api/projects/{project_id}/group-session")
    assert expanded_group.json()["max_mentions"] == 4
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "policies": {
            **dict((stored_project.settings or {}).get("policies") or {}),
            "max_a2a_wakes": 2,
        },
    }
    await env.db.commit()
    limited_group = await env.client.get(f"/api/projects/{project_id}/group-session")
    assert limited_group.json()["max_mentions"] == 1
    over_budget = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Leader plus two participants exceeds total budget",
            "mentions": [str(env.worker_id), str(env.reviewer_id)],
        },
    )
    assert over_budget.status_code == 422

    self_mention = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "No self wake",
            "sender_agent_id": str(env.worker_id),
            "mentions": [str(env.worker_id)],
        },
    )
    assert self_mention.status_code == 422

    history = await env.client.get(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages?limit=500"
    )
    assert history.status_code == 200
    assert len(history.json()["items"]) == 4
    attachment_message = next(
        item for item in history.json()["items"] if item["attachments"]
    )
    assert attachment_message["attachments"][0]["name"] == "brief.md"


async def test_project_group_dispatch_outbox_recovers_on_idempotent_replay(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import ProjectRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Recoverable project dispatch")
    project_id = project["id"]
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    real_dispatch = subagent_runtime.dispatch_project_run

    async def simulated_process_exit(_run_id):
        raise RuntimeError("simulated exit after outbox commit")

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", simulated_process_exit)
    first = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "This message must survive a dispatcher exit",
            "mentions": [str(env.worker_id)],
            "client_message_id": "recoverable-group-message",
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["awakened_agent_ids"] == []
    pending = (
        await env.db.execute(
            select(ProjectRun).where(
                ProjectRun.project_id == uuid.UUID(project_id),
                ProjectRun.trigger_type.in_(["group_leader_message", "group_mention"]),
            )
        )
    ).scalars().all()
    assert len(pending) == 2
    assert all(row.status == "queued" and row.input["dispatch"]["task"] for row in pending)
    pending_ids = [row.id for row in pending]

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", real_dispatch)
    replay = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "This message must survive a dispatcher exit",
            "mentions": [str(env.worker_id)],
            "client_message_id": "recoverable-group-message",
        },
    )
    assert replay.status_code == 201, replay.text
    replay_body = replay.json()
    assert replay_body["idempotent_replay"] is True
    assert replay_body["awakened_agent_ids"] == [str(env.leader_id), str(env.worker_id)]
    assert len(replay_body["subagent_runs"]) == 2
    env.db.expire_all()
    recovered = (
        await env.db.execute(
            select(ProjectRun).where(ProjectRun.id.in_(pending_ids))
        )
    ).scalars().all()
    assert all(row.output["subagent_run_id"] for row in recovered)


async def test_planning_leader_session_never_gets_project_runtime_tools(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services.project_runtime_tools import (
        PROJECT_RUNTIME_TOOL_NAMES,
        execute_project_runtime_tool,
    )
    from app.services.subagent_runtime import prepare_subagent_tools

    env = project_api
    project = await _create_project(env, name="Planning scope isolation")
    project_id = project["id"]

    async def no_normal_tools(_agent_id):
        return []

    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", no_normal_tools)
    response = await env.client.get(f"/api/projects/{project_id}/leader-session")
    assert response.status_code == 200, response.text
    assert response.json()["source_channel"] == "web"

    planning_session_id = uuid.UUID(response.json()["id"])
    planning_tools = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.leader_id,
            planning_session_id,
            execution_user_id=env.owner_id,
        )
    }
    assert planning_tools.isdisjoint(PROJECT_RUNTIME_TOOL_NAMES)
    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await execute_project_runtime_tool(
            "project_get_context",
            {},
            agent_id=env.leader_id,
            execution_user_id=env.owner_id,
            session_id=str(planning_session_id),
            tool_call_id="planning-session-scope-bypass",
            turn_anchor_id=None,
        )


async def test_kickoff_requires_leader_discussion_then_freezes_and_starts(project_api: ProjectApiEnv):
    from app.models.project import Project, ProjectRun, ProjectRunMemberSnapshot
    from app.models.subagent_run import SubagentRun
    from app.services.subagent_runtime import prepare_subagent_tools

    env = project_api
    project = await _create_project(env, name="Plan before execution")
    project_id = project["id"]
    assert project["status"] == "planning"

    first = await env.client.get(f"/api/projects/{project_id}/leader-session")
    second = await env.client.get(f"/api/projects/{project_id}/leader-session")
    assert first.status_code == 200, first.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["source_channel"] == "web"
    assert first.json()["agent_id"] == str(env.leader_id)

    bypass = await env.client.patch(f"/api/projects/{project_id}", json={"status": "running"})
    assert bypass.status_code == 409

    no_discussion = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Start now"},
    )
    assert no_discussion.status_code == 422

    session_id = first.json()["id"]
    env.db.add_all(
        [
            ChatMessage(
                agent_id=env.leader_id,
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Plan the delivery around traceable evidence.",
                conversation_id=session_id,
                message_meta={"attachments": []},
            ),
            ChatMessage(
                agent_id=env.leader_id,
                user_id=env.owner_id,
                sender_agent_id=env.leader_id,
                role="assistant",
                content="I will coordinate the team and commit every deliverable.",
                conversation_id=session_id,
                message_meta={"attachments": []},
            ),
        ]
    )
    await env.db.commit()

    confirmed = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Approved. Execute this plan."},
    )
    assert confirmed.status_code == 202, confirmed.text
    body = confirmed.json()
    assert body["status"] == "running"
    assert body["leader_session_id"] == session_id
    assert body["awakened_agent_ids"] == [str(env.leader_id)]
    assert body["subagent_session_id"] == body["subagent_run_id"]
    assert body["git_start_commit"] != body["transcript_commit"]

    env.db.expire_all()
    stored_project = await env.db.get(Project, uuid.UUID(project_id))
    run = await env.db.get(ProjectRun, uuid.UUID(body["run_id"]))
    child = await env.db.get(SubagentRun, uuid.UUID(body["subagent_run_id"]))
    assert stored_project is not None and stored_project.status == "running"
    assert stored_project.settings["kickoff"]["project_run_id"] == body["run_id"]
    assert run is not None and run.trigger_type == "leader_kickoff"
    assert run.input["conversation_snapshot"]["message_count"] == 2
    assert run.input["conversation_snapshot"]["transcript_sha256"]
    assert run.output["transcript_commit"] == body["transcript_commit"]
    snapshots = (
        await env.db.execute(
            select(ProjectRunMemberSnapshot).where(ProjectRunMemberSnapshot.run_id == run.id)
        )
    ).scalars().all()
    assert len(snapshots) == 3
    assert child is not None and child.project_id == uuid.UUID(project_id)
    assert child.project_member_id == next(item.project_member_id for item in snapshots if item.is_leader)
    kickoff_tool_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.leader_id,
            child.id,
            execution_user_id=env.owner_id,
        )
    }
    assert {"project_get_context", "project_create_work_item", "project_update_plan"} <= kickoff_tool_names

    transcript = subprocess.run(
        [
            "git",
            "-C",
            str(project_repo_path(env.tenant_id, uuid.UUID(project_id))),
            "show",
            f"{body['transcript_commit']}:docs/kickoff-transcript.md",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Plan the delivery around traceable evidence." in transcript
    assert "I will coordinate the team" in transcript
    assert "Approved. Execute this plan." in transcript

    event = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == uuid.UUID(project_id),
                ProjectEvent.event_type == "project.kickoff.confirmed",
            )
        )
    ).scalar_one()
    assert event.run_id == run.id
    assert event.event_metadata["awakened_agent_ids"] == [str(env.leader_id)]
    assert event.event_metadata["transcript_commit"] == body["transcript_commit"]
    kickoff_group_message = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.external_event_key.like(f"project-kickoff:{project_id}:%")
            )
        )
    ).scalar_one()
    assert kickoff_group_message.message_meta["mentions"] == []
    assert kickoff_group_message.message_meta["awakened_agent_ids"] == [str(env.leader_id)]

    repeated = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Do not start twice"},
    )
    assert repeated.status_code == 409


async def test_kickoff_accepts_human_leader_discussion_from_project_group(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Group planning source")
    group = (await env.client.get(f"/api/projects/{project['id']}/group-session")).json()
    env.db.add_all(
        [
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Keep the plan small and make every result traceable.",
                conversation_id=group["id"],
                message_meta={"kind": "project_group_message", "visible_to_group": True},
            ),
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                sender_agent_id=env.leader_id,
                role="assistant",
                content="I will deliver in two milestones with Git evidence.",
                conversation_id=group["id"],
                message_meta={"kind": "project_subagent_reply", "visible_to_group": True},
            ),
            # Participant replies remain in the group audit log but are not
            # part of the Human/Leader agreement frozen at kickoff.
            ChatMessage(
                agent_id=uuid.UUID(group["access_agent_id"]),
                sender_agent_id=env.worker_id,
                role="assistant",
                content="Worker side note",
                conversation_id=group["id"],
                message_meta={"kind": "project_subagent_reply", "visible_to_group": True},
            ),
        ]
    )
    await env.db.commit()

    confirmed = await env.client.post(
        f"/api/projects/{project['id']}/kickoff/confirm",
        json={"confirmation": "方案确认，开始执行。"},
    )
    assert confirmed.status_code == 202, confirmed.text
    payload = confirmed.json()
    assert payload["status"] == "running"
    assert payload["discussion_source"] == "project_group"
    assert payload["discussion_session_id"] == group["id"]

    run = await env.db.get(ProjectRun, uuid.UUID(payload["run_id"]))
    assert run is not None
    assert run.input["conversation_snapshot"]["source"] == "project_group"
    assert run.input["conversation_snapshot"]["session_id"] == group["id"]
    assert run.input["conversation_snapshot"]["message_count"] == 2

    transcript = subprocess.run(
        [
            "git",
            "-C",
            str(project_repo_path(env.tenant_id, uuid.UUID(project["id"]))),
            "show",
            f"{payload['transcript_commit']}:docs/kickoff-transcript.md",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Keep the plan small" in transcript
    assert "I will deliver in two milestones" in transcript
    assert "Worker side note" not in transcript


async def test_kickoff_outbox_recovers_initializing_project(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import Project, ProjectEvent, ProjectRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Recoverable kickoff")
    project_id = project["id"]
    leader_session = (await env.client.get(f"/api/projects/{project_id}/leader-session")).json()
    env.db.add_all(
        [
            ChatMessage(
                agent_id=env.leader_id,
                user_id=env.owner_id,
                sender_user_id=env.owner_id,
                role="user",
                content="Agree the plan before autonomous execution.",
                conversation_id=leader_session["id"],
                message_meta={"attachments": []},
            ),
            ChatMessage(
                agent_id=env.leader_id,
                sender_agent_id=env.leader_id,
                role="assistant",
                content="The traceable plan is ready for confirmation.",
                conversation_id=leader_session["id"],
                message_meta={"attachments": []},
            ),
        ]
    )
    await env.db.commit()
    real_dispatch = subagent_runtime.dispatch_project_run

    async def simulated_process_exit(_run_id):
        raise RuntimeError("simulated exit after kickoff outbox commit")

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", simulated_process_exit)
    first = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "Confirmed once"},
    )
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "initializing"
    run_id = uuid.UUID(first.json()["run_id"])
    stored = await env.db.get(Project, uuid.UUID(project_id))
    pending = await env.db.get(ProjectRun, run_id)
    assert stored is not None and stored.status == "initializing"
    assert pending is not None and pending.input["dispatch"]["task"]

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", real_dispatch)
    recovered = await env.client.post(
        f"/api/projects/{project_id}/kickoff/confirm",
        json={"confirmation": "A retry must recover, not start twice"},
    )
    assert recovered.status_code == 202, recovered.text
    assert recovered.json()["status"] == "running"
    assert uuid.UUID(recovered.json()["run_id"]) == run_id
    env.db.expire_all()
    stored = await env.db.get(Project, uuid.UUID(project_id))
    events = (
        await env.db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == uuid.UUID(project_id),
                ProjectEvent.event_type == "project.kickoff.confirmed",
            )
        )
    ).scalars().all()
    assert stored is not None and stored.status == "running"
    assert len(events) == 1


async def test_project_subagent_reply_materializes_without_resuming_group_root(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectRun
    from app.models.subagent_run import SubagentRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Passive child reply")
    group = (await env.client.get(f"/api/projects/{project['id']}/group-session")).json()
    wake = await env.client.post(
        f"/api/projects/{project['id']}/group-sessions/{group['id']}/messages",
        json={"content": "Worker answer once", "mentions": [str(env.worker_id)]},
    )
    assert wake.status_code == 201, wake.text
    worker_wake = next(
        row
        for row in wake.json()["subagent_runs"]
        if row["agent_id"] == str(env.worker_id)
    )
    child_id = uuid.UUID(worker_wake["session_id"])
    project_run_id = uuid.UUID(worker_wake["project_run_id"])
    child_input = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
            )
        )
    ).scalar_one()
    child_input.message_meta = {
        **dict(child_input.message_meta or {}),
        "subagent_input_state": "processing",
        "subagent_turn_anchor_id": str(child_input.id),
        "turn_status": "running",
    }
    durable_run = await env.db.get(SubagentRun, child_id)
    assert durable_run is not None
    durable_run.status = "running"
    durable_run.lease_owner = subagent_runtime.settings.INSTANCE_ID
    await env.db.commit()

    terminal = await subagent_runtime._finish_subagent_turn(
        run_id=child_id,
        anchor_id=child_input.id,
        reply="Worker result",
        failed=False,
    )
    assert terminal is True
    env.db.expire_all()
    project_run = await env.db.get(ProjectRun, project_run_id)
    assert project_run is not None and project_run.status == "succeeded"
    assert project_run.finished_at is not None
    assert project_run.output["subagent_session_id"] == str(child_id)
    assert project_run.output["result"] == "Worker result"
    completion = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["kind"].as_string() == "subagent_completion",
            )
        )
    ).scalar_one()

    async def forbidden_resume(_anchor):
        raise AssertionError("project group completion must not resume root LLM")

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", forbidden_resume)
    assert await subagent_runtime._dispatch_parent_event(completion.id) is True
    materialized = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.external_event_key == f"project-subagent:{completion.id}"
            )
        )
    ).scalar_one()
    assert materialized.conversation_id == group["id"]
    assert materialized.sender_agent_id == env.worker_id
    assert materialized.message_meta["visible_to_group"] is True
    assert materialized.message_meta["mentions"] == []
    assert materialized.message_meta["awakened_agent_ids"] == []
    assert materialized.message_meta["leader_batch_state"] == "pending"
    assert materialized.message_meta["default_leader_agent_id"] == str(env.leader_id)
    parent = await env.db.get(ChatSession, uuid.UUID(group["id"]))
    assert parent is not None and parent.source_channel == "project"


async def test_project_participant_replies_coalesce_into_one_durable_leader_turn(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.audit import ChatMessage
    from app.models.project import ProjectRun
    from app.models.subagent_run import SubagentRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Coalesced Leader inbox")
    project_id = uuid.UUID(project["id"])
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={
            "content": "Ask two participants and let Leader coordinate results",
            "mentions": [str(env.worker_id), str(env.reviewer_id)],
        },
    )
    assert wake.status_code == 201, wake.text
    run_by_agent = {row["agent_id"]: row for row in wake.json()["subagent_runs"]}
    leader_child_id = uuid.UUID(run_by_agent[str(env.leader_id)]["session_id"])
    worker_child_id = uuid.UUID(run_by_agent[str(env.worker_id)]["session_id"])
    reviewer_child_id = uuid.UUID(run_by_agent[str(env.reviewer_id)]["session_id"])

    completion_rows = [
        ChatMessage(
            agent_id=agent_id,
            sender_agent_id=agent_id,
            role="assistant",
            content=content,
            conversation_id=str(child_id),
            message_meta={
                "kind": "subagent_completion",
                "subagent_wake": True,
                "project_run_ids": [project_run_id],
                "attachments": attachments,
            },
        )
        for agent_id, child_id, content, project_run_id, attachments in [
            (
                env.worker_id,
                worker_child_id,
                "Worker evidence A",
                run_by_agent[str(env.worker_id)]["project_run_id"],
                [{"name": "evidence-a.md", "path": "evidence-a.md"}],
            ),
            (
                env.reviewer_id,
                reviewer_child_id,
                "Reviewer finding B",
                run_by_agent[str(env.reviewer_id)]["project_run_id"],
                [],
            ),
            (
                env.worker_id,
                worker_child_id,
                "Worker follow-up C",
                run_by_agent[str(env.worker_id)]["project_run_id"],
                [],
            ),
        ]
    ]
    env.db.add_all(completion_rows)
    await env.db.commit()

    async def forbidden_resume(_anchor):
        raise AssertionError("participant replies must not resume the project group root")

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", forbidden_resume)
    for completion in completion_rows:
        assert await subagent_runtime._dispatch_parent_event(completion.id) is True

    assert await subagent_runtime._dispatch_project_leader_batch(
        uuid.UUID(group["id"]),
        debounce_seconds=0,
    ) is True
    env.db.expire_all()
    leader_inputs = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(leader_child_id),
                ChatMessage.message_meta["project_leader_batch"].as_boolean().is_(True),
            )
        )
    ).scalars().all()
    assert len(leader_inputs) == 1
    batch_input = leader_inputs[0]
    assert len(batch_input.message_meta["source_group_message_ids"]) == 3
    assert len(batch_input.message_meta["source_replies"]) == 3
    batch_attachments = [
        attachment
        for reply in batch_input.message_meta["source_replies"]
        for attachment in reply["attachments"]
    ]
    assert batch_attachments[0]["name"] == "evidence-a.md"

    materialized = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == group["id"],
                ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
            )
        )
    ).scalars().all()
    assert len(materialized) == 3
    assert {row.message_meta["leader_batch_state"] for row in materialized} == {"delivered"}
    assert len({row.message_meta["leader_batch_id"] for row in materialized}) == 1

    batch_runs = (
        await env.db.execute(
            select(ProjectRun).where(
                ProjectRun.project_id == project_id,
                ProjectRun.trigger_type == "leader_reply_batch",
            )
        )
    ).scalars().all()
    assert len(batch_runs) == 1
    assert batch_runs[0].output["subagent_session_id"] == str(leader_child_id)
    assert batch_runs[0].output["source_count"] == 3
    leader_run = await env.db.get(SubagentRun, leader_child_id)
    assert leader_run is not None and leader_run.parent_session_id == uuid.UUID(group["id"])

    assert await subagent_runtime._dispatch_project_leader_batch(
        uuid.UUID(group["id"]),
        debounce_seconds=0,
    ) is False
    env.db.expire_all()
    replay_inputs = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(leader_child_id),
                ChatMessage.message_meta["project_leader_batch"].as_boolean().is_(True),
            )
        )
    ).scalars().all()
    assert len(replay_inputs) == 1


async def test_project_runtime_tools_are_role_projected_and_double_enforced(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.chat_session import ChatSession
    from app.services.agent_tools import execute_tool
    from app.services.project_runtime_tools import execute_project_runtime_tool
    from app.services.subagent_runtime import prepare_subagent_tools

    env = project_api

    async def no_normal_tools(_agent_id):
        return []

    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", no_normal_tools)
    project = await _create_project(env, name="Role projected tools")
    project_id = project["id"]
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()

    assigned = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": "Worker item",
            "description": "Worker-owned delivery",
            "assignee_agent_id": str(env.worker_id),
            "acceptance_criteria": ["Evidence attached"],
        },
    )
    other = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": "Reviewer item",
            "description": "Reviewer-owned delivery",
            "assignee_agent_id": str(env.reviewer_id),
            "acceptance_criteria": ["Review complete"],
        },
    )
    assert assigned.status_code == other.status_code == 201

    worker_wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Worker runtime", "mentions": [str(env.worker_id)]},
    )
    leader_wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Leader runtime", "mentions": [str(env.leader_id)]},
    )
    assert worker_wake.status_code == leader_wake.status_code == 201
    worker_child_id = uuid.UUID(
        next(
            row
            for row in worker_wake.json()["subagent_runs"]
            if row["agent_id"] == str(env.worker_id)
        )["session_id"]
    )
    leader_child_id = uuid.UUID(
        next(
            row
            for row in leader_wake.json()["subagent_runs"]
            if row["agent_id"] == str(env.leader_id)
        )["session_id"]
    )

    worker_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.worker_id,
            worker_child_id,
            execution_user_id=env.owner_id,
        )
    }
    leader_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.leader_id,
            leader_child_id,
            execution_user_id=env.owner_id,
        )
    }
    assert {"project_get_context", "project_list_work_items", "project_list_files", "project_read_file"} <= worker_names
    assert {"project_update_work_item", "project_write_file", "project_message_agent"} <= worker_names
    assert "project_create_work_item" not in worker_names
    assert "project_update_plan" not in worker_names
    assert {"project_create_work_item", "project_update_plan", "project_restore_commit"} <= leader_names

    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await prepare_subagent_tools(
            env.worker_id,
            worker_child_id,
            execution_user_id=env.viewer_id,
        )
    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await prepare_subagent_tools(
            env.reviewer_id,
            worker_child_id,
            execution_user_id=env.owner_id,
        )
    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await execute_project_runtime_tool(
            "project_get_context",
            {},
            agent_id=env.worker_id,
            execution_user_id=env.viewer_id,
            session_id=str(worker_child_id),
            tool_call_id="wrong-execution-user",
            turn_anchor_id=None,
        )

    settings_update = await env.client.patch(
        f"/api/projects/{project_id}/settings",
        json={"policies": {"project_tools": {"participant_disabled": ["project_write_file"]}}},
    )
    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    worker_member = next(item for item in members if item["agent_id"] == str(env.worker_id))
    member_update = await env.client.patch(
        f"/api/projects/{project_id}/members/{worker_member['id']}",
        json={
            "config_snapshot": {
                **worker_member["config_snapshot"],
                "disabled_project_tools": ["project_message_agent"],
            }
        },
    )
    assert settings_update.status_code == member_update.status_code == 200
    projected_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.worker_id,
            worker_child_id,
            execution_user_id=env.owner_id,
        )
    }
    assert "project_write_file" not in projected_names
    assert "project_message_agent" not in projected_names
    with pytest.raises(ValueError, match="not allowed"):
        await execute_project_runtime_tool(
            "project_write_file",
            {"path": "docs/blocked.md", "content": "must not commit"},
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(worker_child_id),
            tool_call_id="worker-policy-bypass",
            turn_anchor_id=None,
        )

    updated = await execute_project_runtime_tool(
        "project_update_work_item",
        {
            "work_item_id": assigned.json()["id"],
            "status": "in_progress",
            "progress_note": "Implementation started",
            "evidence": ["docs/progress.md"],
        },
        agent_id=env.worker_id,
        execution_user_id=env.owner_id,
        session_id=str(worker_child_id),
        tool_call_id="worker-update",
        turn_anchor_id=None,
    )
    assert json.loads(updated)["status"] == "in_progress"

    with pytest.raises(ValueError, match="only status, progress_note and evidence"):
        await execute_project_runtime_tool(
            "project_update_work_item",
            {"work_item_id": assigned.json()["id"], "title": "Privilege escalation"},
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(worker_child_id),
            tool_call_id="worker-title",
            turn_anchor_id=None,
        )
    with pytest.raises(ValueError, match="assigned to themselves"):
        await execute_project_runtime_tool(
            "project_update_work_item",
            {"work_item_id": other.json()["id"], "status": "done"},
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(worker_child_id),
            tool_call_id="worker-other",
            turn_anchor_id=None,
        )

    denied = await execute_tool(
        "project_update_plan",
        {"goal": "Participant must not change this"},
        env.worker_id,
        env.owner_id,
        session_id=str(worker_child_id),
        tool_call_id="worker-plan",
    )
    assert denied.startswith("❌")
    context = await execute_tool(
        "project_get_context",
        {},
        env.worker_id,
        env.owner_id,
        session_id=str(worker_child_id),
        tool_call_id="worker-context",
    )
    assert json.loads(context)["id"] == project_id
    assert await env.db.get(ChatSession, worker_child_id) is not None


async def test_work_item_mutations_update_dashboard_and_audit(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Work item audit")
    project_id = project["id"]

    parent = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={"title": "Prepare evidence", "priority": "high"},
    )
    assert parent.status_code == 201, parent.text
    child = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": "Review evidence",
            "parent_id": parent.json()["id"],
            "dependency_ids": [parent.json()["id"]],
            "assignee_agent_id": str(env.reviewer.id),
            "status": "todo",
        },
    )
    assert child.status_code == 201, child.text
    completed = await env.client.patch(
        f"/api/projects/{project_id}/work-items/{parent.json()['id']}",
        json={"status": "done", "priority": "urgent"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "done"

    dashboard = (await env.client.get(f"/api/projects/{project_id}/dashboard")).json()
    assert dashboard["progress"] == 50
    assert dashboard["work_item_counts"] == {"done": 1, "todo": 1}
    assert len(dashboard["work_items"]) == 2

    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    created = [event for event in events if event["event_type"] == "work_item.created"]
    updated = next(event for event in events if event["event_type"] == "work_item.updated")
    assert len(created) == 2
    assert updated["work_item_id"] == parent.json()["id"]
    assert updated["event_metadata"]["after"]["status"] == "done"
    assert {"status", "priority"} <= set(updated["event_metadata"]["changed_fields"])


async def test_file_commits_restore_branch_and_milestone_preserve_history(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Git evidence")
    project_id = project["id"]

    initial_git = await env.client.get(f"/api/projects/{project_id}/git")
    assert initial_git.status_code == 200, initial_git.text
    initial_head = initial_git.json()["head"]
    assert {"README.md", "PROJECT.json"} <= set(initial_git.json()["files"])

    write_response = await env.client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "deliverables/report.md", "content": "# Evidence\n\nversion one\n"},
    )
    assert write_response.status_code == 200, write_response.text
    write_commit = write_response.json()["commit"]
    assert write_commit != initial_head

    for unsafe_path in ("../outside.md", ".git/config", ".GIT/config", "deliverables//hidden.md"):
        rejected = await env.client.put(
            f"/api/projects/{project_id}/files",
            json={"path": unsafe_path, "content": "must not be written"},
        )
        assert rejected.status_code == 422, rejected.text

    unchanged = await env.client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "deliverables/report.md", "content": "# Evidence\n\nversion one\n"},
    )
    assert unchanged.status_code == 409

    files_response = await env.client.get(f"/api/projects/{project_id}/files")
    assert files_response.status_code == 200, files_response.text
    files_payload = files_response.json()
    files = files_payload["files"] if isinstance(files_payload, dict) else files_payload
    assert any(
        (item == "deliverables/report.md")
        or (isinstance(item, dict) and item.get("path") == "deliverables/report.md")
        for item in files
    )

    milestone = await env.client.post(
        f"/api/projects/{project_id}/git/commit",
        json={"message": "Acceptance milestone", "milestone": True},
    )
    assert milestone.status_code == 200, milestone.text
    milestone_commit = milestone.json()["commit"]

    restore = await env.client.post(
        f"/api/projects/{project_id}/git/restore",
        json={"commit": initial_head, "message": "Restore initial project tree"},
    )
    assert restore.status_code == 200, restore.text
    restore_commit = restore.json()["commit"]
    assert restore_commit not in {initial_head, write_commit, milestone_commit}

    branch = await env.client.post(
        f"/api/projects/{project_id}/git/branches",
        json={"name": "review/version-one", "from_commit": write_commit},
    )
    assert branch.status_code == 200, branch.text
    assert branch.json()["from_commit"] == write_commit

    final_git = (await env.client.get(f"/api/projects/{project_id}/git?limit=20")).json()
    commit_ids = {item["commit"] for item in final_git["commits"]}
    assert {initial_head, write_commit, milestone_commit, restore_commit} <= commit_ids
    assert "review/version-one" in final_git["branches"]

    # Prove the old commits still resolve in the real repository: restore is a
    # new commit, never reset/force-push history rewriting.
    repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    for commit in (initial_head, write_commit, milestone_commit, restore_commit):
        resolved = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False,
            capture_output=True,
        )
        assert resolved.returncode == 0

    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    event_types = {event["event_type"] for event in events}
    assert {
        "project.file.committed",
        "git.milestone.created",
        "git.restore_commit.created",
        "git.branch.created",
    } <= event_types

    db_events = (
        await env.db.execute(select(ProjectEvent).where(ProjectEvent.project_id == uuid.UUID(project_id)))
    ).scalars().all()
    assert len(db_events) == len(events)
    assert any(event.event_metadata.get("commit") == restore_commit for event in db_events)


async def test_project_head_file_preview_media_range_and_acl(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="HEAD file previews")
    project_id = project["id"]
    repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    (repo / "assets").mkdir()
    binary = b"\x00\x01\x02\x03\x04\x05\xff\x10"
    (repo / "assets" / "sample.mp4").write_bytes(binary)
    (repo / "large.txt").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "--", "assets/sample.mp4", "large.txt"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "Add preview fixtures"],
        check=True,
        capture_output=True,
    )

    env.authenticate_as(env.viewer_id)
    hidden = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "assets/sample.mp4"},
    )
    assert hidden.status_code == 404

    env.authenticate_as(env.owner_id)
    for unsafe_path in ("../README.md", ".git/config", "nested/.GIT/config", "assets//sample.mp4"):
        rejected = await env.client.get(
            f"/api/projects/{project_id}/files/content",
            params={"path": unsafe_path},
        )
        assert rejected.status_code == 422, rejected.text

    listing = (await env.client.get(f"/api/projects/{project_id}/files")).json()
    media_item = next(item for item in listing if item["path"] == "assets/sample.mp4")
    assert media_item["kind"] == "video"
    assert media_item["mime_type"] == "video/mp4"
    assert media_item["preview"] == ""
    assert media_item["is_editable"] is False
    large_item = next(item for item in listing if item["path"] == "large.txt")
    assert large_item["kind"] == "text"
    assert large_item["is_editable"] is False

    media = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "assets/sample.mp4"},
    )
    assert media.status_code == 200, media.text
    media_payload = media.json()
    assert media_payload["content"] is None
    assert media_payload["raw_url"].startswith(f"/api/projects/{project_id}/files/raw?")

    partial = await env.client.get(media_payload["raw_url"], headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206, partial.text
    assert partial.content == binary[2:6]
    assert partial.headers["content-type"] == "video/mp4"
    assert partial.headers["content-range"] == f"bytes 2-5/{len(binary)}"
    assert partial.headers["accept-ranges"] == "bytes"
    assert partial.headers["content-disposition"].startswith("inline;")

    downloaded = await env.client.get(media_payload["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == binary
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    raw_head = await env.client.head(media_payload["raw_url"])
    assert raw_head.status_code == 200
    assert raw_head.content == b""
    assert raw_head.headers["content-length"] == str(len(binary))
    invalid_range = await env.client.get(media_payload["raw_url"], headers={"Range": "bytes=99-100"})
    assert invalid_range.status_code == 416
    assert invalid_range.headers["content-range"] == f"bytes */{len(binary)}"

    large = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "large.txt", "max_chars": 64},
    )
    assert large.status_code == 200
    assert large.json()["content"] == "x" * 64
    assert large.json()["truncated"] is True
    assert large.json()["is_editable"] is False

    shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "shared", "shared_with_user_ids": [str(env.viewer_id)]},
    )
    assert shared.status_code == 200
    env.authenticate_as(env.viewer_id)
    viewer_content = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "assets/sample.mp4"},
    )
    assert viewer_content.status_code == 200

    env.authenticate_as(env.owner_id)
    revoked = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "private", "shared_with_user_ids": []},
    )
    assert revoked.status_code == 200
    # Raw tickets are re-authorized on every request, not bearer URLs that
    # outlive revoked project access.
    old_viewer_raw = await env.client.get(viewer_content.json()["raw_url"])
    assert old_viewer_raw.status_code == 404


async def test_owner_manages_provider_neutral_remotes_and_atomically_clones(
    project_api: ProjectApiEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services import project_git_service

    async def allow_local_git_fixture(raw_url: str) -> str:
        return project_git_service._safe_remote_url(raw_url)

    monkeypatch.setattr(project_git_service, "validate_project_remote_url", allow_local_git_fixture)
    env = project_api
    project = await _create_project(env, name="Clone target")
    project_id = project["id"]
    initial = (await env.client.get(f"/api/projects/{project_id}/git")).json()

    empty = await env.client.get(f"/api/projects/{project_id}/git/remotes")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    configured = await env.client.put(
        f"/api/projects/{project_id}/git/remotes/upstream",
        json={"url": "https://example.com/vendor/project.git"},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["created"] is True
    assert (await env.client.get(f"/api/projects/{project_id}/git")).json()["head"] == initial["head"]
    assert (await env.client.get(f"/api/projects/{project_id}/git/remotes")).json()["items"] == [
        {"name": "upstream", "url": "https://example.com/vendor/project.git"}
    ]
    ssh_remote = await env.client.put(
        f"/api/projects/{project_id}/git/remotes/ssh-upstream",
        json={"url": "ssh://git@example.com/vendor/project.git"},
    )
    assert ssh_remote.status_code == 200, ssh_remote.text
    assert ssh_remote.json()["url"] == "ssh://git@example.com/vendor/project.git"
    scp_remote = await env.client.put(
        f"/api/projects/{project_id}/git/remotes/scp-upstream",
        json={"url": "git@example.com:vendor/project.git"},
    )
    assert scp_remote.status_code == 200, scp_remote.text

    for name, url in [
        ("origin", "file:///tmp/repository.git"),
        ("origin", "/tmp/repository.git"),
        ("origin", "https://token@example.com/vendor/project.git"),
        ("origin", "ssh://git:secret@example.com/vendor/project.git"),
        ("origin", "ssh://git@example.com/vendor/$(touch-pwned).git"),
        ("origin", "ssh://git@example.com/vendor/repo.git;touch-pwned"),
        ("origin", "ssh://git@example.com/vendor/%24%28touch-pwned%29.git"),
        ("origin", "ssh://git@example.com/vendor/../private.git"),
        ("origin", "git@example.com:vendor/project.git;touch-pwned"),
        ("origin", "git@example.com:vendor/$(touch-pwned).git"),
        ("origin", "git@example.com:vendor/../private.git"),
        ("--upload-pack", "https://example.com/vendor/project.git"),
    ]:
        rejected = await env.client.put(
            f"/api/projects/{project_id}/git/remotes/{name}",
            json={"url": url},
        )
        assert rejected.status_code == 422, rejected.text

    env.authenticate_as(env.viewer_id)
    hidden = await env.client.get(f"/api/projects/{project_id}/git/remotes")
    assert hidden.status_code == 404
    env.authenticate_as(env.owner_id)
    deleted = await env.client.delete(f"/api/projects/{project_id}/git/remotes/upstream")
    assert deleted.status_code == 200, deleted.text
    deleted_ssh = await env.client.delete(f"/api/projects/{project_id}/git/remotes/ssh-upstream")
    assert deleted_ssh.status_code == 200, deleted_ssh.text
    deleted_scp = await env.client.delete(f"/api/projects/{project_id}/git/remotes/scp-upstream")
    assert deleted_scp.status_code == 200, deleted_scp.text

    source = tmp_path / "source"
    bare = tmp_path / "served" / "source.git"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Source Author"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "source@example.test"], check=True)
    (source / "SOURCE.md").write_text("# Imported source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "--", "SOURCE.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "Imported baseline"], check=True)
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "--bare", "--", str(source), str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(bare), "update-server-info"], check=True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(bare.parent),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _attempt in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise AssertionError("local Git HTTP fixture did not start")
        clone_url = f"http://127.0.0.1:{port}/source.git"

        # A prepared journal survives the exact process-exit window after the
        # filesystem swap and restores the initialization repository on access.
        from app.models.project import ProjectRepositoryOperation

        model = await env.db.get(Project, uuid.UUID(project_id))
        assert model is not None
        staged_only = await project_git_service.begin_project_repository_clone(model, clone_url, "main")
        env.db.add(
            ProjectRepositoryOperation(
                id=staged_only.id,
                tenant_id=model.tenant_id,
                project_id=model.id,
                operation_type="clone",
                state="prepared",
                old_head=staged_only.old_head,
                new_head=staged_only.new_head,
                backup_name=staged_only.backup.name,
                staging_name=staged_only.staging_root.name,
            )
        )
        await env.db.commit()
        assert staged_only.staging_root.exists()
        assert staged_only.lock_handle is not None
        staged_only.lock_handle.close()  # Simulate exit before the filesystem swap.
        staged_only.lock_handle = None
        assert await project_git_service.reconcile_project_repository_operations(
            model.id,
            db=env.db,
        ) == 1
        await env.db.commit()
        assert not staged_only.staging_root.exists()
        assert not (staged_only.repo / "SOURCE.md").exists()

        abandoned = await project_git_service.begin_project_repository_clone(model, clone_url, "main")
        env.db.add(
            ProjectRepositoryOperation(
                id=abandoned.id,
                tenant_id=model.tenant_id,
                project_id=model.id,
                operation_type="clone",
                state="prepared",
                old_head=abandoned.old_head,
                new_head=abandoned.new_head,
                backup_name=abandoned.backup.name,
                staging_name=abandoned.staging_root.name,
            )
        )
        await env.db.commit()
        await project_git_service.apply_project_repository_clone(abandoned)
        assert (abandoned.repo / "SOURCE.md").exists()
        assert abandoned.lock_handle is not None
        abandoned.lock_handle.close()  # Simulate OS releasing flock on process death.
        abandoned.lock_handle = None
        assert await project_git_service.reconcile_project_repository_operations(
            model.id,
            db=env.db,
        ) == 1
        await env.db.commit()
        assert not (abandoned.repo / "SOURCE.md").exists()
        assert list(abandoned.repo.parent.glob(".repo-backup-*")) == []
        assert (await env.db.execute(select(ProjectRepositoryOperation))).scalars().all() == []

        # The repository swap stays compensatable until settings and the
        # audit row are durable. A database failure restores the one-commit
        # initialization baseline and leaves the endpoint safely retryable.
        original_commit = AsyncSession.commit
        clone_commit_count = {"value": 0}

        async def fail_clone_commit_once(self: AsyncSession):
            if self is env.db:
                clone_commit_count["value"] += 1
            if self is env.db and clone_commit_count["value"] == 2:
                raise RuntimeError("forced clone metadata commit failure")
            return await original_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", fail_clone_commit_once)
        with pytest.raises(RuntimeError, match="forced clone metadata commit failure"):
            await env.client.post(
                f"/api/projects/{project_id}/git/clone",
                json={"url": clone_url, "branch": "main"},
            )
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

        restored_repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
        restored_files = subprocess.run(
            ["git", "-C", str(restored_repo), "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert set(restored_files) == {"PROJECT.json", "README.md"}
        assert not (restored_repo / "SOURCE.md").exists()
        assert subprocess.run(
            ["git", "-C", str(restored_repo), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == "Initialize AI-native project"
        assert list(restored_repo.parent.glob(".repo-backup-*")) == []
        failed_settings = (await env.client.get(f"/api/projects/{project_id}/settings")).json()
        assert failed_settings["git"].get("source") != "cloned"
        failed_events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
        assert "git.repository.cloned" not in {event["event_type"] for event in failed_events}

        # A transport error after the database accepted COMMIT must not roll
        # the repository back to a state that contradicts durable metadata.
        tenant = await env.db.get(Tenant, env.tenant_id)
        assert tenant is not None
        ambiguous_leader = await _agent(env.db, tenant, env.owner, "Clone Leader", "Drive clone recovery")
        await env.db.commit()
        ambiguous_project = await _create_project(
            env,
            name="Ambiguous clone commit",
            leader_id=ambiguous_leader.id,
        )
        ambiguous_project_id = ambiguous_project["id"]
        ambiguous_commit_count = {"value": 0}

        async def commit_then_report_disconnect(self: AsyncSession):
            if self is env.db:
                ambiguous_commit_count["value"] += 1
            if self is env.db and ambiguous_commit_count["value"] == 2:
                await original_commit(self)
                raise RuntimeError("simulated disconnect after metadata commit")
            return await original_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", commit_then_report_disconnect)
        with pytest.raises(RuntimeError, match="simulated disconnect after metadata commit"):
            await env.client.post(
                f"/api/projects/{ambiguous_project_id}/git/clone",
                json={"url": clone_url, "branch": "main"},
            )
        monkeypatch.setattr(AsyncSession, "commit", original_commit)
        ambiguous_repo = project_repo_path(env.tenant_id, uuid.UUID(ambiguous_project_id))
        assert (ambiguous_repo / "SOURCE.md").exists()
        ambiguous_settings = (await env.client.get(f"/api/projects/{ambiguous_project_id}/settings")).json()
        assert ambiguous_settings["git"]["source"] == "cloned"
        ambiguous_events = (
            await env.client.get(f"/api/projects/{ambiguous_project_id}/events?limit=200")
        ).json()
        assert "git.repository.cloned" in {event["event_type"] for event in ambiguous_events}
        ambiguous_journal = (
            await env.db.execute(
                select(ProjectRepositoryOperation).where(
                    ProjectRepositoryOperation.project_id == uuid.UUID(ambiguous_project_id)
                )
            )
        ).scalar_one()
        assert ambiguous_journal.state == "committed"
        recovered_ambiguous_git = await env.client.get(f"/api/projects/{ambiguous_project_id}/git")
        assert recovered_ambiguous_git.status_code == 200
        assert (
            await env.db.execute(
                select(ProjectRepositoryOperation).where(
                    ProjectRepositoryOperation.project_id == uuid.UUID(ambiguous_project_id)
                )
            )
        ).scalars().all() == []

        original_finalize = projects_api.finalize_project_repository_clone

        async def simulate_exit_after_metadata_commit(_operation):
            assert _operation.lock_handle is not None
            _operation.lock_handle.close()  # Process exit releases the kernel flock.
            _operation.lock_handle = None
            raise RuntimeError("simulated exit before clone backup cleanup")

        monkeypatch.setattr(projects_api, "finalize_project_repository_clone", simulate_exit_after_metadata_commit)
        cloned = await env.client.post(
            f"/api/projects/{project_id}/git/clone",
            json={"url": clone_url, "branch": "main"},
        )
        assert cloned.status_code == 200, cloned.text
        assert cloned.json()["operation"] == "clone"
        assert cloned.json()["default_branch"] == "main"
        imported_repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
        assert (imported_repo / "SOURCE.md").read_text(encoding="utf-8") == "# Imported source\n"
        committed_journal = (await env.db.execute(select(ProjectRepositoryOperation))).scalar_one()
        assert committed_journal.state == "committed"
        assert list(imported_repo.parent.glob(".repo-backup-*"))
        monkeypatch.setattr(projects_api, "finalize_project_repository_clone", original_finalize)
        recovered_git = await env.client.get(f"/api/projects/{project_id}/git")
        assert recovered_git.status_code == 200, recovered_git.text
        assert recovered_git.json()["head"] == cloned.json()["head"]
        assert (await env.db.execute(select(ProjectRepositoryOperation))).scalars().all() == []
        assert list(imported_repo.parent.glob(".repo-backup-*")) == []
        settings = (await env.client.get(f"/api/projects/{project_id}/settings")).json()
        assert settings["git"]["mode"] == "managed"
        assert settings["git"]["repository_mode"] == "managed"
        assert settings["git"]["source"] == "cloned"
        assert settings["git"]["remotes"] == [
            {
                "name": "origin",
                "url_sha256": hashlib.sha256(clone_url.encode("utf-8")).hexdigest(),
            }
        ]
        assert settings["git"]["remote_count"] == 1
        second_clone = await env.client.post(
            f"/api/projects/{project_id}/git/clone",
            json={"url": clone_url, "branch": "main"},
        )
        assert second_clone.status_code == 409

        running_model = await env.db.get(Project, uuid.UUID(project_id))
        assert running_model is not None
        running_model.status = "running"
        await env.db.commit()
        running_clone = await env.client.post(
            f"/api/projects/{project_id}/git/clone",
            json={"url": clone_url, "branch": "main"},
        )
        assert running_clone.status_code == 409
        assert "planning" in running_clone.json()["detail"]
    finally:
        server.terminate()
        server.wait(timeout=5)

    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    assert {
        "git.remote.configured",
        "git.remote.deleted",
        "git.repository.cloned",
    } <= {event["event_type"] for event in events}

    configured_event = next(
        event
        for event in events
        if event["event_type"] == "git.remote.configured"
        and event["event_metadata"]["remote_name"] == "upstream"
    )
    assert configured_event["event_metadata"]["remote_name"] == "upstream"
    assert configured_event["event_metadata"]["remote_url_sha256"] == hashlib.sha256(
        b"https://example.com/vendor/project.git"
    ).hexdigest()
    cloned_event = next(event for event in events if event["event_type"] == "git.repository.cloned")
    assert cloned_event["event_metadata"]["remote_name"] == "origin"
    assert cloned_event["event_metadata"]["remote_url_sha256"] == hashlib.sha256(
        clone_url.encode("utf-8")
    ).hexdigest()

    # Audit events are visible to explicitly shared viewers, so no Git event
    # may disclose a provider URL or the remote response object.
    shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "shared", "shared_with_user_ids": [str(env.viewer_id)]},
    )
    assert shared.status_code == 200, shared.text
    env.authenticate_as(env.viewer_id)
    viewer_events_response = await env.client.get(f"/api/projects/{project_id}/events?limit=200")
    assert viewer_events_response.status_code == 200, viewer_events_response.text
    viewer_git_events = [
        event for event in viewer_events_response.json() if event["event_type"].startswith("git.")
    ]
    serialized_events = json.dumps(viewer_git_events, sort_keys=True)
    for private_url in (
        "https://example.com/vendor/project.git",
        "ssh://git@example.com/vendor/project.git",
        "git@example.com:vendor/project.git",
        clone_url,
    ):
        assert private_url not in serialized_events
    assert viewer_git_events
    assert all("remote" not in event["event_metadata"] for event in viewer_git_events)
    assert all("url" not in event["event_metadata"] for event in viewer_git_events)
