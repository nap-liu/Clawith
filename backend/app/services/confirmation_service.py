"""Confirmation service — create, serialize, broadcast, and resolve confirmation cards.

create_confirmation: insert AgentConfirmation into DB, broadcast confirmation_card
    event to web WebSocket clients.
serialize_confirmation_for_display: produce the dict shape consumed by the frontend card.
resolve_confirmation: handle user approve/reject; execute carried action; resume agent loop.
_run_continuation: resume the agent's LLM loop with the resolution outcome text.

Task 8 will add non-web channel text fallbacks.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.agent_confirmation import AgentConfirmation
from app.services.agent_tools import _execute_tool_direct  # no circular dep: agent_tools never imports confirmation_service

logger = logging.getLogger(__name__)

CONFIRMATION_EXPIRY_HOURS = 24


def _action_preview(action: dict | None) -> str:
    if not action:
        return ""
    return str(action.get("tool") or "")


def _looks_like_failure(result: str) -> bool:
    """Heuristic for whether a post-approval tool execution failed.

    `_execute_tool_direct` returns 'Error executing ...' when the tool raised, and
    'Tool ... does not support post-approval execution' for tools it cannot run —
    neither of which executed the carried action successfully.
    """
    low = (result or "").lower()
    return low.startswith("error") or "does not support post-approval" in low


def serialize_confirmation_for_display(c: AgentConfirmation) -> dict:
    return {
        "role": "confirmation",
        "confirmation_id": str(c.id),
        "title": c.title,
        "summary": c.summary,
        "action_preview": _action_preview(c.action),
        "status": c.status,
        "risk_level": c.risk_level,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


async def _broadcast(agent_id: uuid.UUID, conversation_id: str, payload: dict) -> None:
    """Best-effort broadcast to web WebSocket clients. Never raises."""
    try:
        from app.api.websocket import manager  # lazy import — avoids services→api circular dependency

        await manager.send_to_session(str(agent_id), str(conversation_id), payload)
    except Exception:  # best-effort; broadcast failure must not affect DB write
        pass


async def create_confirmation(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    chat_session_id: uuid.UUID | None,
    source_channel: str,
    title: str,
    summary: str,
    action: dict | None,
    risk_level: str,
    requested_by_user_id: uuid.UUID | None,
) -> AgentConfirmation:
    """Insert an AgentConfirmation row, then broadcast a confirmation_card event.

    Opens its own DB session (not caller-supplied) so it can be called from tool
    execution context that already holds a separate session.
    """
    async with async_session() as db:
        tenant_id = None
        try:
            from app.services.agent_tools import _get_agent_tenant_id  # lazy — agent_tools is heavy

            tenant_id = await _get_agent_tenant_id(agent_id)
        except Exception:
            pass

        c = AgentConfirmation(
            agent_id=agent_id,
            tenant_id=tenant_id,
            conversation_id=str(conversation_id),
            chat_session_id=chat_session_id,
            source_channel=source_channel or "web",
            title=title,
            summary=summary,
            action=action,
            risk_level=risk_level,
            status="pending",
            requested_by_user_id=requested_by_user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=CONFIRMATION_EXPIRY_HOURS),
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)

    payload = {"type": "confirmation_card", **serialize_confirmation_for_display(c)}
    await _broadcast(agent_id, conversation_id, payload)
    # Non-web channel text fallback: see Task 8
    return c


async def resolve_confirmation(
    *,
    confirmation_id: uuid.UUID,
    agent_id: uuid.UUID,
    decision: str,
    resolving_user_id: uuid.UUID,
) -> AgentConfirmation:
    """Process a user's approve or cancel decision on a pending confirmation card.

    - Fetches the AgentConfirmation row (LookupError if not found).
    - Idempotent: if already resolved (not pending), returns current state unchanged.
    - Expired (expires_at < now while still pending): marks expired, broadcasts, returns.
    - confirm + action: executes the carried tool via _execute_tool_direct; stores result;
      sets status to "executed" or "failed" depending on whether the result starts with "error".
    - confirm + no action (pure gate): sets result="用户已确认", status="executed".
    - cancel: sets status="cancelled", no tool execution.
    - After any resolution: commits, broadcasts confirmation_update, then fires
      _run_continuation so the agent gets a follow-up LLM turn.
    """
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        # SELECT ... FOR UPDATE: lock the row for the full read→execute→commit region
        # so concurrent resolves of the same card (double-click / retry) serialize.
        # The second request blocks here until the first commits, then re-reads a
        # terminal status and returns early — the carried action runs exactly once.
        row = await db.execute(
            select(AgentConfirmation)
            .where(
                AgentConfirmation.id == confirmation_id,
                AgentConfirmation.agent_id == agent_id,
            )
            .with_for_update()
        )
        c = row.scalar_one_or_none()
        if c is None:
            raise LookupError(f"AgentConfirmation {confirmation_id} not found for agent {agent_id}")

        # Idempotent: already resolved (incl. a concurrent resolve that won the lock)
        if c.status != "pending":
            return c

        # Expired
        if c.expires_at and c.expires_at < now:
            c.status = "expired"
            await db.commit()
            await db.refresh(c)

        if c.status == "expired":
            await _broadcast(
                agent_id,
                c.conversation_id,
                {
                    "type": "confirmation_update",
                    "confirmation_id": str(c.id),
                    "status": c.status,
                    "result": (c.result or "")[:500],
                },
            )
            return c

        # Set resolver metadata
        c.resolved_by = resolving_user_id
        c.resolved_at = now

        title = c.title or ""
        action = c.action  # {"tool": str, "args": dict} or None

        if decision == "cancel":
            c.status = "cancelled"
            cont_text = f"用户拒绝了操作「{title}」。"
        else:  # confirm
            if action:
                tool_name = action.get("tool", "")
                arguments = action.get("args") or {}
                try:
                    result = await _execute_tool_direct(tool_name, arguments, agent_id)
                except Exception as e:
                    result = f"Error executing {tool_name}: {e}"
                c.result = str(result)
                c.status = "failed" if _looks_like_failure(c.result) else "executed"
                cont_text = f"用户已确认操作「{title}」。执行结果:\n{result}"
            else:
                # Pure confirmation gate — no action to execute
                c.result = "用户已确认"
                c.status = "executed"
                cont_text = f"用户已确认操作「{title}」。执行结果:\n{c.result}"

        await db.commit()
        await db.refresh(c)

    await _broadcast(
        agent_id,
        c.conversation_id,
        {
            "type": "confirmation_update",
            "confirmation_id": str(c.id),
            "status": c.status,
            "result": (c.result or "")[:500],
        },
    )

    # Best-effort follow-up turn. Its failure must NOT mask the resolution: the
    # carried action already executed and committed above — swallow & log so the
    # caller (REST endpoint) still gets the committed outcome instead of a 500.
    try:
        await _run_continuation(
            agent_id=agent_id,
            conversation_id=c.conversation_id,
            chat_session_id=c.chat_session_id,
            text=cont_text,
            resolving_user_id=resolving_user_id,
        )
    except Exception:
        logger.exception(
            "Continuation turn after resolving confirmation %s failed; resolution stands.", c.id
        )

    return c


async def _run_continuation(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    chat_session_id: uuid.UUID | None,
    text: str,
    resolving_user_id: uuid.UUID,
) -> None:
    """Resume the agent's LLM loop with the confirmation resolution outcome.

    Uses run_channel_message for per-session lock + connection-independent execution.
    _call_agent_llm does NOT self-persist ChatMessage; callers must save user + assistant
    messages manually (per wecom_stream.py pattern). This function does that.
    """
    from app.models.audit import ChatMessage
    from app.services.channel_dispatch import ChannelReactions, run_channel_message
    from app.services.channel_llm import _call_agent_llm
    from app.services.chat_history import load_history_for_llm, persist_assistant_reply

    async def _work() -> str:
        async with async_session() as db:
            # Load history BEFORE persisting the continuation user message. _call_agent_llm
            # appends `text` itself, so the loaded history must EXCLUDE this turn — otherwise
            # the continuation message is sent to the LLM twice (mirrors wecom_stream order).
            from app.models.agent import Agent as AgentModel, DEFAULT_CONTEXT_WINDOW_SIZE
            from sqlalchemy import select as _select
            agent_row = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_row.scalar_one_or_none()
            ctx_size = (agent_obj.context_window_size if agent_obj else None) or DEFAULT_CONTEXT_WINDOW_SIZE

            history = await load_history_for_llm(
                db,
                agent_id=agent_id,
                conversation_id=conversation_id,
                ctx_size=ctx_size,
            )

            # Persist the continuation user message (audit trail + future turns) AFTER
            # loading history so it isn't double-counted in this turn's prompt.
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=resolving_user_id,
                    role="user",
                    content=text,
                    conversation_id=conversation_id,
                )
            )
            await db.commit()

            reply = await _call_agent_llm(
                db,
                agent_id,
                text,
                session_id=conversation_id,
                user_id=resolving_user_id,
                history=history,
                recovery_hint=None,
            )

        # Persist the assistant reply via its own session (timestamps after tool loop)
        await persist_assistant_reply(
            async_session,
            agent_id=agent_id,
            user_id=resolving_user_id,
            conversation_id=conversation_id,
            content=reply,
        )
        return reply

    await run_channel_message(
        str(conversation_id),
        is_command=False,
        reactions=ChannelReactions(),
        work=_work,
    )
