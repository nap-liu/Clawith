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


def _message_meta(row: ChatMessage) -> dict:
    return dict(row.message_meta) if isinstance(row.message_meta, dict) else {}


def _run_owned_by_parent(run: SubagentRun, parent_session_id: uuid.UUID) -> bool:
    return run.parent_session_id == parent_session_id


def _agent_participates(session: ChatSession, agent_id: uuid.UUID) -> bool:
    return session.agent_id == agent_id or (
        session.source_channel == "agent" and session.peer_agent_id == agent_id
    )


def _task_title(task: str) -> str:
    compact = " ".join(str(task or "").split())
    return (compact[:80] or "Subagent")


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
    name: str | None = None,
    task: str,
    mode: str = "sync",
    model: str | None = None,
    fork: bool = False,
    soul: bool = True,
    memory: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
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
        if agent is None or parent is None or not _agent_participates(parent, agent_id):
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
        child = ChatSession(
            id=child_id,
            agent_id=agent_id,
            user_id=child_user_id,
            title=child_title,
            source_channel=SUBAGENT_CHANNEL,
            is_primary=False,
            is_group=False,
            created_at=now,
            last_message_at=now,
        )
        run = SubagentRun(
            id=child_id,
            parent_session_id=parent.id,
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
                "kind": SUBAGENT_INPUT,
                "subagent_input_state": INPUT_PENDING,
                "attachments": [],
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
                    "kind": SUBAGENT_INPUT,
                    "subagent_input_state": INPUT_PENDING,
                    "attachments": [],
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


async def prepare_subagent_tools(agent_id: uuid.UUID) -> list[dict]:
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
    async with async_session() as db:
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
    parent_tool = row[0] if row else None
    if parent_tool is None:
        raise RuntimeError("send_message_to_parent builtin tool is not seeded")
    assignment = row[1]
    if not parent_tool.enabled or not resolved_agent_tool_enabled(
        parent_tool.name,
        assignment,
    ):
        return child_tools
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
            },
            turn_anchor_id=anchor_id,
        )
        for row in processed:
            meta = _message_meta(row)
            meta["subagent_input_state"] = INPUT_DONE
            meta["turn_status"] = "completed" if not failed else "failed"
            row.message_meta = meta
        child.last_message_at = datetime.now(UTC)
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
                tools = await prepare_subagent_tools(child.agent_id)
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
                    turn_type="subagent",
                    model_name=run.model,
                    include_soul=run.soul,
                    include_memory=run.memory,
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
            from app.services.active_turns import is_current_turn_cancel_requested

            control_plane_cancelled = is_current_turn_cancel_requested()
            async with async_session() as cancel_db:
                owned = await cancel_db.get(SubagentRun, run_id, with_for_update=True)
                if (
                    owned is not None
                    and owned.status == RUN_RUNNING
                    and owned.lease_owner == settings.INSTANCE_ID
                ):
                    owned.status = RUN_CANCELLED if control_plane_cancelled else RUN_QUEUED
                    owned.lease_owner = None
                    owned.lease_expires_at = None
                    if control_plane_cancelled:
                        rows = (
                            await cancel_db.execute(
                                select(ChatMessage).where(
                                    ChatMessage.conversation_id == str(run_id),
                                    ChatMessage.message_meta["kind"].as_string()
                                    == SUBAGENT_INPUT,
                                    ChatMessage.message_meta[
                                        "subagent_input_state"
                                    ].as_string()
                                    == INPUT_PROCESSING,
                                )
                            )
                        ).scalars().all()
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
        if not pending:
            await asyncio.sleep(0.5)
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
        if not made_progress:
            await asyncio.sleep(0.5)


async def start_subagent_daemon() -> None:
    """Run durable child workers and the parent-event dispatcher together."""
    await asyncio.gather(
        _subagent_worker_loop(),
        _subagent_parent_dispatch_loop(),
    )
