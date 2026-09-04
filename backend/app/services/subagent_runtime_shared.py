"""Durable Subagent sessions, execution, messaging, and parent wake-up."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.database import async_session
from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE, Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.services.chat_model_selection import validate_temperature
from app.services.llm.reasoning import validate_reasoning_effort
from app.services.workload_capacity import (
    WorkloadKind,
    WorkloadOverloadedError,
    get_workload_capacity,
)

SUBAGENT_CHANNEL = "subagent"
SUBAGENT_INPUT = "subagent_input"
SUBAGENT_FORK_CONTEXT = "subagent_fork_context"
SUBAGENT_PARENT_MESSAGE = "subagent_parent_message"
SUBAGENT_COMPLETION = "subagent_completion"
SUBAGENT_FAILURE = "subagent_failure"
SUBAGENT_PARENT_EVENT = "subagent_event"

INPUT_PENDING = "pending"
INPUT_PROCESSING = "processing"
INPUT_DONE = "done"
INPUT_CANCELLED = "cancelled"

CAUSAL_ROOT_SESSION_ID = "causal_root_session_id"
CAUSAL_ROOT_ANCHOR_ID = "causal_root_anchor_id"
CAUSAL_ROOT_GENERATION = "causal_root_generation"
CAUSAL_PARENT_SESSION_ID = "causal_parent_session_id"
CAUSAL_PARENT_ANCHOR_ID = "causal_parent_anchor_id"

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_WAITING = "waiting_confirmation"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})

LEASE_SECONDS = 60
WORKER_CONCURRENCY = 4
PROJECT_LEADER_BATCH_MAX_REPLIES = 12
PROJECT_LEADER_BATCH_MAX_BYTES = 24 * 1024
PARENT_EVENT_BATCH_MAX_MESSAGES = 20
PARENT_EVENT_BATCH_MAX_BYTES = 24 * 1024
PARENT_EVENT_BATCH_DEBOUNCE_SECONDS = 0.5
DISPATCH_RECOVERY_INTERVAL_SECONDS = 60.0
LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS = 300.0
DISPATCH_RETRY_INTERVAL_SECONDS = 5.0

SUBAGENT_DISPATCH_PENDING = "pending"
SUBAGENT_DISPATCH_DELIVERED = "delivered"
SUBAGENT_DISPATCH_DISCARDED = "discarded"

settings = get_settings()
_running_tasks: dict[uuid.UUID, asyncio.Task] = {}
_running_task_lease_owners: dict[uuid.UUID, str] = {}
_running_tasks_guard = asyncio.Lock()
_dispatch_wakeup = asyncio.Event()
_project_dispatch_wakeup = asyncio.Event()
_current_subagent_lease_owner: ContextVar[str | None] = ContextVar(
    "current_subagent_lease_owner",
    default=None,
)


def _expected_subagent_lease_owner() -> str:
    return _current_subagent_lease_owner.get() or settings.INSTANCE_ID


def _owns_subagent_lease(run: SubagentRun | None) -> bool:
    return bool(
        run is not None
        and run.status == RUN_RUNNING
        and run.lease_owner == _expected_subagent_lease_owner()
    )


def _signal_dispatch_work() -> None:
    """Wake the local durable dispatcher after a producer commit."""

    _dispatch_wakeup.set()


def _signal_project_dispatch_work() -> None:
    """Wake project-only dispatch without coupling it to ordinary parents."""

    _project_dispatch_wakeup.set()


async def _finish_parent_event_dispatch(
    message_id: uuid.UUID,
    state: str,
) -> None:
    """Move an ordinary wake event out of the active queue idempotently."""

    async with async_session() as db:
        event = await db.get(ChatMessage, message_id, with_for_update=True)
        if event is None:
            return
        event.message_meta = {
            **_message_meta(event),
            "subagent_dispatch_state": state,
        }
        await db.commit()


class SubagentError(ValueError):
    """A safe, user-facing Subagent contract error."""


async def _ensure_parent_continuation(
    db,
    *,
    parent: ChatSession,
    execution_user_id: uuid.UUID,
) -> None:
    """Admit one new durable parent generation for deferred Project work."""

    from app.services.conversation_turn_lifecycle import (
        ACTIVE_TURN_STATUS,
        SUSPENDED_TURN_STATUS,
        conversation_turn_snapshot_for_session,
        transition_conversation_turn,
    )

    snapshot = conversation_turn_snapshot_for_session(parent)
    if snapshot.status == SUSPENDED_TURN_STATUS:
        raise SubagentError("父 Turn 正在等待确认，暂时不能继续 Subagent。")
    if snapshot.status == ACTIVE_TURN_STATUS:
        return
    continuation_anchor = ChatMessage(
        agent_id=parent.agent_id,
        user_id=execution_user_id,
        role="system",
        content="",
        conversation_id=str(parent.id),
        message_meta={
            "kind": "project_subagent_external_continuation",
            "consumed_by_onmessage": True,
            "attachments": [],
        },
    )
    db.add(continuation_anchor)
    await db.flush()
    await transition_conversation_turn(
        db,
        agent_id=parent.agent_id,
        conversation_id=str(parent.id),
        turn_anchor_id=continuation_anchor.id,
        status=ACTIVE_TURN_STATUS,
    )


async def _subagent_input_causality(
    db,
    *,
    parent: ChatSession,
    explicit_parent_anchor_id: uuid.UUID | None = None,
) -> dict[str, str | int]:
    """Snapshot the exact parent generation that admitted one child input."""

    from app.services.conversation_turn_lifecycle import (
        ACTIVE_TURN_STATUS,
        conversation_turn_snapshot_for_session,
    )

    snapshot = conversation_turn_snapshot_for_session(parent)
    # Preserve legacy/internal callers that have not admitted lifecycle state.
    # Once a session has durable lifecycle metadata, only its running owner may
    # admit a child input.
    if snapshot.status == "idle":
        return {}
    if snapshot.status != ACTIVE_TURN_STATUS:
        raise SubagentError("当前父 Turn 已停止，不能再启动或投递 Subagent。")
    if snapshot.anchor_id is None or snapshot.generation < 1:
        raise SubagentError("当前父 Turn 状态无效，不能投递 Subagent。")
    parent_anchor_id = explicit_parent_anchor_id or snapshot.anchor_id
    parent_anchor = await db.get(ChatMessage, parent_anchor_id)
    root_anchor = await db.get(ChatMessage, snapshot.anchor_id)
    if (
        parent_anchor is None
        or root_anchor is None
        or parent_anchor.conversation_id != str(parent.id)
        or parent_anchor.agent_id != parent.agent_id
        or root_anchor.conversation_id != str(parent.id)
        or root_anchor.agent_id != parent.agent_id
    ):
        return {}
    root_meta = _message_meta(root_anchor)
    return {
        CAUSAL_ROOT_SESSION_ID: str(
            root_meta.get(CAUSAL_ROOT_SESSION_ID) or parent.id
        ),
        CAUSAL_ROOT_ANCHOR_ID: str(
            root_meta.get(CAUSAL_ROOT_ANCHOR_ID) or root_anchor.id
        ),
        CAUSAL_ROOT_GENERATION: int(
            root_meta.get(CAUSAL_ROOT_GENERATION) or snapshot.generation
        ),
        CAUSAL_PARENT_SESSION_ID: str(parent.id),
        CAUSAL_PARENT_ANCHOR_ID: str(parent_anchor.id),
    }


def _project_member_origin_tool_call_id(
    member,
    execution_user_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> str:
    membership = dict(dict(member.config_snapshot or {}).get("membership") or {})
    generation = max(1, int(membership.get("generation") or 1))
    base = f"project-member:{member.id}"
    generation_key = base if generation == 1 else f"{base}:v{generation}"
    if execution_user_id is not None and owner_user_id is not None and execution_user_id != owner_user_id:
        return f"{generation_key}:user:{execution_user_id}"
    return generation_key


def _message_meta(row: ChatMessage) -> dict:
    return dict(row.message_meta) if isinstance(row.message_meta, dict) else {}


def _run_owned_by_parent(run: SubagentRun, parent_session_id: uuid.UUID) -> bool:
    return run.parent_session_id == parent_session_id


async def _project_member_runtime_snapshot(
    db,
    *,
    project,
    agent_id: uuid.UUID,
    project_run_id: uuid.UUID | None,
):
    """Resolve live membership plus either frozen-run or immediate runtime data."""

    from app.models.project import (
        ProjectCapabilityBinding,
        ProjectMemberSnapshot,
        ProjectRun,
        ProjectRunMemberSnapshot,
    )

    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise SubagentError("目标 Agent 不是当前项目的已启用成员。")

    if project_run_id is not None:
        project_run = await db.get(ProjectRun, project_run_id)
        if (
            project_run is None
            or project_run.project_id != project.id
            or project_run.tenant_id != project.tenant_id
        ):
            raise SubagentError("项目运行快照不存在或不属于当前项目。")
        frozen = (
            await db.execute(
                select(ProjectRunMemberSnapshot).where(
                    ProjectRunMemberSnapshot.run_id == project_run.id,
                    ProjectRunMemberSnapshot.project_id == project.id,
                    ProjectRunMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectRunMemberSnapshot.project_member_id == member.id,
                    ProjectRunMemberSnapshot.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if frozen is None:
            raise SubagentError("项目运行缺少该成员的冻结配置。")
        return (
            member,
            dict(frozen.member_config_snapshot or {}),
            list(frozen.capability_snapshot or []),
            bool(frozen.is_leader),
            True,
        )

    bindings = (
        (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.is_enabled.is_(True),
                    or_(
                        ProjectCapabilityBinding.source == "shared",
                        ProjectCapabilityBinding.inherited_from_agent_id == agent_id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    capabilities = [
        {
            "binding_id": str(binding.id),
            "capability_id": str(binding.capability_id) if binding.capability_id else None,
            "type": binding.capability_type,
            "name": binding.capability_name,
            "source": binding.source,
            "scope": binding.scope,
            "config": binding.config,
        }
        for binding in bindings
    ]
    return member, dict(member.config_snapshot or {}), capabilities, bool(member.is_leader), False


def _apply_project_runtime_to_session(
    child: ChatSession,
    *,
    member,
    member_config: dict,
    capabilities: list[dict],
    is_leader: bool,
    frozen: bool,
) -> None:
    child.im_config = {
        **dict(child.im_config or {}),
        "project_member_id": str(member.id),
        "project_membership_generation": max(
            1,
            int(dict(member_config.get("membership") or {}).get("generation") or 1),
        ),
        "project_role_snapshot": "leader" if is_leader else "participant",
        "member_config_snapshot": dict(member_config),
        "capability_snapshot": list(capabilities),
        "project_run_frozen": frozen,
    }


async def _project_accepts_new_subagent_anchor(
    db,
    run: SubagentRun,
    *,
    authorized_project_run_id: uuid.UUID | None = None,
) -> bool:
    """Lock and check the authoritative project switch before new work starts.

    An already-processing anchor may finish after a project is paused.  Every
    pending anchor, however, must cross this project-row lock before becoming
    processing so the pause transaction has one deterministic boundary.
    """
    if run.project_id is None:
        return True

    # A process restart can leave an already-authorized project turn in the
    # durable ``processing`` state while its worker lease is returned to the
    # queue.  That turn crossed the project-state boundary before the restart;
    # let the worker reclaim it so one stale row cannot sit at the head of the
    # queue and block every newer project turn.
    processing_anchor_id = await db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.conversation_id == str(run.id),
            ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
            ChatMessage.message_meta["subagent_input_state"].as_string()
            == INPUT_PROCESSING,
        )
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .limit(1)
    )
    if processing_anchor_id is not None:
        return True

    from app.models.project import Project, ProjectRun

    project = await db.get(Project, run.project_id, with_for_update=True)
    if project is None:
        return False
    if project.status == "running":
        return True
    if project.status not in {"initializing", "planning", "paused", "waiting", "completed"}:
        return False
    project_run_id = authorized_project_run_id
    if project_run_id is None and project.status in {"planning", "paused", "waiting", "completed"}:
        raw_project_run_id = await db.scalar(
            select(ChatMessage.message_meta["project_run_id"].as_string())
            .where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
            )
            .order_by(ChatMessage.created_at, ChatMessage.id)
            .limit(1)
        )
        try:
            project_run_id = uuid.UUID(str(raw_project_run_id))
        except (TypeError, ValueError):
            return False
    if project_run_id is None:
        return False
    trigger_type = await db.scalar(
        select(ProjectRun.trigger_type).where(
            ProjectRun.id == project_run_id,
            ProjectRun.project_id == run.project_id,
        )
    )
    return (project.status == "initializing" and trigger_type == "leader_kickoff") or (
        project.status in {"planning", "paused", "waiting", "completed"}
        and trigger_type == "group_leader_message"
    )


async def _subagent_workload_tenant_id(run_id: uuid.UUID) -> str:
    """Resolve project-first admission scope without retaining a DB session."""
    from app.models.project import Project

    async with async_session() as db:
        run = await db.get(SubagentRun, run_id)
        child = await db.get(ChatSession, run_id)
        if run is None or child is None:
            return str(run_id)
        project = await db.get(Project, run.project_id) if run.project_id else None
        agent = await db.get(Agent, child.agent_id) if child.agent_id else None
        for owner in (project, agent):
            if owner is None:
                continue
            for field in ("company_id", "tenant_id"):
                value = getattr(owner, field, None)
                if value:
                    return str(value)
        return str(run.project_id or child.agent_id or run_id)


async def _requeue_capacity_blocked_subagent(
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
) -> None:
    """Return a claimed turn to the durable queue after admission times out."""
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        anchor = await db.get(ChatMessage, anchor_id, with_for_update=True)
        if anchor is None or not _owns_subagent_lease(run):
            return
        meta = _message_meta(anchor)
        if meta.get("subagent_input_state") == INPUT_PROCESSING:
            meta["subagent_input_state"] = INPUT_PENDING
            anchor.message_meta = meta
            raw_project_run_id = meta.get("project_run_id")
            try:
                project_run_id = uuid.UUID(str(raw_project_run_id))
            except (TypeError, ValueError):
                project_run_id = None
            if project_run_id is not None and run.project_id is not None:
                from app.models.project import ProjectRun

                project_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
                if (
                    project_run is not None
                    and project_run.project_id == run.project_id
                    and project_run.finished_at is None
                    and project_run.status not in {"succeeded", "failed", "cancelled"}
                ):
                    project_run.status = "queued"
                    project_run.started_at = None
        run.status = RUN_QUEUED
        run.lease_owner = None
        run.lease_expires_at = None
        await db.commit()


async def _agent_participates(db, session: ChatSession, agent_id: uuid.UUID) -> bool:
    if session.agent_id == agent_id or (session.source_channel == "agent" and session.peer_agent_id == agent_id):
        return True
    if session.source_channel != "project" or session.project_id is None:
        return False
    from app.models.project import ProjectMemberSnapshot

    member_id = (
        await db.execute(
            select(ProjectMemberSnapshot.id).where(
                ProjectMemberSnapshot.project_id == session.project_id,
                ProjectMemberSnapshot.agent_id == agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    return member_id is not None


def _task_title(task: str) -> str:
    compact = " ".join(str(task or "").split())
    return compact[:80] or "Subagent"


def _subagent_title(name: str | None, task: str) -> str:
    """Normalize the caller-authored child name, with old-call compatibility."""
    if name is None:
        return _task_title(task)
    compact = " ".join(str(name).split())
    if not compact:
        raise SubagentError("name 不能为空。")
    if len(compact) > 80:
        raise SubagentError("name 不能超过 80 个字符。")
    return compact


async def _validate_execution_identity(
    db,
    run: SubagentRun,
    child: ChatSession,
) -> Agent:
    """Revalidate the durable principal immediately before unattended work."""
    agent = await db.get(Agent, child.agent_id)
    if agent is None:
        raise RuntimeError("Subagent execution Agent no longer exists")
    if run.project_id is not None:
        from app.models.project import Project, ProjectMemberSnapshot
        from app.services.project_service import resolve_project_execution_user

        project = await db.get(Project, run.project_id)
        if project is None:
            raise RuntimeError("Project no longer exists")
        try:
            await resolve_project_execution_user(db, project, run.execution_user_id)
        except HTTPException as exc:
            raise RuntimeError("Project execution user is no longer available") from exc

        member = await db.get(ProjectMemberSnapshot, run.project_member_id) if run.project_member_id else None
        if (
            member is None
            or member.project_id != run.project_id
            or member.agent_id != child.agent_id
            or not member.is_enabled
            or bool(dict(child.im_config or {}).get("membership_revoked"))
        ):
            raise RuntimeError("Project member is no longer active")
    else:
        from app.services.execution_identity import resolve_execution_user_id

        await resolve_execution_user_id(db, agent, run.execution_user_id)
    return agent


async def _resolve_model_override(
    db,
    agent: Agent,
    requested: str | None,
) -> tuple[uuid.UUID | None, str | None]:
    model_name = str(requested or "").strip()
    if not model_name:
        return None, None
    if agent.tenant_id is None:
        raise SubagentError("当前 Agent 没有租户模型池，不能指定 Subagent 模型。")

    from app.services.chat_model_selection import (
        MODEL_STATUS_AMBIGUOUS,
        MODEL_STATUS_DISABLED,
        MODEL_STATUS_OK,
        resolve_tenant_model_reference,
    )

    resolved = await resolve_tenant_model_reference(
        db,
        tenant_id=agent.tenant_id,
        reference=model_name,
    )
    if resolved.status == MODEL_STATUS_AMBIGUOUS:
        raise SubagentError(f"模型 {model_name} 在当前租户中不唯一，请先清理模型配置。")
    if resolved.status == MODEL_STATUS_DISABLED:
        raise SubagentError(f"模型 {model_name} 已禁用。")
    if resolved.status != MODEL_STATUS_OK or resolved.model is None:
        raise SubagentError(f"找不到可用模型 {model_name}。")
    return resolved.model.id, resolved.model.model


def _fork_row_meta(row) -> dict:
    meta = copy.deepcopy(getattr(row, "message_meta", None) or {})
    for key in (
        "turn_anchor_id",
        "turn_status",
        "delivery_claim",
        "consumed_by_onmessage",
        "onmessage_execution_ids",
        "subagent_input_state",
        "subagent_turn_anchor_id",
        "subagent_wake",
    ):
        meta.pop(key, None)
    meta["kind"] = SUBAGENT_FORK_CONTEXT
    return meta


def _completed_tool_call(row) -> bool:
    if getattr(row, "role", None) != "tool_call":
        return True
    try:
        payload = json.loads(getattr(row, "content", "") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "done"


async def _copy_fork_context(
    db,
    *,
    parent: ChatSession,
    child: ChatSession,
    execution_agent_id: uuid.UUID,
    turn_anchor_id: uuid.UUID,
    created_at: datetime,
    ctx_size: int,
) -> datetime:
    """Materialize the parent's current LLM-visible prefix into the child."""
    from app.services.chat_history import load_messages_for_session

    rows = await load_messages_for_session(
        db,
        agent_id=parent.agent_id,
        conversation_id=str(parent.id),
        ctx_size=ctx_size,
    )
    anchor_index = next(
        (index for index, row in enumerate(rows) if getattr(row, "id", None) == turn_anchor_id),
        None,
    )
    if anchor_index is None:
        raise SubagentError("当前会话锚点已经变化，无法安全 Fork；请重试。")

    next_created_at = created_at
    # The child task replaces the parent's currently executing instruction.
    # Copy only the completed prefix before that turn: carrying the current
    # parent anchor into the child produces two competing user instructions
    # (for example, the parent says "call run_subagent" while the child cannot
    # recursively expose that tool).
    for row in rows[:anchor_index]:
        if not _completed_tool_call(row):
            continue
        next_created_at += timedelta(microseconds=1)
        copied = ChatMessage(
            id=uuid.uuid4(),
            agent_id=execution_agent_id,
            user_id=getattr(row, "user_id", None),
            sender_user_id=getattr(row, "sender_user_id", None),
            sender_agent_id=getattr(row, "sender_agent_id", None),
            role=row.role,
            content=row.content,
            conversation_id=str(child.id),
            message_meta=_fork_row_meta(row),
            thinking=getattr(row, "thinking", None),
            created_at=next_created_at,
        )
        db.add(copied)
    await db.flush()
    return next_created_at

__all__ = [name for name in globals() if name != "__all__" and not name.startswith("__")]
