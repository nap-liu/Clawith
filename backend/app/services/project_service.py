"""Tenant-safe orchestration services for AI-native projects."""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import and_, delete, exists, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.project import (
    Project,
    ProjectAccessGrant,
    ProjectCapabilityBinding,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRun,
    ProjectRunMemberSnapshot,
    ProjectWorkItem,
)
from app.models.skill import Skill
from app.models.subagent_run import SubagentRun
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.schemas.project import (
    ProjectAgentCreate,
    ProjectCapabilityCreate,
    ProjectCreate,
    ProjectMemberCreate,
)
from app.services.project_capability_options import load_project_capability_options

PROJECT_EVENT_SUMMARY_MAX_LENGTH = 500
PROJECT_RUNTIME_STATUS_RUNNING = "running"
PROJECT_RUNTIME_STATUS_PAUSED = "paused"
PROJECT_CONVERSATION_STATUSES = frozenset(
    {
        "planning",
        PROJECT_RUNTIME_STATUS_RUNNING,
        PROJECT_RUNTIME_STATUS_PAUSED,
        "waiting",
        "completed",
    }
)


def ensure_project_running(project: Project) -> None:
    """Reject a new project wake unless the authoritative project is running."""

    if project.status != PROJECT_RUNTIME_STATUS_RUNNING:
        raise HTTPException(
            status_code=409,
            detail="Project runtime is paused or unavailable; resume the project before starting new work",
        )


def ensure_project_accepts_group_message(project: Project) -> None:
    """Allow project conversation independently from execution scheduling.

    Planning, waiting, paused, and completed projects remain conversational. All new
    execution, A2A, scheduling, and specialist wakes still require ``running``.
    """

    if project.status not in PROJECT_CONVERSATION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Project conversation is unavailable; resume the project before sending new messages",
        )


async def project_runtime_allows_agent(db: AsyncSession, agent: Agent) -> bool:
    """Return whether an Agent may accept a new turn under its project switch.

    Standard Agents are not governed by a project. Project Agents always defer
    to their owning Project status, which keeps schedules and triggers aligned
    with the same switch used by Project Runs and collaboration.
    """

    if getattr(agent, "scope", "standard") != "project":
        return True
    if getattr(agent, "project_id", None) is None:
        return False
    status_value = await db.scalar(select(Project.status).where(Project.id == agent.project_id))
    return status_value == PROJECT_RUNTIME_STATUS_RUNNING


def bounded_project_event_summary(event_type: str, summary: str) -> tuple[str, dict[str, Any]]:
    """Return a database-safe one-line summary without silently losing detail.

    Project event detail belongs in ``event_metadata``.  For an unexpectedly
    large summary, use a stable, searchable event-type marker instead of
    slicing user text at an arbitrary Unicode/code-point boundary.  The caller
    receives the complete original summary and its digest for the audit JSON.
    """

    original = str(summary or "")
    normalized = " ".join(original.split())
    if len(normalized) <= PROJECT_EVENT_SUMMARY_MAX_LENGTH:
        return normalized, {}
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    return (
        f"{event_type} · full details stored in event metadata · sha256:{digest}",
        {
            "full_summary": original,
            "summary_sha256": digest,
            "summary_compacted": True,
        },
    )


def _tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="A tenant membership is required")
    return user.tenant_id


def project_execution_user_id(project: Project) -> uuid.UUID:
    """Return the configured project principal, with owner fallback for legacy rows."""

    return project.execution_user_id or project.owner_user_id


async def resolve_project_execution_user(
    db: AsyncSession,
    project: Project,
    execution_user_id: uuid.UUID | None = None,
) -> User:
    """Resolve an active principal that still belongs to the project's human ACL."""

    user_id = execution_user_id or project_execution_user_id(project)
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == project.tenant_id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=422, detail="Project execution user must be active in the project tenant")
    if user.id != project.owner_user_id:
        grant_id = await db.scalar(
            select(ProjectAccessGrant.id).where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
                ProjectAccessGrant.user_id == user.id,
            )
        )
        if project.visibility != "shared" or grant_id is None:
            raise HTTPException(status_code=422, detail="Project execution user must be a shared project user")
    return user


def _is_platform_project_admin(user: User) -> bool:
    from app.core.permissions import is_platform_admin_user

    return is_platform_admin_user(user)


def _is_company_project_admin(user: User) -> bool:
    return user.role == "org_admin" and user.tenant_id is not None


def accessible_projects_clause(user: User, *, edit: bool = False):
    if _is_platform_project_admin(user):
        return true()
    tenant_id = _tenant_id(user)
    if _is_company_project_admin(user):
        return Project.tenant_id == tenant_id
    grant = exists().where(
        ProjectAccessGrant.project_id == Project.id,
        ProjectAccessGrant.tenant_id == tenant_id,
        ProjectAccessGrant.user_id == user.id,
        *([ProjectAccessGrant.role == "edit"] if edit else []),
    )
    return and_(Project.tenant_id == tenant_id, or_(Project.owner_user_id == user.id, grant))


async def require_project(
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    edit: bool = False,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(
        Project.id == project_id,
        accessible_projects_clause(user, edit=edit),
    )
    if lock:
        statement = statement.with_for_update()
    project = (await db.execute(statement)).scalar_one_or_none()
    if project is None:
        # Deliberately hide existence across tenants and unauthorized users.
        raise HTTPException(status_code=404, detail="Project not found")
    if (
        lock
        and project.owner_user_id != user.id
        and not _is_platform_project_admin(user)
        and not _is_company_project_admin(user)
    ):
        grant_role = await db.scalar(
            select(ProjectAccessGrant.role)
            .where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
                ProjectAccessGrant.user_id == user.id,
                *([ProjectAccessGrant.role == "edit"] if edit else []),
            )
            .with_for_update()
        )
        if grant_role is None:
            raise HTTPException(status_code=404, detail="Project not found")
    return project


async def require_owner(
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(Project.id == project_id)
    if _is_platform_project_admin(user):
        pass
    elif _is_company_project_admin(user):
        statement = statement.where(Project.tenant_id == _tenant_id(user))
    else:
        statement = statement.where(
            Project.tenant_id == _tenant_id(user),
            Project.owner_user_id == user.id,
        )
    if lock:
        statement = statement.with_for_update()
    project = (await db.execute(statement)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def ensure_project_group_session(db: AsyncSession, project: Project) -> ChatSession:
    """Return the project's single durable group root.

    The access Agent is fixed at first creation instead of following project-owner
    changes. Project REST endpoints enforce ACL; this session is only the
    append-only conversation/root for durable child runs.
    """
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "project",
                ChatSession.is_group.is_(True),
            )
        )
    ).scalar_one_or_none()
    if session is not None:
        return session
    anchor = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
            .order_by(
                ProjectMemberSnapshot.is_leader.desc(),
                ProjectMemberSnapshot.created_at,
                ProjectMemberSnapshot.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        raise HTTPException(status_code=422, detail="项目需要至少一名已启用的数字员工才能开始群聊")
    session = ChatSession(
        project_id=project.id,
        agent_id=anchor.agent_id,
        title=f"{project.name} · 项目协作",
        source_channel="project",
        external_conv_id=f"project:{project.id}",
        is_group=True,
        group_name=project.name,
        is_primary=False,
        im_config={
            "project_id": str(project.id),
            "access_agent_id": str(anchor.agent_id),
            "append_only": True,
            "wake_policy": "structured_mentions_only",
        },
    )
    db.add(session)
    await db.flush()
    return session


async def ensure_enabled_project_leader(
    db: AsyncSession,
    project: Project,
) -> ProjectMemberSnapshot:
    """Return the enabled project owner and repair legacy projects without one."""

    leader = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .join(Agent, Agent.id == ProjectMemberSnapshot.agent_id)
            .where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_leader.is_(True),
                ProjectMemberSnapshot.is_enabled.is_(True),
                Agent.project_id == project.id,
                Agent.tenant_id == project.tenant_id,
                Agent.scope == "project",
                Agent.is_deleted.is_(False),
                Agent.status.in_(["running", "idle"]),
            )
            .order_by(ProjectMemberSnapshot.created_at, ProjectMemberSnapshot.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if leader is not None:
        await _restore_blocked_project_replies(db, project, leader)
        return leader

    leader = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .join(Agent, Agent.id == ProjectMemberSnapshot.agent_id)
            .where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
                Agent.project_id == project.id,
                Agent.tenant_id == project.tenant_id,
                Agent.scope == "project",
                Agent.is_deleted.is_(False),
                Agent.status.in_(["running", "idle"]),
            )
            .order_by(ProjectMemberSnapshot.created_at, ProjectMemberSnapshot.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if leader is None:
        raise HTTPException(
            status_code=422,
            detail="Project needs at least one enabled digital employee",
        )
    await db.execute(
        ProjectMemberSnapshot.__table__.update()
        .where(ProjectMemberSnapshot.project_id == project.id)
        .values(is_leader=False)
    )
    leader.is_leader = True
    await _restore_blocked_project_replies(db, project, leader)
    add_event(
        db,
        project,
        "leader.repaired",
        f"Assigned {leader.name_snapshot} as project owner",
        actor_agent_id=leader.agent_id,
        metadata={"member_id": str(leader.id), "reason": "missing_enabled_leader"},
    )
    await db.flush()
    return leader


async def _restore_blocked_project_replies(
    db: AsyncSession,
    project: Project,
    leader: ProjectMemberSnapshot,
) -> None:
    """Return replies blocked by a missing owner to the normal durable queue."""

    group_ids = [
        str(value)
        for value in (
            await db.execute(
                select(ChatSession.id).where(
                    ChatSession.project_id == project.id,
                    ChatSession.source_channel == "project",
                )
            )
        ).scalars()
    ]
    if not group_ids:
        return
    rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id.in_(group_ids),
                    ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                    ChatMessage.message_meta["leader_batch_state"].as_string() == "blocked_no_leader",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        metadata = dict(row.message_meta or {})
        metadata.update(
            {
                "leader_batch_state": "pending",
                "wake_policy": "leader_batch_pending",
                "default_leader_agent_id": str(leader.agent_id),
            }
        )
        row.message_meta = metadata


async def ensure_project_leader_session(db: AsyncSession, project: Project) -> ChatSession:
    """Return the legacy project-owner Web session for history compatibility.

    New projects plan in the canonical project group. Their matching Web session
    remains readable so historical links continue to resolve, but it is marked
    read-only and is never selected as the kickoff discussion source.
    """
    external_conv_id = f"project-leader:{project.id}"
    planning_mode = str(dict((project.settings or {}).get("planning") or {}).get("conversation_mode") or "")
    planning_transport = "project_group" if planning_mode == "project_group" else "leader_session"
    session = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "web",
                ChatSession.external_conv_id == external_conv_id,
                ChatSession.is_group.is_(False),
            )
            .order_by(ChatSession.created_at, ChatSession.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    leader = await ensure_enabled_project_leader(db, project)
    if session is not None:
        # Keep historical links attached to the current project owner without
        # creating a second compatibility thread.
        if session.agent_id != leader.agent_id:
            session.agent_id = leader.agent_id
            session.title = f"{project.name} · 项目规划"
        session.im_config = {
            **dict(session.im_config or {}),
            "leader_agent_id": str(leader.agent_id),
            "planning_transport": planning_transport,
            "read_only": True,
        }
        return session
    session = ChatSession(
        project_id=project.id,
        agent_id=leader.agent_id,
        user_id=project.owner_user_id,
        title=f"{project.name} · 项目规划",
        source_channel="web",
        external_conv_id=external_conv_id,
        is_group=False,
        is_primary=False,
        im_config={
            "project_id": str(project.id),
            "project_member_id": str(leader.id),
            "leader_agent_id": str(leader.agent_id),
            "purpose": "project_kickoff_planning",
            "planning_transport": planning_transport,
            "read_only": True,
        },
    )
    db.add(session)
    await db.flush()
    return session


def add_event(
    db: AsyncSession,
    project: Project,
    event_type: str,
    summary: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    from_agent_id: uuid.UUID | None = None,
    to_agent_id: uuid.UUID | None = None,
    work_item_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> ProjectEvent:
    bounded_summary, overflow_metadata = bounded_project_event_summary(event_type, summary)
    event = ProjectEvent(
        tenant_id=project.tenant_id,
        project_id=project.id,
        event_type=event_type,
        summary=bounded_summary,
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        work_item_id=work_item_id,
        run_id=run_id,
        event_metadata={**overflow_metadata, **dict(metadata or {})},
    )
    db.add(event)
    return event


async def _get_project_agent(db: AsyncSession, project: Project, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.tenant_id == project.tenant_id,
                Agent.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=422, detail=f"数字员工 {agent_id} 在当前租户不可用")
    if agent.scope == "project" and agent.project_id != project.id:
        raise HTTPException(status_code=422, detail="项目专用数字员工不能加入其他项目")
    return agent


async def add_member(
    db: AsyncSession,
    project: Project,
    data: ProjectMemberCreate,
    *,
    actor_user_id: uuid.UUID,
) -> ProjectMemberSnapshot:
    agent = await _get_project_agent(db, project, data.agent_id)
    existing = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.agent_id == agent.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        detail = (
            "Agent previously left this project; restore the existing member snapshot"
            if not existing.is_enabled
            else "Agent is already an active project member"
        )
        raise HTTPException(status_code=409, detail=detail)
    if not data.is_enabled:
        raise HTTPException(
            status_code=422, detail="New project members must start active; remove them explicitly later"
        )
    if data.is_leader:
        await db.execute(
            ProjectMemberSnapshot.__table__.update()
            .where(ProjectMemberSnapshot.project_id == project.id)
            .values(is_leader=False)
        )
    member = ProjectMemberSnapshot(
        tenant_id=project.tenant_id,
        project_id=project.id,
        agent_id=agent.id,
        name_snapshot=agent.name,
        role_snapshot=agent.role_description or "",
        source_updated_at=agent.updated_at,
        is_leader=data.is_leader,
        is_enabled=data.is_enabled,
        config_snapshot={
            "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
            "fallback_model_id": str(agent.fallback_model_id) if agent.fallback_model_id else None,
            "autonomy_policy": dict(agent.autonomy_policy or {}),
            "max_tool_rounds": agent.max_tool_rounds,
            "project_instruction": "",
            "source_agent_status": agent.status,
            "enabled_inherited_capability_ids": [str(value) for value in data.enabled_inherited_capability_ids],
            "membership": {
                "state": "active",
                "generation": 1,
                "changed_at": datetime.now(timezone.utc).isoformat(),
                "changed_by_user_id": str(actor_user_id),
                "changed_by_agent_id": None,
                "reason": "project_member_added",
                "history": [],
            },
        },
    )
    db.add(member)
    await db.flush()
    add_event(
        db,
        project,
        "member.snapshot.created",
        f"Added project-local snapshot for {agent.name}",
        actor_user_id=actor_user_id,
        actor_agent_id=agent.id,
        metadata={"member_id": str(member.id), "is_leader": member.is_leader},
    )
    return member


def _membership_config(
    member: ProjectMemberSnapshot,
    *,
    state: str,
    actor_user_id: uuid.UUID | None,
    actor_agent_id: uuid.UUID | None,
    reason: str | None,
    revoked_subagent_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Return a new immutable JSON value for one membership transition."""

    now = datetime.now(timezone.utc).isoformat()
    config = dict(member.config_snapshot or {})
    previous = dict(config.get("membership") or {})
    history = list(previous.get("history") or [])
    transition = {
        "state": state,
        "at": now,
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "actor_agent_id": str(actor_agent_id) if actor_agent_id else None,
        "reason": (reason or "").strip() or None,
    }
    history.append(transition)
    membership = {
        **previous,
        "state": state,
        "changed_at": now,
        "changed_by_user_id": transition["actor_user_id"],
        "changed_by_agent_id": transition["actor_agent_id"],
        "reason": transition["reason"],
        "history": history,
    }
    if state == "departed":
        membership["departed_at"] = now
        membership["revoked_subagent_ids"] = [str(value) for value in (revoked_subagent_ids or [])]
    else:
        membership["restored_at"] = now
        membership["generation"] = max(1, int(previous.get("generation") or 1)) + 1
    config["membership"] = membership
    return config


async def deactivate_project_member(
    db: AsyncSession,
    project: Project,
    member: ProjectMemberSnapshot,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> list[uuid.UUID]:
    """Soft-remove a member and revoke every live project execution.

    The snapshot, child sessions, messages, immutable run-member snapshots and
    audit history remain addressable. Only future authority is removed.
    """

    if member.project_id != project.id or member.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member not found")
    if member.is_leader:
        raise HTTPException(
            status_code=422,
            detail="Transfer project responsibility before removing the project owner",
        )
    if not member.is_enabled:
        return []

    all_children = (
        (
            await db.execute(
                select(SubagentRun).where(
                    SubagentRun.project_id == project.id,
                    SubagentRun.project_member_id == member.id,
                )
            )
        )
        .scalars()
        .all()
    )
    live_children = [row for row in all_children if row.status in {"queued", "running"}]
    child_ids = [row.id for row in all_children]
    cancelled_child_ids = [row.id for row in live_children]
    now = datetime.now(timezone.utc)
    for child in live_children:
        child.status = "cancelled"
        child.lease_owner = None
        child.lease_expires_at = None

    if child_ids:
        sessions = (await db.execute(select(ChatSession).where(ChatSession.id.in_(child_ids)))).scalars().all()
        from app.services.conversation_turn_lifecycle import (
            cancel_current_conversation_turn,
        )

        live_child_ids = set(cancelled_child_ids)
        for session in sessions:
            if session.id in live_child_ids:
                await cancel_current_conversation_turn(
                    db,
                    agent_id=session.agent_id,
                    conversation_id=str(session.id),
                )
            session.im_config = {
                **dict(session.im_config or {}),
                "membership_revoked": True,
                "membership_revoked_at": now.isoformat(),
                "membership_revoked_reason": "project_member_departed",
            }
        inputs = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id.in_([str(value) for value in child_ids]),
                        ChatMessage.message_meta["kind"].as_string() == "subagent_input",
                        ChatMessage.message_meta["subagent_input_state"].as_string().in_(["pending", "processing"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inputs:
            metadata = dict(row.message_meta or {})
            metadata["subagent_input_state"] = "cancelled"
            metadata["cancel_reason"] = "project_member_departed"
            row.message_meta = metadata

    project_runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                    ProjectRun.agent_id == member.agent_id,
                    ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for run in project_runs:
        run.status = "cancelled"
        run.finished_at = now
        run.error = "Project member departed before this run completed"
        run.output = {
            **dict(run.output or {}),
            "cancel_reason": "project_member_departed",
            "project_member_id": str(member.id),
        }

    member.is_enabled = False
    member.is_leader = False
    member.config_snapshot = _membership_config(
        member,
        state="departed",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        reason=reason,
        revoked_subagent_ids=child_ids,
    )
    add_event(
        db,
        project,
        "member.departed",
        f"Removed {member.name_snapshot} from active project participation",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        metadata={
            "member_id": str(member.id),
            "agent_id": str(member.agent_id),
            "reason": (reason or "").strip() or None,
            "revoked_subagent_ids": [str(value) for value in child_ids],
            "cancelled_subagent_ids": [str(value) for value in cancelled_child_ids],
            "cancelled_project_run_ids": [str(row.id) for row in project_runs],
            "snapshot_retained": True,
        },
    )
    await db.flush()
    return cancelled_child_ids


async def restore_project_member(
    db: AsyncSession,
    project: Project,
    member: ProjectMemberSnapshot,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> list[uuid.UUID]:
    """Restore the same member snapshot without reopening revoked sessions."""

    if member.project_id != project.id or member.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member not found")
    if member.is_enabled:
        return []
    await _get_project_agent(db, project, member.agent_id)

    member.is_enabled = True
    member.config_snapshot = _membership_config(
        member,
        state="active",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        reason=reason,
    )
    add_event(
        db,
        project,
        "member.restored",
        f"Restored {member.name_snapshot} to active project participation",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        metadata={
            "member_id": str(member.id),
            "agent_id": str(member.agent_id),
            "reason": (reason or "").strip() or None,
            "restored_subagent_ids": [],
            "old_sessions_remain_read_only": True,
            "snapshot_reused": True,
        },
    )
    await db.flush()
    return []


async def project_session_access_mode(
    db: AsyncSession,
    user: User,
    session: ChatSession,
) -> str | None:
    """Return ``read``/``edit`` for an auditable project session.

    Group sessions use the project ACL directly. Historical Subagent sessions
    remain readable after departure; writing them additionally requires the
    exact durable member snapshot to still be enabled.
    """

    if session.project_id is None:
        return None
    project = await db.get(Project, session.project_id)
    if project is None:
        return None

    if _is_platform_project_admin(user):
        human_role = "edit"
    elif user.tenant_id is None or project.tenant_id != user.tenant_id:
        return None
    elif _is_company_project_admin(user) or project.owner_user_id == user.id:
        human_role = "edit"
    else:
        grant = (
            await db.execute(
                select(ProjectAccessGrant).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                    ProjectAccessGrant.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            return None
        human_role = "edit" if grant.role == "edit" else "read"

    if session.source_channel == "project" and session.is_group:
        return human_role
    if session.source_channel != "subagent":
        return None

    run = await db.get(SubagentRun, session.id)
    if (
        run is None
        or run.project_id != project.id
        or run.project_member_id is None
        or session.agent_id is None
    ):
        return None
    member = await db.get(ProjectMemberSnapshot, run.project_member_id)
    if (
        member is None
        or member.project_id != project.id
        or member.tenant_id != project.tenant_id
        or member.agent_id != session.agent_id
    ):
        return None
    member_is_writable = member.is_enabled and not bool(dict(session.im_config or {}).get("membership_revoked"))
    return "edit" if human_role == "edit" and member_is_writable else "read"


async def _resolve_capability(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    data: ProjectCapabilityCreate,
) -> str:
    if data.capability_id is None:
        if not data.capability_name:
            raise HTTPException(status_code=422, detail="capability_name is required without capability_id")
        return data.capability_name
    if data.capability_type == "skill":
        item = (
            await db.execute(
                select(Skill).where(
                    Skill.id == data.capability_id,
                    or_(Skill.tenant_id == tenant_id, Skill.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=422, detail="Skill is unavailable in this tenant")
        return item.name
    if data.capability_type == "mcp":
        item = (
            await db.execute(
                select(MCPServer).where(
                    MCPServer.id == data.capability_id,
                    or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=422, detail="MCP server is unavailable in this tenant")
        return item.display_name or item.name
    return data.capability_name or str(data.capability_id)


async def add_capability(
    db: AsyncSession,
    project: Project,
    data: ProjectCapabilityCreate,
    *,
    actor_user_id: uuid.UUID,
) -> ProjectCapabilityBinding:
    if data.source == "inherited":
        if data.inherited_from_agent_id is None:
            raise HTTPException(status_code=422, detail="inherited_from_agent_id is required")
        is_member = (
            await db.execute(
                select(ProjectMemberSnapshot.id).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id == data.inherited_from_agent_id,
                )
            )
        ).scalar_one_or_none()
        if is_member is None:
            raise HTTPException(status_code=422, detail="Inherited capability source must be a project member")
    name = await _resolve_capability(db, project.tenant_id, data)
    binding = ProjectCapabilityBinding(
        tenant_id=project.tenant_id,
        project_id=project.id,
        capability_type=data.capability_type,
        capability_id=data.capability_id,
        capability_name=name,
        source=data.source,
        inherited_from_agent_id=data.inherited_from_agent_id,
        is_enabled=data.is_enabled,
        scope=data.scope,
        config=data.config,
    )
    db.add(binding)
    await db.flush()
    add_event(
        db,
        project,
        "capability.bound",
        f"Bound {data.capability_type} capability {name}",
        actor_user_id=actor_user_id,
        metadata={"binding_id": str(binding.id), "source": binding.source},
    )
    return binding


async def replace_access_grants(
    db: AsyncSession,
    project: Project,
    user_ids: list[uuid.UUID],
    *,
    actor_user_id: uuid.UUID,
) -> None:
    unique_ids = set(user_ids)
    unique_ids.discard(project.owner_user_id)
    existing_roles = dict(
        (
            await db.execute(
                select(ProjectAccessGrant.user_id, ProjectAccessGrant.role).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                )
            )
        ).all()
    )
    if unique_ids:
        valid_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(unique_ids),
                        User.tenant_id == project.tenant_id,
                        User.is_active.is_(True),
                    )
                )
            ).scalars()
        )
        if valid_ids != unique_ids:
            raise HTTPException(status_code=422, detail="Every shared user must be active in the project tenant")
    await db.execute(
        delete(ProjectAccessGrant).where(
            ProjectAccessGrant.project_id == project.id,
            ProjectAccessGrant.tenant_id == project.tenant_id,
        )
    )
    for user_id in sorted(unique_ids, key=str):
        db.add(
            ProjectAccessGrant(
                tenant_id=project.tenant_id,
                project_id=project.id,
                user_id=user_id,
                role=existing_roles.get(user_id, "view"),
                created_by_user_id=actor_user_id,
            )
        )
    project.visibility = "shared" if unique_ids else "private"


async def _validate_project_create_inputs(
    db: AsyncSession,
    user: User,
    data: ProjectCreate,
    tenant_id: uuid.UUID,
    *,
    allow_template_agents: bool = False,
) -> list[ProjectMemberCreate]:
    """Validate every external reference before creating managed storage."""

    git_config = dict((data.settings or {}).get("git") or {})
    git_mode = git_config.get("mode") or git_config.get("repository_mode", "managed")
    if git_mode != "managed":
        raise HTTPException(status_code=501, detail="External Git repositories require a connector")
    shared_user_ids = set(data.shared_with_user_ids)
    shared_user_ids.discard(user.id)
    if data.visibility == "shared" and not shared_user_ids:
        raise HTTPException(status_code=422, detail="shared visibility requires shared_with_user_ids")
    if shared_user_ids:
        valid_user_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(shared_user_ids),
                        User.tenant_id == tenant_id,
                        User.is_active.is_(True),
                    )
                )
            ).scalars()
        )
        if valid_user_ids != shared_user_ids:
            raise HTTPException(status_code=422, detail="Every shared user must be active in the project tenant")

    members = [member.model_copy(deep=True) for member in data.members]
    if not members and not allow_template_agents:
        raise HTTPException(
            status_code=422,
            detail="A project requires at least one digital employee",
        )
    member_ids = [member.agent_id for member in members]
    if len(member_ids) != len(set(member_ids)):
        raise HTTPException(status_code=422, detail="A project cannot include the same digital employee twice")
    leader_count = sum(member.is_leader for member in members)
    if leader_count > 1:
        raise HTTPException(status_code=422, detail="A project can have only one leader")
    if members and leader_count == 0:
        members[0] = members[0].model_copy(update={"is_leader": True})

    if member_ids:
        from app.core.permissions import build_visible_agents_query

        sources = (
            (
                await db.execute(
                    build_visible_agents_query(user, tenant_id=tenant_id).where(Agent.id.in_(set(member_ids)))
                )
            )
            .scalars()
            .all()
        )
        sources_by_id = {source.id: source for source in sources}
        if set(sources_by_id) != set(member_ids):
            raise HTTPException(status_code=422, detail="源数字员工不可用")
        if any(source.agent_type != "native" for source in sources):
            raise HTTPException(status_code=422, detail="仅原生数字员工可复制到项目")
    else:
        sources = []

    inherited_sources = {
        capability.inherited_from_agent_id
        for capability in data.capabilities
        if capability.source == "inherited"
    }
    if None in inherited_sources or not inherited_sources.issubset(set(member_ids)):
        raise HTTPException(status_code=422, detail="Inherited capability source must be a selected project member")

    capability_options = await load_project_capability_options(db, tenant_id, sources)

    for capability in data.capabilities:
        if capability.capability_type not in {"mcp", "skill"} or capability.capability_id is None:
            continue
        allowed = (
            capability_options.allows_shared(capability.capability_type, capability.capability_id)
            if capability.source == "shared"
            else capability_options.allows(
                capability.inherited_from_agent_id,
                capability.capability_type,
                capability.capability_id,
            )
        )
        if not allowed:
            raise HTTPException(status_code=422, detail="Selected project capability is unavailable")

    for capability in data.capabilities:
        await _resolve_capability(db, tenant_id, capability)

    selected_tool_ids = {
        capability_id
        for member in members
        for capability_id in member.enabled_inherited_capability_ids
    }
    selected_tool_ids.update(
        setting.tool_id
        for member in members
        if member.settings is not None
        for setting in member.settings.tools
    )
    explicit_tool_ids = {
        capability.capability_id
        for capability in data.capabilities
        if capability.capability_type == "tool" and capability.capability_id is not None
    }
    tool_ids = selected_tool_ids | explicit_tool_ids
    if tool_ids:
        available_tool_ids = set(
            (
                await db.execute(
                    select(Tool.id).where(
                        Tool.id.in_(tool_ids),
                        Tool.enabled.is_(True),
                        or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                    )
                )
            ).scalars()
        )
        if available_tool_ids != tool_ids:
            raise HTTPException(status_code=422, detail="One or more selected tools are unavailable")

    for member in members:
        if member.settings is None:
            continue
        if any(
            not capability_options.allows_tool(member.agent_id, setting.tool_id)
            for setting in member.settings.tools
        ):
            raise HTTPException(status_code=422, detail="One or more selected tools are unavailable for this digital employee")
        if any(
            not capability_options.allows(member.agent_id, "mcp", capability_id)
            for capability_id in member.settings.mcp_capability_ids
        ):
            raise HTTPException(status_code=422, detail="One or more selected MCP services are unavailable")
        override_server_ids = [
            setting.server_id for setting in member.settings.mcp_server_overrides
        ]
        if len(override_server_ids) != len(set(override_server_ids)):
            raise HTTPException(status_code=422, detail="MCP service configuration is duplicated")
        if any(
            not capability_options.allows(member.agent_id, "mcp", server_id)
            for server_id in override_server_ids
        ):
            raise HTTPException(status_code=422, detail="One or more configured MCP services are unavailable")
        enabled_tool_ids = [
            setting.tool_id for setting in member.settings.tools if setting.enabled
        ]
        enabled_mcp_server_ids = set(
            (
                await db.execute(
                    select(Tool.mcp_server_id).where(
                        Tool.id.in_(enabled_tool_ids),
                        Tool.type == "mcp",
                        Tool.mcp_server_id.is_not(None),
                    )
                )
            ).scalars()
        ) if enabled_tool_ids else set()
        if not set(override_server_ids).issubset(enabled_mcp_server_ids):
            raise HTTPException(
                status_code=422,
                detail="MCP configuration requires at least one enabled tool from that service",
            )
        if any(
            not capability_options.allows(member.agent_id, "skill", capability_id)
            for capability_id in member.settings.skill_capability_ids
        ):
            raise HTTPException(status_code=422, detail="One or more selected Skills are unavailable")

    for capability_id in set(data.shared_capability_ids):
        if capability_id not in capability_options.market_skill_ids | capability_options.shared_mcp_ids:
            raise HTTPException(status_code=422, detail=f"Capability {capability_id} is unavailable")
    return members


async def create_project(
    db: AsyncSession,
    user: User,
    data: ProjectCreate,
    *,
    allow_template_agents: bool = False,
) -> Project:
    tenant_id = _tenant_id(user)
    members = await _validate_project_create_inputs(
        db,
        user,
        data,
        tenant_id,
        allow_template_agents=allow_template_agents,
    )
    project_id = uuid.uuid4()
    try:
        return await _create_project_uncompensated(
            db,
            user,
            data,
            tenant_id=tenant_id,
            project_id=project_id,
            members=members,
        )
    except Exception:
        from app.services.project_git_service import remove_project_repository

        cleanup_target = Project(id=project_id, tenant_id=tenant_id)
        try:
            await remove_project_repository(cleanup_target)
        except Exception:
            # Preserve the original create failure. Storage cleanup is
            # idempotent and is retried by the API transaction boundary.
            pass
        raise


async def _create_project_uncompensated(
    db: AsyncSession,
    user: User,
    data: ProjectCreate,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    members: list[ProjectMemberCreate],
) -> Project:
    settings = dict(data.settings or {})
    settings["planning"] = {
        **dict(settings.get("planning") or {}),
        "conversation_mode": "project_group",
    }
    project = Project(
        id=project_id,
        tenant_id=tenant_id,
        owner_user_id=user.id,
        template_id=data.template_id,
        name=data.name,
        description=data.description,
        goal=data.goal,
        success_criteria=data.success_criteria,
        visibility="private",
        status=data.status,
        settings=settings,
    )
    from app.services.project_git_service import (
        initialize_project_repo,
        project_user_git_email,
        reconcile_project_repository_operations,
    )

    db.add(project)
    await db.flush()
    git_config = dict((project.settings or {}).get("git") or {})
    git_mode = git_config.get("mode") or git_config.get("repository_mode", "managed")
    git_state = await initialize_project_repo(
        project,
        author_name=user.display_name,
        author_email=project_user_git_email(user.id),
    )
    project.settings = {
        **(project.settings or {}),
        "git": {**git_config, "mode": git_mode, **git_state},
    }
    await reconcile_project_repository_operations(project.id, db=db)

    # A newly created project owns independent digital employees. The selected
    # standard employees are sources for a one-time copy, never live members of
    # the project. This keeps professional identity, tools and Skill files inside
    # the project repository while source memory and workspace history stay in
    # global scope.
    from app.services.project_agent_service import create_project_agent

    project_agent_ids: list[uuid.UUID] = []
    source_to_project_agent: dict[uuid.UUID, uuid.UUID] = {}
    for member in members:
        project_agent, _project_member = await create_project_agent(
            db,
            project,
            user,
            ProjectAgentCreate(
                source_agent_id=member.agent_id,
                is_leader=member.is_leader,
            ),
        )
        project_agent_ids.append(project_agent.id)
        source_to_project_agent[member.agent_id] = project_agent.id

    capabilities: list[ProjectCapabilityCreate] = []
    for capability in data.capabilities:
        inherited_from_agent_id = capability.inherited_from_agent_id
        if capability.source == "inherited" and inherited_from_agent_id is not None:
            inherited_from_agent_id = source_to_project_agent.get(
                inherited_from_agent_id,
                inherited_from_agent_id,
            )
        capabilities.append(
            capability.model_copy(
                update={"inherited_from_agent_id": inherited_from_agent_id},
            )
        )

    explicit_capability_ids = {capability.capability_id for capability in capabilities}
    for capability_id in data.shared_capability_ids:
        if capability_id in explicit_capability_ids:
            continue
        skill = (
            await db.execute(
                select(Skill).where(
                    Skill.id == capability_id,
                    or_(Skill.tenant_id == tenant_id, Skill.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        capability_type = "skill"
        capability_name = skill.name if skill else None
        if skill is None:
            mcp = (
                await db.execute(
                    select(MCPServer).where(
                        MCPServer.id == capability_id,
                        or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None)),
                    )
                )
            ).scalar_one_or_none()
            if mcp is None:
                raise HTTPException(status_code=422, detail=f"Capability {capability_id} is unavailable")
            capability_type = "mcp"
            capability_name = mcp.display_name or mcp.name
        capabilities.append(
            ProjectCapabilityCreate(
                capability_type=capability_type,
                capability_id=capability_id,
                capability_name=capability_name,
                source="shared",
            )
        )

    # The capability picker starts from the normalized project starter set and
    # may add further Tool/MCP dependencies explicitly. Apply only those chosen
    # identifiers to the copied project employee; source Agent assignments and
    # configuration are never reused.
    for member in members:
        project_agent_id = source_to_project_agent[member.agent_id]
        for capability_id in member.enabled_inherited_capability_ids:
            tool = (
                await db.execute(
                    select(Tool).where(
                        Tool.id == capability_id,
                        Tool.enabled.is_(True),
                        or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                    )
                )
            ).scalar_one_or_none()
            if tool is None:
                continue
            normalized_type = "mcp" if tool.type == "mcp" else "tool"
            normalized_id = tool.mcp_server_id if normalized_type == "mcp" else tool.id
            if normalized_id is None:
                continue
            capabilities.append(
                ProjectCapabilityCreate(
                    capability_type=normalized_type,
                    capability_id=normalized_id,
                    capability_name=tool.mcp_server_name or tool.display_name or tool.name,
                    source="inherited",
                    inherited_from_agent_id=project_agent_id,
                )
            )

    from app.services.project_member_runtime import (
        apply_project_agent_tool_settings,
        merge_project_member_runtime_config,
        sync_project_capability_assignment,
    )
    from app.services.project_skill_assets import bind_library_skill_to_project_agent

    for capability in capabilities:
        if capability.capability_type == "skill":
            targets = (
                project_agent_ids
                if capability.source == "shared"
                else [capability.inherited_from_agent_id]
            )
            for target_agent_id in dict.fromkeys(targets):
                if target_agent_id is None:
                    continue
                existing_skill = (
                    await db.execute(
                        select(ProjectCapabilityBinding.id).where(
                            ProjectCapabilityBinding.project_id == project.id,
                            ProjectCapabilityBinding.tenant_id == project.tenant_id,
                            ProjectCapabilityBinding.capability_type == "skill",
                            ProjectCapabilityBinding.capability_id == capability.capability_id,
                            ProjectCapabilityBinding.inherited_from_agent_id == target_agent_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing_skill is not None:
                    continue
                await bind_library_skill_to_project_agent(
                    db,
                    project,
                    skill_id=capability.capability_id,
                    project_agent_id=target_agent_id,
                    is_enabled=capability.is_enabled,
                    scope=capability.scope,
                    actor_user_id=user.id,
                    actor_display_name=user.display_name,
                )
            continue

        existing_capability = (
            await db.execute(
                select(ProjectCapabilityBinding.id).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.capability_type == capability.capability_type,
                    ProjectCapabilityBinding.capability_id == capability.capability_id,
                    ProjectCapabilityBinding.source == capability.source,
                    ProjectCapabilityBinding.inherited_from_agent_id
                    == capability.inherited_from_agent_id,
                )
            )
        ).scalar_one_or_none()
        if existing_capability is not None:
            continue
        binding = await add_capability(db, project, capability, actor_user_id=user.id)
        await sync_project_capability_assignment(db, project, binding)

    for source_member in members:
        if source_member.settings is None:
            continue
        project_agent_id = source_to_project_agent[source_member.agent_id]
        project_member = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.agent_id == project_agent_id,
                )
            )
        ).scalar_one()
        project_member.config_snapshot = await merge_project_member_runtime_config(
            db,
            project,
            project_member,
            source_member.settings.config_snapshot,
        )
        await apply_project_agent_tool_settings(
            db,
            project,
            project_agent_id=project_agent_id,
            settings=[
                (setting.tool_id, setting.enabled, setting.config)
                for setting in source_member.settings.tools
            ],
        )
        for override in source_member.settings.mcp_server_overrides:
            db.add(
                MCPServerOverride(
                    mcp_server_id=override.server_id,
                    scope_type="agent",
                    scope_id=project_agent_id,
                    system_prompt_block=override.system_prompt_block,
                    url_template=override.url_template,
                    headers_template=override.headers_template,
                    credential_template=override.credential_template,
                    command_template=override.command_template,
                    args_template=override.args_template,
                    env_template=override.env_template,
                    last_modified_by_user_id=user.id,
                )
            )
        enabled_mcp_tool_ids = [
            setting.tool_id
            for setting in source_member.settings.tools
            if setting.enabled
        ]
        selected_mcp_server_ids = list(
            (
                await db.execute(
                    select(Tool.mcp_server_id).where(
                        Tool.id.in_(enabled_mcp_tool_ids),
                        Tool.type == "mcp",
                        Tool.mcp_server_id.is_not(None),
                    )
                )
            ).scalars()
        ) if enabled_mcp_tool_ids else []
        selected_mcp_server_id_set = set(selected_mcp_server_ids)
        for capability_type, capability_ids in (
            (
                "mcp",
                [
                    *source_member.settings.mcp_capability_ids,
                    *selected_mcp_server_ids,
                ],
            ),
            ("skill", source_member.settings.skill_capability_ids),
        ):
            for capability_id in dict.fromkeys(capability_ids):
                existing = (
                    await db.execute(
                        select(ProjectCapabilityBinding.id).where(
                            ProjectCapabilityBinding.project_id == project.id,
                            ProjectCapabilityBinding.capability_type == capability_type,
                            ProjectCapabilityBinding.capability_id == capability_id,
                            ProjectCapabilityBinding.inherited_from_agent_id == project_agent_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                capability = ProjectCapabilityCreate(
                    capability_type=capability_type,
                    capability_id=capability_id,
                    source="inherited",
                    inherited_from_agent_id=project_agent_id,
                    is_enabled=True,
                )
                if capability_type == "skill":
                    await bind_library_skill_to_project_agent(
                        db,
                        project,
                        skill_id=capability_id,
                        project_agent_id=project_agent_id,
                        is_enabled=True,
                        scope={},
                        actor_user_id=user.id,
                        actor_display_name=user.display_name,
                    )
                else:
                    binding = await add_capability(
                        db,
                        project,
                        capability,
                        actor_user_id=user.id,
                    )
                    if not (
                        capability_type == "mcp"
                        and capability_id in selected_mcp_server_id_set
                    ):
                        await sync_project_capability_assignment(db, project, binding)

    if data.visibility == "shared" and not data.shared_with_user_ids:
        raise HTTPException(status_code=422, detail="shared visibility requires shared_with_user_ids")
    await replace_access_grants(db, project, data.shared_with_user_ids, actor_user_id=user.id)
    # A project with Agents owns its canonical group root from creation time;
    # GET remains a safe fallback for projects created before this migration.
    if members:
        await ensure_project_group_session(db, project)
        await ensure_project_leader_session(db, project)
    add_event(db, project, "project.created", f"Created project {project.name}", actor_user_id=user.id)
    if project.status == "initializing":
        project.status = "planning"
    if project.status == "planning":
        add_event(
            db,
            project,
            "project.initialized",
            "Project snapshots and capability bindings are ready for project-owner planning",
            actor_user_id=user.id,
        )
    await db.flush()
    # PostgreSQL server-side timestamps are expired after the final UPDATE.
    # Refresh while we are still inside the async greenlet so response
    # serialization never triggers implicit synchronous IO (MissingGreenlet).
    await db.refresh(project)
    return project


async def freeze_run_members(
    db: AsyncSession,
    project: Project,
    run: ProjectRun,
    *,
    source_run_id: uuid.UUID | None = None,
) -> list[ProjectRunMemberSnapshot]:
    if source_run_id is not None:
        source_snapshots = (
            (
                await db.execute(
                    select(ProjectRunMemberSnapshot).where(
                        ProjectRunMemberSnapshot.run_id == source_run_id,
                        ProjectRunMemberSnapshot.project_id == project.id,
                        ProjectRunMemberSnapshot.tenant_id == project.tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not source_snapshots:
            raise HTTPException(status_code=422, detail="Parent project run has no member snapshots")
        inherited = [
            ProjectRunMemberSnapshot(
                tenant_id=project.tenant_id,
                project_id=project.id,
                run_id=run.id,
                project_member_id=snapshot.project_member_id,
                agent_id=snapshot.agent_id,
                is_leader=snapshot.is_leader,
                member_config_snapshot=dict(snapshot.member_config_snapshot or {}),
                capability_snapshot=list(snapshot.capability_snapshot or []),
            )
            for snapshot in source_snapshots
        ]
        db.add_all(inherited)
        await db.flush()
        return inherited

    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    capabilities = (
        (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    agent_tools = (
        (
            await db.execute(
                select(AgentTool, Tool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id.in_([member.agent_id for member in members]),
                    Tool.enabled.is_(True),
                    or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
                )
            )
        ).all()
        if members
        else []
    )
    from app.services.tool_enablement import resolved_agent_tool_enabled, tool_is_required

    tool_rows_by_agent: dict[uuid.UUID, dict[str, tuple[AgentTool, Tool]]] = {}
    for assignment, tool in agent_tools:
        tool_rows_by_agent.setdefault(assignment.agent_id, {})[tool.name] = (
            assignment,
            tool,
        )
    override_names = {
        str(name)
        for member in members
        for key in ("enabled_platform_tools", "disabled_platform_tools")
        for name in dict(member.config_snapshot or {}).get(key, [])
    }
    override_tools = (
        (
            await db.execute(
                select(Tool).where(
                    Tool.name.in_(override_names),
                    Tool.enabled.is_(True),
                    Tool.source.in_(("builtin", "admin")),
                    or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
                )
            )
        ).scalars().all()
        if override_names
        else []
    )
    override_tool_by_name = {tool.name: tool for tool in override_tools}
    member_agents = {
        agent.id: agent
        for agent in (
            (
                await db.execute(
                    select(Agent).where(Agent.id.in_([member.agent_id for member in members]))
                )
            )
            .scalars()
            .all()
        )
    }
    frozen: list[ProjectRunMemberSnapshot] = []
    for member in members:
        effective = [
            {
                "binding_id": str(binding.id),
                "type": binding.capability_type,
                "capability_id": str(binding.capability_id) if binding.capability_id else None,
                "name": binding.capability_name,
                "source": binding.source,
                "scope": binding.scope,
                "config": binding.config,
            }
            for binding in capabilities
            if binding.source == "shared" or binding.inherited_from_agent_id == member.agent_id
        ]
        member_config = dict(member.config_snapshot or {})
        member_agent = member_agents.get(member.agent_id)
        is_project_agent = bool(
            member_agent
            and member_agent.scope == "project"
            and member_agent.project_id == project.id
        )
        enabled_overrides = (
            set()
            if is_project_agent
            else {str(name) for name in member_config.get("enabled_platform_tools", [])}
        )
        disabled_overrides = (
            set()
            if is_project_agent
            else {str(name) for name in member_config.get("disabled_platform_tools", [])}
        )
        frozen_tool_names: set[str] = set()
        for tool_name, (assignment, tool) in tool_rows_by_agent.get(member.agent_id, {}).items():
            enabled = (
                resolved_agent_tool_enabled(tool_name, assignment)
                if is_project_agent
                else tool_is_required(tool_name)
                or tool_name in enabled_overrides
                or (
                    resolved_agent_tool_enabled(tool_name, assignment)
                    and tool_name not in disabled_overrides
                )
            )
            if not enabled:
                continue
            effective.append(
                {
                    "type": "agent_tool",
                    "tool_id": str(tool.id),
                    "name": tool.name,
                    "config": dict(assignment.config or {}),
                }
            )
            frozen_tool_names.add(tool_name)
        for tool_name in sorted(enabled_overrides - frozen_tool_names):
            tool = override_tool_by_name.get(tool_name)
            if tool is None:
                continue
            effective.append(
                {
                    "type": "agent_tool",
                    "tool_id": str(tool.id),
                    "name": tool.name,
                    "config": {},
                }
            )
        snapshot = ProjectRunMemberSnapshot(
            tenant_id=project.tenant_id,
            project_id=project.id,
            run_id=run.id,
            project_member_id=member.id,
            agent_id=member.agent_id,
            is_leader=member.is_leader,
            member_config_snapshot=dict(member.config_snapshot or {}),
            capability_snapshot=effective,
        )
        db.add(snapshot)
        frozen.append(snapshot)
    await db.flush()
    return frozen


def _trace_uuid(*values: Any) -> uuid.UUID | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            continue
    return None


async def serialize_project_run_member_snapshots(
    db: AsyncSession,
    project: Project,
    snapshots: list[ProjectRunMemberSnapshot],
) -> list[dict[str, Any]]:
    """Expose frozen execution facts through one stable product summary.

    The durable snapshots retain IDs, configuration payloads, and assignment
    details for execution. Public APIs intentionally return model names,
    product settings, and neutral capability entries instead of that internal
    persistence structure.
    """

    if not snapshots:
        return []
    member_ids = {snapshot.project_member_id for snapshot in snapshots}
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.id.in_(member_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    member_by_id = {member.id: member for member in members}
    model_ids = {
        model_id
        for snapshot in snapshots
        for field in ("primary_model_id", "fallback_model_id")
        if (model_id := _trace_uuid(dict(snapshot.member_config_snapshot or {}).get(field))) is not None
    }
    models = (
        (
            await db.execute(
                select(LLMModel).where(
                    LLMModel.id.in_(model_ids),
                    or_(LLMModel.tenant_id == project.tenant_id, LLMModel.tenant_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
        if model_ids
        else []
    )
    model_names = {model.id: model.label or model.model for model in models}

    def model_summary(config: dict[str, Any], field: str) -> dict[str, Any] | None:
        raw_value = config.get(field)
        if raw_value in (None, ""):
            return None
        model_id = _trace_uuid(raw_value)
        name = model_names.get(model_id) if model_id else None
        return {"name": name, "availability": "available" if name else "missing"}

    payloads: list[dict[str, Any]] = []
    for snapshot in snapshots:
        member = member_by_id.get(snapshot.project_member_id)
        config = dict(snapshot.member_config_snapshot or {})
        capability_items: dict[tuple[str, str], dict[str, str]] = {}
        for raw_item in list(snapshot.capability_snapshot or []):
            item = dict(raw_item or {})
            raw_type = str(item.get("type") or "").strip()
            capability_type = "tool" if raw_type == "agent_tool" else raw_type
            if capability_type not in {"tool", "mcp", "skill"}:
                capability_type = "other"
            key = str(item.get("key") or item.get("name") or "").strip()
            if not key:
                continue
            name = str(item.get("display_name") or item.get("name") or key).strip()
            source = "project" if item.get("source") == "shared" else "member"
            capability_items[(capability_type, key)] = {
                "type": capability_type,
                "key": key,
                "name": name,
                "source": source,
            }
        items = sorted(capability_items.values(), key=lambda item: (item["type"], item["name"], item["key"]))
        by_type: dict[str, int] = {"tool": 0, "mcp": 0, "skill": 0}
        for item in items:
            by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        member_summary = {
            "project_member_id": snapshot.project_member_id,
            "agent_id": snapshot.agent_id,
            "name": member.name_snapshot if member else None,
            "responsibility": member.role_snapshot if member else "",
            "is_leader": snapshot.is_leader,
            "configuration": {
                "primary_model": model_summary(config, "primary_model_id"),
                "fallback_model": model_summary(config, "fallback_model_id"),
                "max_tool_rounds": config.get("max_tool_rounds"),
                "has_project_instruction": bool(str(config.get("project_instruction") or "").strip()),
            },
            "capabilities": {
                "total": len(items),
                "by_type": by_type,
                "items": items,
            },
        }
        payloads.append(
            {
                "id": snapshot.id,
                "project_id": snapshot.project_id,
                "run_id": snapshot.run_id,
                "project_member_id": snapshot.project_member_id,
                "agent_id": snapshot.agent_id,
                "is_leader": snapshot.is_leader,
                "member_snapshot": member_summary,
                "created_at": snapshot.created_at,
            }
        )
    return payloads


def _project_a2a_receipt(result: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the structured receipt emitted by the native project transport.

    Legacy transports return a human-readable confirmation and are resolved by
    the scoped pair lookup. A value that claims to be JSON must be valid: a
    malformed native receipt must never silently attach the run to whichever
    same-pair conversation happened to be updated most recently.
    """

    stripped = str(result or "").strip()
    if not stripped.startswith("{"):
        return None, None
    try:
        receipt = json.loads(stripped)
    except json.JSONDecodeError:
        return None, "Project A2A transport returned a malformed JSON receipt"
    if not isinstance(receipt, dict):
        return None, "Project A2A transport receipt must be a JSON object"
    return receipt, None


async def _resolve_project_a2a_session_info(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    project_run_id: uuid.UUID,
    source_agent_id: uuid.UUID,
    target_agent_id: uuid.UUID,
    result: str,
) -> tuple[dict[str, Any], str | None]:
    """Resolve and validate the exact native A2A/session receipt.

    Native delivery returns all durable identities. They are treated as one
    integrity boundary: any mismatch fails delivery instead of falling back to
    a latest-session guess. Only legacy plain-text transports use the scoped
    pair lookup retained at the end of this function.
    """

    receipt, receipt_error = _project_a2a_receipt(result)
    if receipt_error:
        return {}, receipt_error

    if receipt is not None:
        raw_session_id = receipt.get("a2a_session_id") or receipt.get("session_id")
        raw_run_id = receipt.get("project_run_id")
        raw_subagent_run_id = receipt.get("subagent_run_id")
        raw_subagent_session_id = receipt.get("subagent_session_id")
        if not all(
            (
                raw_session_id,
                raw_run_id,
                raw_subagent_run_id,
                raw_subagent_session_id,
            )
        ):
            return {}, "Project A2A transport receipt is missing durable identity fields"

        session_id = _trace_uuid(raw_session_id)
        receipt_run_id = _trace_uuid(raw_run_id)
        subagent_run_id = _trace_uuid(raw_subagent_run_id)
        subagent_session_id = _trace_uuid(raw_subagent_session_id)
        if None in {
            session_id,
            receipt_run_id,
            subagent_run_id,
            subagent_session_id,
        }:
            return {}, "Project A2A transport receipt contains an invalid UUID"
        if receipt_run_id != project_run_id:
            return {}, "Project A2A transport receipt references another project run"
        if subagent_run_id != subagent_session_id:
            return {}, "Project A2A child run and child session identities do not match"

        session = await db.get(ChatSession, session_id)
        expected_pair = {source_agent_id, target_agent_id}
        if (
            session is None
            or session.project_id != project_id
            or session.source_channel != "agent"
            or {session.agent_id, session.peer_agent_id} != expected_pair
        ):
            return {}, "Project A2A transport receipt references an invalid collaboration session"

        child_run = await db.get(SubagentRun, subagent_run_id)
        child_session = await db.get(ChatSession, subagent_session_id)
        if (
            child_run is None
            or child_session is None
            or child_run.project_id != project_id
            or child_run.parent_session_id != session.id
            or child_session.project_id != project_id
            or child_session.source_channel != "subagent"
            or child_session.agent_id != target_agent_id
        ):
            return {}, "Project A2A transport receipt references an invalid execution session"

        return (
            {
                "session_id": str(session.id),
                "a2a_session_id": str(session.id),
                "session_agent_id": str(session.agent_id),
                "session_access_agent_id": str(session.agent_id),
                "session_title": session.title,
                "project_run_id": str(project_run_id),
                "subagent_run_id": str(child_run.id),
                "subagent_session_id": str(child_session.id),
            },
            None,
        )

    session_agent_id = min(source_agent_id, target_agent_id, key=str)
    session_peer_id = max(source_agent_id, target_agent_id, key=str)
    session = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.project_id == project_id,
                ChatSession.source_channel == "agent",
                ChatSession.agent_id == session_agent_id,
                ChatSession.peer_agent_id == session_peer_id,
            )
            .order_by(
                ChatSession.last_message_at.desc().nulls_last(),
                ChatSession.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if session is None:
        return {}, None
    return (
        {
            "session_id": str(session.id),
            "a2a_session_id": str(session.id),
            "session_agent_id": str(session.agent_id),
            "session_access_agent_id": str(session.agent_id),
            "session_title": session.title,
        },
        None,
    )


async def serialize_project_runs(
    db: AsyncSession,
    project: Project,
    runs: list[ProjectRun],
) -> list[dict[str, Any]]:
    """Return ProjectRuns with immutable member and exact session identity.

    Consumers must never infer a conversation from a group root or from the
    currently active member list.  The execution-time snapshot survives member
    removal, while the explicit session fields distinguish the visible A2A
    conversation from the durable worker child.
    """
    if not runs:
        return []
    run_ids = [run.id for run in runs]
    snapshots = (
        (
            await db.execute(
                select(ProjectRunMemberSnapshot).where(
                    ProjectRunMemberSnapshot.project_id == project.id,
                    ProjectRunMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectRunMemberSnapshot.run_id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    public_snapshots = await serialize_project_run_member_snapshots(db, project, list(snapshots))
    public_by_snapshot_id = {payload["id"]: payload["member_snapshot"] for payload in public_snapshots}
    snapshots_by_run: dict[uuid.UUID, list[ProjectRunMemberSnapshot]] = {}
    for snapshot in snapshots:
        snapshots_by_run.setdefault(snapshot.run_id, []).append(snapshot)

    payloads: list[dict[str, Any]] = []
    for run in runs:
        output = dict(run.output or {})
        input_data = dict(run.input or {})
        dispatch = dict(input_data.get("dispatch") or {})
        responsible = next(
            (snapshot for snapshot in snapshots_by_run.get(run.id, []) if snapshot.agent_id == run.agent_id),
            None,
        )
        subagent_session_id = _trace_uuid(
            output.get("subagent_session_id"),
            output.get("subagent_run_id"),
        )
        visible_session_id = _trace_uuid(
            output.get("session_id"),
            output.get("a2a_session_id"),
            subagent_session_id,
        )
        group_session_id = _trace_uuid(
            output.get("group_session_id"),
            input_data.get("group_session_id"),
            dispatch.get("group_session_id"),
        )
        member_snapshot = public_by_snapshot_id.get(responsible.id) if responsible else None
        payloads.append(
            {
                "id": run.id,
                "project_id": run.project_id,
                "work_item_id": run.work_item_id,
                "agent_id": run.agent_id,
                "initiated_by_user_id": run.initiated_by_user_id,
                "status": run.status,
                "trigger_type": run.trigger_type,
                "title": str(input_data.get("title") or "").strip() or None,
                "input": input_data,
                "output": output,
                "error": run.error,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "project_member_id": responsible.project_member_id if responsible else None,
                "agent_name": member_snapshot.get("name") if member_snapshot else None,
                "member_snapshot": member_snapshot,
                "session_id": visible_session_id,
                "subagent_session_id": subagent_session_id,
                "group_session_id": group_session_id,
            }
        )
    return payloads


async def serialize_project_events(
    db: AsyncSession,
    project: Project,
    events: list[ProjectEvent],
) -> list[dict[str, Any]]:
    """Attach the same frozen product summary to run-linked audit events."""

    if not events:
        return []
    run_ids = {event.run_id for event in events if event.run_id is not None}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.id.in_(run_ids),
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    run_payloads = await serialize_project_runs(db, project, list(runs))
    member_summary_by_run = {payload["id"]: payload.get("member_snapshot") for payload in run_payloads}
    return [
        {
            "id": event.id,
            "project_id": event.project_id,
            "work_item_id": event.work_item_id,
            "run_id": event.run_id,
            "actor_user_id": event.actor_user_id,
            "actor_agent_id": event.actor_agent_id,
            "from_agent_id": event.from_agent_id,
            "to_agent_id": event.to_agent_id,
            "event_type": event.event_type,
            "summary": event.summary,
            "event_metadata": dict(event.event_metadata or {}),
            "member_snapshot": member_summary_by_run.get(event.run_id),
            "created_at": event.created_at,
        }
        for event in events
    ]


async def project_summary(
    db: AsyncSession,
    project: Project,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> dict:
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectMemberSnapshot.is_leader.desc(), ProjectMemberSnapshot.created_at)
            )
        )
        .scalars()
        .all()
    )
    grants = (
        await db.execute(
            select(ProjectAccessGrant, User.display_name)
            .join(User, User.id == ProjectAccessGrant.user_id)
            .where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
            )
        )
    ).all()
    work_counts = dict(
        (
            await db.execute(
                select(ProjectWorkItem.status, func.count(ProjectWorkItem.id))
                .where(ProjectWorkItem.project_id == project.id, ProjectWorkItem.tenant_id == project.tenant_id)
                .group_by(ProjectWorkItem.status)
            )
        ).all()
    )
    total = sum(work_counts.values())
    done = work_counts.get("done", 0)
    leader = next((member for member in members if member.is_leader), None)
    owner_name = (
        await db.execute(
            select(User.display_name).where(User.id == project.owner_user_id, User.tenant_id == project.tenant_id)
        )
    ).scalar_one_or_none()
    execution_user_id = project_execution_user_id(project)
    execution_user_name = (
        await db.execute(
            select(User.display_name).where(User.id == execution_user_id, User.tenant_id == project.tenant_id)
        )
    ).scalar_one_or_none()
    access_role = "owner" if actor_user_id == project.owner_user_id else None
    if access_role is None and actor_user_id is not None:
        actor = await db.get(User, actor_user_id)
        if actor is not None and (
            _is_platform_project_admin(actor)
            or (_is_company_project_admin(actor) and actor.tenant_id == project.tenant_id)
        ):
            access_role = "owner"
    if access_role is None and actor_user_id is not None:
        access_role = next(
            (grant.role for grant, _display_name in grants if grant.user_id == actor_user_id),
            None,
        )
    return {
        "id": str(project.id),
        "tenant_id": str(project.tenant_id),
        "owner_user_id": str(project.owner_user_id),
        "execution_user_id": str(execution_user_id),
        "execution_user_name": execution_user_name,
        "template_id": str(project.template_id) if project.template_id else None,
        "name": project.name,
        "description": project.description,
        "goal": project.goal,
        "objective": project.goal,
        "success_criteria": project.success_criteria,
        "visibility": project.visibility,
        "status": project.status,
        "settings": project.settings,
        "progress": round(done * 100 / total) if total else 0,
        "work_item_counts": work_counts,
        "members": [
            {
                "id": str(member.id),
                "agent_id": str(member.agent_id),
                "agent_name": member.name_snapshot,
                "name_snapshot": member.name_snapshot,
                "role_snapshot": member.role_snapshot,
                "is_leader": member.is_leader,
                "is_enabled": member.is_enabled,
                "status": "active" if member.is_enabled else "departed",
                "config_snapshot": member.config_snapshot,
            }
            for member in members
        ],
        "leader_name": leader.name_snapshot if leader else None,
        "active_agent_count": sum(member.is_enabled for member in members),
        "current_signal": project.settings.get("current_signal"),
        "next_action": project.settings.get("next_action"),
        "owner_name": owner_name,
        "access_role": access_role,
        "shared_with_user_ids": [str(grant.user_id) for grant, _ in grants],
        "shared_with_names": [display_name for _, display_name in grants],
        "shared_with": [
            {"user_id": str(grant.user_id), "display_name": display_name, "role": grant.role}
            for grant, display_name in grants
        ],
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }


def apply_run_status(run: ProjectRun, status: str) -> None:
    # A ProjectRun is an append-only execution fact. Retrying creates another
    # run; it must never move an already terminal row back to a live state.
    reconcile_project_run_terminal_state(run)
    if run.status in TERMINAL_PROJECT_RUN_STATUSES:
        return
    run.status = status
    now = datetime.now(timezone.utc)
    if status == "running" and run.started_at is None:
        run.started_at = now
    if status in TERMINAL_PROJECT_RUN_STATUSES:
        run.finished_at = now


TERMINAL_PROJECT_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def reconcile_project_run_terminal_state(run: ProjectRun) -> bool:
    """Repair the durable invariant ``finished_at => terminal status``.

    A previous dispatch race could commit ``running`` after the child turn had
    already written ``finished_at``. The timestamp is the stronger completion
    fact, so recovery promotes the row to a terminal status and never clears
    evidence/output. The operation is idempotent and safe in request/daemon
    recovery paths.
    """

    if run.finished_at is None or run.status in TERMINAL_PROJECT_RUN_STATUSES:
        return False
    run.status = "failed" if (run.error or "").strip() else "succeeded"
    if run.started_at is None:
        run.started_at = run.created_at or run.finished_at
    return True


async def reconcile_project_runs(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Repair stale non-terminal ProjectRuns for one tenant-scoped project."""

    conditions = [
        ProjectRun.project_id == project_id,
        ProjectRun.finished_at.is_not(None),
        ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
    ]
    if tenant_id is not None:
        conditions.append(ProjectRun.tenant_id == tenant_id)
    runs = (await db.execute(select(ProjectRun).where(*conditions))).scalars().all()
    return sum(reconcile_project_run_terminal_state(run) for run in runs)


async def deliver_project_a2a(run_id: uuid.UUID) -> None:
    """Consume one persisted project A2A run through the existing sender.

    This runs after the request transaction. Delivery status and its audit
    event are persisted in a fresh transaction, including truthful failures.
    """

    from app.services.agent_tools import _send_message_to_agent

    async with async_session() as db:
        run = await db.get(ProjectRun, run_id)
        if run is None or run.status != "queued" or run.trigger_type != "a2a":
            return
        project = await db.get(Project, run.project_id)
        if project is None:
            return
        if project.status != PROJECT_RUNTIME_STATUS_RUNNING:
            apply_run_status(run, "cancelled")
            run.error = "Member collaboration was cancelled because the project is paused"
            add_event(
                db,
                project,
                "a2a.cancelled",
                "Member collaboration was cancelled because the project is paused",
                actor_user_id=run.initiated_by_user_id,
                from_agent_id=uuid.UUID(str((run.input or {})["from_agent_id"])),
                to_agent_id=uuid.UUID(str((run.input or {})["to_agent_id"])),
                run_id=run.id,
                metadata={"reason": "project_paused"},
            )
            await db.commit()
            return
        payload = dict(run.input or {})
        project_id = run.project_id
        initiated_by_user_id = run.initiated_by_user_id
        from_agent_id = uuid.UUID(payload["from_agent_id"])
        to_agent_id = uuid.UUID(payload["to_agent_id"])
        active_member_ids = set(
            (
                await db.execute(
                    select(ProjectMemberSnapshot.agent_id).where(
                        ProjectMemberSnapshot.project_id == project.id,
                        ProjectMemberSnapshot.tenant_id == project.tenant_id,
                        ProjectMemberSnapshot.agent_id.in_([from_agent_id, to_agent_id]),
                        ProjectMemberSnapshot.is_enabled.is_(True),
                    )
                )
            ).scalars()
        )
        if active_member_ids != {from_agent_id, to_agent_id}:
            apply_run_status(run, "cancelled")
            run.error = "Member collaboration was cancelled because a member is no longer active"
            add_event(
                db,
                project,
                "a2a.cancelled",
                "Member collaboration was cancelled because a member is no longer active",
                actor_user_id=initiated_by_user_id,
                from_agent_id=from_agent_id,
                to_agent_id=to_agent_id,
                run_id=run.id,
                metadata={"reason": "project_member_departed"},
            )
            await db.commit()
            return
        mode_map = {"delegate": "task_delegate", "review": "consult"}
        msg_type = mode_map.get(payload.get("mode"), payload.get("mode", "notify"))
        apply_run_status(run, "running")
        await db.commit()

    try:
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_id": payload["to_agent_id"],
                "message": payload["message"],
                "msg_type": msg_type,
                "force_async": True,
                "new_conversation": bool(payload.get("new_conversation")),
                "_project_id": str(project_id),
                "_project_run_id": str(run_id),
            },
            user_id=initiated_by_user_id,
        )
    except Exception:  # delivery failures must become durable run state
        logger.exception("Project member collaboration delivery failed for run {}", run_id)
        result = "❌ Member collaboration could not be delivered"
    receipt, _ = _project_a2a_receipt(result)
    failed = result.startswith("❌") or bool(receipt and receipt.get("status") == "error")
    session_info: dict[str, Any] = {}
    async with async_session() as session_db:
        source_id = uuid.UUID(payload["from_agent_id"])
        target_id = uuid.UUID(payload["to_agent_id"])
        session_info, identity_error = await _resolve_project_a2a_session_info(
            session_db,
            project_id=project_id,
            project_run_id=run_id,
            source_agent_id=source_id,
            target_agent_id=target_id,
            result=result,
        )
        if identity_error:
            failed = True
            logger.warning(
                "Project member collaboration receipt rejected for run {}: {}",
                run_id,
                identity_error,
            )
            result = "❌ Member collaboration could not be delivered"
    async with async_session() as db:
        run = await db.get(ProjectRun, run_id)
        project = await db.get(Project, run.project_id) if run else None
        if run is None or project is None:
            return
        if failed:
            apply_run_status(run, "failed")
        elif not dict(run.output or {}).get("subagent_run_id"):
            # Compatibility for non-native/legacy transports that confirm
            # delivery but do not create a project execution child. Native
            # project A2A remains live until the durable child turn finishes.
            apply_run_status(run, "succeeded")
        delivery_metadata = {
            "delivery_status": "failed" if failed else "delivered",
            "group_session_id": payload.get("group_session_id"),
            **session_info,
        }
        run.output = {**dict(run.output or {}), **delivery_metadata}
        if failed:
            run.error = "Member collaboration could not be delivered"
        existing_delivery_event = (
            await db.execute(
                select(ProjectEvent.id).where(
                    ProjectEvent.run_id == run.id,
                    ProjectEvent.event_type == ("a2a.delivery_failed" if failed else "a2a.delivered"),
                )
            )
        ).scalar_one_or_none()
        if existing_delivery_event is None:
            add_event(
                db,
                project,
                "a2a.delivery_failed" if failed else "a2a.delivered",
                "Member collaboration could not be delivered" if failed else "Member collaboration message delivered",
                actor_user_id=run.initiated_by_user_id,
                actor_agent_id=uuid.UUID(payload["from_agent_id"]),
                from_agent_id=uuid.UUID(payload["from_agent_id"]),
                to_agent_id=uuid.UUID(payload["to_agent_id"]),
                work_item_id=run.work_item_id,
                run_id=run.id,
                metadata=delivery_metadata,
            )
        await db.commit()
