"""Gateway API for OpenClaw agent communication.

OpenClaw agents authenticate via X-Api-Key header and use these endpoints
to poll for messages, report results, and send heartbeat pings.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.agent import Agent
from app.models.gateway_message import GatewayMessage
from app.models.user import User
from app.schemas.schemas import GatewayPollResponse, GatewayMessageOut, GatewayReportRequest, GatewayHistoryItem, GatewayRelationshipItem

router = APIRouter(prefix="/gateway", tags=["gateway"])


def _hash_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


async def _get_agent_by_key(api_key: str, db: AsyncSession) -> Agent:
    """Authenticate an OpenClaw agent by its API key."""
    key_hash = _hash_key(api_key)
    result = await db.execute(
        select(Agent).where(
            Agent.api_key_hash == key_hash,
            Agent.agent_type == "openclaw",
        )
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return agent


# ─── Generate / Regenerate API Key ──────────────────────

@router.post("/generate-key/{agent_id}")
async def generate_api_key(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    # JWT auth for this endpoint (requires the agent creator)
    current_user: "User" = Depends(None),  # placeholder, will use real dependency
):
    """Generate or regenerate an API key for an OpenClaw agent.

    Called from the frontend by the agent creator.
    """
    from app.api.agents import get_current_user
    raise HTTPException(status_code=501, detail="Use the /agents/{id}/api-key endpoint instead")


@router.post("/agents/{agent_id}/api-key")
async def generate_agent_api_key(agent_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Generate or regenerate API key for an OpenClaw agent.

    This is an internal endpoint called by the agents API.
    """
    result = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.agent_type == "openclaw"))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="OpenClaw agent not found")

    # Generate a new key
    raw_key = f"oc-{secrets.token_urlsafe(32)}"
    agent.api_key_hash = _hash_key(raw_key)
    await db.commit()

    return {"api_key": raw_key, "message": "Save this key — it won't be shown again."}


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
    print(f"[Gateway] poll called, key_prefix={x_api_key[:8]}...")
    agent = await _get_agent_by_key(x_api_key, db)

    # Update last seen
    agent.openclaw_last_seen = datetime.now(timezone.utc)
    agent.status = "running"

    # Fetch pending messages
    result = await db.execute(
        select(GatewayMessage)
        .where(GatewayMessage.agent_id == agent.id, GatewayMessage.status == "pending")
        .order_by(GatewayMessage.created_at.asc())
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
                if h.role == "user" and h.user_id:
                    r = await db.execute(select(User.display_name).where(User.id == h.user_id))
                    h_sender = r.scalar_one_or_none()
                elif h.role == "assistant":
                    h_sender = agent.name
                history.append(GatewayHistoryItem(
                    role=h.role,
                    content=h.content or "",
                    sender_name=h_sender,
                    created_at=h.created_at,
                ))

        out.append(GatewayMessageOut(
            id=msg.id,
            conversation_id=msg.conversation_id,
            sender_agent_name=sender_agent_name,
            sender_user_name=sender_user_name,
            sender_user_id=str(msg.sender_user_id) if msg.sender_user_id else None,
            content=msg.content,
            created_at=msg.created_at,
            history=history,
        ))

    # Fetch agent relationships for context
    from app.models.org import AgentRelationship, AgentAgentRelationship
    from sqlalchemy.orm import selectinload

    rel_items = []

    # Human relationships
    h_result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentRelationship.member))
    )
    for r in h_result.scalars().all():
        if r.member:
            rel_items.append(GatewayRelationshipItem(
                name=r.member.name,
                type="human",
                role=r.relation,
                description=r.description or None,
            ))

    # Agent-to-agent relationships
    a_result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    for r in a_result.scalars().all():
        if r.target_agent:
            rel_items.append(GatewayRelationshipItem(
                name=r.target_agent.name,
                type="agent",
                role=r.relation,
                description=r.description or None,
            ))

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
    print(f"[Gateway] report called, key_prefix={x_api_key[:8]}..., msg_id={body.message_id}")
    agent = await _get_agent_by_key(x_api_key, db)

    result = await db.execute(
        select(GatewayMessage).where(
            GatewayMessage.id == body.message_id,
            GatewayMessage.agent_id == agent.id,
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    msg.status = "completed"
    msg.result = body.result
    msg.completed_at = datetime.now(timezone.utc)

    # Update last seen
    agent.openclaw_last_seen = datetime.now(timezone.utc)

    # Save result as assistant chat message and push via WebSocket
    if body.result and msg.conversation_id:
        from app.models.audit import ChatMessage
        assistant_msg = ChatMessage(
            agent_id=agent.id,
            user_id=msg.sender_user_id,
            role="assistant",
            content=body.result,
            conversation_id=msg.conversation_id,
        )
        db.add(assistant_msg)

    await db.commit()

    # Push to WebSocket if user is connected
    if body.result and msg.conversation_id:
        try:
            from app.api.websocket import manager
            await manager.send_message(str(agent.id), {
                "type": "done",
                "role": "assistant",
                "content": body.result,
            })
        except Exception:
            pass  # User may have disconnected

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


# ─── Setup guide ────────────────────────────────────────

@router.get("/setup-guide/{agent_id}")
async def get_setup_guide(
    agent_id: uuid.UUID,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Return the pre-filled Skill file and Heartbeat instruction for this agent."""
    agent = await _get_agent_by_key(x_api_key, db)
    if agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Key does not match this agent")

    # Note: we use the raw key from the header since the agent already authenticated
    base_url = "https://try.clawith.ai"

    skill_content = f"""---
name: clawith_sync
description: Sync with Clawith platform — check inbox and submit results.
---

# Clawith Sync

## When to use
Check for new messages from the Clawith platform during every heartbeat cycle.

## Instructions

### 1. Check inbox
Make an HTTP GET request:
- URL: {base_url}/api/gateway/poll
- Header: X-Api-Key: {x_api_key}

The response contains a `messages` array. Each message includes:
- `id` — unique message ID (use this for reporting)
- `content` — the message text
- `sender_user_name` — name of the Clawith user who sent it
- `sender_user_id` — unique ID of the sender (to distinguish different users)
- `conversation_id` — the conversation this message belongs to
- `history` — array of previous messages in this conversation for context
  Each history item has: `role` (user/assistant), `content`, `sender_name`, `created_at`

The response also contains a `relationships` array describing your colleagues:
- `name` — the person or agent name
- `type` — "human" (person) or "agent" (AI agent)
- `role` — relationship type (e.g. collaborator, supervisor, mentor)
- `description` — additional context about the relationship

**IMPORTANT**: Use the `history` array to understand the conversation context before replying.
If the same user sent previous messages, your reply should be contextually aware.
Different `sender_user_name` values mean different people — address them accordingly.
Use your `relationships` to understand who you work with and how to interact with them.

### 2. Report results
For each completed message, make an HTTP POST request:
- URL: {base_url}/api/gateway/report
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"message_id": "<id from the message>", "result": "<your response>"}}
"""

    heartbeat_line = "- Check Clawith inbox using the clawith_sync skill and process any pending messages"

    return {
        "skill_filename": "clawith_sync.md",
        "skill_content": skill_content,
        "heartbeat_addition": heartbeat_line,
    }
