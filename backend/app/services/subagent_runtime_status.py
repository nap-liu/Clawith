"""Read-only status projection for an owned Subagent session."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.services.subagent_runtime_shared import SubagentError


async def get_subagent_status(
    *,
    agent_id: uuid.UUID,
    parent_session_id: str,
    subagent_id: str,
    execution_user_id: uuid.UUID,
) -> dict:
    """Return lifecycle and latest observable result for one owned child."""
    try:
        parent_id = uuid.UUID(str(parent_session_id))
        child_id = uuid.UUID(str(subagent_id))
    except (TypeError, ValueError):
        raise SubagentError("subagent_id 无效。") from None

    async with async_session() as db:
        run = await db.get(SubagentRun, child_id)
        if run is None or run.parent_session_id != parent_id:
            raise SubagentError("Subagent 不存在，或不属于当前 Session。")
        if run.execution_user_id != execution_user_id:
            raise SubagentError("当前执行身份无权查看这个 Subagent。")
        child = await db.get(ChatSession, child_id)
        if child is None or child.agent_id != agent_id:
            raise SubagentError("当前 Agent 无权查看这个 Subagent。")

        latest_result = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(child_id),
                    ChatMessage.role == "assistant",
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        pending_inputs = await db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["kind"].as_string() == "subagent_input",
                ChatMessage.message_meta["subagent_input_state"].as_string() == "pending",
            )
        )
        result_meta = dict(latest_result.message_meta or {}) if latest_result else {}
        return {
            "subagent_id": str(run.id),
            "session_id": str(run.id),
            "status": run.status,
            "mode": run.mode,
            "name": child.title,
            "pending_messages": int(pending_inputs or 0),
            "result": latest_result.content if latest_result else None,
            "failed": result_meta.get("kind") in {"subagent_failure", "subagent_turn_failure"},
            "updated_at": child.last_message_at.isoformat() if child.last_message_at else None,
        }
