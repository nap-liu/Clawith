"""Activity and canonical chat-history API."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    can_view_all_agent_chat_sessions,
    check_agent_access,
    filter_tenant_safe_chat_sessions,
    is_agent_creator,
    require_current_agent_tenant,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User

router = APIRouter(tags=["activity"])


@router.get("/agents/{agent_id}/activity")
async def get_agent_activity(
    agent_id: uuid.UUID,
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get recent activity logs for an Agent creator."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    if not is_agent_creator(current_user, agent):
        return []

    logs = (
        await db.execute(
            select(AgentActivityLog)
            .where(AgentActivityLog.agent_id == agent_id)
            .order_by(AgentActivityLog.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": str(log.id),
            "action_type": log.action_type,
            "summary": log.summary,
            "detail": log.detail_json,
            "related_id": str(log.related_id) if log.related_id else None,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


@router.get("/agents/{agent_id}/chat-history/conversations")
async def list_conversations(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List only canonical, tenant-proven ChatSession conversations.

    Historical provider-prefixed message buckets are intentionally excluded:
    without a canonical ChatSession they cannot prove a tenant or counterpart.
    """
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    if not can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

    rows = (
        await db.execute(
            select(ChatSession).where(
                (ChatSession.agent_id == agent_id)
                | (ChatSession.peer_agent_id == agent_id)
            )
        )
    ).scalars().all()
    sessions = await filter_tenant_safe_chat_sessions(
        db, list(rows), agent.tenant_id
    )
    conversations = []
    for session in sessions:
        stats = (
            await db.execute(
                select(func.count(ChatMessage.id), func.max(ChatMessage.created_at))
                .where(ChatMessage.conversation_id == str(session.id))
            )
        ).one()
        count, last_at = int(stats[0] or 0), stats[1]
        if count == 0:
            continue
        last_content = (
            await db.execute(
                select(ChatMessage.content)
                .where(ChatMessage.conversation_id == str(session.id))
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or ""

        if session.source_channel == "agent":
            partner_id = (
                session.peer_agent_id
                if session.agent_id == agent_id
                else session.agent_id
            )
            partner_name = (
                await db.execute(select(Agent.name).where(Agent.id == partner_id))
            ).scalar_one_or_none() or "未知数字员工"
            partner_type = "agent"
            display_name = f"🤖 {partner_name}"
        elif session.is_group:
            partner_id = None
            partner_type = "group"
            display_name = session.group_name or session.title or "群聊"
        else:
            partner_id = session.user_id
            partner_type = "user"
            display_name = (
                await db.execute(
                    select(User.display_name).where(User.id == session.user_id)
                )
            ).scalar_one_or_none() or "未知用户"

        conversations.append(
            {
                "conv_id": str(session.id),
                "partner_type": partner_type,
                "partner_id": str(partner_id) if partner_id else None,
                "partner_name": display_name,
                "last_message": last_content[:80],
                "message_count": count,
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    conversations.sort(key=lambda item: item["last_at"] or "", reverse=True)
    return conversations


@router.get("/agents/{agent_id}/chat-history/{conv_id:path}")
async def get_conversation_messages(
    agent_id: uuid.UUID,
    conv_id: str,
    limit: int = Query(100, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read one canonical ChatSession; legacy string buckets fail closed."""
    try:
        session_id = uuid.UUID(conv_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc

    from app.api.chat_sessions import _load_accessible_session

    _agent, session, _scope = await _load_accessible_session(
        db, current_user, agent_id, session_id
    )

    messages = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(session.id))
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": str(message.id),
            "role": message.role,
            "sender_user_id": (
                str(message.sender_user_id) if message.sender_user_id else None
            ),
            "sender_agent_id": (
                str(message.sender_agent_id) if message.sender_agent_id else None
            ),
            "content": message.content,
            "created_at": (
                message.created_at.isoformat() if message.created_at else None
            ),
        }
        for message in messages
    ]
