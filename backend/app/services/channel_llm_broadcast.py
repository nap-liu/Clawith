"""Shared channel-to-Web message projections."""

import uuid
from sqlalchemy import select
from app.database import async_session

async def _broadcast_to_web_session(agent_id, session_id, payload: dict) -> None:
    """Best-effort mirror of one event to every web client viewing this session.

    The web ConnectionManager is a single-process in-memory registry shared by
    the whole backend (uvicorn runs one process), so this reaches the WS chat
    connections registered under the same session_id. Lazy import avoids a
    services->api import cycle; any failure must never affect channel delivery.
    """
    if not session_id:
        return
    try:
        from app.api.websocket import manager as _ws_manager

        await _ws_manager.send_to_session(str(agent_id), str(session_id), payload)
    except Exception:
        pass


async def _resolve_web_sender_profile(agent_id, user_id) -> tuple[str | None, str | None]:
    """Resolve the canonical sender profile within the Agent's tenant."""
    try:
        resolved_agent_id = uuid.UUID(str(agent_id))
        resolved_user_id = uuid.UUID(str(user_id))
    except (TypeError, ValueError):
        return None, None

    from app.models.agent import Agent
    from app.models.user import User

    async with async_session() as db:
        row = (
            await db.execute(
                select(User.display_name, User.avatar_url)
                .join(Agent, Agent.tenant_id == User.tenant_id)
                .where(Agent.id == resolved_agent_id, User.id == resolved_user_id)
            )
        ).one_or_none()
    return (row[0], row[1]) if row else (None, None)


async def broadcast_channel_user_message(
    agent_id,
    session_id,
    *,
    message,
    sender_name: str | None = None,
    user_id=None,
) -> None:
    """Mirror an inbound IM (channel) user message to web clients viewing the
    SAME session in real time. Without this, a person watching a DingTalk/Feishu
    conversation in the web UI sees the agent's reply stream (see
    ``_call_agent_llm``) but the channel user's OWN message would not appear
    until reload. Serialize the persisted message through the same contract as
    history so the live bubble matches what a reload would render."""
    from app.services.chat_message_serializer import serialize_chat_message_for_client

    resolved_name, sender_avatar_url = await _resolve_web_sender_profile(agent_id, user_id)
    payload = serialize_chat_message_for_client(
        message,
        sender_name=sender_name or resolved_name,
        sender_user_id=user_id,
        sender_avatar_url=sender_avatar_url,
    )
    payload["type"] = "channel_user_message"
    # One-release compatibility for older Web clients.
    payload["user_id"] = str(user_id) if user_id is not None else None
    await _broadcast_to_web_session(agent_id, session_id, payload)


