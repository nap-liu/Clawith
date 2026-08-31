"""Shared persistence helpers for confirmation tool-call tests."""

import json
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage


async def _make_turn_anchor(agent_id, user_id, conv: str) -> uuid.UUID:
    from app.services.chat_history import persist_incoming_user_message

    async with async_session() as db:
        row = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="请执行危险操作",
        )
        await db.commit()
        return row.id


async def _row_payload(row_id) -> dict:
    async with async_session() as db:
        row = (await db.execute(select(ChatMessage).where(ChatMessage.id == row_id))).scalar_one()
    return json.loads(row.content)
