"""Shared helper: find-or-create ChatSession by external channel conv_id.

Used by feishu.py, slack.py, discord_bot.py, wecom.py, teams.py — eliminates in-process caches.
"""
import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.services.session_identity import require_same_tenant_session_user


async def find_or_create_channel_session(
    db: AsyncSession,
    agent_id: _uuid.UUID,
    user_id: _uuid.UUID,
    external_conv_id: str,
    source_channel: str,
    first_message_title: str,
    is_group: bool = False,
    group_name: str | None = None,
) -> ChatSession:
    """Find an existing ChatSession by channel-scoped conversation id, or create one.

    Relies on the UNIQUE constraint on
    ``(agent_id, source_channel, external_conv_id)`` in the DB.  Provider
    conversation ids are not globally namespaced (Teams in particular uses the
    raw id), so omitting ``source_channel`` can attach a message to another
    transport's session.

    Args:
        is_group: True for group chat sessions (Feishu group, Slack channel, etc.).
                  Group sessions keep user_id as the agent creator (placeholder) and
                  are excluded from the user's "mine" session list.
        group_name: Display name for group sessions (e.g. IM group/channel name).
    """
    if not is_group:
        await require_same_tenant_session_user(db, agent_id, user_id)
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.agent_id == agent_id,
            ChatSession.source_channel == source_channel,
            ChatSession.external_conv_id == external_conv_id,
        )
    )
    session = result.scalar_one_or_none()

    if session is None:
        now = datetime.now(timezone.utc)
        candidate = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title=group_name[:40] if (is_group and group_name) else first_message_title[:40],
            source_channel=source_channel,
            external_conv_id=external_conv_id,
            is_group=is_group,
            group_name=group_name,
            created_at=now,
        )
        try:
            async with db.begin_nested():
                db.add(candidate)
                await db.flush()  # populate candidate.id
            session = candidate
        except IntegrityError:
            # Another replica created the same channel-scoped conversation
            # between our SELECT and INSERT.  The unique constraint is the
            # arbitration point; reuse the winner without rolling back the
            # caller's surrounding inbound-event transaction.
            session = (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.source_channel == source_channel,
                        ChatSession.external_conv_id == external_conv_id,
                    )
                )
            ).scalar_one()
    else:
        # For P2P sessions: re-attribute to the correct user
        # (fixes legacy sessions stored under creator_id)
        if not session.is_group and session.user_id != user_id:
            session.user_id = user_id

        # Upgrade legacy rows that were created before is_group was passed
        # by the channel entry. Idempotent: when callers correctly mark a
        # session as group, the existing row gets retroactively flagged
        # and gains a group_name + title (downstream UI / listing surfaces
        # rely on session.is_group being accurate).
        if is_group and not session.is_group:
            session.is_group = True
            if group_name:
                session.group_name = group_name
                session.title = group_name[:40]

        # For group sessions: update group_name if it changed
        if session.is_group and group_name and session.group_name != group_name:
            session.group_name = group_name
            session.title = group_name[:40]

    return session
