"""Provider-neutral reconstruction of IM progress reactions after restart."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.channel_dispatch import ChannelReactions
from app.services.turn_inbox import (
    CHANNEL_RECEIPT_PROVIDER_META_KEY,
    bind_durable_channel_receipt_anchor,
    durable_channel_receipt_anchor_id,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class RecoveryReactionContext:
    db: AsyncSession
    session: ChatSession
    receipt_message: ChatMessage
    provider_receipt: dict


RecoveryReactionFactory = Callable[
    [RecoveryReactionContext],
    Awaitable[ChannelReactions],
]

_factories: dict[str, RecoveryReactionFactory] = {}
_defaults_loaded = False


def register_recovery_reaction_factory(
    source_channel: str,
    factory: RecoveryReactionFactory,
) -> None:
    """Register one transport adapter without coupling turn recovery to it."""

    _factories[source_channel.lower()] = factory


def _load_default_adapters() -> None:
    global _defaults_loaded
    if _defaults_loaded:
        return
    # Importing an adapter registers its optional recovery capability. Channels
    # without progress reactions intentionally have no factory.
    from app.services import dingtalk_reaction  # noqa: F401

    _defaults_loaded = True


def supports_recovered_channel_reactions(source_channel: str | None) -> bool:
    """Return whether the transport registered durable recovery feedback."""

    _load_default_adapters()
    return str(source_channel or "").lower() in _factories


async def load_recovered_channel_reactions(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> ChannelReactions:
    """Rebuild optional reaction hooks from the current durable receipt anchor."""

    try:
        session_id = uuid.UUID(str(conversation_id))
    except (TypeError, ValueError):
        return ChannelReactions()

    _load_default_adapters()
    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == agent_id,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            return ChannelReactions()
        factory = _factories.get(str(session.source_channel or "").lower())
        if factory is None:
            return ChannelReactions()
        receipt_message_id = durable_channel_receipt_anchor_id(session)
        if receipt_message_id is None:
            from app.services.conversation_turn_lifecycle import (
                ACTIVE_TURN_STATUS,
                conversation_turn_snapshot_for_session,
            )

            snapshot = conversation_turn_snapshot_for_session(session)
            if snapshot.status != ACTIVE_TURN_STATUS:
                return ChannelReactions()
            receipt_message_id = snapshot.anchor_id
        if receipt_message_id is None:
            return ChannelReactions()
        receipt_message = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.id == receipt_message_id,
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if receipt_message is None:
            return ChannelReactions()
        meta = (
            dict(receipt_message.message_meta or {})
            if isinstance(receipt_message.message_meta, dict)
            else {}
        )
        provider_receipt = meta.get(CHANNEL_RECEIPT_PROVIDER_META_KEY)
        if not isinstance(provider_receipt, dict):
            return ChannelReactions()
        marker_bound = bind_durable_channel_receipt_anchor(
            session,
            receipt_message.id,
        )
        reactions = await factory(
            RecoveryReactionContext(
                db=db,
                session=session,
                receipt_message=receipt_message,
                provider_receipt=dict(provider_receipt),
            )
        )
        if reactions.bind_receipt_context is not None:
            reactions.bind_receipt_context(
                agent_id,
                conversation_id,
                receipt_message.id,
            )
        if marker_bound:
            await db.commit()
        return reactions
