"""Trigger business completion and delivery over the shared durable turn."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.audit import AuditLog, ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta


_NAMESPACE = uuid.UUID("1cb1fc5c-c7c4-4f02-aa83-fab956557622")


def trigger_completion_snapshot(triggers: list[AgentTrigger]) -> list[dict]:
    """Keep routing data, never duplicate webhook credentials or payloads."""
    keys = (
        "_a2a_session_id", "_origin_session_id", "_origin_source_channel",
        "_origin_external_conv_id", "_origin_actor_ref", "_origin_actor_ref_type",
        "_notification_summary",
    )
    return [
        {"id": str(trigger.id), "name": trigger.name, "type": trigger.type,
         "reason": trigger.reason,
         "config": {key: trigger.config[key] for key in keys if key in (trigger.config or {})}}
        for trigger in triggers
    ]


def _execution(anchor: ChatMessage) -> dict:
    return dict((anchor.message_meta or {}).get("background_execution") or {})


async def _sent_by_tool(db, anchor: ChatMessage) -> bool:
    rows = (await db.execute(select(ChatMessage.content).where(
        ChatMessage.conversation_id == anchor.conversation_id,
        ChatMessage.role == "tool_call",
        ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
    ))).scalars()
    for content in rows:
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            continue
        if (payload.get("name") == "send_platform_message"
                and payload.get("status") == "done"
                and str(payload.get("result") or "").startswith("✅")):
            return True
    return False


async def _project_reply(db, anchor, reply_row, *, agent_id, target, content, a2a=False):
    """Commit a deterministic destination row before any transport I/O."""
    row_id = uuid.uuid5(_NAMESPACE, f"trigger-result:{anchor.id}:{target.id}")
    row = await db.get(ChatMessage, row_id)
    if row is None:
        meta = {"trigger_turn_anchor_id": str(anchor.id)}
        participant = None
        if a2a:
            participant = (await db.execute(select(Participant).where(
                Participant.type == "agent", Participant.ref_id == agent_id,
            ))).scalar_one_or_none()
            meta.update({"direction": "inbound", "source_channel": "agent",
                         "actor_ref": str(participant.id if participant else agent_id)})
        row = ChatMessage(
            id=row_id,
            agent_id=target.agent_id,
            conversation_id=str(target.id),
            role="assistant",
            content=content,
            user_id=anchor.user_id,
            participant_id=participant.id if participant else None,
            thinking=reply_row.thinking,
            external_event_key=f"trigger-result:{anchor.id}:{target.id}",
            message_meta=attach_delivery_to_meta(
                meta, IMDeliveryResult.pending(str(target.source_channel or "web")),
            ) if a2a else meta,
        )
        db.add(row)
        target.last_message_at = datetime.now(timezone.utc)
        await db.flush()
        if a2a:
            from app.services.trigger_runtime.evaluator import match_incoming_chat_message

            await match_incoming_chat_message(db, row, target)
    return row


async def finalize_trigger_turn(db, anchor: ChatMessage, reply_row: ChatMessage | None) -> None:
    """Commit execution terminal state, webhook consumption and callbacks once."""
    from app.services.trigger_daemon_delivery import _advance_webhook_trigger

    execution = _execution(anchor)
    completion = execution.get("completion") or {}
    agent_id = uuid.UUID(str((execution.get("settings") or {}).get("execution_agent_id") or anchor.agent_id))
    status = str((anchor.message_meta or {}).get("turn_status") or "")
    if reply_row is not None:
        status = str((reply_row.message_meta or {}).get("turn_status") or status)
    if status not in {"completed", "failed", "cancelled"}:
        return
    ids = [uuid.UUID(str(value)) for value in completion.get("execution_ids", [])]
    rows = []
    if ids:
        rows = (await db.execute(select(TriggerExecution).where(
            TriggerExecution.id.in_(ids), TriggerExecution.agent_id == agent_id,
        ).order_by(TriggerExecution.id).with_for_update())).scalars().all()
    now = datetime.now(timezone.utc)
    newly_terminal = []
    for row in rows:
        if row.status in {"completed", "failed", "cancelled"}:
            continue
        row.status = status
        row.finished_at = now
        row.lease_owner = None
        row.lease_expires_at = None
        row.last_error = (reply_row.content if reply_row else "Stopped by user") if status != "completed" else None
        newly_terminal.append(row)
    # STOP is not consumption of a webhook batch. Neither service cancellation
    # nor user cancellation may silently pop the next queued business event.
    if status != "cancelled":
        for row in newly_terminal:
            trigger = (await db.execute(select(AgentTrigger).where(
                AgentTrigger.id == row.trigger_id,
            ).with_for_update())).scalar_one_or_none()
            if trigger is not None and trigger.type == "webhook":
                _advance_webhook_trigger(db, trigger, reply_row.content if reply_row else None)
    if reply_row is None:
        return
    if (reply_row.message_meta or {}).get("trigger_completion_written"):
        return
    triggers = completion.get("triggers") or []
    delivery_ids = []
    if completion.get("origin"):
        # The canonical assistant is already in the subscribed origin Session.
        origin = await db.get(ChatSession, uuid.UUID(anchor.conversation_id))
        if origin is not None:
            reply_row.message_meta = attach_delivery_to_meta(
                reply_row.message_meta, IMDeliveryResult.pending(str(origin.source_channel or "web")),
            )
            if origin.source_channel == "agent":
                participant = (await db.execute(select(Participant).where(
                    Participant.type == "agent", Participant.ref_id == agent_id,
                ))).scalar_one_or_none()
                reply_row.participant_id = participant.id if participant else None
                reply_row.message_meta = {
                    **reply_row.message_meta, "direction": "inbound", "source_channel": "agent",
                    "actor_ref": str(participant.id if participant else agent_id),
                }
                await db.flush()
                from app.services.trigger_runtime.evaluator import match_incoming_chat_message

                await match_incoming_chat_message(db, reply_row, origin)
            delivery_ids.append(str(reply_row.id))
    elif reply_row.content:
        for trigger in triggers:
            cfg = trigger.get("config") or {}
            target_id = cfg.get("_a2a_session_id") or cfg.get("_origin_session_id")
            if not target_id:
                continue
            target = await db.get(ChatSession, uuid.UUID(str(target_id)))
            if target is None or agent_id not in {target.agent_id, target.peer_agent_id}:
                continue
            a2a = bool(cfg.get("_a2a_session_id"))
            if not a2a:
                if target.agent_id != agent_id or await _sent_by_tool(db, anchor):
                    continue
                if cfg.get("_origin_source_channel") and target.source_channel != cfg["_origin_source_channel"]:
                    continue
                if "_origin_external_conv_id" in cfg and target.external_conv_id != cfg["_origin_external_conv_id"]:
                    continue
            summary = str(cfg.get("_notification_summary") or trigger.get("reason") or trigger.get("name") or "").strip()
            content = reply_row.content if a2a else f"⚡ {summary[:80]}\n\n{reply_row.content}"
            projected = await _project_reply(db, anchor, reply_row, agent_id=agent_id,
                                             target=target, content=content, a2a=a2a)
            delivery_ids.append(str(projected.id))
            break
    reply_row.message_meta = {
        **(reply_row.message_meta or {}),
        "trigger_completion_written": True,
        "trigger_delivery_ids": delivery_ids,
    }
    db.add(AuditLog(agent_id=agent_id, action="trigger_fired", details={
        "turn_anchor_id": str(anchor.id),
        "triggers": [{"name": item.get("name"), "type": item.get("type")} for item in triggers],
    }))


async def deliver_trigger_turn(anchor: ChatMessage, reply_row: ChatMessage | None) -> bool:
    """Publish background summaries on the platform; preserve actual continuations."""
    from app.api.websocket import manager as ws_manager
    from app.services.turn_runtime import deliver_recovered_reply_to_origin

    if reply_row is None:
        return True
    execution = _execution(anchor)
    completion = execution.get("completion") or {}
    triggers = completion.get("triggers") or []
    cfg = triggers[0].get("config") or {} if triggers else {}
    platform_summary = not completion.get("origin") and not cfg.get("_a2a_session_id")
    for row_id in (reply_row.message_meta or {}).get("trigger_delivery_ids", []):
        async with async_session() as db:
            row = await db.get(ChatMessage, uuid.UUID(str(row_id)))
            if row is None:
                return False
            if platform_summary:
                target = await db.get(ChatSession, uuid.UUID(row.conversation_id))
                if target is None or target.agent_id != anchor.agent_id:
                    continue
                owner_user_id = target.user_id
            else:
                owner_user_id = None
            receipt = dict((row.message_meta or {}).get("delivery") or {})
            if not platform_summary:
                if receipt.get("status") in {"sent", "unsupported"}:
                    continue
                if receipt.get("status") != "pending":
                    return False
            conversation_id, content = row.conversation_id, row.content
            storage_agent_id = row.agent_id
        if platform_summary:
            # This also handles pre-fix summaries with pending IM receipts:
            # reconciliation must never turn a platform summary into an IM send.
            if owner_user_id:
                await ws_manager.send_to_user(str(storage_agent_id), str(owner_user_id), {
                    "type": "trigger_notification", "content": content,
                    "triggers": [item.get("name") for item in triggers],
                    "session_id": conversation_id,
                })
            continue
        delivered = await deliver_recovered_reply_to_origin(
            agent_id=storage_agent_id,
            conversation_id=conversation_id,
            reply=content,
            message_id=row_id,
            require_transport=True,
            origin_actor_ref=str(cfg.get("_origin_actor_ref") or "") or None,
            origin_actor_ref_type=str(cfg.get("_origin_actor_ref_type") or "") or None,
            expected_source_channel=str(cfg.get("_origin_source_channel") or "") or None,
            expected_external_conv_id=cfg.get("_origin_external_conv_id"),
            validate_external_conv_id="_origin_external_conv_id" in cfg,
        )
        if not delivered:
            return False
    return True
