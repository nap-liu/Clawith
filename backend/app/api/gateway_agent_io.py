"""Gateway polling, reporting, and heartbeat routes."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.gateway_support import _get_agent_by_key
from app.core.permissions import evaluate_agent_relationship_status
from app.database import get_db
from app.models.agent import Agent
from app.models.gateway_message import GatewayMessage
from app.models.user import User
from app.schemas.schemas import (
    GatewayHistoryItem,
    GatewayMessageOut,
    GatewayPollResponse,
    GatewayRelationshipItem,
    GatewayReportRequest,
)

router = APIRouter()

# ─── Poll for messages ──────────────────────────────────


@router.get("/poll", response_model=GatewayPollResponse)
async def poll_messages(
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent polls for pending messages.

    Returns all pending messages and marks them as delivered.
    Also updates openclaw_last_seen for online status tracking.
    """
    logger.info(f"[Gateway] poll called, key_prefix={x_api_key[:8]}...")
    agent = await _get_agent_by_key(x_api_key, db)

    # Update last seen
    agent.openclaw_last_seen = datetime.now(timezone.utc)
    agent.status = "running"

    # Fetch pending messages
    result = await db.execute(
        select(GatewayMessage)
        .where(GatewayMessage.agent_id == agent.id, GatewayMessage.status == "pending")
        .order_by(GatewayMessage.created_at.asc())
        .with_for_update(skip_locked=True)
    )
    messages = result.scalars().all()

    # Mark as delivered
    now = datetime.now(timezone.utc)
    out = []
    for msg in messages:
        msg.status = "delivered"
        msg.delivered_at = now

        # Resolve sender names
        sender_agent_name = None
        sender_user_name = None
        if msg.sender_agent_id:
            r = await db.execute(select(Agent.name).where(Agent.id == msg.sender_agent_id))
            sender_agent_name = r.scalar_one_or_none()
        if msg.sender_user_id:
            r = await db.execute(select(User.display_name).where(User.id == msg.sender_user_id))
            sender_user_name = r.scalar_one_or_none()

        # Fetch conversation history (last 10 messages) for context
        history = []
        if msg.conversation_id:
            from app.models.audit import ChatMessage

            hist_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == msg.conversation_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(10)
            )
            hist_msgs = list(reversed(hist_result.scalars().all()))
            for h in hist_msgs:
                # Resolve sender name for each history message
                h_sender = None
                if getattr(h, "sender_user_id", None):
                    r = await db.execute(select(User.display_name).where(User.id == h.sender_user_id))
                    h_sender = r.scalar_one_or_none()
                elif getattr(h, "sender_agent_id", None):
                    r = await db.execute(select(Agent.name).where(Agent.id == h.sender_agent_id))
                    h_sender = r.scalar_one_or_none()
                history.append(
                    GatewayHistoryItem(
                        role=h.role,
                        content=h.content or "",
                        sender_name=h_sender,
                        sender_user_id=getattr(h, "sender_user_id", None),
                        sender_agent_id=getattr(h, "sender_agent_id", None),
                        created_at=h.created_at,
                    )
                )

        out.append(
            GatewayMessageOut(
                id=msg.id,
                conversation_id=msg.conversation_id,
                sender_agent_name=sender_agent_name,
                sender_agent_id=msg.sender_agent_id,
                sender_user_name=sender_user_name,
                sender_user_id=str(msg.sender_user_id) if msg.sender_user_id else None,
                content=msg.content,
                created_at=msg.created_at,
                history=history,
            )
        )

    # Fetch agent relationships for context
    from sqlalchemy.orm import selectinload

    from app.models.org import AgentAgentRelationship, AgentRelationship
    from app.services.recipient_resolver import load_human_recipient_profiles

    rel_items = []
    human_items: dict[uuid.UUID, GatewayRelationshipItem] = {}

    # Human relationships (with available channels)
    h_result = await db.execute(select(AgentRelationship).where(AgentRelationship.agent_id == agent.id))
    human_relationships = list(h_result.scalars().all())
    profiles = await load_human_recipient_profiles(db, agent, human_relationships)
    for r in human_relationships:
        profile = profiles.get(r.user_id)
        if profile and profile.access_status == "active":
            existing = human_items.get(r.user_id)
            channels = list(profile.channels)
            if existing:
                existing.channels = sorted(set(existing.channels + channels))
            else:
                human_items[r.user_id] = GatewayRelationshipItem(
                    display_name=profile.user.display_name,
                    user_id=r.user_id,
                    role=r.relation,
                    description=r.description or None,
                    channels=channels,
                )

    rel_items.extend(human_items.values())

    # Agent-to-agent relationships
    a_result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    for r in a_result.scalars().all():
        status_info = await evaluate_agent_relationship_status(db, r)
        if r.target_agent and status_info["access_status"] == "active":
            rel_items.append(
                GatewayRelationshipItem(
                    display_name=r.target_agent.name,
                    agent_id=r.target_agent.id,
                    role=r.relation,
                    description=r.description or None,
                    channels=["agent"],
                )
            )

    await db.commit()
    return GatewayPollResponse(messages=out, relationships=rel_items)


# ─── Report results ─────────────────────────────────────


@router.post("/report")
async def report_result(
    body: GatewayReportRequest,
    x_api_key: str = Header(None, alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent reports the result of a processed message."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    logger.info(f"[Gateway] report called, key_prefix={x_api_key[:8]}..., msg_id={body.message_id}")
    agent = await _get_agent_by_key(x_api_key, db)

    result = await db.execute(
        select(GatewayMessage)
        .where(
            GatewayMessage.id == body.message_id,
            GatewayMessage.agent_id == agent.id,
        )
        .with_for_update()
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    first_completion = msg.status != "completed"
    msg.status = "completed"
    msg.result = body.result
    msg.completed_at = datetime.now(timezone.utc)

    # Update last seen
    agent.openclaw_last_seen = datetime.now(timezone.utc)

    # Save result as assistant chat message and push via WebSocket
    # (works for both user-originated and agent-to-agent messages)
    turn_snapshot = None
    terminal_message_id = None
    if body.result and msg.conversation_id:
        from app.models.audit import ChatMessage
        from app.models.chat_session import ChatSession
        from app.models.participant import Participant

        # Look up OpenClaw agent's participant_id
        part_r = await db.execute(
            select(Participant).where(Participant.type == "agent", Participant.ref_id == agent.id)
        )
        participant = part_r.scalar_one_or_none()

        turn_anchor = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent.id,
                    ChatMessage.conversation_id == msg.conversation_id,
                    ChatMessage.role.in_(("user", "system")),
                    ChatMessage.message_meta["gateway_message_id"].as_string() == str(msg.id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        # A gateway client may retry /report. Persist one stable reply row so
        # the same remote event cannot enqueue a second on_message execution.
        report_event_key = f"gateway-report:{msg.id}"
        assistant_msg = (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == report_event_key))
        ).scalar_one_or_none()
        if assistant_msg is None:
            assistant_msg = ChatMessage(
                agent_id=agent.id,
                user_id=msg.sender_user_id or getattr(agent, "creator_id", agent.id),
                sender_agent_id=agent.id,
                role="assistant",
                content=body.result,
                conversation_id=msg.conversation_id,
                participant_id=participant.id if participant else None,
                external_event_key=report_event_key,
                message_meta={
                    "direction": "inbound",
                    "source_channel": "agent",
                    "actor_ref": str(participant.id if participant else agent.id),
                    **(
                        {
                            "turn_anchor_id": str(turn_anchor.id),
                            "turn_status": "completed",
                        }
                        if turn_anchor is not None
                        else {}
                    ),
                },
            )
            db.add(assistant_msg)
            await db.flush()
            try:
                report_session = await db.get(ChatSession, uuid.UUID(str(msg.conversation_id)))
            except (TypeError, ValueError):
                report_session = None
            if report_session is not None:
                from app.services.trigger_runtime.evaluator import (
                    match_incoming_chat_message,
                )

                await match_incoming_chat_message(db, assistant_msg, report_session)
        terminal_message_id = assistant_msg.id
        if turn_anchor is not None:
            from app.services.conversation_turn_lifecycle import transition_conversation_turn

            turn_snapshot = await transition_conversation_turn(
                db,
                agent_id=agent.id,
                conversation_id=msg.conversation_id,
                turn_anchor_id=turn_anchor.id,
                status="completed",
            )

    # Route an A2A reply in the same transaction as completion and exact
    # on_message matching. Concurrent/retried reports therefore create neither
    # a duplicate wake nor a duplicate gateway reply.
    if body.result and msg.sender_agent_id and first_completion:
        db.add(
            GatewayMessage(
                agent_id=msg.sender_agent_id,
                sender_agent_id=agent.id,
                content=body.result,
                status="pending",
                conversation_id=msg.conversation_id or f"gw_agent_{msg.sender_agent_id}_{agent.id}",
            )
        )

    await db.commit()

    # Publish only after the reply row and terminal lifecycle committed.
    if body.result and msg.conversation_id and turn_snapshot is not None:
        from app.services.conversation_turn_lifecycle import publish_conversation_turn_event

        await publish_conversation_turn_event(
            agent_id=agent.id,
            conversation_id=msg.conversation_id,
            payload={
                "type": "done",
                "role": "assistant",
                "content": body.result,
                "message_id": str(terminal_message_id) if terminal_message_id else None,
            },
            snapshot=turn_snapshot,
            event_kind="turn_terminal",
        )

    if body.result and msg.sender_agent_id and first_completion:
        logger.info(f"[Gateway] Reply routed back to sender agent {msg.sender_agent_id}")

    return {"status": "ok"}


# ─── Heartbeat ──────────────────────────────────────────


@router.post("/heartbeat")
async def heartbeat(
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Pure heartbeat ping — keeps the OpenClaw agent marked as online."""
    agent = await _get_agent_by_key(x_api_key, db)
    agent.openclaw_last_seen = datetime.now(timezone.utc)
    agent.status = "running"
    await db.commit()
    return {"status": "ok", "agent_id": str(agent.id)}
