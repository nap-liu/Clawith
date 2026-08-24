"""Durable Subagent sessions, execution, messaging, and parent wake-up."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import String, and_, cast, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.database import async_session
from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE, Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
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

settings = get_settings()
_running_tasks: dict[uuid.UUID, asyncio.Task] = {}
_running_tasks_guard = asyncio.Lock()


class SubagentError(ValueError):
    """A safe, user-facing Subagent contract error."""


def _project_member_origin_tool_call_id(member) -> str:
    membership = dict(dict(member.config_snapshot or {}).get("membership") or {})
    generation = max(1, int(membership.get("generation") or 1))
    base = f"project-member:{member.id}"
    return base if generation == 1 else f"{base}:v{generation}"


def _message_meta(row: ChatMessage) -> dict:
    return dict(row.message_meta) if isinstance(row.message_meta, dict) else {}


def _run_owned_by_parent(run: SubagentRun, parent_session_id: uuid.UUID) -> bool:
    return run.parent_session_id == parent_session_id


async def _project_accepts_new_subagent_anchor(
    db,
    run: SubagentRun,
    *,
    initializing_project_run_id: uuid.UUID | None = None,
) -> bool:
    """Lock and check the authoritative project switch before new work starts.

    An already-processing anchor may finish after a project is paused.  Every
    pending anchor, however, must cross this project-row lock before becoming
    processing so the pause transaction has one deterministic boundary.
    """
    if run.project_id is None:
        return True

    from app.models.project import Project, ProjectRun

    project = await db.get(Project, run.project_id, with_for_update=True)
    if project is None:
        return False
    if project.status == "running":
        return True
    if project.status != "initializing" or initializing_project_run_id is None:
        return False
    trigger_type = await db.scalar(
        select(ProjectRun.trigger_type).where(
            ProjectRun.id == initializing_project_run_id,
            ProjectRun.project_id == run.project_id,
        )
    )
    return trigger_type == "leader_kickoff"


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
        if run is None or anchor is None or run.status != RUN_RUNNING or run.lease_owner != settings.INSTANCE_ID:
            return
        meta = _message_meta(anchor)
        if meta.get("subagent_input_state") == INPUT_PROCESSING:
            meta["subagent_input_state"] = INPUT_PENDING
            meta["turn_status"] = "pending"
            anchor.message_meta = meta
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
    from app.services.execution_identity import resolve_execution_user_id

    agent = await db.get(Agent, child.agent_id)
    if agent is None:
        raise RuntimeError("Subagent execution Agent no longer exists")
    await resolve_execution_user_id(db, agent, run.execution_user_id)
    if run.project_id is not None:
        from app.models.project import ProjectMemberSnapshot

        member = await db.get(ProjectMemberSnapshot, run.project_member_id) if run.project_member_id else None
        if (
            member is None
            or member.project_id != run.project_id
            or member.agent_id != child.agent_id
            or not member.is_enabled
            or bool(dict(child.im_config or {}).get("membership_revoked"))
        ):
            raise RuntimeError("Project member is no longer active")
    return agent


async def _resolve_model_name(db, agent: Agent, requested: str | None) -> str | None:
    model_name = str(requested or "").strip()
    if not model_name:
        return None
    if agent.tenant_id is None:
        raise SubagentError("当前 Agent 没有租户模型池，不能指定 Subagent 模型。")

    from app.services.chat_model_selection import (
        MODEL_STATUS_AMBIGUOUS,
        MODEL_STATUS_DISABLED,
        MODEL_STATUS_OK,
        resolve_tenant_model_by_name,
    )

    resolved = await resolve_tenant_model_by_name(
        db,
        tenant_id=agent.tenant_id,
        model_name=model_name,
    )
    if resolved.status == MODEL_STATUS_AMBIGUOUS:
        raise SubagentError(f"模型 {model_name} 在当前租户中不唯一，请先清理模型配置。")
    if resolved.status == MODEL_STATUS_DISABLED:
        raise SubagentError(f"模型 {model_name} 已禁用。")
    if resolved.status != MODEL_STATUS_OK or resolved.model is None:
        raise SubagentError(f"找不到可用模型 {model_name}。")
    return resolved.model.model


async def _project_turn_model_id(db, agent: Agent, project) -> str:
    from app.services.chat_model_selection import resolve_project_runtime_models

    resolved = await resolve_project_runtime_models(
        db,
        agent=agent,
        project_settings=project.settings,
    )
    if resolved.primary_model is None:
        raise SubagentError("当前 Agent、项目和租户均没有可用的 LLM 模型，请先在项目设置或租户模型池中配置。")
    return str(resolved.primary_model.id)


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


async def create_subagent(
    *,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    parent_session_id: str,
    origin_tool_call_id: str,
    name: str | None = None,
    task: str,
    mode: str = "sync",
    model: str | None = None,
    fork: bool = False,
    soul: bool = True,
    memory: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
    project_run_id: uuid.UUID | None = None,
    input_metadata: dict | None = None,
) -> tuple[SubagentRun, bool]:
    """Create one child Session and lifecycle row, idempotent per parent tool call."""
    task_text = str(task or "").strip()
    if not task_text:
        raise SubagentError("task 不能为空。")
    child_title = _subagent_title(name, task_text)
    normalized_mode = str(mode or "sync").strip().lower()
    if normalized_mode not in {"sync", "async"}:
        raise SubagentError("mode 只支持 sync 或 async。")
    call_id = str(origin_tool_call_id or "").strip()
    if not call_id:
        raise SubagentError("缺少当前工具调用标识，无法创建可恢复的 Subagent。")
    try:
        parent_id = uuid.UUID(str(parent_session_id))
    except (TypeError, ValueError):
        raise SubagentError("当前 Session 无效。") from None

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        parent = await db.get(ChatSession, parent_id)
        if agent is None or parent is None or not await _agent_participates(db, parent, agent_id):
            raise SubagentError("当前 Agent 无权从这个 Session 创建 Subagent。")
        if parent.source_channel == SUBAGENT_CHANNEL:
            raise SubagentError("Subagent 不能继续创建 Subagent。")

        from app.services.execution_identity import resolve_execution_user_id

        resolved_user_id = await resolve_execution_user_id(
            db,
            agent,
            execution_user_id,
        )

        async def _load_authorized_existing() -> SubagentRun | None:
            existing_run = (
                await db.execute(
                    select(SubagentRun).where(
                        SubagentRun.parent_session_id == parent_id,
                        SubagentRun.origin_tool_call_id == call_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_run is None:
                return None
            existing_child = await db.get(ChatSession, existing_run.id)
            if (
                existing_child is None
                or existing_child.source_channel != SUBAGENT_CHANNEL
                or existing_child.agent_id != agent_id
                or existing_run.execution_user_id != resolved_user_id
                or existing_run.parent_session_id != parent_id
            ):
                raise SubagentError("当前执行身份无权恢复这个 Subagent。")
            return existing_run

        existing = await _load_authorized_existing()
        if existing is not None:
            return existing, False

        canonical_model = await _resolve_model_name(db, agent, model)
        now = datetime.now(UTC)
        child_id = uuid.uuid4()
        child_user_id = (
            parent.user_id
            if parent.user_id == resolved_user_id
            and parent.source_channel not in {"agent", "trigger", SUBAGENT_CHANNEL}
            else None
        )
        project = None
        project_member = None
        project_capabilities: list[dict] = []
        project_tool_policy_snapshot: dict = {}
        agent_runtime_workspace_snapshot: dict[str, str] = {}
        if parent.project_id is not None:
            from app.models.project import Project, ProjectCapabilityBinding, ProjectMemberSnapshot

            project = await db.get(Project, parent.project_id)
            project_member = (
                await db.execute(
                    select(ProjectMemberSnapshot).where(
                        ProjectMemberSnapshot.project_id == parent.project_id,
                        ProjectMemberSnapshot.agent_id == agent_id,
                        ProjectMemberSnapshot.is_enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if project_member is None:
                raise SubagentError("目标 Agent 不是当前项目的已启用成员。")
            bindings = (
                (
                    await db.execute(
                        select(ProjectCapabilityBinding).where(
                            ProjectCapabilityBinding.project_id == parent.project_id,
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
            project_capabilities = [
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
            project_tool_policy_snapshot = (
                dict(dict((project.settings or {}).get("policies") or {}).get("project_tools") or {})
                if project is not None
                else {}
            )
            if str(getattr(agent, "scope", "") or "").strip().lower() == "project":
                from app.services.agent_runtime_workspace import (
                    project_agent_runtime_workspace,
                )

                if project is None or getattr(agent, "project_id", None) != project.id:
                    raise SubagentError("项目专用 Agent 不能在其他项目中运行。")
                agent_runtime_workspace_snapshot = project_agent_runtime_workspace(
                    agent_id=agent.id,
                    tenant_id=project.tenant_id,
                    project_id=project.id,
                ).as_session_config()

        task_metadata = dict(input_metadata or {})
        if project is not None and canonical_model is None and not task_metadata.get("model_id"):
            task_metadata["model_id"] = await _project_turn_model_id(db, agent, project)

        child = ChatSession(
            id=child_id,
            agent_id=agent_id,
            project_id=parent.project_id,
            user_id=child_user_id,
            title=child_title,
            source_channel=SUBAGENT_CHANNEL,
            is_primary=False,
            is_group=False,
            im_config={
                "project_id": str(parent.project_id) if parent.project_id else None,
                "project_group_session_id": str(parent.id) if parent.project_id else None,
                "project_member_id": str(project_member.id) if project_member else None,
                "project_membership_generation": (
                    max(
                        1,
                        int(
                            dict(dict(project_member.config_snapshot or {}).get("membership") or {}).get("generation")
                            or 1
                        ),
                    )
                    if project_member
                    else None
                ),
                "membership_revoked": False,
                "project_role_snapshot": "leader" if project_member and project_member.is_leader else "participant",
                "project_name_snapshot": project.name if project is not None else "",
                "project_goal_snapshot": project.goal if project is not None else "",
                "project_success_criteria_snapshot": list(project.success_criteria or [])
                if project is not None
                else [],
                "project_member_name_snapshot": project_member.name_snapshot if project_member else agent.name,
                "project_member_role_snapshot": project_member.role_snapshot if project_member else "",
                "project_tool_policy_snapshot": project_tool_policy_snapshot,
                "member_config_snapshot": dict(project_member.config_snapshot or {}) if project_member else {},
                "capability_snapshot": project_capabilities,
                "agent_runtime_workspace": agent_runtime_workspace_snapshot,
            },
            created_at=now,
            last_message_at=now,
        )
        run = SubagentRun(
            id=child_id,
            parent_session_id=parent.id,
            project_id=parent.project_id,
            project_member_id=project_member.id if project_member else None,
            execution_user_id=resolved_user_id,
            origin_tool_call_id=call_id,
            mode=normalized_mode,
            model=canonical_model,
            soul=bool(soul),
            memory=bool(memory),
            status=RUN_QUEUED,
        )
        db.add_all([child, run])
        await db.flush()

        message_time = now
        if fork:
            if turn_anchor_id is None:
                raise SubagentError("Fork 当前会话需要一个有效的当前轮次锚点。")
            message_time = await _copy_fork_context(
                db,
                parent=parent,
                child=child,
                execution_agent_id=agent_id,
                turn_anchor_id=turn_anchor_id,
                created_at=message_time,
                ctx_size=agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE,
            )

        message_time += timedelta(microseconds=1)
        task_row = ChatMessage(
            agent_id=agent_id,
            user_id=resolved_user_id,
            sender_user_id=resolved_user_id,
            role="user",
            content=task_text,
            conversation_id=str(child_id),
            message_meta={
                **task_metadata,
                "kind": SUBAGENT_INPUT,
                "subagent_input_state": INPUT_PENDING,
                "attachments": [],
                **({"project_run_id": str(project_run_id)} if project_run_id else {}),
            },
            created_at=message_time,
        )
        db.add(task_row)
        child.last_message_at = message_time
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await _load_authorized_existing()
            if existing is None:
                raise
            return existing, False
        await db.refresh(run)
        return run, True


async def append_subagent_message(
    *,
    agent_id: uuid.UUID,
    parent_session_id: str,
    subagent_id: str,
    message: str,
    execution_user_id: uuid.UUID,
    origin_tool_call_id: str,
    project_run_id: uuid.UUID | None = None,
    input_metadata: dict | None = None,
) -> str:
    content = str(message or "").strip()
    if not content:
        raise SubagentError("message 不能为空。")
    try:
        parent_id = uuid.UUID(str(parent_session_id))
        child_id = uuid.UUID(str(subagent_id))
    except (TypeError, ValueError):
        raise SubagentError("subagent_id 无效。") from None
    call_id = str(origin_tool_call_id or "").strip()
    if not call_id:
        raise SubagentError("缺少当前工具调用标识，无法可靠追加消息。")
    event_key = f"subagent-parent-input:{child_id}:{call_id}"

    async with async_session() as db:
        run = await db.get(SubagentRun, child_id, with_for_update=True)
        if run is None or not _run_owned_by_parent(run, parent_id):
            raise SubagentError("Subagent 不存在，或不属于当前 Session。")
        if run.execution_user_id != execution_user_id:
            raise SubagentError("当前执行身份无权操作这个 Subagent。")
        if run.status == RUN_CANCELLED:
            raise SubagentError("Subagent 已停止，不能恢复。")

        child = await db.get(ChatSession, child_id)
        if child is None or child.agent_id != agent_id:
            raise SubagentError("当前 Agent 无权操作这个 Subagent。")
        try:
            agent = await _validate_execution_identity(db, run, child)
        except RuntimeError as exc:
            raise SubagentError("项目成员已退出，不能继续这个工作会话。") from exc
        existing = (
            await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == event_key))
        ).scalar_one_or_none()
        if existing is not None:
            return run.status
        supplied_metadata = dict(input_metadata or {})
        if not await _project_accepts_new_subagent_anchor(
            db,
            run,
            initializing_project_run_id=(project_run_id if supplied_metadata.get("project_dispatch") else None),
        ):
            raise SubagentError("项目已暂停；恢复项目后才能继续这个工作会话。")
        from app.services.confirmation_service import (
            find_pending_confirmation,
            ignore_pending_confirmation_for_new_input,
        )

        pending_confirmation = await find_pending_confirmation(
            db,
            agent_id=child.agent_id,
            conversation_id=str(child.id),
        )
        ignored_confirmation = None
        if pending_confirmation is not None:
            if pending_confirmation.force_confirmation:
                raise SubagentError("Subagent 正在等待人工确认，不能追加新的项目消息。")
            ignored_confirmation = await ignore_pending_confirmation_for_new_input(
                db,
                pending_confirmation,
            )
        now = datetime.now(UTC)
        if run.project_id is not None and not run.model and not supplied_metadata.get("model_id"):
            from app.models.project import Project

            project = await db.get(Project, run.project_id)
            if project is None:
                raise SubagentError("项目不存在，不能继续这个工作会话。")
            supplied_metadata["model_id"] = await _project_turn_model_id(db, agent, project)
        supplied_attachments = list(supplied_metadata.pop("attachments", []) or [])
        db.add(
            ChatMessage(
                agent_id=child.agent_id,
                user_id=run.execution_user_id,
                sender_user_id=run.execution_user_id,
                role="user",
                content=content,
                conversation_id=str(child_id),
                external_event_key=event_key,
                message_meta={
                    **supplied_metadata,
                    "kind": SUBAGENT_INPUT,
                    "subagent_input_state": INPUT_PENDING,
                    "attachments": supplied_attachments,
                    **({"project_run_id": str(project_run_id)} if project_run_id else {}),
                },
                created_at=now,
            )
        )
        child.last_message_at = now
        if run.status in {RUN_COMPLETED, RUN_FAILED, RUN_WAITING}:
            run.status = RUN_QUEUED
            run.mode = "async"
            run.lease_owner = None
            run.lease_expires_at = None
        await db.commit()
        if ignored_confirmation:
            from app.services.confirmation_service import publish_ignored_confirmation

            await publish_ignored_confirmation(
                pending_confirmation,
                ignored_confirmation,
            )
        return run.status


async def send_subagent_message_to_parent(
    *,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    origin_tool_call_id: str,
    subagent_session_id: str,
    message: str,
) -> None:
    content = str(message or "").strip()
    if not content:
        raise SubagentError("message 不能为空。")
    try:
        child_id = uuid.UUID(str(subagent_session_id))
    except (TypeError, ValueError):
        raise SubagentError("当前 Subagent Session 无效。") from None
    call_id = str(origin_tool_call_id or "").strip()
    if not call_id:
        raise SubagentError("缺少当前工具调用标识，无法可靠发送消息。")
    event_key = f"subagent-child-message:{child_id}:{call_id}"

    async with async_session() as db:
        run = await db.get(SubagentRun, child_id, with_for_update=True)
        child = await db.get(ChatSession, child_id)
        if run is None or child is None or child.source_channel != SUBAGENT_CHANNEL:
            raise SubagentError("send_message_to_parent 只能由 Subagent 使用。")
        if child.agent_id != agent_id or run.execution_user_id != execution_user_id:
            raise SubagentError("当前执行身份无权从这个 Subagent 发送消息。")
        if run.status == RUN_CANCELLED:
            raise SubagentError("Subagent 已停止。")
        try:
            await _validate_execution_identity(db, run, child)
        except RuntimeError as exc:
            raise SubagentError("项目成员已退出，不能继续发送项目消息。") from exc
        existing = (
            await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == event_key))
        ).scalar_one_or_none()
        if existing is not None:
            return
        now = datetime.now(UTC)
        db.add(
            ChatMessage(
                agent_id=child.agent_id,
                user_id=run.execution_user_id,
                sender_agent_id=child.agent_id,
                role="assistant",
                content=content,
                conversation_id=str(child_id),
                external_event_key=event_key,
                message_meta={
                    "kind": SUBAGENT_PARENT_MESSAGE,
                    "subagent_wake": run.mode == "async",
                    "attachments": [],
                },
                created_at=now,
            )
        )
        child.last_message_at = now
        await db.commit()


async def stop_subagent(
    *,
    agent_id: uuid.UUID,
    parent_session_id: str,
    subagent_id: str,
    execution_user_id: uuid.UUID,
) -> str:
    try:
        parent_id = uuid.UUID(str(parent_session_id))
        child_id = uuid.UUID(str(subagent_id))
    except (TypeError, ValueError):
        raise SubagentError("subagent_id 无效。") from None

    async with async_session() as db:
        run = await db.get(SubagentRun, child_id, with_for_update=True)
        if run is None or not _run_owned_by_parent(run, parent_id):
            raise SubagentError("Subagent 不存在，或不属于当前 Session。")
        if run.execution_user_id != execution_user_id:
            raise SubagentError("当前执行身份无权操作这个 Subagent。")
        child = await db.get(ChatSession, child_id)
        if child is None or child.agent_id != agent_id:
            raise SubagentError("当前 Agent 无权操作这个 Subagent。")
        if run.status in TERMINAL_STATUSES:
            return run.status

        run.status = RUN_CANCELLED
        run.lease_owner = None
        run.lease_expires_at = None
        rows = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(child_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"]
                        .as_string()
                        .in_([INPUT_PENDING, INPUT_PROCESSING]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            meta = _message_meta(row)
            meta["subagent_input_state"] = INPUT_CANCELLED
            meta["turn_status"] = "cancelled"
            row.message_meta = meta
        await db.commit()

    async with _running_tasks_guard:
        task = _running_tasks.get(child_id)
        if task is not None and not task.done():
            task.cancel()
    return RUN_CANCELLED


async def cancel_local_subagent_tasks(run_ids: list[uuid.UUID]) -> None:
    """Interrupt local workers after their durable runs were revoked.

    Cross-instance workers observe the cancelled status on their next lease,
    round or tool boundary. This local fast path closes the same-instance gap.
    """

    if not run_ids:
        return
    async with _running_tasks_guard:
        tasks = [_running_tasks.get(run_id) for run_id in run_ids if _running_tasks.get(run_id) is not None]
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()


async def prepare_subagent_tools(
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    execution_user_id: uuid.UUID | None = None,
) -> list[dict]:
    """Return the Agent's normal tools with the child-only protocol surface."""
    from app.models.tool import AgentTool, Tool
    from app.services.agent_tools import get_agent_tools_for_llm
    from app.services.tool_enablement import resolved_agent_tool_enabled

    tools = await get_agent_tools_for_llm(agent_id)
    hidden = {
        "run_subagent",
        "send_message_to_subagent",
        "stop_subagent",
        "send_message_to_parent",
        "request_confirmation",
    }
    child_tools = [tool for tool in tools if tool.get("function", {}).get("name") not in hidden]
    project_tools: list[dict] = []
    is_project_runtime = False
    async with async_session() as db:
        child = await db.get(ChatSession, session_id) if session_id else None
        if child is not None and child.source_channel == SUBAGENT_CHANNEL and child.project_id is not None:
            is_project_runtime = True
            if execution_user_id is None:
                raise ValueError("Project Subagent tool preparation requires its execution user identity")
            from app.services.project_runtime_tools import (
                load_project_runtime_scope,
                project_runtime_tool_schemas,
            )

            project, member, scoped_child, _run = await load_project_runtime_scope(
                db,
                session_id=child.id,
                agent_id=agent_id,
                execution_user_id=execution_user_id,
            )
            runtime_config = dict(scoped_child.im_config or {})
            # Builtins remain governed by the normal Agent tool policy. MCP is
            # deny-by-default and re-enabled only by the immutable project
            # snapshot captured when this validated child was created.
            from app.models.tool import Tool

            all_mcp_names = set((await db.execute(select(Tool.name).where(Tool.type == "mcp"))).scalars())
            allowed_server_ids = {
                uuid.UUID(str(entry["capability_id"]))
                for entry in runtime_config.get("capability_snapshot", [])
                if isinstance(entry, dict) and entry.get("type") == "mcp" and entry.get("capability_id")
            }
            allowed_mcp_names = set()
            if allowed_server_ids:
                allowed_mcp_names = set(
                    (
                        await db.execute(
                            select(Tool.name).where(
                                Tool.type == "mcp",
                                Tool.mcp_server_id.in_(allowed_server_ids),
                            )
                        )
                    ).scalars()
                )
            child_tools = [
                item
                for item in child_tools
                if item.get("function", {}).get("name") not in all_mcp_names
                or item.get("function", {}).get("name") in allowed_mcp_names
            ]
            # Project collaboration has one durable, scoped path. Generic
            # Agent/session messaging bypasses the project member snapshot,
            # role contract, A2A causality, and project audit trail; exposing
            # it here caused cross-session delivery failures and mechanical
            # status notifications instead of professional collaboration.
            project_collaboration_bypass_tools = {
                "send_message_to_agent",
                "send_file_to_agent",
                "send_message_to_parent",
                "send_session_message",
            }
            child_tools = [
                item
                for item in child_tools
                if item.get("function", {}).get("name") not in project_collaboration_bypass_tools
            ]
            project_tools = project_runtime_tool_schemas(project, member)
        row = (
            await db.execute(
                select(Tool, AgentTool)
                .outerjoin(
                    AgentTool,
                    (AgentTool.tool_id == Tool.id) & (AgentTool.agent_id == agent_id),
                )
                .where(Tool.name == "send_message_to_parent")
                .limit(1)
            )
        ).first()
    if project_tools:
        projected_names = {item["function"]["name"] for item in project_tools}
        child_tools = [item for item in child_tools if item.get("function", {}).get("name") not in projected_names]
    if is_project_runtime:
        return [*child_tools, *project_tools]
    parent_tool = row[0] if row else None
    if parent_tool is None:
        raise RuntimeError("send_message_to_parent builtin tool is not seeded")
    assignment = row[1]
    if not parent_tool.enabled or not resolved_agent_tool_enabled(
        parent_tool.name,
        assignment,
    ):
        return [*child_tools, *project_tools]
    child_tools.append(
        {
            "type": "function",
            "function": {
                "name": parent_tool.name,
                "description": parent_tool.description,
                "parameters": parent_tool.parameters_schema,
            },
        }
    )
    child_tools.extend(project_tools)
    return child_tools


async def _claim_subagent(run_id: uuid.UUID | None = None) -> uuid.UUID | None:
    from app.models.project import Project

    now = datetime.now(UTC)
    async with async_session() as db:
        conditions = [
            or_(
                SubagentRun.status == RUN_QUEUED,
                and_(
                    SubagentRun.status == RUN_RUNNING,
                    SubagentRun.lease_expires_at < now,
                ),
            ),
            or_(
                SubagentRun.project_id.is_(None),
                exists(
                    select(Project.id).where(
                        Project.id == SubagentRun.project_id,
                        Project.status == "running",
                    )
                ),
            ),
        ]
        if run_id is not None:
            conditions.append(SubagentRun.id == run_id)
        run = (
            await db.execute(
                select(SubagentRun)
                .where(*conditions)
                .order_by(SubagentRun.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        if not await _project_accepts_new_subagent_anchor(db, run):
            return None
        run.status = RUN_RUNNING
        run.lease_owner = settings.INSTANCE_ID
        run.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return run.id


async def _mark_input_project_runs_running(
    db,
    *,
    run: SubagentRun,
    input_rows: list[ChatMessage],
) -> None:
    """Advance only the ProjectRuns explicitly owned by active child inputs.

    A project child is reusable, so neither its session id nor its claim time
    identifies which ProjectRun is executing.  The durable input row is the
    outbox/inbox hand-off and carries the exact ``project_run_id``.  Updating
    from that association in the same transaction that marks the input as
    processing makes the transition idempotent and safe when an expired lease
    is reclaimed after restart.
    """
    if run.project_id is None:
        return

    project_run_ids: set[uuid.UUID] = set()
    for row in input_rows:
        raw_project_run_id = _message_meta(row).get("project_run_id")
        if not raw_project_run_id:
            continue
        try:
            project_run_ids.add(uuid.UUID(str(raw_project_run_id)))
        except (TypeError, ValueError):
            logger.warning(
                "[subagent] ignoring invalid project_run_id on active input=%s",
                row.id,
            )
    if not project_run_ids:
        return

    from app.models.project import ProjectRun
    from app.services.project_service import apply_run_status

    project_runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.id.in_(project_run_ids),
                    ProjectRun.project_id == run.project_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for project_run in project_runs:
        # apply_run_status reconciles finished_at first and never regresses a
        # succeeded/failed/cancelled execution back to a live state.
        apply_run_status(project_run, "running")


async def _load_or_start_input(
    run_id: uuid.UUID,
) -> tuple[ChatMessage, bool] | None:
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status != RUN_RUNNING or run.lease_owner != settings.INSTANCE_ID:
            return None
        processing = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                    ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        anchor = processing
        recovering = anchor is not None
        if anchor is None:
            anchor = (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
        if anchor is None:
            run.status = RUN_COMPLETED
            run.lease_owner = None
            run.lease_expires_at = None
            await db.commit()
            return None
        if not recovering and not await _project_accepts_new_subagent_anchor(db, run):
            # Keep the pending input durable and release this worker.  The
            # canonical claim query will pick it up after the project resumes.
            run.status = RUN_QUEUED
            run.lease_owner = None
            run.lease_expires_at = None
            await db.commit()
            return None
        meta = _message_meta(anchor)
        meta.update(
            {
                "subagent_input_state": INPUT_PROCESSING,
                "subagent_turn_anchor_id": str(anchor.id),
                "turn_status": "running",
            }
        )
        anchor.message_meta = meta
        await db.flush()
        active_inputs = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string() == str(anchor.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        await _mark_input_project_runs_running(
            db,
            run=run,
            input_rows=list(active_inputs),
        )
        run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return anchor, recovering


async def _assert_subagent_running(run_id: uuid.UUID) -> None:
    """Fail before a new round/tool side effect after stop, lease loss, or revocation."""
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id)
        if run is None or run.status == RUN_CANCELLED:
            raise asyncio.CancelledError
        if run.status != RUN_RUNNING or run.lease_owner != settings.INSTANCE_ID:
            raise RuntimeError("Subagent lease lost")
        child = await db.get(ChatSession, run_id)
        if child is None:
            raise RuntimeError("Subagent session no longer exists")
        await _validate_execution_identity(db, run, child)


async def _drain_subagent_inbox(
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
) -> list[dict]:
    """Atomically inject appended parent messages at an LLM round boundary."""
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status == RUN_CANCELLED:
            raise asyncio.CancelledError
        if run.status != RUN_RUNNING or run.lease_owner != settings.INSTANCE_ID:
            raise RuntimeError("Subagent lease lost")
        pending = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        injected: list[dict] = []
        for row in pending:
            meta = _message_meta(row)
            meta.update(
                {
                    "subagent_input_state": INPUT_PROCESSING,
                    "subagent_turn_anchor_id": str(anchor_id),
                    "turn_status": "running",
                }
            )
            row.message_meta = meta
            injected.append({"role": "user", "content": row.content})
        await _mark_input_project_runs_running(
            db,
            run=run,
            input_rows=list(pending),
        )
        run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return injected


async def _park_subagent_confirmation(
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
) -> bool:
    """Release a child lease when the unified caller suspended on confirmation.

    The confirmation tool row remains the single durable truth.  A pending row
    parks the Run; a row resolved before this worker observes it re-queues the
    same processing anchor for standard restart recovery.  No confirmation
    transcript or continuation logic is duplicated here.
    """
    from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME

    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status == RUN_CANCELLED:
            return False
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.role == "tool_call",
                        ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor_id),
                    )
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                )
            )
            .scalars()
            .all()
        )
        confirmation_payload = None
        for row in rows:
            try:
                payload = json.loads(row.content or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME:
                confirmation_payload = payload
                break
        if confirmation_payload is None:
            return False
        pending = confirmation_payload.get("status") == "pending"
        run.status = RUN_WAITING if pending else RUN_QUEUED
        run.lease_owner = None
        run.lease_expires_at = None
        anchor = await db.get(ChatMessage, anchor_id)
        if anchor is not None:
            anchor.message_meta = {
                **_message_meta(anchor),
                "turn_status": "waiting_confirmation" if pending else "running",
            }
        from app.models.project import Project, ProjectEvent, ProjectRun
        from app.services.project_service import add_event

        processing = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string() == str(anchor_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        project_run_ids = {
            uuid.UUID(str(value)) for row in processing for value in [_message_meta(row).get("project_run_id")] if value
        }
        if project_run_ids:
            project_runs = (
                (
                    await db.execute(
                        select(ProjectRun).where(
                            ProjectRun.id.in_(project_run_ids),
                            ProjectRun.project_id == run.project_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for project_run in project_runs:
                if project_run.status not in {"succeeded", "failed", "cancelled"}:
                    project_run.status = "waiting" if pending else "queued"
                if pending:
                    existing = (
                        await db.execute(
                            select(ProjectEvent.id).where(
                                ProjectEvent.run_id == project_run.id,
                                ProjectEvent.event_type == "run.waiting_confirmation",
                            )
                        )
                    ).scalar_one_or_none()
                    project = await db.get(Project, project_run.project_id)
                    if existing is None and project is not None:
                        add_event(
                            db,
                            project,
                            "run.waiting_confirmation",
                            "Project run is waiting for human confirmation",
                            actor_agent_id=project_run.agent_id,
                            work_item_id=project_run.work_item_id,
                            run_id=project_run.id,
                            metadata={
                                "project_run_id": str(project_run.id),
                                "session_id": str(run.id),
                                "subagent_session_id": str(run.id),
                            },
                        )
        await db.commit()
        return True


async def resume_subagent_after_confirmation(run_id: uuid.UUID) -> bool:
    """Persist a resolved child confirmation and run it when its project allows."""
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status in TERMINAL_STATUSES:
            return False
        if run.status == RUN_WAITING:
            run.status = RUN_QUEUED
            run.lease_owner = None
            run.lease_expires_at = None
            await db.commit()
        elif run.status == RUN_RUNNING:
            # The original worker will observe the resolved tool row and queue
            # restart recovery after the caller returns.
            return True
    # Confirmation resolution is durable even while the project is paused.
    # The canonical claim boundary leaves it queued until runtime resumes.
    claimed = await _claim_subagent(run_id)
    if claimed is not None:
        await execute_claimed_subagent(claimed)
    return True


async def _finish_subagent_turn(
    *,
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
    reply: str,
    failed: bool,
    thinking: str | None = None,
    reply_quality: dict | None = None,
) -> bool:
    """Persist the reply and lifecycle transition behind the same Run lock."""
    from app.services.active_turns import wait_for_current_turn_stop_resolution
    from app.services.chat_history import persist_assistant_reply_row

    # Resolve a concurrent stop before opening a transaction. Waiting after the
    # input rows have been mutated can autoflush the anchor and deadlock the
    # stop transaction that must mark that same row as cancelled.
    await wait_for_current_turn_stop_resolution()

    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status != RUN_RUNNING or run.lease_owner != settings.INSTANCE_ID:
            return False
        child = await db.get(ChatSession, run_id)
        if child is None:
            return False
        processed = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string() == str(anchor_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in processed:
            meta = _message_meta(row)
            meta["subagent_input_state"] = INPUT_DONE
            meta["turn_status"] = "completed" if not failed else "failed"
            row.message_meta = meta

        pending_exists = bool(
            (
                await db.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        )
        # Pending parent input always wins over the current turn's outcome.  A
        # failed provider/tool round may finish its already-dispatched inputs,
        # but it cannot strand later pending messages behind a terminal Run.
        terminal = not pending_exists
        kind = (
            SUBAGENT_FAILURE
            if failed and terminal
            else "subagent_turn_failure"
            if failed
            else SUBAGENT_COMPLETION
            if terminal
            else "subagent_turn_result"
        )
        content = (reply or "").strip() or ("Subagent 执行失败，未返回错误详情。" if failed else "Subagent 已完成。")
        # One durable child Session may process many separately auditable
        # project mentions. Complete every ProjectRun whose exact input was
        # consumed by this turn; do not leave the Runs UI permanently queued.
        from app.models.project import Project, ProjectEvent, ProjectRun
        from app.services.project_service import add_event

        now = datetime.now(UTC)
        project_run_ids: set[uuid.UUID] = set()
        for row in processed:
            raw_project_run_id = _message_meta(row).get("project_run_id")
            if raw_project_run_id:
                try:
                    project_run_ids.add(uuid.UUID(str(raw_project_run_id)))
                except (TypeError, ValueError):
                    logger.warning(
                        "[subagent] ignoring invalid project_run_id on input=%s",
                        row.id,
                    )
        if project_run_ids:
            project_runs = (
                (
                    await db.execute(
                        select(ProjectRun).where(
                            ProjectRun.id.in_(project_run_ids),
                            ProjectRun.project_id == run.project_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for project_run in project_runs:
                project_run.status = "failed" if failed else "succeeded"
                project_run.finished_at = now
                project_run.output = {
                    **dict(project_run.output or {}),
                    "subagent_run_id": str(run.id),
                    "subagent_session_id": str(run.id),
                    "result": content,
                    **({"reply_quality": reply_quality} if reply_quality else {}),
                }
                project_run.error = content if failed else None
                terminal_event_type = "run.failed" if failed else "run.succeeded"
                terminal_event_exists = (
                    await db.execute(
                        select(ProjectEvent.id).where(
                            ProjectEvent.run_id == project_run.id,
                            ProjectEvent.event_type == terminal_event_type,
                        )
                    )
                ).scalar_one_or_none()
                project = await db.get(Project, project_run.project_id)
                if project is not None and terminal_event_exists is None:
                    add_event(
                        db,
                        project,
                        terminal_event_type,
                        "Project run failed" if failed else "Project run succeeded",
                        actor_user_id=project_run.initiated_by_user_id,
                        actor_agent_id=child.agent_id,
                        work_item_id=project_run.work_item_id,
                        run_id=project_run.id,
                        metadata={
                            "project_run_id": str(project_run.id),
                            "subagent_run_id": str(run.id),
                            "subagent_session_id": str(run.id),
                            "session_id": str(dict(project_run.output or {}).get("session_id") or run.id),
                            "status": project_run.status,
                        },
                    )
        await persist_assistant_reply_row(
            db,
            agent_id=child.agent_id,
            user_id=run.execution_user_id,
            conversation_id=str(run_id),
            content=content,
            thinking=thinking,
            message_meta={
                "kind": kind,
                "subagent_wake": terminal and run.mode == "async",
                "attachments": [],
                "project_run_ids": [str(value) for value in sorted(project_run_ids, key=str)],
                **({"reply_quality": reply_quality} if reply_quality else {}),
            },
            turn_anchor_id=anchor_id,
        )
        for row in processed:
            meta = _message_meta(row)
            meta["subagent_input_state"] = INPUT_DONE
            meta["turn_status"] = "completed" if not failed else "failed"
            row.message_meta = meta
        child.last_message_at = now
        if terminal:
            run.status = RUN_FAILED if failed else RUN_COMPLETED
            run.lease_owner = None
            run.lease_expires_at = None
        else:
            run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return terminal


async def execute_claimed_subagent(run_id: uuid.UUID) -> None:
    """Run one claimed child until its inbox is empty or ownership is lost."""
    from app.services.channel_llm import _call_agent_llm
    from app.services.chat_history import load_history_prefix_before_anchor
    from app.services.llm.caller import is_error_result

    current = asyncio.current_task()

    async def _lease_heartbeat() -> None:
        failures = 0
        while True:
            await asyncio.sleep(max(5, LEASE_SECONDS // 3))
            try:
                async with async_session() as heartbeat_db:
                    owned = await heartbeat_db.get(
                        SubagentRun,
                        run_id,
                        with_for_update=True,
                    )
                    if owned is None or owned.status != RUN_RUNNING or owned.lease_owner != settings.INSTANCE_ID:
                        if current is not None and not current.done():
                            current.cancel()
                        return
                    owned.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
                    await heartbeat_db.commit()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - lease safety boundary
                failures += 1
                logger.warning(f"[subagent] lease renewal failed run={run_id} attempt={failures}: {exc}")
                if failures >= 3:
                    if current is not None and not current.done():
                        current.cancel()
                    return

    heartbeat = asyncio.create_task(
        _lease_heartbeat(),
        name=f"subagent-lease:{run_id}",
    )
    if current is not None:
        async with _running_tasks_guard:
            _running_tasks[run_id] = current
    current_anchor_id: uuid.UUID | None = None
    active_turn_capacity: AsyncExitStack | None = None
    try:
        while True:
            claimed_input = await _load_or_start_input(run_id)
            if claimed_input is None:
                return
            anchor, recovering = claimed_input
            current_anchor_id = anchor.id
            anchor_id = anchor.id
            anchor_content = anchor.content
            tenant_id = await _subagent_workload_tenant_id(run_id)
            active_turn_capacity = AsyncExitStack()
            try:
                await active_turn_capacity.enter_async_context(
                    get_workload_capacity().slot(WorkloadKind.PROJECT, tenant_id)
                )
            except WorkloadOverloadedError:
                await active_turn_capacity.aclose()
                active_turn_capacity = None
                await _requeue_capacity_blocked_subagent(run_id, anchor_id)
                current_anchor_id = None
                return
            async with async_session() as db:
                run = await db.get(SubagentRun, run_id)
                child = await db.get(ChatSession, run_id)
                if run is None or child is None:
                    await active_turn_capacity.aclose()
                    active_turn_capacity = None
                    return
                agent = await _validate_execution_identity(db, run, child)
                ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
                if recovering:
                    from app.services.turn_recovery import (
                        prepare_recoverable_turn_history,
                    )

                    history = await prepare_recoverable_turn_history(
                        db,
                        anchor,
                        execution_agent_id=child.agent_id,
                        ctx_size=ctx_size,
                    )
                    if not history:
                        raise RuntimeError("Subagent durable turn recovery failed")
                else:
                    history = await load_history_prefix_before_anchor(
                        db,
                        agent_id=child.agent_id,
                        conversation_id=str(run_id),
                        turn_anchor_id=anchor.id,
                        ctx_size=ctx_size,
                    )
                    if history is None:
                        raise RuntimeError("Subagent fresh turn prefix changed")
                child_agent_id = child.agent_id
                child_session_id = child.id
                execution_user_id = run.execution_user_id
                model_name = run.model
                include_soul = run.soul
                include_memory = run.memory
                from app.services.agent_runtime_workspace import resolve_agent_runtime_workspace

                runtime_workspace = resolve_agent_runtime_workspace(
                    agent_id=agent.id,
                    agent_scope=getattr(agent, "scope", None),
                    agent_project_id=getattr(agent, "project_id", None),
                    tenant_id=agent.tenant_id,
                    session_project_id=child.project_id,
                    session_config=dict(child.im_config or {}),
                )

            tools = await prepare_subagent_tools(
                child_agent_id,
                child_session_id,
                execution_user_id=execution_user_id,
            )
            thinking_parts: list[str] = []

            async def _capture_thinking(
                text: str,
                parts: list[str] = thinking_parts,
            ) -> None:
                if text:
                    parts.append(str(text))

            async with async_session() as llm_db:

                async def _before_round(
                    _round: int,
                    active_anchor_id: uuid.UUID = anchor_id,
                ) -> list[dict]:
                    # ``_call_agent_llm`` resolves the Agent, model, scene, and
                    # anchor through this session before entering the provider
                    # loop. End that read transaction immediately before every
                    # network dispatch so concurrent long-running Subagents do
                    # not pin one database-pool connection each. The session
                    # factory uses ``expire_on_commit=False``, so the resolved
                    # ORM values remain valid for the provider call.
                    if llm_db.in_transaction():
                        await llm_db.commit()
                    return await _drain_subagent_inbox(run_id, active_anchor_id)

                reply = await _call_agent_llm(
                    llm_db,
                    child_agent_id,
                    "" if recovering else anchor_content,
                    session_id=str(run_id),
                    user_id=execution_user_id,
                    history=history,
                    recovery_hint=None,
                    continue_turn=recovering,
                    recovery_mode=recovering,
                    turn_anchor_id=anchor_id,
                    turn_type="subagent",
                    model_name=model_name,
                    include_soul=include_soul,
                    include_memory=include_memory,
                    prepared_tools=tools,
                    on_thinking=_capture_thinking,
                    before_round=_before_round,
                    before_tool_execution=lambda: _assert_subagent_running(run_id),
                    # Exact-session drawers are subscribers, never a second
                    # execution runtime.  Reuse the unified channel bridge for
                    # standard thinking/chunk/tool/done packets while this
                    # durable worker remains the sole model caller.
                    broadcast_web=True,
                    runtime_session=child,
                    runtime_workspace=runtime_workspace,
                )
            if not str(reply or "").strip() and await _park_subagent_confirmation(
                run_id,
                anchor_id,
            ):
                # ``request_confirmation`` is a standard suspended tool call.
                # Its ChatMessage row is the durable continuation point; this
                # worker only releases the child lease.
                await active_turn_capacity.aclose()
                active_turn_capacity = None
                return
            reply_text = str(reply or "")
            failed = (
                is_error_result(reply_text)
                or reply_text.startswith(
                    (
                        "⚠️ 数字员工未找到",
                        "⚠️ Subagent 指定模型 ",
                    )
                )
                or (reply_text.startswith("⚠️ ") and "未配置 LLM 模型" in reply_text)
            )
            reply_quality: dict | None = None
            if child.project_id is not None and not failed:
                from app.services.project_reply_quality import (
                    assess_project_reply,
                    build_project_reply_correction_prompt,
                )

                assessment = assess_project_reply(reply_text)
                if assessment.needs_correction:
                    correction_history = [dict(message) for message in history]
                    if not recovering:
                        correction_history.append({"role": "user", "content": anchor_content})
                    correction_history.append({"role": "assistant", "content": reply_text})
                    project_runtime = dict(child.im_config or {})
                    correction_prompt = build_project_reply_correction_prompt(
                        reply_text,
                        role=project_runtime.get("project_member_role_snapshot"),
                        is_owner=project_runtime.get("project_role_snapshot") == "leader",
                    )
                    async with async_session() as correction_db:
                        corrected_reply = await _call_agent_llm(
                            correction_db,
                            child_agent_id,
                            correction_prompt,
                            session_id=str(run_id),
                            user_id=execution_user_id,
                            history=correction_history,
                            recovery_hint=None,
                            continue_turn=False,
                            # This synthetic correction is already bounded by
                            # the original frozen context and must not trigger a
                            # second compaction/recovery branch.
                            recovery_mode=True,
                            turn_anchor_id=anchor_id,
                            turn_type="subagent",
                            model_name=model_name,
                            include_soul=include_soul,
                            include_memory=include_memory,
                            # Correction may improve prose only. It cannot replay
                            # tools, drain new inbox input, broadcast another
                            # stream, or create any project/A2A wake-up.
                            prepared_tools=[],
                            on_thinking=_capture_thinking,
                            before_tool_execution=lambda: _assert_subagent_running(run_id),
                            broadcast_web=False,
                            runtime_session=child,
                            runtime_workspace=runtime_workspace,
                        )
                    corrected_text = str(corrected_reply or "").strip()
                    corrected_failed = (
                        not corrected_text or is_error_result(corrected_text) or corrected_text.startswith("⚠️ ")
                    )
                    if not corrected_failed:
                        reply = corrected_reply
                        reply_text = corrected_text
                    reply_quality = {
                        "correction_attempted": True,
                        "correction_applied": not corrected_failed,
                        "initial_reasons": list(assessment.reasons),
                        "final_needs_correction": assess_project_reply(reply_text).needs_correction,
                    }
            terminal = await _finish_subagent_turn(
                run_id=run_id,
                anchor_id=anchor_id,
                reply=reply,
                failed=failed,
                thinking="".join(thinking_parts) or None,
                reply_quality=reply_quality,
            )
            await active_turn_capacity.aclose()
            active_turn_capacity = None
            if terminal:
                return
    except asyncio.CancelledError:
        try:
            from app.services.active_turns import is_current_turn_cancel_requested

            control_plane_cancelled = is_current_turn_cancel_requested()
            async with async_session() as cancel_db:
                owned = await cancel_db.get(SubagentRun, run_id, with_for_update=True)
                if owned is not None and owned.status == RUN_RUNNING and owned.lease_owner == settings.INSTANCE_ID:
                    owned.status = RUN_CANCELLED if control_plane_cancelled else RUN_QUEUED
                    owned.lease_owner = None
                    owned.lease_expires_at = None
                    if control_plane_cancelled:
                        rows = (
                            (
                                await cancel_db.execute(
                                    select(ChatMessage).where(
                                        ChatMessage.conversation_id == str(run_id),
                                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                                        ChatMessage.message_meta["subagent_input_state"].as_string()
                                        == INPUT_PROCESSING,
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        for row in rows:
                            meta = _message_meta(row)
                            meta["subagent_input_state"] = INPUT_CANCELLED
                            meta["turn_status"] = "cancelled"
                            row.message_meta = meta
                    await cancel_db.commit()
        except Exception as exc:  # noqa: BLE001 - lease expiry remains the fallback
            logger.warning(f"[subagent] cancelled run release deferred run={run_id}: {exc}")
        raise
    except Exception as exc:  # noqa: BLE001 - durable worker failure boundary
        logger.exception(f"[subagent] execution failed run={run_id}: {exc}")
        if current_anchor_id is not None:
            terminal = await _finish_subagent_turn(
                run_id=run_id,
                anchor_id=current_anchor_id,
                reply=f"Subagent 执行失败: {type(exc).__name__}: {str(exc)[:400]}",
                failed=True,
            )
            if not terminal:
                # `_finish_subagent_turn` deliberately lets later parent input
                # win over this failed round.  Release the lease immediately so
                # sync callers and another worker can process that pending input
                # without waiting for lease expiry.
                async with async_session() as retry_db:
                    owned = await retry_db.get(
                        SubagentRun,
                        run_id,
                        with_for_update=True,
                    )
                    pending_exists = bool(
                        owned is not None
                        and (
                            await retry_db.execute(
                                select(ChatMessage.id)
                                .where(
                                    ChatMessage.conversation_id == str(run_id),
                                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                                    ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                                )
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                    )
                    if (
                        owned is not None
                        and owned.status == RUN_RUNNING
                        and owned.lease_owner == settings.INSTANCE_ID
                        and pending_exists
                    ):
                        owned.status = RUN_QUEUED
                        owned.lease_owner = None
                        owned.lease_expires_at = None
                        await retry_db.commit()
    finally:
        if active_turn_capacity is not None:
            await active_turn_capacity.aclose()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        async with _running_tasks_guard:
            if _running_tasks.get(run_id) is current:
                _running_tasks.pop(run_id, None)


async def _latest_subagent_result(run_id: uuid.UUID) -> tuple[str, str, list[str]]:
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id)
        if run is None:
            raise SubagentError("Subagent 不存在。")
        result = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string().in_([SUBAGENT_COMPLETION, SUBAGENT_FAILURE]),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        parent_messages = (
            (
                await db.execute(
                    select(ChatMessage.content)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        return (
            run.status,
            result.content if result is not None else "",
            list(parent_messages),
        )


async def run_subagent_sync(run_id: uuid.UUID) -> tuple[str, str, list[str]]:
    """Claim locally when possible; otherwise wait for the durable owner."""
    while True:
        status, result, parent_messages = await _latest_subagent_result(run_id)
        if status in TERMINAL_STATUSES:
            return status, result, parent_messages
        claimed = await _claim_subagent(run_id)
        if claimed is not None:
            await execute_claimed_subagent(claimed)
        else:
            await asyncio.sleep(0.25)


async def _subagent_worker_loop() -> None:
    semaphore = asyncio.Semaphore(WORKER_CONCURRENCY)
    active: set[asyncio.Task] = set()

    async def _execute(run_id: uuid.UUID) -> None:
        async with semaphore:
            await execute_claimed_subagent(run_id)

    while True:
        active = {task for task in active if not task.done()}
        if len(active) >= WORKER_CONCURRENCY:
            await asyncio.sleep(0.1)
            continue
        try:
            claimed = await _claim_subagent()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - daemon must survive transient DB faults
            logger.exception(f"[subagent] worker claim failed: {exc}")
            await asyncio.sleep(1)
            continue
        if claimed is None:
            await asyncio.sleep(0.5)
            continue
        task = asyncio.create_task(_execute(claimed), name=f"subagent:{claimed}")
        active.add(task)


async def _pending_parent_events(limit: int = 50) -> list[uuid.UUID]:
    async with async_session() as db:
        parent_anchor = aliased(ChatMessage)
        parent_final = aliased(ChatMessage)
        project_materialized = aliased(ChatMessage)
        parent_session = aliased(ChatSession)
        completed_exists = exists(
            select(parent_final.id)
            .select_from(parent_anchor)
            .join(
                parent_final,
                parent_final.conversation_id == parent_anchor.conversation_id,
            )
            .where(
                parent_anchor.external_event_key == ("subagent-parent:" + cast(ChatMessage.id, String)),
                parent_final.role == "assistant",
                parent_final.message_meta["turn_anchor_id"].as_string() == cast(parent_anchor.id, String),
            )
        )
        project_materialized_exists = exists(
            select(project_materialized.id).where(
                project_materialized.external_event_key == ("project-subagent:" + cast(ChatMessage.id, String))
            )
        )
        rows = (
            (
                await db.execute(
                    select(ChatMessage.id)
                    .join(
                        SubagentRun,
                        cast(ChatMessage.conversation_id, String) == cast(SubagentRun.id, String),
                    )
                    .where(
                        ChatMessage.message_meta["subagent_wake"].as_boolean().is_(True),
                        ChatMessage.message_meta["kind"]
                        .as_string()
                        .in_(
                            [
                                SUBAGENT_PARENT_MESSAGE,
                                SUBAGENT_COMPLETION,
                                SUBAGENT_FAILURE,
                            ]
                        ),
                        ~completed_exists,
                        ~project_materialized_exists,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        # A crash can happen after the exact A2A timeline is committed (the
        # `project-subagent:*` key exists) but before its project-group handoff
        # is written.  That half-delivered row is no longer in the ordinary
        # parent queue, so reconcile it explicitly and idempotently.
        a2a_candidates = (
            (
                await db.execute(
                    select(ChatMessage.id)
                    .join(
                        SubagentRun,
                        cast(ChatMessage.conversation_id, String) == cast(SubagentRun.id, String),
                    )
                    .join(parent_session, parent_session.id == SubagentRun.parent_session_id)
                    .where(
                        ChatMessage.message_meta["subagent_wake"].as_boolean().is_(True),
                        ChatMessage.message_meta["kind"].as_string().in_([SUBAGENT_COMPLETION, SUBAGENT_FAILURE]),
                        parent_session.source_channel == "agent",
                        parent_session.project_id.is_not(None),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        a2a_handoffs: list[uuid.UUID] = []
        if a2a_candidates:
            expected_keys = {
                key
                for message_id in a2a_candidates
                for key in (
                    f"project-subagent:{message_id}",
                    f"project-a2a-group-reply:{message_id}",
                )
            }
            stored_keys = set(
                (
                    await db.execute(
                        select(ChatMessage.external_event_key).where(ChatMessage.external_event_key.in_(expected_keys))
                    )
                )
                .scalars()
                .all()
            )
            a2a_handoffs = [
                message_id
                for message_id in a2a_candidates
                if f"project-subagent:{message_id}" in stored_keys
                and f"project-a2a-group-reply:{message_id}" not in stored_keys
            ]
        return list(dict.fromkeys([*rows, *a2a_handoffs]))[:limit]


async def _a2a_completion_leader_policy(
    db,
    *,
    project_run,
    leader_agent_id: uuid.UUID | None,
    failed: bool,
) -> tuple[bool, str]:
    """Wake the owner only for owner-originated work or explicit escalation."""
    from app.models.project import ProjectWorkItem

    a2a = dict(dict((project_run.input or {}).get("dispatch") or {}).get("a2a") or {})
    try:
        from_agent_id = uuid.UUID(str(a2a.get("from_agent_id")))
    except (TypeError, ValueError):
        from_agent_id = None
    if leader_agent_id is not None and from_agent_id == leader_agent_id:
        return True, "owner_requested_completion"
    if failed:
        return True, "failed_completion"
    if str(a2a.get("mode") or "").strip() == "consult":
        return True, "decision_consult"
    if project_run.work_item_id is not None:
        work_item = await db.get(ProjectWorkItem, project_run.work_item_id)
        if work_item is not None and work_item.status in {"blocked", "review"}:
            return True, f"work_item_{work_item.status}"
    return False, "peer_completion_recorded"


async def _materialize_project_a2a_turn(
    *,
    event: ChatMessage,
    child: ChatSession,
    run: SubagentRun,
    parent: ChatSession,
) -> bool:
    """Project one child turn onto its exact visible A2A Session.

    The execution child remains the source of truth.  Timeline rows are copied
    with stable external keys so the ordinary Web Chat renderer can display the
    same thinking/tool/final-reply contract without inventing an A2A renderer.
    """
    from app.models.project import Project, ProjectEvent, ProjectRun
    from app.services.project_service import add_event

    event_meta = _message_meta(event)
    raw_project_run_ids = list(event_meta.get("project_run_ids") or [])
    project_run_ids: list[uuid.UUID] = []
    for value in raw_project_run_ids:
        try:
            project_run_ids.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not project_run_ids:
        return True

    async with async_session() as db:
        project_runs = (
            (
                await db.execute(
                    select(ProjectRun).where(
                        ProjectRun.id.in_(project_run_ids),
                        ProjectRun.project_id == parent.project_id,
                        ProjectRun.trigger_type == "a2a",
                    )
                )
            )
            .scalars()
            .all()
        )
        if not project_runs:
            return True
        project_run = project_runs[0]
        anchor = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(child.id),
                    ChatMessage.message_meta["project_run_id"].as_string() == str(project_run.id),
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if anchor is None:
            return True

        trace_rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(child.id),
                        or_(
                            ChatMessage.id == event.id,
                            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
                        ),
                        ChatMessage.role.in_(["assistant", "tool_call"]),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        if not any(row.id == event.id for row in trace_rows):
            trace_rows.append(event)
            trace_rows.sort(key=lambda row: (row.created_at, row.id))

        for row in trace_rows:
            external_key = f"project-subagent:{row.id}" if row.id == event.id else f"project-a2a-trace:{row.id}"
            exists_row = (
                await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == external_key))
            ).scalar_one_or_none()
            if exists_row is not None:
                continue
            metadata = copy.deepcopy(_message_meta(row))
            metadata.pop("subagent_wake", None)
            metadata.update(
                {
                    "kind": "project_a2a_trace",
                    "project_id": str(parent.project_id),
                    "project_run_id": str(project_run.id),
                    "a2a_session_id": str(parent.id),
                    "subagent_run_id": str(child.id),
                    "subagent_session_id": str(child.id),
                    "source_child_message_id": str(row.id),
                    "execution_agent_id": str(child.agent_id),
                    "visible_to_group": False,
                }
            )
            db.add(
                ChatMessage(
                    agent_id=parent.agent_id,
                    user_id=run.execution_user_id,
                    sender_agent_id=child.agent_id,
                    role=row.role,
                    content=row.content,
                    conversation_id=str(parent.id),
                    external_event_key=external_key,
                    message_meta=metadata,
                    thinking=row.thinking,
                    created_at=row.created_at,
                )
            )
        stored_parent = await db.get(ChatSession, parent.id, with_for_update=True)
        if stored_parent is not None:
            stored_parent.last_message_at = event.created_at or datetime.now(UTC)
        project = await db.get(Project, parent.project_id)
        existing_event = (
            await db.execute(
                select(ProjectEvent.id).where(
                    ProjectEvent.run_id == project_run.id,
                    ProjectEvent.event_type == "a2a.completed",
                )
            )
        ).scalar_one_or_none()
        dispatch_a2a = dict(dict((project_run.input or {}).get("dispatch") or {}).get("a2a") or {})
        if project is not None and existing_event is None:
            add_event(
                db,
                project,
                "a2a.completed",
                "Project Agent completed one exact A2A turn",
                actor_agent_id=child.agent_id,
                from_agent_id=(
                    uuid.UUID(str(dispatch_a2a["from_agent_id"])) if dispatch_a2a.get("from_agent_id") else None
                ),
                to_agent_id=child.agent_id,
                run_id=project_run.id,
                metadata={
                    "session_id": str(parent.id),
                    "a2a_session_id": str(parent.id),
                    "subagent_run_id": str(child.id),
                    "subagent_session_id": str(child.id),
                    "result_message_id": str(event.id),
                    "trace_message_ids": [str(row.id) for row in trace_rows],
                },
            )

        # Exact A2A is the visible peer-to-peer conversation, but its durable
        # completion must also re-enter the project's coordination loop.  The
        # project group is deliberately append-only: we mirror one final reply
        # there. Owner-originated work and explicit decision/blocking gates enter
        # the coalesced owner inbox; ordinary peer completion stays observable
        # without creating an unnecessary owner turn.
        group = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.project_id == parent.project_id,
                    ChatSession.source_channel == "project",
                )
                .order_by(ChatSession.created_at, ChatSession.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if group is not None:
            from app.models.project import ProjectMemberSnapshot

            group_external_key = f"project-a2a-group-reply:{event.id}"
            group_reply_exists = (
                await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == group_external_key))
            ).scalar_one_or_none()
            if group_reply_exists is None:
                leader_agent_id = (
                    await db.execute(
                        select(ProjectMemberSnapshot.agent_id).where(
                            ProjectMemberSnapshot.project_id == parent.project_id,
                            ProjectMemberSnapshot.is_leader.is_(True),
                            ProjectMemberSnapshot.is_enabled.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                is_leader_reply = leader_agent_id == child.agent_id
                should_wake_leader, completion_policy = await _a2a_completion_leader_policy(
                    db,
                    project_run=project_run,
                    leader_agent_id=leader_agent_id,
                    failed=event_meta.get("kind") == SUBAGENT_FAILURE,
                )
                leader_batch_state = (
                    "ignored_leader_self"
                    if is_leader_reply
                    else "pending"
                    if should_wake_leader
                    else "observed_peer_completion"
                )
                db.add(
                    ChatMessage(
                        agent_id=group.agent_id,
                        sender_agent_id=child.agent_id,
                        role="assistant",
                        content=event.content,
                        conversation_id=str(group.id),
                        external_event_key=group_external_key,
                        message_meta={
                            "kind": "project_subagent_reply",
                            "project_id": str(parent.project_id),
                            "visible_to_group": True,
                            "mentions": [],
                            "awakened_agent_ids": [],
                            "subagent_id": str(child.id),
                            "child_message_id": str(event.id),
                            "source_project_run_ids": [str(value) for value in project_run_ids],
                            "source_a2a_session_id": str(parent.id),
                            "attachments": event_meta.get("attachments", []),
                            "wake_policy": (
                                "leader_self_no_wake"
                                if is_leader_reply
                                else "leader_batch_pending"
                                if should_wake_leader
                                else "peer_completion_no_owner_wake"
                            ),
                            "leader_batch_state": leader_batch_state,
                            "leader_escalation_reason": completion_policy,
                            "default_leader_agent_id": (str(leader_agent_id) if leader_agent_id else None),
                        },
                        created_at=event.created_at,
                    )
                )
                group.last_message_at = event.created_at or datetime.now(UTC)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
        return True


async def _dispatch_parent_event(child_message_id: uuid.UUID) -> bool:
    from app.services.channel_dispatch import (
        ChannelReactions,
        chat_session_lock_key,
        run_channel_message,
    )
    from app.services.execution_identity import ExecutionIdentityError
    from app.services.turn_recovery import resume_turn

    async with async_session() as db:
        event = await db.get(ChatMessage, child_message_id)
        if event is None:
            return True
        try:
            child_id = uuid.UUID(str(event.conversation_id))
        except (TypeError, ValueError):
            return True
        run = await db.get(SubagentRun, child_id)
        child = await db.get(ChatSession, child_id)
        parent = await db.get(ChatSession, run.parent_session_id) if run else None
        if run is None or child is None or parent is None:
            return True
        lock_key = chat_session_lock_key(parent)

    if parent.source_channel == "agent" and parent.project_id is not None and child.project_id == parent.project_id:
        return await _materialize_project_a2a_turn(
            event=event,
            child=child,
            run=run,
            parent=parent,
        )

    if parent.source_channel == "project" and parent.project_id is not None:
        # Project Agent Group is an append-only coordination surface. Child
        # output becomes a visible group reply but never resumes the root/Leader
        # LLM, so completion cannot fan out into an implicit broadcast storm.
        external_key = f"project-subagent:{child_message_id}"
        async with async_session() as db:
            from app.models.project import ProjectMemberSnapshot

            existing = (
                await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == external_key))
            ).scalar_one_or_none()
            if existing is not None:
                return True
            leader_agent_id = (
                await db.execute(
                    select(ProjectMemberSnapshot.agent_id).where(
                        ProjectMemberSnapshot.project_id == parent.project_id,
                        ProjectMemberSnapshot.is_leader.is_(True),
                        ProjectMemberSnapshot.is_enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            is_leader_reply = leader_agent_id == child.agent_id
            event_meta = _message_meta(event)
            materialized = ChatMessage(
                agent_id=parent.agent_id,
                sender_agent_id=child.agent_id,
                role="assistant",
                content=event.content,
                conversation_id=str(parent.id),
                external_event_key=external_key,
                message_meta={
                    "kind": "project_subagent_reply",
                    "project_id": str(parent.project_id),
                    "visible_to_group": True,
                    "mentions": [],
                    "awakened_agent_ids": [],
                    "subagent_id": str(child.id),
                    "child_message_id": str(child_message_id),
                    "source_project_run_ids": event_meta.get("project_run_ids", []),
                    "attachments": event_meta.get("attachments", []),
                    "wake_policy": "leader_batch_pending" if not is_leader_reply else "leader_self_no_wake",
                    "leader_batch_state": "ignored_leader_self" if is_leader_reply else "pending",
                    "default_leader_agent_id": str(leader_agent_id) if leader_agent_id else None,
                },
            )
            db.add(materialized)
            stored_parent = await db.get(ChatSession, parent.id)
            if stored_parent is not None:
                stored_parent.last_message_at = datetime.now(UTC)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
            return True

    external_key = f"subagent-parent:{child_message_id}"

    async def _work() -> str:
        anchor: ChatMessage | None = None
        async with async_session() as db:
            locked_parent = await db.get(
                ChatSession,
                parent.id,
                with_for_update=True,
            )
            if locked_parent is None:
                return "gone"
            anchor = (
                await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == external_key))
            ).scalar_one_or_none()
            latest = (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(parent.id))
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if anchor is None:
                if latest is not None and latest.role != "assistant":
                    return "busy"
                event_meta = _message_meta(event)
                event_kind = event_meta.get("kind")
                label = {
                    SUBAGENT_PARENT_MESSAGE: "message",
                    SUBAGENT_COMPLETION: "completed",
                    SUBAGENT_FAILURE: "failed",
                }.get(event_kind, "event")
                anchor = ChatMessage(
                    agent_id=parent.agent_id,
                    user_id=run.execution_user_id,
                    sender_agent_id=child.agent_id,
                    role="user",
                    content=(
                        f'<subagent-event subagent_id="{child.id}" type="{label}">\n{event.content}\n</subagent-event>'
                    ),
                    conversation_id=str(parent.id),
                    external_event_key=external_key,
                    message_meta={
                        "kind": SUBAGENT_PARENT_EVENT,
                        "execution_agent_id": str(child.agent_id),
                        "subagent_id": str(child.id),
                        "child_message_id": str(child_message_id),
                        "attachments": [],
                        "turn_status": "running",
                    },
                )
                db.add(anchor)
                locked_parent.last_message_at = datetime.now(UTC)
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    anchor = (
                        await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == external_key))
                    ).scalar_one()
            else:
                completed = (
                    await db.execute(
                        select(ChatMessage.id)
                        .where(
                            ChatMessage.conversation_id == str(parent.id),
                            ChatMessage.role == "assistant",
                            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if completed is not None:
                    return "already_processed"
                if latest is not None and latest.id != anchor.id and latest.role != "assistant":
                    return "busy"
        async with async_session() as check_db:
            fresh_latest = (
                await check_db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(parent.id))
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if fresh_latest is not None and fresh_latest.id != anchor.id and fresh_latest.role != "assistant":
                return "busy"
            live_run = await check_db.get(SubagentRun, child.id)
            live_child = await check_db.get(ChatSession, child.id)
            try:
                if live_run is None or live_child is None:
                    raise RuntimeError("Subagent lifecycle no longer exists")
                await _validate_execution_identity(check_db, live_run, live_child)
            except (ExecutionIdentityError, RuntimeError) as exc:
                from app.services.chat_history import persist_assistant_reply_row

                async with async_session() as failed_db:
                    stored_anchor = await failed_db.get(
                        ChatMessage,
                        anchor.id,
                        with_for_update=True,
                    )
                    if stored_anchor is None:
                        return "gone"
                    completed = (
                        await failed_db.execute(
                            select(ChatMessage.id)
                            .where(
                                ChatMessage.conversation_id == str(parent.id),
                                ChatMessage.role == "assistant",
                                ChatMessage.message_meta["turn_anchor_id"].as_string() == str(stored_anchor.id),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if completed is None:
                        meta = _message_meta(stored_anchor)
                        meta["turn_status"] = "failed"
                        stored_anchor.message_meta = meta
                        await persist_assistant_reply_row(
                            failed_db,
                            agent_id=child.agent_id,
                            user_id=run.execution_user_id,
                            conversation_id=str(parent.id),
                            content=(
                                "Subagent 事件未继续执行：原执行身份已失效或不再具有 "
                                f"Agent 访问权限。({type(exc).__name__})"
                            ),
                            message_meta={
                                "kind": "subagent_event_failure",
                                "attachments": [],
                            },
                            turn_anchor_id=stored_anchor.id,
                        )
                        failed_parent = await failed_db.get(ChatSession, parent.id)
                        if failed_parent is not None:
                            failed_parent.last_message_at = datetime.now(UTC)
                        await failed_db.commit()
                return "processed"
        resumed = await resume_turn(anchor)
        return "processed" if resumed else "busy"

    result = await run_channel_message(
        lock_key,
        is_command=False,
        reactions=ChannelReactions(),
        work=_work,
        distributed=True,
        workload_kind=WorkloadKind.PROJECT,
        tenant_id=await _subagent_workload_tenant_id(child.id),
    )
    return result != "busy"


async def _pending_project_leader_groups(
    *,
    debounce_seconds: float = 0.5,
    limit: int = 20,
) -> list[uuid.UUID]:
    """Find durable project reply queues ready for one Leader batch turn."""
    cutoff = datetime.now(UTC) - timedelta(seconds=max(0.0, debounce_seconds))
    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(ChatMessage.conversation_id)
                    .where(
                        ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                        ChatMessage.message_meta["leader_batch_state"].as_string().in_(["pending", "claimed"]),
                        ChatMessage.created_at <= cutoff,
                    )
                    .distinct()
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    groups: list[uuid.UUID] = []
    for value in rows:
        try:
            groups.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return groups


async def _resolve_batch_work_item_lineage(
    db,
    project_id: uuid.UUID,
    source_rows: list[ChatMessage],
) -> tuple[uuid.UUID | None, list[uuid.UUID]]:
    """Resolve only exact structured Run lineage for a coalesced reply batch.

    A batch receives one ``work_item_id`` only when every source reply maps to
    the same single project work item. Mixed, missing, malformed, or
    cross-project references remain deliberately unbound; their valid related
    work items are still retained as a set for traceability.
    """
    from app.models.project import ProjectRun

    row_run_ids: list[list[uuid.UUID] | None] = []
    all_run_ids: set[uuid.UUID] = set()
    for row in source_rows:
        raw_ids = _message_meta(row).get("source_project_run_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            row_run_ids.append(None)
            continue
        parsed: list[uuid.UUID] = []
        malformed = False
        for raw_id in raw_ids:
            try:
                parsed.append(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError):
                malformed = True
                break
        if malformed or not parsed:
            row_run_ids.append(None)
            continue
        row_run_ids.append(parsed)
        all_run_ids.update(parsed)

    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.id.in_(all_run_ids),
                )
            )
        )
        .scalars()
        .all()
        if all_run_ids
        else []
    )
    run_by_id = {run.id: run for run in runs}
    related_work_item_ids = sorted(
        {run.work_item_id for run in runs if run.work_item_id is not None},
        key=str,
    )
    row_work_item_ids: list[uuid.UUID] = []
    for run_ids in row_run_ids:
        if run_ids is None or any(run_id not in run_by_id for run_id in run_ids):
            return None, related_work_item_ids
        work_item_ids = {run_by_id[run_id].work_item_id for run_id in run_ids}
        if None in work_item_ids or len(work_item_ids) != 1:
            return None, related_work_item_ids
        row_work_item_ids.append(next(iter(work_item_ids)))
    exact_work_item_ids = set(row_work_item_ids)
    return (
        next(iter(exact_work_item_ids)) if len(exact_work_item_ids) == 1 else None,
        related_work_item_ids,
    )


def _batch_row_run_ids(row: ChatMessage) -> list[uuid.UUID]:
    parsed: list[uuid.UUID] = []
    raw_ids = _message_meta(row).get("source_project_run_ids")
    if not isinstance(raw_ids, list):
        return parsed
    for raw_id in raw_ids:
        try:
            parsed.append(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError):
            return []
    return parsed


async def _leader_batch_causal_keys(
    db,
    project_id: uuid.UUID,
    rows: list[ChatMessage],
) -> dict[uuid.UUID, str]:
    """Keep unrelated work items and request chains out of one owner turn."""
    from app.models.project import ProjectRun

    run_ids = {run_id for row in rows for run_id in _batch_row_run_ids(row)}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    run_by_id = {run.id: run for run in runs}
    keys: dict[uuid.UUID, str] = {}
    for row in rows:
        related = [run_by_id[run_id] for run_id in _batch_row_run_ids(row) if run_id in run_by_id]
        work_item_ids = {run.work_item_id for run in related if run.work_item_id is not None}
        if len(work_item_ids) == 1:
            keys[row.id] = f"work-item:{next(iter(work_item_ids))}"
            continue

        causes: set[str] = set()
        for run in related:
            payload = dict(run.input or {})
            dispatch = dict(payload.get("dispatch") or {})
            cause = (
                payload.get("parent_project_run_id")
                or payload.get("group_message_id")
                or dispatch.get("turn_anchor_id")
            )
            if cause:
                causes.add(str(cause))
        if len(causes) == 1:
            keys[row.id] = f"cause:{next(iter(causes))}"
            continue

        a2a_session_id = _message_meta(row).get("source_a2a_session_id")
        keys[row.id] = f"a2a:{a2a_session_id}" if a2a_session_id else f"reply:{row.id}"
    return keys


def _select_leader_batch_rows(
    rows: list[ChatMessage],
    causal_keys: dict[uuid.UUID, str],
) -> list[ChatMessage]:
    """Select one bounded causal slice while leaving the remainder pending."""
    if not rows:
        return []
    selected: list[ChatMessage] = []
    selected_bytes = 0
    first_key = causal_keys[rows[0].id]
    for row in rows:
        if causal_keys[row.id] != first_key:
            continue
        row_bytes = len(str(row.content or "").encode("utf-8")) + 512
        if selected and (
            len(selected) >= PROJECT_LEADER_BATCH_MAX_REPLIES
            or selected_bytes + row_bytes > PROJECT_LEADER_BATCH_MAX_BYTES
        ):
            break
        selected.append(row)
        selected_bytes += min(row_bytes, PROJECT_LEADER_BATCH_MAX_BYTES)
    return selected


def _truncate_batch_content(content: str, *, limit: int = PROJECT_LEADER_BATCH_MAX_BYTES) -> str:
    raw = str(content or "").encode("utf-8")
    if len(raw) <= limit:
        return str(content or "")
    marker = "\n\n[内容已截断；完整记录保留在原会话]"
    marker_bytes = marker.encode("utf-8")
    return raw[: max(0, limit - len(marker_bytes))].decode("utf-8", errors="ignore") + marker


def _batch_attachment_refs(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    refs: list[dict] = []
    for attachment in value[:8]:
        if not isinstance(attachment, dict):
            continue
        ref = {
            key: str(attachment[key])[:256]
            for key in ("id", "name", "path", "type", "mime_type")
            if attachment.get(key) is not None
        }
        if ref:
            refs.append(ref)
    return refs


async def _project_work_item_snapshots(
    db,
    project_id: uuid.UUID,
    work_item_ids: list[uuid.UUID],
) -> list[dict]:
    """Freeze bounded work-item context for one collaboration dispatch."""

    from app.models.project import ProjectEvent, ProjectWorkItem
    from app.services.project_collaboration_prompt import normalize_project_work_item_snapshot

    ordered_ids = list(dict.fromkeys(work_item_ids))
    if not ordered_ids:
        return []
    work_items = (
        (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.project_id == project_id,
                    ProjectWorkItem.id.in_(ordered_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    item_by_id = {item.id: item for item in work_items}
    dependency_ids: set[uuid.UUID] = set()
    for item in work_items:
        for raw_id in list(item.dependency_ids or [])[:12]:
            try:
                dependency_ids.add(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError):
                continue
    dependencies = (
        (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.project_id == project_id,
                    ProjectWorkItem.id.in_(dependency_ids),
                )
            )
        )
        .scalars()
        .all()
        if dependency_ids
        else []
    )
    dependency_by_id = {item.id: item for item in dependencies}
    events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project_id,
                    ProjectEvent.work_item_id.in_(ordered_ids),
                    ProjectEvent.event_type == "work_item.updated",
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
                .limit(max(32, len(ordered_ids) * 24))
            )
        )
        .scalars()
        .all()
    )
    evidence_by_item: dict[uuid.UUID, list[object]] = {item_id: [] for item_id in ordered_ids}
    for event in events:
        if event.work_item_id not in evidence_by_item:
            continue
        evidence = dict(event.event_metadata or {}).get("evidence")
        if not isinstance(evidence, list):
            continue
        for value in evidence:
            if value not in evidence_by_item[event.work_item_id]:
                evidence_by_item[event.work_item_id].append(value)

    snapshots: list[dict] = []
    for item_id in ordered_ids:
        item = item_by_id.get(item_id)
        if item is None:
            continue
        item_dependencies: list[dict[str, str]] = []
        for raw_id in list(item.dependency_ids or [])[:12]:
            try:
                dependency_id = uuid.UUID(str(raw_id))
            except (TypeError, ValueError):
                continue
            dependency = dependency_by_id.get(dependency_id)
            item_dependencies.append(
                {
                    "id": str(dependency_id),
                    "title": dependency.title if dependency else "",
                    "status": dependency.status if dependency else "unknown",
                }
            )
        normalized = normalize_project_work_item_snapshot(
            {
                "id": str(item.id),
                "title": item.title,
                "description": item.description,
                "status": item.status,
                "acceptance_criteria": list(item.acceptance_criteria or []),
                "dependencies": item_dependencies,
                "evidence": evidence_by_item.get(item.id, []),
            }
        )
        if normalized is not None:
            snapshots.append(normalized)
    return snapshots


async def _batch_original_human_request(
    db,
    project_id: uuid.UUID,
    group_session_id: uuid.UUID,
    source_rows: list[ChatMessage],
) -> dict[str, str] | None:
    """Resolve the bounded Human request at the root of a reply batch."""

    from app.models.project import ProjectRun
    from app.services.project_collaboration_prompt import (
        PROJECT_HUMAN_REQUEST_MAX_CHARS,
        bounded_project_text,
    )

    frontier = {run_id for row in source_rows for run_id in _batch_row_run_ids(row)}
    visited: set[uuid.UUID] = set()
    message_ids: set[uuid.UUID] = set()
    for _depth in range(8):
        current = frontier - visited
        if not current:
            break
        visited.update(current)
        runs = (
            (
                await db.execute(
                    select(ProjectRun).where(
                        ProjectRun.project_id == project_id,
                        ProjectRun.id.in_(current),
                    )
                )
            )
            .scalars()
            .all()
        )
        frontier = set()
        for run in runs:
            payload = dict(run.input or {})
            dispatch = dict(payload.get("dispatch") or {})
            for raw_message_id in (payload.get("group_message_id"), dispatch.get("turn_anchor_id")):
                try:
                    message_ids.add(uuid.UUID(str(raw_message_id)))
                except (TypeError, ValueError):
                    continue
            try:
                frontier.add(uuid.UUID(str(payload.get("parent_project_run_id"))))
            except (TypeError, ValueError):
                continue

    if not message_ids:
        return None
    request = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.id.in_(message_ids),
                ChatMessage.conversation_id == str(group_session_id),
                ChatMessage.role == "user",
                ChatMessage.sender_user_id.is_not(None),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if request is None:
        return None
    return {
        "message_id": str(request.id),
        "content": bounded_project_text(request.content, PROJECT_HUMAN_REQUEST_MAX_CHARS),
    }


async def _dispatch_project_leader_batch(
    group_session_id: uuid.UUID,
    *,
    debounce_seconds: float = 0.5,
) -> bool:
    """Atomically claim pending Agent replies and enqueue one Leader input.

    Claimed rows and the batch ProjectRun are durable before child dispatch. A
    crash retries the same batch id and append_subagent_message's external key
    makes the Leader input idempotent.
    """
    from app.models.project import Project, ProjectMemberSnapshot, ProjectRun
    from app.services.project_service import add_event, freeze_run_members

    cutoff = datetime.now(UTC) - timedelta(seconds=max(0.0, debounce_seconds))
    async with async_session() as db:
        group = await db.get(ChatSession, group_session_id, with_for_update=True)
        if group is None or group.source_channel != "project" or group.project_id is None:
            return True
        project = await db.get(Project, group.project_id, with_for_update=True)
        leader = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == group.project_id,
                    ProjectMemberSnapshot.is_leader.is_(True),
                    ProjectMemberSnapshot.is_enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if project is None or leader is None:
            return False
        if project.status != "running":
            return False

        claimed = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(group.id),
                        ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                        ChatMessage.message_meta["leader_batch_state"].as_string() == "claimed",
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        if claimed:
            batch_id = str(_message_meta(claimed[0]).get("leader_batch_id") or "")
            source_rows = [row for row in claimed if str(_message_meta(row).get("leader_batch_id") or "") == batch_id]
            raw_project_run_id = _message_meta(source_rows[0]).get("leader_batch_project_run_id")
            try:
                project_run_id = uuid.UUID(str(raw_project_run_id))
            except (TypeError, ValueError):
                return False
            project_run = await db.get(ProjectRun, project_run_id)
            if project_run is None:
                return False
            persisted_batch_input = dict(project_run.input or {})
            original_human_request = persisted_batch_input.get("original_human_request")
            if not isinstance(original_human_request, dict):
                original_human_request = None
            work_item_snapshots = persisted_batch_input.get("work_item_snapshots")
            if not isinstance(work_item_snapshots, list):
                work_item_snapshots = []
        else:
            pending_rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(group.id),
                            ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                            ChatMessage.message_meta["leader_batch_state"].as_string() == "pending",
                            ChatMessage.created_at <= cutoff,
                            ChatMessage.sender_agent_id != leader.agent_id,
                        )
                        .order_by(ChatMessage.created_at, ChatMessage.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if not pending_rows:
                return False
            causal_keys = await _leader_batch_causal_keys(db, project.id, pending_rows)
            source_rows = _select_leader_batch_rows(pending_rows, causal_keys)
            batch_id = str(uuid.uuid4())
            work_item_id, related_work_item_ids = await _resolve_batch_work_item_lineage(
                db,
                project.id,
                source_rows,
            )
            original_human_request = await _batch_original_human_request(
                db,
                project.id,
                group.id,
                source_rows,
            )
            work_item_snapshots = await _project_work_item_snapshots(
                db,
                project.id,
                related_work_item_ids,
            )
            project_run = ProjectRun(
                tenant_id=project.tenant_id,
                project_id=project.id,
                work_item_id=work_item_id,
                agent_id=leader.agent_id,
                initiated_by_user_id=project.owner_user_id,
                status="queued",
                trigger_type="leader_reply_batch",
                input={
                    "title": f"汇总 {len(source_rows)} 条成员回复并推进项目",
                    "group_session_id": str(group.id),
                    "leader_agent_id": str(leader.agent_id),
                    "leader_batch_id": batch_id,
                    "source_group_message_ids": [str(row.id) for row in source_rows],
                    "related_work_item_ids": [str(value) for value in related_work_item_ids],
                    "original_human_request": original_human_request,
                    "work_item_snapshots": work_item_snapshots,
                    "batch_limits": {
                        "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
                        "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
                    },
                },
                output={
                    "group_session_id": str(group.id),
                    "leader_batch_id": batch_id,
                    "related_work_item_ids": [str(value) for value in related_work_item_ids],
                },
            )
            db.add(project_run)
            await db.flush()
            await freeze_run_members(db, project, project_run)
            for row in source_rows:
                row.message_meta = {
                    **_message_meta(row),
                    "leader_batch_state": "claimed",
                    "leader_batch_id": batch_id,
                    "leader_batch_project_run_id": str(project_run.id),
                    "leader_batch_claimed_at": datetime.now(UTC).isoformat(),
                }
        source_agent_ids = {row.sender_agent_id for row in source_rows if row.sender_agent_id is not None}
        source_members = (
            (
                await db.execute(
                    select(ProjectMemberSnapshot).where(
                        ProjectMemberSnapshot.project_id == project.id,
                        ProjectMemberSnapshot.agent_id.in_(source_agent_ids),
                    )
                )
            )
            .scalars()
            .all()
            if source_agent_ids
            else []
        )
        source_member_by_agent = {member.agent_id: member for member in source_members}
        await db.commit()

    async with async_session() as db:
        resumed_project = await db.get(Project, project.id)
        if resumed_project is None or resumed_project.status != "running":
            return False

    source_content_limit = max(
        512,
        (PROJECT_LEADER_BATCH_MAX_BYTES - 8 * 1024) // max(1, len(source_rows)),
    )
    sources = [
        {
            "group_message_id": str(row.id),
            "source_agent_id": str(row.sender_agent_id) if row.sender_agent_id else None,
            "source_agent_name": (
                source_member_by_agent[row.sender_agent_id].name_snapshot
                if row.sender_agent_id in source_member_by_agent
                else None
            ),
            "source_role_snapshot": (
                source_member_by_agent[row.sender_agent_id].role_snapshot
                if row.sender_agent_id in source_member_by_agent
                else None
            ),
            "child_message_id": _message_meta(row).get("child_message_id"),
            "subagent_session_id": _message_meta(row).get("subagent_id"),
            "subagent_run_id": _message_meta(row).get("subagent_id"),
            "project_run_ids": _message_meta(row).get("source_project_run_ids", []),
            "content": _truncate_batch_content(row.content, limit=source_content_limit),
            "attachments": _batch_attachment_refs(_message_meta(row).get("attachments", [])),
        }
        for row in source_rows
    ]
    from app.services.project_collaboration_prompt import build_project_owner_batch_task

    task = build_project_owner_batch_task(
        batch_id=batch_id,
        group_session_id=str(group_session_id),
        replies=sources,
        original_human_request=original_human_request,
        work_item_snapshots=work_item_snapshots,
    )
    if len(task.encode("utf-8")) > PROJECT_LEADER_BATCH_MAX_BYTES:
        sources = [
            {
                **source,
                "content": _truncate_batch_content(str(source.get("content") or ""), limit=256),
                "attachments": [],
            }
            for source in sources
        ]
        task = build_project_owner_batch_task(
            batch_id=batch_id,
            group_session_id=str(group_session_id),
            replies=sources,
            original_human_request=original_human_request,
            work_item_snapshots=work_item_snapshots,
        )
    if len(task.encode("utf-8")) > PROJECT_LEADER_BATCH_MAX_BYTES:
        task = _truncate_batch_content(task, limit=PROJECT_LEADER_BATCH_MAX_BYTES)
    async with async_session() as db:
        existing_child = (
            await db.execute(
                select(SubagentRun)
                .where(
                    SubagentRun.parent_session_id == group_session_id,
                    SubagentRun.project_member_id == leader.id,
                    SubagentRun.origin_tool_call_id == _project_member_origin_tool_call_id(leader),
                )
                .order_by(SubagentRun.id)
                .limit(1)
            )
        ).scalar_one_or_none()
    input_metadata = {
        "project_leader_batch": True,
        "leader_batch_id": batch_id,
        "source_group_message_ids": [str(row.id) for row in source_rows],
        "source_replies": sources,
        "work_item_id": str(project_run.work_item_id) if project_run.work_item_id else None,
        "related_work_item_ids": list(dict(project_run.input or {}).get("related_work_item_ids") or []),
        "original_human_request": original_human_request,
        "work_item_snapshots": work_item_snapshots,
        "batch_limits": {
            "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
            "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
        },
        "batch_input_bytes": len(task.encode("utf-8")),
    }
    if existing_child is None:
        durable_run, created = await create_subagent(
            agent_id=leader.agent_id,
            execution_user_id=project.owner_user_id,
            parent_session_id=str(group_session_id),
            origin_tool_call_id=_project_member_origin_tool_call_id(leader),
            task=task,
            mode="async",
            fork=True,
            turn_anchor_id=source_rows[-1].id,
            project_run_id=project_run.id,
            input_metadata=input_metadata,
        )
        child_id = durable_run.id
        child_status = durable_run.status
        if not created and not await _project_run_has_child_input(child_id, project_run.id):
            child_status = await append_subagent_message(
                agent_id=leader.agent_id,
                parent_session_id=str(group_session_id),
                subagent_id=str(durable_run.id),
                message=task,
                execution_user_id=durable_run.execution_user_id,
                origin_tool_call_id=f"project-leader-batch:{batch_id}",
                project_run_id=project_run.id,
                input_metadata=input_metadata,
            )
    else:
        child_id = existing_child.id
        child_status = existing_child.status
        if not await _project_run_has_child_input(child_id, project_run.id):
            child_status = await append_subagent_message(
                agent_id=leader.agent_id,
                parent_session_id=str(group_session_id),
                subagent_id=str(existing_child.id),
                message=task,
                execution_user_id=existing_child.execution_user_id,
                origin_tool_call_id=f"project-leader-batch:{batch_id}",
                project_run_id=project_run.id,
                input_metadata=input_metadata,
            )

    async with async_session() as db:
        project_run = await db.get(ProjectRun, project_run.id, with_for_update=True)
        batch_input = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(child_id),
                    ChatMessage.message_meta["leader_batch_id"].as_string() == batch_id,
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        delivered_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(group_session_id),
                        ChatMessage.message_meta["leader_batch_id"].as_string() == batch_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in delivered_rows:
            row.message_meta = {
                **_message_meta(row),
                "leader_batch_state": "delivered",
                "leader_batch_input_id": str(batch_input.id) if batch_input else None,
                "leader_subagent_session_id": str(child_id),
            }
        if project_run is not None:
            project_run.status = "queued" if child_status == RUN_QUEUED else "running"
            project_run.output = {
                **dict(project_run.output or {}),
                "subagent_run_id": str(child_id),
                "subagent_session_id": str(child_id),
                "leader_batch_input_id": str(batch_input.id) if batch_input else None,
                "source_count": len(delivered_rows),
                "batch_limits": {
                    "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
                    "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
                },
            }
        attached_project = await db.get(Project, project.id)
        if attached_project is not None:
            add_event(
                db,
                attached_project,
                "project.agent_reply_batch.dispatched",
                f"Coalesced {len(delivered_rows)} Agent replies into one Leader turn",
                actor_agent_id=leader.agent_id,
                work_item_id=project_run.work_item_id if project_run else None,
                run_id=project_run.id if project_run else None,
                metadata={
                    "leader_batch_id": batch_id,
                    "leader_agent_id": str(leader.agent_id),
                    "leader_subagent_session_id": str(child_id),
                    "leader_batch_input_id": str(batch_input.id) if batch_input else None,
                    "source_group_message_ids": [str(row.id) for row in delivered_rows],
                    "source_count": len(delivered_rows),
                    "batch_limits": {
                        "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
                        "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
                    },
                    "related_work_item_ids": (
                        list(dict(project_run.input or {}).get("related_work_item_ids") or []) if project_run else []
                    ),
                },
            )
        await db.commit()
    return True


PROJECT_DISPATCH_TRIGGERS = frozenset(
    {
        "a2a",
        "leader_kickoff",
        "group_leader_message",
        "group_mention",
        "manual",
        "leader",
        "schedule",
        "retry",
    }
)
PROJECT_DISPATCH_BATCH_SIZE = 50
ProjectDispatchCursor = tuple[datetime, uuid.UUID]


async def enqueue_project_a2a_run(
    *,
    project_id: uuid.UUID,
    a2a_session_id: uuid.UUID,
    outbound_message_id: uuid.UUID,
    from_agent_id: uuid.UUID,
    to_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    message: str,
    mode: str,
    project_run_id: uuid.UUID | None = None,
    parent_project_run_id: uuid.UUID | None = None,
    work_item_id: uuid.UUID | None = None,
    run_title: str | None = None,
) -> dict:
    """Queue one exact, project-scoped Agent-to-Agent wake.

    The visible A2A ``ChatSession`` is only the collaboration timeline.  The
    target executes in its reusable project ``SubagentRun`` child so project
    tools, frozen capability policy, membership revocation and durable recovery
    are identical to project-group execution.  No other member is awakened.
    """
    from app.models.project import (
        Project,
        ProjectEvent,
        ProjectMemberSnapshot,
        ProjectRun,
        ProjectWorkItem,
    )
    from app.services.project_service import add_event, freeze_run_members

    task_text = str(message or "").strip()
    if not task_text:
        raise SubagentError("项目 A2A 消息不能为空。")
    if from_agent_id == to_agent_id:
        raise SubagentError("项目 A2A 发送方和接收方不能相同。")

    async with async_session() as db:
        project = await db.get(Project, project_id)
        if project is None:
            raise SubagentError("项目 A2A 会话或成员作用域已经失效。")
        if project.status != "running":
            raise SubagentError("项目已暂停；恢复项目后才能唤醒数字员工。")
        parent = await db.get(ChatSession, a2a_session_id)
        outbound = await db.get(ChatMessage, outbound_message_id, with_for_update=True)
        members = (
            (
                await db.execute(
                    select(ProjectMemberSnapshot).where(
                        ProjectMemberSnapshot.project_id == project_id,
                        ProjectMemberSnapshot.agent_id.in_([from_agent_id, to_agent_id]),
                        ProjectMemberSnapshot.is_enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        member_by_agent = {member.agent_id: member for member in members}
        source_member = member_by_agent.get(from_agent_id)
        target_member = member_by_agent.get(to_agent_id)
        if (
            parent is None
            or parent.project_id != project_id
            or parent.source_channel != "agent"
            or outbound is None
            or outbound.conversation_id != str(parent.id)
            or outbound.sender_agent_id != from_agent_id
            or {parent.agent_id, parent.peer_agent_id} != {from_agent_id, to_agent_id}
            or set(member_by_agent) != {from_agent_id, to_agent_id}
            or source_member is None
            or target_member is None
        ):
            raise SubagentError("项目 A2A 会话或成员作用域已经失效。")

        run = await db.get(ProjectRun, project_run_id, with_for_update=True) if project_run_id else None
        if project_run_id is not None and (
            run is None or run.project_id != project_id or run.agent_id != to_agent_id or run.trigger_type != "a2a"
        ):
            raise SubagentError("项目 A2A Run 与当前消息不匹配。")
        parent_run = await db.get(ProjectRun, parent_project_run_id) if parent_project_run_id else None
        if parent_project_run_id is not None and (
            parent_run is None or parent_run.project_id != project_id or parent_run.agent_id != from_agent_id
        ):
            raise SubagentError("项目 A2A 父 Run 与当前发送方不匹配。")

        parent_work_item_id = parent_run.work_item_id if parent_run else None
        resolved_work_item_id = work_item_id or parent_work_item_id or (run.work_item_id if run else None)
        work_item_title = None
        work_item_snapshot = None
        if resolved_work_item_id is not None:
            work_item = await db.get(ProjectWorkItem, resolved_work_item_id)
            if work_item is None or work_item.project_id != project_id:
                raise SubagentError("项目 A2A 工作项不属于当前项目。")
            work_item_title = work_item.title
            persisted_snapshot = dict(run.input or {}).get("work_item_snapshot") if run else None
            if isinstance(persisted_snapshot, dict):
                work_item_snapshot = persisted_snapshot
            else:
                snapshots = await _project_work_item_snapshots(
                    db,
                    project_id,
                    [resolved_work_item_id],
                )
                work_item_snapshot = snapshots[0] if snapshots else None
        if run is not None and work_item_id is not None and run.work_item_id not in {None, work_item_id}:
            raise SubagentError("项目 A2A Run 与显式工作项不匹配。")
        parent_title = str(dict(parent_run.input or {}).get("title") or "").strip() if parent_run else ""
        persisted_title = (
            str(run_title or "").strip() or parent_title or str(work_item_title or "").strip() or "处理 Agent 协作请求"
        )[:120]
        from app.services.project_collaboration_prompt import build_project_a2a_task

        dispatch = {
            "group_session_id": str(parent.id),
            "project_member_id": str(target_member.id),
            "turn_anchor_id": str(outbound_message_id),
            "task": build_project_a2a_task(
                task_text,
                source_name=source_member.name_snapshot,
                source_role=source_member.role_snapshot,
                target_name=target_member.name_snapshot,
                target_role=target_member.role_snapshot,
                work_item_title=str(work_item_title or ""),
                work_item_snapshot=work_item_snapshot,
            ),
            "a2a": {
                "session_id": str(parent.id),
                "message_id": str(outbound_message_id),
                "from_agent_id": str(from_agent_id),
                "to_agent_id": str(to_agent_id),
                "from_agent_name_snapshot": source_member.name_snapshot,
                "from_agent_role_snapshot": source_member.role_snapshot,
                "to_agent_name_snapshot": target_member.name_snapshot,
                "to_agent_role_snapshot": target_member.role_snapshot,
                "mode": str(mode or "notify"),
            },
        }
        if run is None:
            run = ProjectRun(
                tenant_id=project.tenant_id,
                project_id=project.id,
                work_item_id=resolved_work_item_id,
                agent_id=to_agent_id,
                initiated_by_user_id=execution_user_id,
                status="queued",
                trigger_type="a2a",
                input={
                    "title": persisted_title,
                    "parent_project_run_id": (str(parent_project_run_id) if parent_project_run_id else None),
                    "work_item_id": str(resolved_work_item_id) if resolved_work_item_id else None,
                    "work_item_snapshot": work_item_snapshot,
                    "from_agent_id": str(from_agent_id),
                    "to_agent_id": str(to_agent_id),
                    "message": task_text,
                    "mode": str(mode or "notify"),
                    "session_id": str(parent.id),
                    "dispatch": dispatch,
                },
                output={
                    "session_id": str(parent.id),
                    "session_agent_id": str(parent.agent_id),
                    "session_access_agent_id": str(parent.agent_id),
                    "session_title": parent.title,
                },
            )
            db.add(run)
            await db.flush()
            await freeze_run_members(db, project, run)
        else:
            run.input = {
                **dict(run.input or {}),
                "title": str(dict(run.input or {}).get("title") or persisted_title),
                "parent_project_run_id": (str(parent_project_run_id) if parent_project_run_id else None),
                "work_item_id": str(resolved_work_item_id) if resolved_work_item_id else None,
                "work_item_snapshot": work_item_snapshot,
                "session_id": str(parent.id),
                "dispatch": dispatch,
            }
            # An explicit work-item reference is the source of truth. Otherwise
            # inherit the exact parent Run lineage, while preserving a
            # pre-created API Run's existing work-item association.
            run.work_item_id = resolved_work_item_id
            run.output = {
                **dict(run.output or {}),
                "session_id": str(parent.id),
                "session_agent_id": str(parent.agent_id),
                "session_access_agent_id": str(parent.agent_id),
                "session_title": parent.title,
            }
        outbound.message_meta = {
            **_message_meta(outbound),
            "project_run_id": str(run.id),
            "work_item_id": str(run.work_item_id) if run.work_item_id else None,
        }
        existing_event = (
            await db.execute(
                select(ProjectEvent.id).where(
                    ProjectEvent.run_id == run.id,
                    ProjectEvent.event_type == "a2a.queued",
                )
            )
        ).scalar_one_or_none()
        if existing_event is None:
            add_event(
                db,
                project,
                "a2a.queued",
                "Queued one explicit project A2A wake",
                actor_user_id=execution_user_id,
                actor_agent_id=from_agent_id,
                from_agent_id=from_agent_id,
                to_agent_id=to_agent_id,
                work_item_id=run.work_item_id,
                run_id=run.id,
                metadata={
                    "mode": str(mode or "notify"),
                    "session_id": str(parent.id),
                    "message_id": str(outbound_message_id),
                    "awakened_agent_ids": [str(to_agent_id)],
                    "broadcast": False,
                    "parent_project_run_id": (str(parent_project_run_id) if parent_project_run_id else None),
                    "work_item_id": str(run.work_item_id) if run.work_item_id else None,
                },
            )
        await db.commit()
        run_id = run.id

    result = await dispatch_project_run(run_id)
    return {**result, "project_run_id": str(run_id)}


async def _project_run_has_child_input(
    child_id: uuid.UUID,
    project_run_id: uuid.UUID,
) -> bool:
    async with async_session() as db:
        existing = (
            await db.execute(
                select(ChatMessage.id)
                .where(
                    ChatMessage.conversation_id == str(child_id),
                    ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return existing is not None


async def dispatch_project_run(project_run_id: uuid.UUID) -> dict:
    """Idempotently deliver one durable project dispatch outbox row.

    Project endpoints commit the ProjectRun before calling this helper. The
    daemon scans the same queued rows, so a process exit between the database
    commit and Subagent creation cannot strand kickoff or a group mention.
    """
    from app.models.project import Project, ProjectEvent, ProjectMemberSnapshot, ProjectRun
    from app.services.project_service import (
        TERMINAL_PROJECT_RUN_STATUSES,
        add_event,
        reconcile_project_run_terminal_state,
    )

    async with async_session() as db:
        project_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
        if project_run is None or project_run.trigger_type not in PROJECT_DISPATCH_TRIGGERS:
            return {"status": "gone"}
        repaired = reconcile_project_run_terminal_state(project_run)
        output = dict(project_run.output or {})
        if output.get("subagent_run_id"):
            if repaired:
                await db.commit()
            return {**output, "status": project_run.status}
        dispatch = dict((project_run.input or {}).get("dispatch") or {})
        try:
            group_session_id = uuid.UUID(str(dispatch["group_session_id"]))
            member_id = uuid.UUID(str(dispatch["project_member_id"]))
            anchor_id = uuid.UUID(str(dispatch["turn_anchor_id"]))
        except (KeyError, TypeError, ValueError):
            return {"status": "not_dispatchable"}
        task = str(dispatch.get("task") or "").strip()
        if not task:
            return {"status": "not_dispatchable"}
        project = await db.get(Project, project_run.project_id, with_for_update=True)
        parent = await db.get(ChatSession, group_session_id)
        member = await db.get(ProjectMemberSnapshot, member_id)
        runtime_ready = project is not None and (
            project.status == "running"
            or (project_run.trigger_type == "leader_kickoff" and project.status == "initializing")
        )
        if project is not None and not runtime_ready:
            return {"status": "paused"}
        if (
            project is None
            or parent is None
            or parent.project_id != project.id
            or parent.source_channel not in {"project", "agent"}
            or (project_run.trigger_type != "a2a" and parent.source_channel != "project")
            or (project_run.trigger_type == "a2a" and parent.source_channel != "agent")
            or member is None
            or member.project_id != project.id
            or member.agent_id != project_run.agent_id
            or not member.is_enabled
        ):
            project_run.status = "failed"
            project_run.error = "Project dispatch identity is no longer valid"
            project_run.finished_at = datetime.now(UTC)
            await db.commit()
            return {"status": "failed", "error": project_run.error}
        agent_id = member.agent_id
        execution_user_id = project.owner_user_id
        if project_run.trigger_type == "group_leader_message":
            group_anchor = await db.get(ChatMessage, anchor_id)
            explicit_mentions = (
                {str(value) for value in _message_meta(group_anchor).get("mentions", [])}
                if group_anchor is not None
                else set()
            )
            if explicit_mentions and str(agent_id) not in explicit_mentions:
                project_run.status = "cancelled"
                project_run.finished_at = datetime.now(UTC)
                project_run.output = {
                    **dict(project_run.output or {}),
                    "status": "skipped",
                    "skip_reason": "explicit_mentions_route_to_specialists",
                }
                if group_anchor is not None:
                    group_anchor.message_meta = {
                        **_message_meta(group_anchor),
                        "wake_policy": "structured_mentions_only",
                        "owner_deferred_until_specialist_result": True,
                    }
                await db.commit()
                return dict(project_run.output)
        existing_child = (
            await db.execute(
                select(SubagentRun)
                .where(
                    SubagentRun.parent_session_id == parent.id,
                    SubagentRun.project_member_id == member.id,
                    SubagentRun.origin_tool_call_id == _project_member_origin_tool_call_id(member),
                )
                .order_by(SubagentRun.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if project_run.status not in TERMINAL_PROJECT_RUN_STATUSES:
            project_run.status = "running"
            project_run.started_at = project_run.started_at or datetime.now(UTC)
            project_run.output = {
                **dict(project_run.output or {}),
                "dispatch_claimed_at": dict(project_run.output or {}).get("dispatch_claimed_at")
                or datetime.now(UTC).isoformat(),
            }
        await db.commit()

    async with async_session() as db:
        current_project = await db.get(Project, project_run.project_id)
        runtime_ready = current_project is not None and (
            current_project.status == "running"
            or (project_run.trigger_type == "leader_kickoff" and current_project.status == "initializing")
        )
        if not runtime_ready:
            return {"status": "paused" if current_project is not None else "gone"}

    try:
        created = False
        if existing_child is None:
            child, created = await create_subagent(
                agent_id=agent_id,
                execution_user_id=execution_user_id,
                parent_session_id=str(group_session_id),
                origin_tool_call_id=_project_member_origin_tool_call_id(member),
                task=task,
                mode="async",
                fork=True,
                turn_anchor_id=anchor_id,
                project_run_id=project_run_id,
                input_metadata={
                    "project_dispatch": True,
                    "project_a2a": project_run.trigger_type == "a2a",
                    "a2a_session_id": str(parent.id) if project_run.trigger_type == "a2a" else None,
                },
            )
            child_id = child.id
            child_status = child.status
        else:
            child_id = existing_child.id
            child_status = existing_child.status
        if not created and not await _project_run_has_child_input(child_id, project_run_id):
            child_status = await append_subagent_message(
                agent_id=agent_id,
                parent_session_id=str(group_session_id),
                subagent_id=str(child_id),
                message=task,
                execution_user_id=execution_user_id,
                origin_tool_call_id=f"project-dispatch:{project_run_id}",
                project_run_id=project_run_id,
                input_metadata={
                    "project_dispatch": True,
                    "project_a2a": project_run.trigger_type == "a2a",
                    "a2a_session_id": str(parent.id) if project_run.trigger_type == "a2a" else None,
                },
            )
    except SubagentError as exc:
        async with async_session() as db:
            failed_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
            if (
                failed_run is not None
                and failed_run.finished_at is None
                and failed_run.status not in TERMINAL_PROJECT_RUN_STATUSES
                and not dict(failed_run.output or {}).get("subagent_run_id")
            ):
                failed_run.status = "failed"
                failed_run.finished_at = datetime.now(UTC)
                failed_run.error = str(exc)
                await db.commit()
        return {"status": "failed", "error": str(exc)}

    async with async_session() as db:
        project_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
        if project_run is None:
            return {"status": "gone"}
        dispatch = dict((project_run.input or {}).get("dispatch") or {})
        # The child may finish between append_subagent_message() and this
        # transaction. Its completion transaction writes finished_at first;
        # dispatch must enrich output without regressing that terminal fact.
        reconcile_project_run_terminal_state(project_run)
        if project_run.status not in TERMINAL_PROJECT_RUN_STATUSES:
            project_run.status = "queued" if child_status == RUN_QUEUED else "running"
            if project_run.status == "running" and project_run.started_at is None:
                project_run.started_at = datetime.now(UTC)
        project_run.output = {
            **dict(project_run.output or {}),
            "subagent_run_id": str(child_id),
            "subagent_session_id": str(child_id),
            **(
                {
                    "session_id": str(parent.id),
                    "a2a_session_id": str(parent.id),
                }
                if project_run.trigger_type == "a2a"
                else {}
            ),
            "status": child_status,
            "subagent_status": child_status,
        }

        project = await db.get(Project, project_run.project_id, with_for_update=True)
        anchor = await db.get(ChatMessage, anchor_id, with_for_update=True)
        if project_run.trigger_type == "leader_kickoff" and project is not None:
            kickoff = dict(dispatch.get("kickoff") or {})
            project.status = "running"
            project.settings = {
                **dict(project.settings or {}),
                "kickoff": {
                    **kickoff,
                    "project_run_id": str(project_run.id),
                    "subagent_session_id": str(child_id),
                },
            }
            if anchor is not None:
                anchor.message_meta = {
                    **_message_meta(anchor),
                    "awakened_agent_ids": [str(agent_id)],
                    "project_run_id": str(project_run.id),
                    "subagent_session_id": str(child_id),
                }
            existing_event = (
                await db.execute(
                    select(ProjectEvent.id).where(
                        ProjectEvent.run_id == project_run.id,
                        ProjectEvent.event_type == "project.kickoff.confirmed",
                    )
                )
            ).scalar_one_or_none()
            if existing_event is None:
                add_event(
                    db,
                    project,
                    "project.kickoff.confirmed",
                    "User confirmed the Leader plan and autonomous execution started",
                    actor_user_id=project_run.initiated_by_user_id,
                    actor_agent_id=agent_id,
                    run_id=project_run.id,
                    metadata={
                        **kickoff,
                        "leader_agent_id": str(agent_id),
                        "awakened_agent_ids": [str(agent_id)],
                        "project_run_id": str(project_run.id),
                        "subagent_run_id": str(child_id),
                        "subagent_session_id": str(child_id),
                    },
                )
        elif project_run.trigger_type == "a2a" and anchor is not None:
            a2a = dict(dispatch.get("a2a") or {})
            anchor.message_meta = {
                **_message_meta(anchor),
                "project_run_id": str(project_run.id),
                "a2a_session_id": str(parent.id),
                "subagent_run_id": str(child_id),
                "subagent_session_id": str(child_id),
                "awakened_agent_ids": [str(agent_id)],
                "wake_policy": "single_explicit_project_target",
            }
            existing_event = (
                await db.execute(
                    select(ProjectEvent)
                    .where(
                        ProjectEvent.run_id == project_run.id,
                        ProjectEvent.event_type == "a2a.delivered",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            event_metadata = {
                "session_id": str(parent.id),
                "a2a_session_id": str(parent.id),
                "message_id": str(anchor.id),
                "subagent_run_id": str(child_id),
                "subagent_session_id": str(child_id),
                "from_agent_id": a2a.get("from_agent_id"),
                "to_agent_id": a2a.get("to_agent_id") or str(agent_id),
                "awakened_agent_ids": [str(agent_id)],
                "broadcast": False,
            }
            if existing_event is None and project is not None:
                add_event(
                    db,
                    project,
                    "a2a.delivered",
                    "Project A2A message delivered to one durable project Agent runtime",
                    actor_user_id=project_run.initiated_by_user_id,
                    actor_agent_id=(uuid.UUID(str(a2a["from_agent_id"])) if a2a.get("from_agent_id") else None),
                    from_agent_id=(uuid.UUID(str(a2a["from_agent_id"])) if a2a.get("from_agent_id") else None),
                    to_agent_id=agent_id,
                    run_id=project_run.id,
                    metadata=event_metadata,
                )
            elif existing_event is not None:
                existing_event.event_metadata = {
                    **dict(existing_event.event_metadata or {}),
                    **event_metadata,
                }
        elif anchor is not None:
            meta = _message_meta(anchor)
            awakened = list(dict.fromkeys([*meta.get("awakened_agent_ids", []), str(agent_id)]))
            subagent_rows = list(meta.get("subagent_runs", []))
            if not any(str(row.get("project_run_id")) == str(project_run.id) for row in subagent_rows):
                subagent_rows.append(
                    {
                        "project_run_id": str(project_run.id),
                        "run_id": str(child_id),
                        "session_id": str(child_id),
                        "agent_id": str(agent_id),
                        "status": child_status,
                    }
                )
            anchor.message_meta = {
                **meta,
                "awakened_agent_ids": awakened,
                "subagent_runs": subagent_rows,
            }
            existing_event = (
                await db.execute(
                    select(ProjectEvent)
                    .where(
                        ProjectEvent.project_id == project_run.project_id,
                        ProjectEvent.event_type == "group.message.created",
                        ProjectEvent.event_metadata["group_message_id"].as_string() == str(anchor.id),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_event is None and project is not None:
                add_event(
                    db,
                    project,
                    "group.message.created",
                    "Appended project group message and dispatched Leader plus structured mentions",
                    actor_user_id=project_run.initiated_by_user_id,
                    metadata={
                        "group_session_id": str(group_session_id),
                        "group_message_id": str(anchor.id),
                        "initiator_user_id": str(project_run.initiated_by_user_id),
                        "visible_to_group": True,
                        "mentioned_agent_ids": meta.get("mentions", []),
                        "default_leader_agent_id": meta.get("default_leader_agent_id"),
                        "awakened_agent_ids": awakened,
                        "subagent_runs": subagent_rows,
                        "zero_wake_default": False,
                    },
                )
            elif existing_event is not None:
                existing_event.event_metadata = {
                    **dict(existing_event.event_metadata or {}),
                    "awakened_agent_ids": awakened,
                    "subagent_runs": subagent_rows,
                }
        await db.commit()
        return {**dict(project_run.output or {}), "status": project_run.status}


async def _pending_project_dispatch_batch(
    *,
    after: ProjectDispatchCursor | None = None,
) -> tuple[list[uuid.UUID], ProjectDispatchCursor | None]:
    from app.models.project import Project, ProjectRun
    from app.services.project_service import reconcile_project_run_terminal_state

    async with async_session() as db:
        # Repair terminal timestamps independently so stale completed rows can
        # never consume capacity from the durable dispatch outbox query.
        repair_rows = (
            (
                await db.execute(
                    select(ProjectRun)
                    .where(
                        ProjectRun.status.in_(["queued", "running"]),
                        ProjectRun.trigger_type.in_(PROJECT_DISPATCH_TRIGGERS),
                        ProjectRun.finished_at.is_not(None),
                    )
                    .order_by(ProjectRun.created_at, ProjectRun.id)
                    .limit(PROJECT_DISPATCH_BATCH_SIZE)
                )
            )
            .scalars()
            .all()
        )
        repaired = sum(reconcile_project_run_terminal_state(row) for row in repair_rows)
        if repaired:
            await db.commit()

        # JSON ``as_string`` compiles to JSON_EXTRACT on SQLite and ->> on
        # PostgreSQL. Applying every durable marker in SQL ensures an arbitrary
        # number of active, already-dispatched Runs cannot starve newer work.
        query = (
            select(ProjectRun.id, ProjectRun.created_at)
            .join(Project, Project.id == ProjectRun.project_id)
            .where(
                or_(
                    Project.status == "running",
                    and_(
                        Project.status == "initializing",
                        ProjectRun.trigger_type == "leader_kickoff",
                    ),
                ),
                ProjectRun.status.in_(["queued", "running"]),
                ProjectRun.trigger_type.in_(PROJECT_DISPATCH_TRIGGERS),
                ProjectRun.finished_at.is_(None),
                ProjectRun.input["dispatch"]["group_session_id"].as_string().is_not(None),
                ProjectRun.input["dispatch"]["project_member_id"].as_string().is_not(None),
                ProjectRun.input["dispatch"]["turn_anchor_id"].as_string().is_not(None),
                ProjectRun.input["dispatch"]["task"].as_string().is_not(None),
                ProjectRun.output["subagent_run_id"].as_string().is_(None),
            )
        )
        if after is not None:
            created_at, run_id = after
            query = query.where(
                or_(
                    ProjectRun.created_at > created_at,
                    and_(ProjectRun.created_at == created_at, ProjectRun.id > run_id),
                )
            )
        rows = (
            await db.execute(query.order_by(ProjectRun.created_at, ProjectRun.id).limit(PROJECT_DISPATCH_BATCH_SIZE))
        ).all()
        if not rows:
            return [], None
        last = rows[-1]
        return [row.id for row in rows], (last.created_at, last.id)


async def _pending_project_dispatch_runs() -> list[uuid.UUID]:
    pending, _cursor = await _pending_project_dispatch_batch()
    return pending


async def _recover_project_dispatch_outbox_once() -> bool:
    """Visit every queued dispatch once without retaining a database session."""

    cursor: ProjectDispatchCursor | None = None
    made_progress = False
    while True:
        pending, next_cursor = await _pending_project_dispatch_batch(after=cursor)
        if not pending:
            return made_progress
        for project_run_id in pending:
            try:
                result = await dispatch_project_run(project_run_id)
                made_progress = result.get("status") not in {"gone", "not_dispatchable"} or made_progress
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate each durable outbox row
                logger.exception(f"[subagent] project dispatch recovery failed run={project_run_id}: {exc}")
        if len(pending) < PROJECT_DISPATCH_BATCH_SIZE or next_cursor is None:
            return made_progress
        cursor = next_cursor
        await asyncio.sleep(0)


async def _subagent_parent_dispatch_loop() -> None:
    while True:
        try:
            pending = await _pending_parent_events()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - daemon must survive transient DB faults
            logger.exception(f"[subagent] parent event scan failed: {exc}")
            await asyncio.sleep(1)
            continue
        made_progress = False
        for message_id in pending:
            try:
                made_progress = await _dispatch_parent_event(message_id) or made_progress
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate each durable event
                logger.exception(f"[subagent] parent event dispatch failed message={message_id}: {exc}")
        try:
            leader_groups = await _pending_project_leader_groups()
            for group_id in leader_groups:
                made_progress = await _dispatch_project_leader_batch(group_id) or made_progress
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable claimed batches retry on the next scan
            logger.exception(f"[subagent] project Leader batch dispatch failed: {exc}")
        try:
            made_progress = await _recover_project_dispatch_outbox_once() or made_progress
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - daemon must survive transient scan faults
            logger.exception(f"[subagent] project dispatch scan failed: {exc}")
        if not made_progress:
            await asyncio.sleep(0.5)


async def start_subagent_daemon() -> None:
    """Run durable child workers and the parent-event dispatcher together."""
    await asyncio.gather(
        _subagent_worker_loop(),
        _subagent_parent_dispatch_loop(),
    )
