"""Adapt optional H5 launch context to the ordinary conversation lifecycle.

The caller owns the transaction and schedules a returned anchor only after commit.
"""

import json
import uuid
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.openapi_interaction import OpenAPIInteraction
from app.services.chat_history_ingest import ingest_incoming_chat_message
from app.services.conversation_turn_lifecycle import transition_conversation_turn
from app.services.gateway_message_queue import enqueue_user_gateway_message
from app.services.llm.failure_outcome import render_message
from app.services.openapi_applications import digest, fail, now
from app.services.quota_guard import (
    AgentExpired, QuotaExceeded, check_agent_expired, check_conversation_quota,
)
from app.services.scene_activation import resolve_session_scene
from app.services.scene_service import SCENE_STATUS_OK, resolve_scene_for_activation, scene_message_meta


async def _lock(db, key: str):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(key)[:15], 16)})


def _path(employee_id, session_id=None):
    path = f"/h5/agents/{employee_id}/chat"
    return f"{path}?session_id={session_id}" if session_id else path


async def prepare_interaction(db, application, user, employee_id, instance_ref, interaction: dict):
    """Prepare once; retries neither extend expiry nor start execution."""
    request_id = interaction["request_id"]
    payload = {"message": interaction["message"], "context": interaction.get("context")}
    fingerprint = digest(json.dumps(
        {"employee_id": str(employee_id), "instance_ref": instance_ref, **payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ))
    await _lock(db, f"openapi:request:{application.id}:{user.id}:{request_id}")
    record = await db.scalar(select(OpenAPIInteraction).where(
        OpenAPIInteraction.application_id == application.id,
        OpenAPIInteraction.user_id == user.id,
        OpenAPIInteraction.request_id == request_id,
    ).with_for_update())
    if record:
        if record.fingerprint != fingerprint:
            fail("interaction_conflict", 409)
        _require_available(record)
        return record
    record = OpenAPIInteraction(
        application_id=application.id, user_id=user.id, employee_id=employee_id,
        request_id=request_id, fingerprint=fingerprint, instance_ref=instance_ref,
        payload=payload, expires_at=now() + timedelta(minutes=10),
    )
    db.add(record)
    await db.flush()
    return record


def _require_available(record):
    if record.activated_at:
        if not record.session_id or not record.first_message_id:
            fail("interaction_unavailable", 410)
    elif record.expires_at <= now() or record.payload is None:
        fail("interaction_expired", 410)


async def cleanup_pending_interactions(db: AsyncSession):
    """Discard unused snapshots while retaining their durable replay tombstones."""
    await db.execute(update(OpenAPIInteraction).where(
        OpenAPIInteraction.activated_at.is_(None),
        OpenAPIInteraction.expires_at <= now(),
        OpenAPIInteraction.payload.is_not(None),
    ).values(payload=None))


async def _new_session(db, user, employee_id, title, external_conv_id=None):
    session = ChatSession(
        agent_id=employee_id, user_id=user.id, title=title[:200],
        source_channel="miniprogram", external_conv_id=external_conv_id,
        is_primary=False, is_group=False,
    )
    db.add(session)
    await db.flush()
    return session


async def _scene_metadata(db, employee_id, session):
    manifest = await resolve_session_scene(db, employee_id, session)
    if manifest is None:
        resolved = await resolve_scene_for_activation(db, employee_id, "default")
        if resolved.status == SCENE_STATUS_OK:
            manifest = resolved.manifest
    return {**scene_message_meta(manifest), "scene_resolved": True}


async def activate_launcher(db, application, user, launcher: dict) -> tuple[str, ChatMessage | None]:
    """Atomically select an instance session or activate exactly one new question."""
    try:
        employee_id = uuid.UUID(str(launcher["employee_id"]))
    except (KeyError, ValueError, TypeError):
        fail("invalid_interaction", 400)
    agent, _ = await check_agent_access(db, user, employee_id)
    if agent.scope != "standard" or agent.is_deleted:
        fail("employee_unavailable", 404)
    if agent.tenant_id != application.tenant_id or user.tenant_id != application.tenant_id:
        fail("access_denied", 403)
    instance_ref = launcher.get("instance_ref")
    interaction_id = launcher.get("interaction_id")
    record = None
    if interaction_id:
        try:
            interaction_uuid = uuid.UUID(str(interaction_id))
        except (TypeError, ValueError):
            fail("invalid_interaction", 400)
        record = await db.scalar(select(OpenAPIInteraction).where(
            OpenAPIInteraction.id == interaction_uuid,
            OpenAPIInteraction.application_id == application.id,
            OpenAPIInteraction.user_id == user.id,
            OpenAPIInteraction.employee_id == employee_id,
        ).with_for_update())
        if record is None or record.instance_ref != instance_ref:
            fail("access_denied", 403)
        _require_available(record)
        if record.activated_at:
            session = await db.get(ChatSession, record.session_id)
            if session is None or session.user_id != user.id or session.agent_id != employee_id:
                fail("interaction_unavailable", 410)
            return _path(employee_id, session.id), None
    if record is None and instance_ref is None:
        return _path(employee_id), None
    external_conv_id = None
    current_session = None
    if instance_ref is not None:
        external_conv_id = "openapi:" + digest(json.dumps(
            [str(application.id), str(user.id), str(employee_id), instance_ref],
            ensure_ascii=False, separators=(",", ":"),
        ))
        await _lock(db, external_conv_id)
        current_session = await db.scalar(select(ChatSession).where(
            ChatSession.agent_id == employee_id,
            ChatSession.user_id == user.id,
            ChatSession.source_channel == "miniprogram",
            ChatSession.external_conv_id == external_conv_id,
        ).with_for_update())
        if current_session is not None and record is None:
            return _path(employee_id, current_session.id), None
    if record is None:
        session = await _new_session(
            db, user, employee_id, render_message("openapi.sessionTitle"), external_conv_id,
        )
        return _path(employee_id, session.id), None
    try:
        await check_conversation_quota(user.id)
        await check_agent_expired(employee_id)
    except QuotaExceeded:
        fail("quota_exceeded", 403)
    except AgentExpired:
        fail("employee_unavailable", 403)
    payload = record.payload
    message = payload["message"]
    context = payload.get("context")
    if current_session is not None:
        current_session.external_conv_id = None
        await db.flush()
    session = await _new_session(db, user, employee_id, message, external_conv_id)
    content = message
    metadata = {
        "display_content": message,
        "openapi_interaction_id": str(record.id),
        **await _scene_metadata(db, employee_id, session),
    }
    if context is not None:
        metadata["external_context"] = context
        content += "\n\n" + render_message("openapi.externalContextReference") + "\n" + json.dumps(
            context, ensure_ascii=False, allow_nan=False,
        )
    result = await ingest_incoming_chat_message(
        db, session=session, agent_id=employee_id, user_id=user.id,
        content=content, source_channel="miniprogram", allow_turn_inbox=False,
        provider_event_id=f"openapi-interaction:{record.id}", message_meta=metadata,
    )
    anchor = None
    if not result.consumed_by_onmessage:
        await transition_conversation_turn(
            db, agent_id=employee_id, conversation_id=str(session.id),
            turn_anchor_id=result.message.id, status="running",
        )
        anchor = result.message
        if agent.agent_type == "openclaw":
            await enqueue_user_gateway_message(
                db, agent_id=employee_id, user_id=user.id,
                conversation_id=str(session.id), content=content,
                turn_anchor_id=result.message.id,
            )
            anchor = None
    session.last_message_at = now()
    record.session_id = session.id
    record.first_message_id = result.message.id
    record.activated_at = now()
    # The message is now the sole context owner; deletion cannot leave a snapshot.
    record.payload = None
    await db.flush()
    return _path(employee_id, session.id), anchor
