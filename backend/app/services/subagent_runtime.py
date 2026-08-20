"""Durable Subagent sessions, execution, messaging, and parent wake-up."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
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
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})

LEASE_SECONDS = 60
WORKER_CONCURRENCY = 4

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


async def _agent_participates(db, session: ChatSession, agent_id: uuid.UUID) -> bool:
    if session.agent_id == agent_id or (
        session.source_channel == "agent" and session.peer_agent_id == agent_id
    ):
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
    return (compact[:80] or "Subagent")


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
        (
            index
            for index, row in enumerate(rows)
            if getattr(row, "id", None) == turn_anchor_id
        ),
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
    task: str,
    mode: str = "sync",
    model: str | None = None,
    fork: bool = False,
    turn_anchor_id: uuid.UUID | None = None,
    project_run_id: uuid.UUID | None = None,
    input_metadata: dict | None = None,
) -> tuple[SubagentRun, bool]:
    """Create one child Session and lifecycle row, idempotent per parent tool call."""
    task_text = str(task or "").strip()
    if not task_text:
        raise SubagentError("task 不能为空。")
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
        project_member = None
        project_capabilities: list[dict] = []
        project_tool_policy_snapshot: dict = {}
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
            ).scalars().all()
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
            project_tool_policy_snapshot = dict(
                dict((project.settings or {}).get("policies") or {}).get("project_tools") or {}
            ) if project is not None else {}

        child = ChatSession(
            id=child_id,
            agent_id=agent_id,
            project_id=parent.project_id,
            user_id=child_user_id,
            title=_task_title(task_text),
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
                            dict(dict(project_member.config_snapshot or {}).get("membership") or {}).get(
                                "generation"
                            )
                            or 1
                        ),
                    )
                    if project_member
                    else None
                ),
                "membership_revoked": False,
                "project_role_snapshot": "leader" if project_member and project_member.is_leader else "participant",
                "project_tool_policy_snapshot": project_tool_policy_snapshot,
                "member_config_snapshot": dict(project_member.config_snapshot or {}) if project_member else {},
                "capability_snapshot": project_capabilities,
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
                **dict(input_metadata or {}),
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
            await _validate_execution_identity(db, run, child)
        except RuntimeError as exc:
            raise SubagentError("项目成员已退出，不能继续这个工作会话。") from exc
        existing = (
            await db.execute(
                select(ChatMessage.id).where(
                    ChatMessage.external_event_key == event_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return run.status
        now = datetime.now(UTC)
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
                    **dict(input_metadata or {}),
                    "kind": SUBAGENT_INPUT,
                    "subagent_input_state": INPUT_PENDING,
                    "attachments": [],
                    **({"project_run_id": str(project_run_id)} if project_run_id else {}),
                },
                created_at=now,
            )
        )
        child.last_message_at = now
        if run.status in {RUN_COMPLETED, RUN_FAILED}:
            run.status = RUN_QUEUED
            run.mode = "async"
            run.lease_owner = None
            run.lease_expires_at = None
        await db.commit()
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
            await db.execute(
                select(ChatMessage.id).where(
                    ChatMessage.external_event_key == event_key
                )
            )
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
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(child_id),
                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                    ChatMessage.message_meta["subagent_input_state"].as_string().in_(
                        [INPUT_PENDING, INPUT_PROCESSING]
                    ),
                )
            )
        ).scalars().all()
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
        tasks = [
            _running_tasks.get(run_id)
            for run_id in run_ids
            if _running_tasks.get(run_id) is not None
        ]
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
    child_tools = [
        tool
        for tool in tools
        if tool.get("function", {}).get("name") not in hidden
    ]
    project_tools: list[dict] = []
    async with async_session() as db:
        child = await db.get(ChatSession, session_id) if session_id else None
        if child is not None and child.source_channel == SUBAGENT_CHANNEL and child.project_id is not None:
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

            all_mcp_names = set(
                (
                    await db.execute(select(Tool.name).where(Tool.type == "mcp"))
                ).scalars()
            )
            allowed_server_ids = {
                uuid.UUID(str(entry["capability_id"]))
                for entry in runtime_config.get("capability_snapshot", [])
                if isinstance(entry, dict)
                and entry.get("type") == "mcp"
                and entry.get("capability_id")
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
            project_tools = project_runtime_tool_schemas(project, member)
        row = (
            await db.execute(
                select(Tool, AgentTool)
                .outerjoin(
                    AgentTool,
                    (AgentTool.tool_id == Tool.id) & (AgentTool.agent_id == agent_id),
                )
                .where(Tool.name == "send_message_to_parent")
            )
        ).one_or_none()
    if project_tools:
        projected_names = {item["function"]["name"] for item in project_tools}
        child_tools = [
            item
            for item in child_tools
            if item.get("function", {}).get("name") not in projected_names
        ]
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
    now = datetime.now(UTC)
    async with async_session() as db:
        conditions = [
            or_(
                SubagentRun.status == RUN_QUEUED,
                and_(
                    SubagentRun.status == RUN_RUNNING,
                    SubagentRun.lease_expires_at < now,
                ),
            )
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
        run.status = RUN_RUNNING
        run.lease_owner = settings.INSTANCE_ID
        run.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return run.id


async def _load_or_start_input(
    run_id: uuid.UUID,
) -> tuple[ChatMessage, bool] | None:
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if (
            run is None
            or run.status != RUN_RUNNING
            or run.lease_owner != settings.INSTANCE_ID
        ):
            return None
        processing = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                    ChatMessage.message_meta["subagent_input_state"].as_string()
                    == INPUT_PROCESSING,
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
                        ChatMessage.message_meta["subagent_input_state"].as_string()
                        == INPUT_PENDING,
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
        meta = _message_meta(anchor)
        meta.update(
            {
                "subagent_input_state": INPUT_PROCESSING,
                "subagent_turn_anchor_id": str(anchor.id),
                "turn_status": "running",
            }
        )
        anchor.message_meta = meta
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
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                    ChatMessage.message_meta["subagent_input_state"].as_string()
                    == INPUT_PENDING,
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .with_for_update()
            )
        ).scalars().all()
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
        run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return injected


async def _finish_subagent_turn(
    *,
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
    reply: str,
    failed: bool,
) -> bool:
    """Persist the reply and lifecycle transition behind the same Run lock."""
    from app.services.chat_history import persist_assistant_reply_row

    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if (
            run is None
            or run.status != RUN_RUNNING
            or run.lease_owner != settings.INSTANCE_ID
        ):
            return False
        child = await db.get(ChatSession, run_id)
        if child is None:
            return False
        processed = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                    ChatMessage.message_meta["subagent_input_state"].as_string()
                    == INPUT_PROCESSING,
                    ChatMessage.message_meta["subagent_turn_anchor_id"].as_string()
                    == str(anchor_id),
                )
            )
        ).scalars().all()
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
                        ChatMessage.message_meta["subagent_input_state"].as_string()
                        == INPUT_PENDING,
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
        content = (reply or "").strip() or (
            "Subagent 执行失败，未返回错误详情。" if failed else "Subagent 已完成。"
        )
        # One durable child Session may process many separately auditable
        # project mentions. Complete every ProjectRun whose exact input was
        # consumed by this turn; do not leave the Runs UI permanently queued.
        from app.models.project import ProjectRun

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
                await db.execute(
                    select(ProjectRun).where(
                        ProjectRun.id.in_(project_run_ids),
                        ProjectRun.project_id == run.project_id,
                    )
                )
            ).scalars().all()
            for project_run in project_runs:
                project_run.status = "failed" if failed else "succeeded"
                project_run.finished_at = now
                project_run.output = {
                    **dict(project_run.output or {}),
                    "subagent_run_id": str(run.id),
                    "subagent_session_id": str(run.id),
                    "result": content,
                }
                project_run.error = content if failed else None
        await persist_assistant_reply_row(
            db,
            agent_id=child.agent_id,
            user_id=run.execution_user_id,
            conversation_id=str(run_id),
            content=content,
            message_meta={
                "kind": kind,
                "subagent_wake": terminal and run.mode == "async",
                "attachments": [],
                "project_run_ids": [str(value) for value in sorted(project_run_ids, key=str)],
            },
            turn_anchor_id=anchor_id,
        )
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
                    if (
                        owned is None
                        or owned.status != RUN_RUNNING
                        or owned.lease_owner != settings.INSTANCE_ID
                    ):
                        if current is not None and not current.done():
                            current.cancel()
                        return
                    owned.lease_expires_at = datetime.now(UTC) + timedelta(
                        seconds=LEASE_SECONDS
                    )
                    await heartbeat_db.commit()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - lease safety boundary
                failures += 1
                logger.warning(
                    f"[subagent] lease renewal failed run={run_id} "
                    f"attempt={failures}: {exc}"
                )
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
    try:
        while True:
            claimed_input = await _load_or_start_input(run_id)
            if claimed_input is None:
                return
            anchor, recovering = claimed_input
            current_anchor_id = anchor.id
            async with async_session() as db:
                run = await db.get(SubagentRun, run_id)
                child = await db.get(ChatSession, run_id)
                if run is None or child is None:
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
                tools = await prepare_subagent_tools(
                    child.agent_id,
                    child.id,
                    execution_user_id=run.execution_user_id,
                )
                reply = await _call_agent_llm(
                    db,
                    child.agent_id,
                    "" if recovering else anchor.content,
                    session_id=str(run_id),
                    user_id=run.execution_user_id,
                    history=history,
                    recovery_hint=None,
                    continue_turn=recovering,
                    recovery_mode=recovering,
                    turn_anchor_id=anchor.id,
                    model_name=run.model,
                    prepared_tools=tools,
                    before_round=lambda _round, aid=anchor.id: _drain_subagent_inbox(
                        run_id, aid
                    ),
                    before_tool_execution=lambda: _assert_subagent_running(run_id),
                    broadcast_web=False,
                )
            reply_text = str(reply or "")
            failed = is_error_result(reply_text) or reply_text.startswith(
                (
                    "⚠️ 数字员工未找到",
                    "⚠️ Subagent 指定模型 ",
                )
            ) or (
                reply_text.startswith("⚠️ ")
                and "未配置 LLM 模型" in reply_text
            )
            terminal = await _finish_subagent_turn(
                run_id=run_id,
                anchor_id=anchor.id,
                reply=reply,
                failed=failed,
            )
            if terminal:
                return
    except asyncio.CancelledError:
        try:
            async with async_session() as cancel_db:
                owned = await cancel_db.get(SubagentRun, run_id, with_for_update=True)
                if (
                    owned is not None
                    and owned.status == RUN_RUNNING
                    and owned.lease_owner == settings.INSTANCE_ID
                ):
                    owned.status = RUN_QUEUED
                    owned.lease_owner = None
                    owned.lease_expires_at = None
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
                                    ChatMessage.message_meta["kind"].as_string()
                                    == SUBAGENT_INPUT,
                                    ChatMessage.message_meta[
                                        "subagent_input_state"
                                    ].as_string()
                                    == INPUT_PENDING,
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
                    ChatMessage.message_meta["kind"].as_string().in_(
                        [SUBAGENT_COMPLETION, SUBAGENT_FAILURE]
                    ),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        parent_messages = (
            await db.execute(
                select(ChatMessage.content)
                .where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string()
                    == SUBAGENT_PARENT_MESSAGE,
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).scalars().all()
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
        completed_exists = exists(
            select(parent_final.id)
            .select_from(parent_anchor)
            .join(
                parent_final,
                parent_final.conversation_id == parent_anchor.conversation_id,
            )
            .where(
                parent_anchor.external_event_key
                == ("subagent-parent:" + cast(ChatMessage.id, String)),
                parent_final.role == "assistant",
                parent_final.message_meta["turn_anchor_id"].as_string()
                == cast(parent_anchor.id, String),
            )
        )
        project_materialized_exists = exists(
            select(project_materialized.id).where(
                project_materialized.external_event_key
                == ("project-subagent:" + cast(ChatMessage.id, String))
            )
        )
        rows = (
            await db.execute(
                select(ChatMessage.id)
                .join(
                    SubagentRun,
                    cast(ChatMessage.conversation_id, String)
                    == cast(SubagentRun.id, String),
                )
                .where(
                    ChatMessage.message_meta["subagent_wake"].as_boolean().is_(True),
                    ChatMessage.message_meta["kind"].as_string().in_(
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
        ).scalars().all()
        return list(rows)


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

    if parent.source_channel == "project" and parent.project_id is not None:
        # Project Agent Group is an append-only coordination surface. Child
        # output becomes a visible group reply but never resumes the root/Leader
        # LLM, so completion cannot fan out into an implicit broadcast storm.
        external_key = f"project-subagent:{child_message_id}"
        async with async_session() as db:
            from app.models.project import ProjectMemberSnapshot

            existing = (
                await db.execute(
                    select(ChatMessage.id).where(
                        ChatMessage.external_event_key == external_key
                    )
                )
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
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.external_event_key == external_key
                    )
                )
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
                        f"<subagent-event subagent_id=\"{child.id}\" "
                        f"type=\"{label}\">\n{event.content}\n</subagent-event>"
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
                        await db.execute(
                            select(ChatMessage).where(
                                ChatMessage.external_event_key == external_key
                            )
                        )
                    ).scalar_one()
            else:
                completed = (
                    await db.execute(
                        select(ChatMessage.id)
                        .where(
                            ChatMessage.conversation_id == str(parent.id),
                            ChatMessage.role == "assistant",
                            ChatMessage.message_meta["turn_anchor_id"].as_string()
                            == str(anchor.id),
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
            if (
                fresh_latest is not None
                and fresh_latest.id != anchor.id
                and fresh_latest.role != "assistant"
            ):
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
                                ChatMessage.message_meta[
                                    "turn_anchor_id"
                                ].as_string()
                                == str(stored_anchor.id),
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
        ).scalars().all()
    groups: list[uuid.UUID] = []
    for value in rows:
        try:
            groups.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return groups


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
        project = await db.get(Project, group.project_id)
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

        claimed = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(group.id),
                    ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                    ChatMessage.message_meta["leader_batch_state"].as_string() == "claimed",
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).scalars().all()
        if claimed:
            batch_id = str(_message_meta(claimed[0]).get("leader_batch_id") or "")
            source_rows = [
                row for row in claimed if str(_message_meta(row).get("leader_batch_id") or "") == batch_id
            ]
            raw_project_run_id = _message_meta(source_rows[0]).get("leader_batch_project_run_id")
            try:
                project_run_id = uuid.UUID(str(raw_project_run_id))
            except (TypeError, ValueError):
                return False
            project_run = await db.get(ProjectRun, project_run_id)
            if project_run is None:
                return False
        else:
            source_rows = (
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
            ).scalars().all()
            if not source_rows:
                return False
            batch_id = str(uuid.uuid4())
            project_run = ProjectRun(
                tenant_id=project.tenant_id,
                project_id=project.id,
                agent_id=leader.agent_id,
                initiated_by_user_id=project.owner_user_id,
                status="queued",
                trigger_type="leader_reply_batch",
                input={
                    "group_session_id": str(group.id),
                    "leader_agent_id": str(leader.agent_id),
                    "leader_batch_id": batch_id,
                    "source_group_message_ids": [str(row.id) for row in source_rows],
                },
                output={"group_session_id": str(group.id), "leader_batch_id": batch_id},
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
        await db.commit()

    sources = [
        {
            "group_message_id": str(row.id),
            "source_agent_id": str(row.sender_agent_id) if row.sender_agent_id else None,
            "child_message_id": _message_meta(row).get("child_message_id"),
            "subagent_session_id": _message_meta(row).get("subagent_id"),
            "subagent_run_id": _message_meta(row).get("subagent_id"),
            "project_run_ids": _message_meta(row).get("source_project_run_ids", []),
            "content": row.content,
            "attachments": _message_meta(row).get("attachments", []),
        }
        for row in source_rows
    ]
    task = (
        "Process this coalesced batch of project Agent replies in one Leader turn. "
        "Update project coordination as needed; do not broadcast or wake anyone unless you explicitly target them.\n"
        + json.dumps(
            {"leader_batch_id": batch_id, "group_session_id": str(group_session_id), "replies": sources},
            ensure_ascii=False,
        )
    )
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
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(group_session_id),
                    ChatMessage.message_meta["leader_batch_id"].as_string() == batch_id,
                )
            )
        ).scalars().all()
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
            }
        attached_project = await db.get(Project, project.id)
        if attached_project is not None:
            add_event(
                db,
                attached_project,
                "project.agent_reply_batch.dispatched",
                f"Coalesced {len(delivered_rows)} Agent replies into one Leader turn",
                actor_agent_id=leader.agent_id,
                run_id=project_run.id if project_run else None,
                metadata={
                    "leader_batch_id": batch_id,
                    "leader_agent_id": str(leader.agent_id),
                    "leader_subagent_session_id": str(child_id),
                    "leader_batch_input_id": str(batch_input.id) if batch_input else None,
                    "source_group_message_ids": [str(row.id) for row in delivered_rows],
                    "source_count": len(delivered_rows),
                },
            )
        await db.commit()
    return True


PROJECT_DISPATCH_TRIGGERS = frozenset(
    {
        "leader_kickoff",
        "group_leader_message",
        "group_mention",
        "manual",
        "leader",
        "schedule",
        "retry",
    }
)


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
                    ChatMessage.message_meta["project_run_id"].as_string()
                    == str(project_run_id),
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
        project_run = await db.get(ProjectRun, project_run_id)
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
        project = await db.get(Project, project_run.project_id)
        parent = await db.get(ChatSession, group_session_id)
        member = await db.get(ProjectMemberSnapshot, member_id)
        if (
            project is None
            or parent is None
            or parent.project_id != project.id
            or parent.source_channel != "project"
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
                input_metadata={"project_dispatch": True},
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
                input_metadata={"project_dispatch": True},
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
                        ProjectEvent.event_metadata["group_message_id"].as_string()
                        == str(anchor.id),
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


async def _pending_project_dispatch_runs() -> list[uuid.UUID]:
    from app.models.project import ProjectRun
    from app.services.project_service import reconcile_project_run_terminal_state

    async with async_session() as db:
        rows = (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.status.in_(["queued", "running"]),
                    ProjectRun.trigger_type.in_(PROJECT_DISPATCH_TRIGGERS),
                )
                .order_by(ProjectRun.created_at, ProjectRun.id)
                .limit(50)
            )
        ).scalars().all()
        repaired = sum(reconcile_project_run_terminal_state(row) for row in rows)
        if repaired:
            await db.commit()
        return [
            row.id
            for row in rows
            if row.finished_at is None
            if dict((row.input or {}).get("dispatch") or {})
            and not dict(row.output or {}).get("subagent_run_id")
        ]


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
                made_progress = (
                    await _dispatch_parent_event(message_id) or made_progress
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate each durable event
                logger.exception(
                    f"[subagent] parent event dispatch failed message={message_id}: {exc}"
                )
        try:
            leader_groups = await _pending_project_leader_groups()
            for group_id in leader_groups:
                made_progress = await _dispatch_project_leader_batch(group_id) or made_progress
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable claimed batches retry on the next scan
            logger.exception(f"[subagent] project Leader batch dispatch failed: {exc}")
        try:
            for project_run_id in await _pending_project_dispatch_runs():
                result = await dispatch_project_run(project_run_id)
                made_progress = result.get("status") not in {"gone", "not_dispatchable"} or made_progress
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - queued outbox rows remain retryable
            logger.exception(f"[subagent] project dispatch recovery failed: {exc}")
        if not made_progress:
            await asyncio.sleep(0.5)


async def start_subagent_daemon() -> None:
    """Run durable child workers and the parent-event dispatcher together."""
    await asyncio.gather(
        _subagent_worker_loop(),
        _subagent_parent_dispatch_loop(),
    )
