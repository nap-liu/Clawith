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
from app.models.agent import Agent
from app.models.gateway_message import GatewayMessage, GatewaySendReceipt
from app.models.user import User
from app.schemas.schemas import (
    GatewayHistoryItem,
    GatewayMessageOut,
    GatewayPollResponse,
    GatewayRelationshipItem,
    GatewayReportRequest,
    GatewaySendMessageRequest,
)
from app.services.workload_capacity import (
    WorkloadKind,
    WorkloadLease,
    WorkloadOverloadedError,
    get_workload_capacity,
)

router = APIRouter(prefix="/gateway", tags=["gateway"])
from app.api.gateway_support import _get_agent_by_key, _hash_key
from app.api import gateway_agent_io as _agent_io
from app.api.gateway_agent_io import (
    heartbeat,
    poll_messages,
    report_result,
    router as agent_io_router,
)

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


async def _gateway_native_tenant_id(target_agent_id: str) -> uuid.UUID:
    """Resolve one durable tenant key without retaining a database session."""

    target_id = uuid.UUID(str(target_agent_id))
    async with async_session() as db:
        tenant_id = await db.scalar(select(Agent.tenant_id).where(Agent.id == target_id))
    return tenant_id or target_id


async def _run_gateway_native_turn_with_lease(
    lease: WorkloadLease,
    *args,
) -> None:
    """Keep an already-admitted permit for exactly one native Gateway turn."""

    recovery_anchor = None
    try:
        recovery_anchor = await _send_to_agent_background(*args)
    finally:
        await lease.release()
    if recovery_anchor is not None:
        _schedule_gateway_turn_recovery(recovery_anchor)


async def _recover_gateway_turn(anchor) -> None:
    """Retry an accepted durable Gateway turn after its capacity lease exits."""
    from app.services.redis_lease_lock import RedisLeaseError
    from app.services.turn_recovery import resume_turn

    for delay in (0, 0.25, 1, 4, 10):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await resume_turn(anchor):
                return
        except asyncio.CancelledError:
            raise
        except RedisLeaseError as exc:
            logger.warning(
                "[Gateway] durable turn lease retry anchor={} delay={}: {}",
                anchor.id,
                delay,
                exc,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "[Gateway] durable turn retry failed anchor={} delay={}",
                anchor.id,
                delay,
            )
    logger.error(
        "[Gateway] durable turn remains recoverable after local retries anchor={}",
        anchor.id,
    )


def _schedule_gateway_turn_recovery(anchor) -> None:
    task = asyncio.create_task(
        _recover_gateway_turn(anchor),
        name=f"gateway-turn-recovery:{anchor.id}",
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _send_to_agent_background(
    source_agent_id: str,
    source_agent_name: str,
    target_agent_id: str,
    target_agent_name: str,
    target_primary_model_id: str,
    target_role_description: str,
    target_creator_id: str,
    content: str,
    source_event_id: str,
):
    """Background task: invoke target agent LLM and write reply to gateway_messages.

    Accepts plain values (not ORM objects) to avoid stale session references
    since this runs after the request's DB session has closed.
    """
    logger.info(f"[Gateway] _send_to_agent_background started: {source_agent_name} -> {target_agent_name}")
    recovery_anchor = None
    try:
        from app.models.audit import ChatMessage
        from app.models.chat_session import ChatSession
        from app.models.llm import LLMModel
        from app.models.participant import Participant
        from app.services.llm import call_llm

        async with async_session() as db:
            # Load target agent's LLM model
            if not target_primary_model_id:
                logger.warning(f"Target agent {target_agent_name} has no LLM model")
                return
            result = await db.execute(select(LLMModel).where(LLMModel.id == target_primary_model_id))
            model = result.scalar_one_or_none()
            if not model:
                return
            # Skip if model is disabled by admin
            if not model.enabled:
                logger.warning(f"Target agent {target_agent_name}'s model {model.model} is disabled, skipping")
                return

            # Create or find a ChatSession for this agent pair
            # Use deterministic UUID so the same pair always gets the same session
            import uuid as _uuid

            _ns = _uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
            # Sort IDs so session is the same regardless of who initiates
            session_agent_id = min(source_agent_id, target_agent_id, key=str)
            session_peer_id = max(source_agent_id, target_agent_id, key=str)
            session_uuid = _uuid.uuid5(_ns, f"{session_agent_id}_{session_peer_id}")
            conv_id = str(session_uuid)

            # Find or create the ChatSession
            existing = await db.execute(select(ChatSession).where(ChatSession.id == session_uuid))
            session = existing.scalar_one_or_none()
            if not session:
                from datetime import datetime, timezone

                await db.execute(
                    pg_insert(ChatSession)
                    .values(
                        id=session_uuid,
                        agent_id=uuid.UUID(str(session_agent_id)),
                        title=f"{source_agent_name} ↔ {target_agent_name}",
                        source_channel="agent",
                        peer_agent_id=uuid.UUID(str(session_peer_id)),
                        created_at=datetime.now(timezone.utc),
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                await db.commit()
                session = await db.get(ChatSession, session_uuid)
                if session is None:
                    raise RuntimeError("gateway A2A session was not persisted")

                # Migrate any existing messages from old gw_agent_ format
                old_conv_id = f"gw_agent_{source_agent_id}_{target_agent_id}"
                from sqlalchemy import update

                await db.execute(
                    update(ChatMessage)
                    .where(ChatMessage.conversation_id == old_conv_id)
                    .values(conversation_id=conv_id)
                )
                await db.commit()

            expected_pair = {
                uuid.UUID(str(source_agent_id)),
                uuid.UUID(str(target_agent_id)),
            }
            session_pair = {session.agent_id, session.peer_agent_id}
            tenant_rows = list(
                (
                    await db.execute(
                        select(Agent.id, Agent.tenant_id).where(
                            Agent.id.in_(expected_pair)
                        )
                    )
                ).all()
            )
            if (
                session.source_channel != "agent"
                or session_pair != expected_pair
                or len(tenant_rows) != 2
                or len({tenant_id for _agent_id, tenant_id in tenant_rows}) != 1
            ):
                raise RuntimeError("gateway A2A session identity mismatch")

            storage_agent_id = uuid.UUID(str(session.agent_id))
            execution_agent_id = uuid.UUID(str(target_agent_id))
            execution_user_id = uuid.UUID(str(target_creator_id))
            gateway_reply_id = _uuid.uuid5(
                _ns,
                f"gateway-direct-reply:{source_event_id}",
            )

            # Update last_message_at
            from datetime import datetime, timezone

            session.last_message_at = datetime.now(timezone.utc)

            # Agent-to-agent communication context (injected as prefix to user message
            # since call_llm builds the full system prompt internally)
            agent_comm_alert = (
                "--- Agent-to-Agent Communication Alert ---\n"
                f"You are receiving a direct message from another digital employee ({source_agent_name}). "
                "CRITICAL INSTRUCTION: Your direct text reply will automatically be delivered back to them. "
                "DO NOT use the `send_message_to_agent` tool to reply to this conversation. Just reply naturally in text.\n"
                "If they are asking you to create or analyze a file, deliver the file using `send_file_to_agent` after writing it."
            )

            # Load recent conversation history for context
            hist_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(10)
            )
            hist_msgs = list(reversed(hist_result.scalars().all()))

            from app.services.llm.utils import convert_chat_messages_to_llm_format as _conv

            messages = _conv(hist_msgs)

            # Add the new message with agent communication context
            user_msg = f"{agent_comm_alert}\n\n[Message from agent: {source_agent_name}]\n{content}"
            messages.append({"role": "user", "content": user_msg})

            # Lookup participants for both agents
            src_part_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == source_agent_id)
            )
            tgt_part_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == target_agent_id)
            )
            src_participant = src_part_r.scalar_one_or_none()
            tgt_participant = tgt_part_r.scalar_one_or_none()

            from app.services.chat_history import ingest_incoming_chat_message

            ingested = await ingest_incoming_chat_message(
                db,
                session=session,
                agent_id=storage_agent_id,
                user_id=execution_user_id,
                content=user_msg,
                source_channel="agent",
                provider_event_id=source_event_id,
                channel_config_id="gateway-direct",
                actor_ref=str(src_participant.id if src_participant else source_agent_id),
                participant_id=src_participant.id if src_participant else None,
                message_meta={
                    "execution_agent_id": str(execution_agent_id),
                    "gateway_direct_reply": {
                        "message_id": str(gateway_reply_id),
                        "agent_id": str(source_agent_id),
                        "sender_agent_id": str(execution_agent_id),
                    },
                },
            )
            ingested.message.sender_user_id = None
            ingested.message.sender_agent_id = uuid.UUID(str(source_agent_id))
            await db.commit()
            recovery_anchor = ingested.message

            if ingested.consumed_by_onmessage:
                logger.info("[Gateway] Direct A2A event %s routed to on_message", source_event_id)
                return

        # Call LLM
        collected = []

        async def on_chunk(text):
            collected.append(text)

        from app.services.subagent_runtime import build_parent_subagent_before_round

        before_round = build_parent_subagent_before_round(
            parent_session_id=conv_id,
            active_turn_anchor_id=ingested.message.id,
            execution_agent_id=execution_agent_id,
            execution_user_id=execution_user_id,
            turn_anchor_agent_id=storage_agent_id,
            include_turn_inbox=True,
        )

        reply = await call_llm(
            model=model,
            messages=messages,
            agent_name=target_agent_name,
            role_description=target_role_description,
            agent_id=target_agent_id,
            user_id=target_creator_id,
            session_id=conv_id,
            on_chunk=on_chunk,
            turn_anchor_id=ingested.message.id,
            turn_anchor_agent_id=storage_agent_id,
            turn_type="gateway",
            before_round=before_round,
        )
        final_reply = reply or "".join(collected)

        # Save assistant reply to conversation
        async with async_session() as db:
            from app.models.participant import Participant
            from app.services.chat_history import persist_assistant_reply_row

            tgt_part_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == target_agent_id)
            )
            tgt_participant = tgt_part_r.scalar_one_or_none()
            assistant_message_id = await persist_assistant_reply_row(
                db,
                agent_id=storage_agent_id,
                user_id=execution_user_id,
                conversation_id=conv_id,
                content=final_reply,
                turn_anchor_id=ingested.message.id,
                sender_agent_id=execution_agent_id,
                message_id=gateway_reply_id,
                message_meta={
                    "direction": "inbound",
                    "source_channel": "agent",
                    "actor_ref": str(
                        tgt_participant.id if tgt_participant else target_agent_id
                    ),
                },
            )
            reply_row = await db.get(ChatMessage, assistant_message_id)
            if reply_row is None:
                raise RuntimeError("gateway direct reply row was not persisted")
            reply_row.participant_id = (
                tgt_participant.id if tgt_participant else None
            )
            from app.services.trigger_runtime.evaluator import match_incoming_chat_message

            await match_incoming_chat_message(db, reply_row, session)

            # Write reply to gateway_messages for source (OpenClaw) to poll
            await db.execute(
                pg_insert(GatewayMessage)
                .values(
                    id=gateway_reply_id,
                    agent_id=uuid.UUID(str(source_agent_id)),
                    sender_agent_id=execution_agent_id,
                    content=final_reply,
                    status="pending",
                    conversation_id=conv_id,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await db.commit()

        from app.services.conversation_turn_lifecycle import (
            publish_committed_turn_terminal,
        )

        await publish_committed_turn_terminal(
            agent_id=storage_agent_id,
            conversation_id=conv_id,
            turn_anchor_id=ingested.message.id,
            message_id=assistant_message_id,
            content=final_reply,
        )

        logger.info(f"[Gateway] Agent {target_agent_name} replied to {source_agent_name}")

    except Exception as e:
        logger.error(f"[Gateway] send_to_agent_background failed: {e}")
        import traceback

        traceback.print_exc()
        if recovery_anchor is not None:
            return recovery_anchor
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
            tenant_id = await _gateway_native_tenant_id(_tgt_id)
            try:
                capacity_lease = await get_workload_capacity().acquire(
                    WorkloadKind.PROJECT,
                    tenant_id,
                )
            except WorkloadOverloadedError as exc:
                detail = {
                    "code": "gateway_capacity_busy",
                    "retryable": True,
                    "message": "Gateway agent capacity is busy. Please retry shortly.",
                }
                await _complete_gateway_send_error(db, receipt_id, 503, detail)
                raise HTTPException(
                    status_code=503,
                    detail=detail,
                    headers={"Retry-After": str(max(1, round(exc.timeout_seconds)))},
                ) from exc
            source_event_id = f"{agent.id}:{x_idempotency_key}"[:500] if x_idempotency_key else str(uuid.uuid4())
            task = asyncio.create_task(
                _run_gateway_native_turn_with_lease(
                    capacity_lease,
                    _src_id,
                    _src_name,
                    _tgt_id,
                    _tgt_name,
                    _tgt_model,
                    _tgt_role,
                    _tgt_creator,
                    content,
                    source_event_id,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            response = {
                "status": "accepted",
                "agent_id": str(target_agent.id),
                "display_name": target_agent.name,
                "type": "agent",
                "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
            }
            return await _complete_gateway_send(db, receipt_id, response)

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
