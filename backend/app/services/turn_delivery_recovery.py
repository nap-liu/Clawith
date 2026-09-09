"""Finish committed reply delivery without reopening a model Turn."""

import uuid

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.im_delivery import (
    IMDeliveryResult, attach_delivery_to_meta, deliver_persisted_message,
    register_delivery,
)
from app.services.turn_runtime import TurnRuntime

LOCAL_CHANNELS = frozenset({"web", "miniprogram", "wechat_miniprogram", "mcp", "agent"})
BUSINESS_CHANNELS = frozenset({"trigger", "subagent", "project", "task", "schedule"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


async def prepare_terminal_delivery(db, message: ChatMessage) -> None:
    """Freeze an exact reply destination in the existing message receipt."""
    meta = dict(message.message_meta or {})
    try:
        anchor_id = uuid.UUID(str(meta.get("turn_anchor_id")))
        session_id = uuid.UUID(str(message.conversation_id))
    except (ValueError, TypeError):
        return
    anchor = await db.get(ChatMessage, anchor_id)
    session = await db.get(ChatSession, session_id)
    if (anchor is None or session is None or anchor.agent_id != message.agent_id
            or anchor.conversation_id != message.conversation_id
            or session.agent_id != message.agent_id
            or session.source_channel in BUSINESS_CHANNELS
            or (anchor.message_meta or {}).get("background_execution")):
        return
    execution_id = (anchor.message_meta or {}).get("execution_agent_id") or message.agent_id
    agent = await db.get(Agent, uuid.UUID(str(execution_id)))
    if agent is None or agent.agent_type == "openclaw":
        return
    delivery = dict(meta.get("delivery") or {})
    if not delivery:
        if (session.source_channel not in LOCAL_CHANNELS
                or meta.get("turn_status") not in TERMINAL_STATUSES):
            return
        meta = attach_delivery_to_meta(meta, IMDeliveryResult.pending(session.source_channel))
        delivery = dict(meta["delivery"])
    if delivery.get("status") != "pending":
        return
    delivery.setdefault("origin", {
        "source_channel": session.source_channel,
        "external_conv_id": session.external_conv_id,
        "is_group": bool(session.is_group),
    })
    meta["delivery"] = delivery
    message.message_meta = meta


async def acknowledge_terminal_event(message_id) -> None:
    """A committed Web/history reply was published through its normal owner."""
    try:
        local_id = uuid.UUID(str(message_id))
    except (TypeError, ValueError):
        return
    async with async_session() as db:
        row = await db.get(ChatMessage, local_id)
        delivery = dict((row.message_meta or {}).get("delivery") or {}) if row else {}
        if delivery.get("status") != "pending" or delivery.get("channel") not in LOCAL_CHANNELS:
            return
        try:
            anchor = await db.get(ChatMessage, uuid.UUID(str((row.message_meta or {}).get("turn_anchor_id"))))
            session = await db.get(ChatSession, uuid.UUID(str(row.conversation_id)))
        except (TypeError, ValueError):
            return
        # Background finalizers own their receipt even in a Web/A2A Session.
        if (anchor is None or session is None
                or (anchor.message_meta or {}).get("background_execution")
                or session.source_channel in BUSINESS_CHANNELS):
            return
        channel = delivery["channel"]
        conversation_id = row.conversation_id
    await register_delivery(local_id, IMDeliveryResult.unsupported_delivery(
        channel, "websocket", conversation_ref=conversation_id,
    ))


async def recover_terminal_delivery(candidate: ChatMessage) -> bool:
    """Deliver the exact old reply even after its Session admits a later Turn."""
    async with async_session() as db:
        row = await db.get(ChatMessage, candidate.id)
        if row is None or row.role != "assistant":
            return False
        meta = dict(row.message_meta or {})
        delivery = dict(meta.get("delivery") or {})
        origin = delivery.get("origin")
        if (delivery.get("status") != "pending" or not isinstance(origin, dict)
                or meta.get("turn_status") not in TERMINAL_STATUSES):
            return False
        try:
            anchor = await db.get(ChatMessage, uuid.UUID(str(meta["turn_anchor_id"])))
            session = await db.get(ChatSession, uuid.UUID(row.conversation_id))
        except (KeyError, ValueError, TypeError):
            return False
        if (anchor is None or session is None or anchor.agent_id != row.agent_id
                or anchor.conversation_id != row.conversation_id
                or session.agent_id != row.agent_id
                or (anchor.message_meta or {}).get("turn_status") != meta["turn_status"]
                or (anchor.message_meta or {}).get("background_execution")
                or session.source_channel in BUSINESS_CHANNELS):
            return False
        if origin != {"source_channel": session.source_channel,
                      "external_conv_id": session.external_conv_id,
                      "is_group": bool(session.is_group)}:
            return False
        execution_id = uuid.UUID(str((anchor.message_meta or {}).get("execution_agent_id") or row.agent_id))
        agent = await db.get(Agent, execution_id)
        if agent is None or agent.agent_type == "openclaw":
            return False
        from app.services.conversation_turn_lifecycle import get_conversation_turn_snapshot

        snapshot = await get_conversation_turn_snapshot(
            db, agent_id=row.agent_id, conversation_id=row.conversation_id,
            turn_anchor_id=anchor.id,
        )
        if (anchor.message_meta or {}).get("gateway_direct_reply"):
            from app.services.turn_recovery import _upsert_gateway_direct_reply

            if not await _upsert_gateway_direct_reply(
                db, anchor=anchor, reply=row.content, execution_agent_id=execution_id,
            ):
                return False
            await db.commit()
        runtime = TurnRuntime(True, origin["source_channel"], row.conversation_id,
                              origin["external_conv_id"], origin["is_group"])
        message_id, agent_id, conversation_id, content = row.id, row.agent_id, row.conversation_id, row.content
    # No database transaction spans observer/provider I/O.
    if runtime.source_channel in LOCAL_CHANNELS:
        from app.services.conversation_turn_lifecycle import publish_conversation_turn_event

        await publish_conversation_turn_event(
            agent_id=agent_id, conversation_id=conversation_id,
            payload={"type": "done", "role": "assistant", "content": content,
                     "message_id": str(message_id)},
            snapshot=snapshot, event_kind="turn_terminal",
        )
        return True
    if delivery.get("parts"):
        # The provider already accepted an artifact. Never send it twice.
        await register_delivery(message_id, IMDeliveryResult.unknown(
            runtime.source_channel, "interrupted_delivery_requires_provider_reconciliation",
        ))
        return False
    result = await deliver_persisted_message(
        message_id=message_id, agent_id=agent_id, runtime=runtime, message=content,
    )
    return result.ok
