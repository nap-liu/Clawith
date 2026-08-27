"""Durable, session-exact A2A file-delivery timeline writes."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.models.subagent_run import SubagentRun
from app.services.chat_attachments import attachment_from_workspace_path


@dataclass(frozen=True)
class A2AFileOriginScope:
    """Relational scope derived from the tool's real originating session."""

    project_id: uuid.UUID | None
    preferred_session_id: uuid.UUID | None


def _as_uuid(value: str | uuid.UUID, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a complete platform UUID") from exc


async def resolve_a2a_file_origin_scope(
    db,
    *,
    origin_session_id: str | uuid.UUID | None,
    sender_agent_id: uuid.UUID,
) -> A2AFileOriginScope:
    """Resolve project and preferred A2A thread from durable session rows.

    Project member executions run in a standard ``subagent`` ChatSession whose
    ``SubagentRun.parent_session_id`` is the visible A2A conversation.  That
    parent relationship is the authoritative route for files created during
    the child execution; configuration hints never override it.
    """
    if not origin_session_id:
        return A2AFileOriginScope(project_id=None, preferred_session_id=None)

    origin_id = _as_uuid(origin_session_id, "origin_session_id")
    origin = await db.get(ChatSession, origin_id)
    if origin is None:
        raise ValueError("The originating conversation no longer exists")

    if origin.source_channel == "agent":
        if sender_agent_id not in {origin.agent_id, origin.peer_agent_id}:
            raise ValueError("The originating A2A conversation does not include the sending Agent")
        return A2AFileOriginScope(
            project_id=origin.project_id,
            preferred_session_id=origin.id,
        )

    if origin.agent_id != sender_agent_id:
        raise ValueError("The originating conversation does not belong to the sending Agent")

    if origin.source_channel == "subagent":
        run = await db.get(SubagentRun, origin.id)
        parent = await db.get(ChatSession, run.parent_session_id) if run is not None else None
        if (
            run is None
            or origin.project_id is None
            or run.project_id != origin.project_id
            or parent is None
            or parent.source_channel != "agent"
            or parent.project_id != origin.project_id
            or sender_agent_id not in {parent.agent_id, parent.peer_agent_id}
        ):
            raise ValueError("The project child conversation has no valid parent A2A route")
        return A2AFileOriginScope(
            project_id=origin.project_id,
            preferred_session_id=parent.id,
        )

    return A2AFileOriginScope(
        project_id=origin.project_id,
        preferred_session_id=None,
    )


def _route_lock_id(route_key: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(route_key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


async def _lock_route(db, route_key: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _route_lock_id(route_key)},
    )


def _session_matches_route(
    session: ChatSession | None,
    *,
    sender_agent_id: uuid.UUID,
    target_agent_id: uuid.UUID,
    project_id: uuid.UUID | None,
) -> bool:
    return bool(
        session is not None
        and session.source_channel == "agent"
        and session.project_id == project_id
        and {session.agent_id, session.peer_agent_id} == {sender_agent_id, target_agent_id}
    )


async def append_a2a_file_delivery_message(
    db,
    *,
    sender_agent_id: uuid.UUID,
    target_agent_id: uuid.UUID,
    sender_creator_id: uuid.UUID,
    sender_name: str,
    target_name: str,
    project_id: uuid.UUID | None,
    preferred_session_id: uuid.UUID | None,
    source_path: str,
    delivered_path: str,
    delivered_name: str,
    delivery_note: str,
    file_size: int,
    created_at: datetime,
    external_event_key: str | None = None,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Append one file message to the exact A2A timeline.

    Multiple sessions for the same Agent pair are valid.  A preferred session
    obtained from the current child/parent chain wins.  Without that anchor,
    the most recently active session in the exact project scope is selected by
    an ordered, one-row query.  Route creation is serialized across replicas.
    """
    session_agent_id = min(sender_agent_id, target_agent_id, key=str)
    session_peer_id = max(sender_agent_id, target_agent_id, key=str)
    route_key = f"a2a-file-route:{project_id or 'global'}:{session_agent_id}:{session_peer_id}"

    if external_event_key:
        await _lock_route(db, f"a2a-file-operation:{external_event_key}")
        existing = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == external_event_key).with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing_session = await db.get(
                ChatSession,
                _as_uuid(existing.conversation_id, "conversation_id"),
            )
            metadata = dict(existing.message_meta or {})
            if (
                not _session_matches_route(
                    existing_session,
                    sender_agent_id=sender_agent_id,
                    target_agent_id=target_agent_id,
                    project_id=project_id,
                )
                or str(metadata.get("target_agent_id") or "") != str(target_agent_id)
                or str(metadata.get("source_agent_id") or "") != str(sender_agent_id)
            ):
                raise ValueError("The replayed file receipt does not match the requested A2A route")
            return existing_session.id

    chat_session = await db.get(ChatSession, preferred_session_id) if preferred_session_id is not None else None
    if chat_session is not None and not _session_matches_route(
        chat_session,
        sender_agent_id=sender_agent_id,
        target_agent_id=target_agent_id,
        project_id=project_id,
    ):
        chat_session = None

    if chat_session is None:
        await _lock_route(db, route_key)
        chat_session = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == session_agent_id,
                    ChatSession.peer_agent_id == session_peer_id,
                    ChatSession.source_channel == "agent",
                    (
                        ChatSession.project_id == project_id
                        if project_id is not None
                        else ChatSession.project_id.is_(None)
                    ),
                )
                .order_by(
                    ChatSession.last_message_at.desc().nulls_last(),
                    ChatSession.created_at.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    if chat_session is None:
        external_conv_id = (
            f"project-a2a:{project_id}:{session_peer_id}" if project_id is not None else f"a2a-file:{session_peer_id}"
        )
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "agent",
                    Participant.ref_id == sender_agent_id,
                )
            )
        ).scalar_one_or_none()
        chat_session = ChatSession(
            agent_id=session_agent_id,
            project_id=project_id,
            title=f"{sender_name} ↔ {target_name}",
            source_channel="agent",
            participant_id=participant.id if participant else None,
            peer_agent_id=session_peer_id,
            external_conv_id=external_conv_id,
        )
        db.add(chat_session)
        await db.flush()

    participant = (
        await db.execute(
            select(Participant).where(
                Participant.type == "agent",
                Participant.ref_id == sender_agent_id,
            )
        )
    ).scalar_one_or_none()
    content = (
        f"[File delivery from {sender_name}]\n"
        f"{sender_name} sent you a file: {delivered_name}\n"
        f"File path: {delivered_path}\n"
        f'Use read_file(path="{delivered_path}") to inspect it.'
    )
    if delivery_note:
        content += f"\nNote: {delivery_note}"

    attachment = attachment_from_workspace_path(
        delivered_path,
        display_name=delivered_name,
        size_bytes=file_size,
    )
    db.add(
        ChatMessage(
            agent_id=session_agent_id,
            user_id=sender_creator_id,
            sender_agent_id=sender_agent_id,
            role="user",
            content=content,
            conversation_id=str(chat_session.id),
            participant_id=participant.id if participant else None,
            external_event_key=external_event_key,
            message_meta={
                "attachments": [attachment],
                "direction": "outbound",
                "source_channel": "agent",
                "source_agent_id": str(sender_agent_id),
                "target_agent_id": str(target_agent_id),
                "target_name": target_name,
                "origin_session_id": str(origin_session_id or ""),
                "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                "tool_call_id": str(tool_call_id or ""),
                "file_delivery": {
                    "source_path": source_path,
                    "delivered_path": delivered_path,
                },
            },
        )
    )
    chat_session.last_message_at = created_at
    await db.flush()
    return chat_session.id
