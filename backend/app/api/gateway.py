"""Gateway API for OpenClaw agent communication.

OpenClaw agents authenticate via X-Api-Key header and use these endpoints
to poll for messages, report results, send messages, and send heartbeat pings.
"""

import asyncio
import hashlib
import json
import sys
import types
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import evaluate_agent_relationship_status
from app.database import async_session, get_db
from app.models.gateway_message import GatewayMessage, GatewaySendReceipt
from app.models.user import User
from app.schemas.schemas import GatewaySendMessageRequest

from app.api.gateway_support import _get_agent_by_key
from app.api import gateway_agent_io as _agent_io
from app.api.gateway_agent_io import (
    heartbeat,
    poll_messages,
    report_result,
    router as agent_io_router,
)

router = APIRouter(prefix="/gateway", tags=["gateway"])

def _gateway_send_request_hash(body: GatewaySendMessageRequest) -> str:
    payload = {
        "user_id": str(body.user_id) if body.user_id else None,
        "agent_id": str(body.agent_id) if body.agent_id else None,
        "content": body.content.strip(),
        "channel": (body.channel or "").strip().lower() or None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _claim_gateway_send(
    db: AsyncSession,
    source_agent_id: uuid.UUID,
    body: GatewaySendMessageRequest,
    idempotency_key: str | None,
) -> tuple[uuid.UUID | None, dict | None]:
    key = (idempotency_key or "").strip()
    if not key:
        return None, None
    if len(key) > 200:
        raise HTTPException(status_code=422, detail="X-Idempotency-Key exceeds 200 characters")

    request_hash = _gateway_send_request_hash(body)
    receipt_id = uuid.uuid4()
    await db.execute(
        pg_insert(GatewaySendReceipt)
        .values(
            id=receipt_id,
            source_agent_id=source_agent_id,
            idempotency_key=key,
            request_hash=request_hash,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["source_agent_id", "idempotency_key"])
    )
    # Persist the claim before any external side effect. A crash can therefore
    # leave an explicit in-progress/unknown receipt, but can never silently
    # resend the same key and duplicate the operation.
    await db.commit()
    receipt = (
        await db.execute(
            select(GatewaySendReceipt).where(
                GatewaySendReceipt.source_agent_id == source_agent_id,
                GatewaySendReceipt.idempotency_key == key,
            )
        )
    ).scalar_one()
    if receipt.request_hash != request_hash:
        raise HTTPException(
            status_code=409,
            detail="X-Idempotency-Key was already used with a different target or payload",
        )
    if receipt.id != receipt_id:
        if receipt.status == "completed" and receipt.response_payload is not None:
            replay = dict(receipt.response_payload)
            error_status = replay.pop("__http_status", None)
            if error_status:
                raise HTTPException(status_code=int(error_status), detail=replay.get("detail"))
            return None, replay
        raise HTTPException(
            status_code=409,
            detail="The idempotent send is still in progress or its outcome is unknown",
        )
    return receipt.id, None


async def _complete_gateway_send(
    db: AsyncSession,
    receipt_id: uuid.UUID | None,
    response: dict,
) -> dict:
    if receipt_id is not None:
        receipt = await db.get(GatewaySendReceipt, receipt_id)
        if receipt is None:
            raise HTTPException(status_code=500, detail="Gateway idempotency receipt was lost")
        receipt.status = "completed"
        receipt.response_payload = response
        receipt.completed_at = datetime.now(timezone.utc)
    await db.commit()
    return response


async def _complete_gateway_send_error(
    db: AsyncSession,
    receipt_id: uuid.UUID | None,
    status_code: int,
    detail,
) -> None:
    await _complete_gateway_send(
        db,
        receipt_id,
        {"__http_status": status_code, "detail": detail},
    )

for _gateway_endpoint in (poll_messages, report_result, heartbeat):
    _gateway_endpoint.__module__ = __name__

router.include_router(agent_io_router)


class _GatewayApiFacadeModule(types.ModuleType):
    """Keep historical root-module monkeypatch targets effective."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if hasattr(_agent_io, name):
            setattr(_agent_io, name, value)


sys.modules[__name__].__class__ = _GatewayApiFacadeModule

# ─── Send message ───────────────────────────────────────

# Track background tasks to prevent garbage collection
_background_tasks: set = set()


async def _send_to_agent_background(
    source_agent_id: str,
    source_agent_name: str,
    target_agent_id: str,
    target_agent_name: str,
    _target_primary_model_id: str,
    target_role_description: str,
    target_creator_id: str,
    content: str,
    source_event_id: str,
    admitted_anchor=None,
):
    """Execute the already accepted native Turn through shared recovery."""
    from app.api.gateway_native_turn import admit_native_gateway_turn
    from app.services.turn_recovery_startup import resume_startup_anchor
    from app.services.turn_interruption import TurnInterrupted

    anchor = admitted_anchor
    if anchor is None:
        async with async_session() as db:
            anchor = await admit_native_gateway_turn(
                db, source_agent_id, source_agent_name, target_agent_id,
                target_agent_name, target_creator_id, content, source_event_id,
            )
            await db.commit()
    try:
        await resume_startup_anchor(anchor)
    except TurnInterrupted:
        logger.info("[Gateway] Native execution interrupted; anchor={} remains recoverable", anchor.id)
    except Exception:
        logger.opt(exception=True).warning("[Gateway] Native execution deferred anchor={}", anchor.id)
        return anchor
    return None


@router.post("/send-message")
async def send_message(
    body: GatewaySendMessageRequest,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Send by exactly one canonical user_id or agent_id."""
    agent = await _get_agent_by_key(x_api_key, db)
    agent.openclaw_last_seen = datetime.now(timezone.utc)

    content = body.content.strip()
    channel_hint = (body.channel or "").strip().lower()

    # 1. Try to find target as another Agent, limited to active relationships.
    from sqlalchemy.orm import selectinload

    from app.models.org import AgentAgentRelationship

    target_agent = None
    if body.agent_id:
        rel_result = await db.execute(
            select(AgentAgentRelationship)
            .where(
                AgentAgentRelationship.agent_id == agent.id,
                AgentAgentRelationship.target_agent_id == body.agent_id,
            )
            .options(selectinload(AgentAgentRelationship.target_agent))
        )
        rel = rel_result.scalar_one_or_none()
        if rel:
            status_info = await evaluate_agent_relationship_status(db, rel)
            if status_info["access_status"] == "active":
                target_agent = rel.target_agent
        if not target_agent:
            raise HTTPException(
                status_code=404,
                detail="agent_id is not an active related digital employee",
            )

    logger.info(
        "[Gateway] send_message: user_id=%s agent_id=%s channel=%s",
        body.user_id,
        body.agent_id,
        channel_hint,
    )

    receipt_id = None
    if target_agent:
        receipt_id, replay = await _claim_gateway_send(db, agent.id, body, x_idempotency_key)
        if replay is not None:
            return replay
        conv_id = f"gw_agent_{agent.id}_{target_agent.id}"

        if getattr(target_agent, "agent_type", None) == "openclaw":
            # OpenClaw-to-OpenClaw: write to gateway_messages directly
            gw_msg = GatewayMessage(
                agent_id=target_agent.id,
                sender_agent_id=agent.id,
                content=content,
                status="pending",
                conversation_id=conv_id,
            )
            db.add(gw_msg)
            response = {
                "status": "accepted",
                "agent_id": str(target_agent.id),
                "display_name": target_agent.name,
                "type": "openclaw_agent",
                "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
            }
            return await _complete_gateway_send(db, receipt_id, response)
        else:
            # Native agent: async LLM processing
            # Extract plain values before session closes to avoid stale ORM references
            _src_id = str(agent.id)
            _src_name = agent.name
            _tgt_id = str(target_agent.id)
            _tgt_name = target_agent.name
            _tgt_model = str(target_agent.primary_model_id) if target_agent.primary_model_id else ""
            _tgt_role = target_agent.role_description or ""
            _tgt_creator = str(target_agent.creator_id) if target_agent.creator_id else ""
            await db.commit()
            source_event_id = f"{agent.id}:{x_idempotency_key}"[:500] if x_idempotency_key else str(uuid.uuid4())
            from app.api.gateway_native_turn import admit_native_gateway_turn

            admitted_anchor = await admit_native_gateway_turn(
                db, _src_id, _src_name, _tgt_id, _tgt_name,
                _tgt_creator, content, source_event_id,
            )
            response = {
                "status": "accepted", "agent_id": _tgt_id,
                "display_name": _tgt_name, "type": "agent",
                "message": f"Message sent to {_tgt_name}. Reply will appear in your next poll.",
            }
            await _complete_gateway_send(db, receipt_id, response)
            task = asyncio.create_task(
                _send_to_agent_background(
                    _src_id,
                    _src_name,
                    _tgt_id,
                    _tgt_name,
                    _tgt_model,
                    _tgt_role,
                    _tgt_creator,
                    content,
                    source_event_id,
                    admitted_anchor,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            return response

    if not body.user_id:
        raise HTTPException(status_code=422, detail="user_id or agent_id is required")

    user_result = await db.execute(select(User).where(User.id == body.user_id, User.tenant_id == agent.tenant_id))
    target_user = user_result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="user_id not found in agent tenant")
    receipt_id, replay = await _claim_gateway_send(db, agent.id, body, x_idempotency_key)
    if replay is not None:
        return replay

    from app.services.agent_tools import _send_channel_message, _send_platform_message

    if channel_hint in {"platform", "web"}:
        send_result = await _send_platform_message(agent.id, {"user_id": str(body.user_id), "message": content})
        selected_channel = "platform"
    else:
        send_args = {"user_id": str(body.user_id), "message": content}
        if channel_hint:
            send_args["channel"] = channel_hint
        send_result = await _send_channel_message(agent.id, send_args)
        selected_channel = channel_hint or None

    if send_result.startswith("❌"):
        await _complete_gateway_send_error(db, receipt_id, 400, send_result)
        raise HTTPException(status_code=400, detail=send_result)
    if send_result.startswith("{"):
        import json as _json

        try:
            structured = _json.loads(send_result)
        except ValueError:
            structured = None
        if isinstance(structured, dict) and structured.get("status") == "error":
            await _complete_gateway_send_error(db, receipt_id, 400, structured)
            raise HTTPException(status_code=400, detail=structured)
    response = {
        "status": "sent",
        "user_id": str(body.user_id),
        "display_name": target_user.display_name,
        "channel": selected_channel,
        "message": send_result,
    }
    return await _complete_gateway_send(db, receipt_id, response)


# ─── Setup guide ────────────────────────────────────────


@router.get("/setup-guide/{agent_id}")
async def get_setup_guide(
    agent_id: uuid.UUID,
    request: Request,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Return the pre-filled Skill file and Heartbeat instruction for this agent."""
    agent = await _get_agent_by_key(x_api_key, db)
    if agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Key does not match this agent")

    # Resolve base URL dynamically using tenant fallback chain
    from app.core.domain import resolve_base_url

    base_url = await resolve_base_url(db, request=request, tenant_id=str(agent.tenant_id))

    from app.config import get_settings as _gs

    platform_name = _gs().PLATFORM_NAME

    skill_content = f"""---
name: platform_sync
description: Sync with {platform_name} — check inbox, submit results, and send messages.
---

# Platform Sync

## When to use
Check for new messages from the {platform_name} platform during every heartbeat cycle.
You can also proactively send messages to people and agents in your relationships.

## Instructions

### 1. Check inbox
Make an HTTP GET request:
- URL: {base_url}/api/gateway/poll
- Header: X-Api-Key: {x_api_key}

The response contains a `messages` array. Each message includes:
- `id` — unique message ID (use this for reporting)
- `content` — the message text
- `sender_user_name` — name of the {platform_name} user who sent it
- `sender_user_id` — unique ID of the sender
- `sender_agent_id` — canonical digital-employee sender ID for A2A messages
- `conversation_id` — the conversation this message belongs to
- `history` — array of previous messages in this conversation for context

The response also contains a `relationships` array describing your colleagues:
- `display_name` — display-only person or agent name
- `user_id` or `agent_id` — exactly one canonical execution identifier
- `role` — relationship type (e.g. collaborator, supervisor)
- `channels` — available communication channels (e.g. ["feishu"], ["agent"])

**IMPORTANT**: Use the `history` array to understand conversation context before replying.
Different `sender_user_name` values mean different people — address them accordingly.

### 2. Report results
For each completed message, make an HTTP POST request:
- URL: {base_url}/api/gateway/report
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"message_id": "<id from the message>", "result": "<your response>"}}

### 3. Send a message to someone
To proactively contact a person or agent, make an HTTP POST request:
- URL: {base_url}/api/gateway/send-message
- Header: X-Api-Key: {x_api_key}
- Header: X-Idempotency-Key: <stable unique ID for this logical send; reuse the same key only when retrying it>
- Header: Content-Type: application/json
- Human body: {{"user_id": "<user_id>", "content": "<your message>", "channel": "<chosen route>"}}
- Digital employee body: {{"agent_id": "<agent_id>", "content": "<your message>"}}

Names are never execution locators. If a human has multiple valid channels, choose one from `relationships.channels`; the platform will not pick the first route. Always include X-Idempotency-Key and reuse it for retries of the same logical message. Agent replies appear in your next poll.
"""

    heartbeat_line = (
        f"- Check {platform_name} inbox using the platform_sync skill "
        "and process any pending messages"
    )

    return {
        "skill_filename": "platform_sync.md",
        "skill_content": skill_content,
        "heartbeat_addition": heartbeat_line,
    }
