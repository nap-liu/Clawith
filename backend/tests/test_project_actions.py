"""API-level acceptance tests for the AI-native project closed loop.

These tests intentionally use the real SQLAlchemy models and managed Git
implementation behind a small FastAPI app.  They verify observable API
contracts and durable state, rather than mocking the project service itself.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
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
import app.models.org  # noqa: F401
import app.models.participant  # noqa: F401
import app.models.project  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.user  # noqa: F401
import app.models.subagent_run  # noqa: F401
from app.api import projects as projects_api
from app.core.security import get_current_user
from app.database import Base, get_db
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.project import ProjectEvent
from app.models.tenant import Tenant
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

    tenant = Tenant(name="Project API", slug=f"project-api-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    owner = await _user(session, tenant, "Owner")
    viewer = await _user(session, tenant, "Viewer")
    leader = await _agent(session, tenant, owner, "Leader", "Drive outcomes")
    worker = await _agent(session, tenant, owner, "Worker", "Build deliverables")
    reviewer = await _agent(session, tenant, owner, "Reviewer", "Review evidence")
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


async def _create_project(env: ProjectApiEnv, *, name: str = "Closed loop") -> dict[str, Any]:
    response = await env.client.post(
        "/api/projects",
        json={
            "name": name,
            "description": "API acceptance project",
            "goal": "Deliver a traceable result",
            "success_criteria": ["Evidence is committed", "Review is recorded"],
            "members": [
                {"agent_id": str(env.leader_id), "is_leader": True},
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
    assert project["status"] == "running"

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

    env.authenticate_as(env.owner_id)
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

    run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(worker_id), "trigger_type": "manual", "input": {"objective": "Build v1"}},
    )
    assert run_response.status_code == 201, run_response.text
    run_id = run_response.json()["id"]

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


async def test_project_group_mentions_are_explicit_bounded_and_reuse_durable_child(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Project Agent Group")
    project_id = project["id"]

    group_response = await env.client.get(f"/api/projects/{project_id}/group-session")
    assert group_response.status_code == 200, group_response.text
    group = group_response.json()
    assert group["source_channel"] == "project"

    passive = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Visible update only", "mentions": [], "attachments": []},
    )
    assert passive.status_code == 201, passive.text
    assert passive.json()["awakened_agent_ids"] == []
    assert passive.json()["subagent_runs"] == []
    assert passive.json()["message"]["message_meta"]["visible_to_group"] is True

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
    assert body["awakened_agent_ids"] == [str(env.worker_id), str(env.reviewer_id)]
    assert len(body["subagent_runs"]) == 2
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
    assert reused.json()["subagent_runs"][0]["session_id"] == worker_child["session_id"]
    assert reused.json()["subagent_runs"][0]["project_run_id"] != worker_child["project_run_id"]

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
    assert len(history.json()["items"]) == 3
    attachment_message = next(
        item for item in history.json()["items"] if item["attachments"]
    )
    assert attachment_message["attachments"][0]["name"] == "brief.md"


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
    child_id = uuid.UUID(wake.json()["subagent_runs"][0]["session_id"])
    project_run_id = uuid.UUID(wake.json()["subagent_runs"][0]["project_run_id"])
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
    parent = await env.db.get(ChatSession, uuid.UUID(group["id"]))
    assert parent is not None and parent.source_channel == "project"


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
