"""Durable lifecycle contract for one logical conversation turn.

The user/inbound ``ChatMessage`` remains the turn anchor.  Lifecycle state is
stored on that existing audit row so reconnecting and cross-instance viewers do
not depend on the process-local active-turn registry or on transient websocket
messages.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

TURN_LIFECYCLE_KEY = "conversation_turn_lifecycle"
TURN_GENERATION_KEY = "turn_generation"
TURN_REVISION_KEY = "turn_revision"
TURN_STATUS_KEY = "turn_status"
TURN_SESSION_KEY = "conversation_turn"
TURN_STATE_TOKEN_KEY = "turn_state_token"

ACTIVE_TURN_STATUS = "running"
SUSPENDED_TURN_STATUS = "suspended"
TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "cancelled"})
TURN_STATUSES = frozenset(
    {
        ACTIVE_TURN_STATUS,
        SUSPENDED_TURN_STATUS,
        *TERMINAL_TURN_STATUSES,
    }
)


class ConversationTurnConflict(RuntimeError):
    """Another durable non-terminal turn already owns this conversation."""


@dataclass(frozen=True, slots=True)
class ConversationTurnSnapshot:
    anchor_id: uuid.UUID | None
    generation: int
    revision: int
    status: str

    @property
    def phase(self) -> str:
        if self.status == ACTIVE_TURN_STATUS:
            return "active"
        if self.status == SUSPENDED_TURN_STATUS:
            return "suspended"
        return "idle"

    def to_client_dict(self) -> dict[str, Any]:
        return {
            "turn_anchor_id": str(self.anchor_id) if self.anchor_id is not None else None,
            "generation": self.generation,
            "revision": self.revision,
            "status": self.status,
            "phase": self.phase,
        }


IDLE_TURN_SNAPSHOT = ConversationTurnSnapshot(
    anchor_id=None,
    generation=0,
    revision=0,
    status="idle",
)


def _snapshot(anchor: ChatMessage) -> ConversationTurnSnapshot:
    metadata = dict(getattr(anchor, "message_meta", None) or {})
    try:
        generation = max(0, int(metadata.get(TURN_GENERATION_KEY) or 0))
    except (TypeError, ValueError):
        generation = 0
    try:
        revision = max(0, int(metadata.get(TURN_REVISION_KEY) or 0))
    except (TypeError, ValueError):
        revision = 0
    return ConversationTurnSnapshot(
        anchor_id=getattr(anchor, "id", None),
        generation=generation,
        revision=revision,
        status=str(metadata.get(TURN_STATUS_KEY) or "idle"),
    )


def _session_snapshot(session: ChatSession) -> ConversationTurnSnapshot | None:
    raw = dict(getattr(session, "im_config", None) or {}).get(TURN_SESSION_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        anchor_id = uuid.UUID(str(raw["turn_anchor_id"]))
        generation = int(raw["generation"])
        revision = int(raw["revision"])
        status = str(raw["status"])
    except (KeyError, TypeError, ValueError):
        return None
    if generation < 1 or revision < 1 or status not in TURN_STATUSES:
        return None
    return ConversationTurnSnapshot(anchor_id, generation, revision, status)


def _validate_transition(previous: str, target: str) -> None:
    if target not in TURN_STATUSES:
        raise ValueError(f"unsupported conversation turn status: {target}")
    if previous in TERMINAL_TURN_STATUSES and previous != target:
        raise ValueError(f"terminal conversation turn cannot transition from {previous} to {target}")
    if previous == SUSPENDED_TURN_STATUS and target not in {
        ACTIVE_TURN_STATUS,
        SUSPENDED_TURN_STATUS,
        *TERMINAL_TURN_STATUSES,
    }:
        raise ValueError(f"suspended conversation turn cannot transition to {target}")


async def transition_conversation_turn(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    status: str,
    state_token: str | None = None,
) -> ConversationTurnSnapshot:
    """Lock and transition one exact durable turn anchor.

    The caller owns commit/rollback.  Publishing the returned snapshot is only
    valid after that transaction commits.
    """

    try:
        session_id = uuid.UUID(conversation_id)
    except (TypeError, ValueError) as exc:
        raise LookupError("conversation lifecycle requires a durable chat session") from exc
    session = (
        await db.execute(
            select(ChatSession)
            .where(ChatSession.id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise LookupError("conversation lifecycle session not found")
    if session.agent_id != agent_id:
        raise LookupError("conversation lifecycle session changed owner")

    anchor = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.id == turn_anchor_id,
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role.in_(("user", "system")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if anchor is None:
        raise LookupError("conversation turn anchor not found")

    metadata = dict(anchor.message_meta or {})
    previous = str(metadata.get(TURN_STATUS_KEY) or "")
    if (
        previous == status
        and metadata.get(TURN_LIFECYCLE_KEY) is True
        and (
            state_token is None
            or str(metadata.get(TURN_STATE_TOKEN_KEY) or "") == state_token
        )
    ):
        return _snapshot(anchor)
    _validate_transition(previous, status)

    current_snapshot = _session_snapshot(session)
    anchor_generation = int(metadata.get(TURN_GENERATION_KEY) or 0)
    anchor_is_current = bool(
        current_snapshot is not None
        and current_snapshot.anchor_id == anchor.id
        and current_snapshot.generation == anchor_generation
    )

    if metadata.get(TURN_LIFECYCLE_KEY) is not True:
        latest_snapshot = current_snapshot
        if latest_snapshot is None:
            latest_lifecycle = (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == conversation_id,
                        ChatMessage.message_meta[TURN_LIFECYCLE_KEY].as_boolean().is_(True),
                    )
                    .order_by(ChatMessage.message_meta[TURN_GENERATION_KEY].as_integer().desc().nullslast())
                    .limit(1)
                )
            ).scalar_one_or_none()
            latest_snapshot = _snapshot(latest_lifecycle) if latest_lifecycle is not None else None
        if latest_snapshot is not None:
            if latest_snapshot.status in {ACTIVE_TURN_STATUS, SUSPENDED_TURN_STATUS}:
                raise ConversationTurnConflict(
                    f"conversation already owned by turn {latest_snapshot.anchor_id}"
                )
            latest_generation = latest_snapshot.generation
        else:
            latest_generation = 0
        metadata[TURN_GENERATION_KEY] = latest_generation + 1
        anchor_is_current = True
    elif status in {ACTIVE_TURN_STATUS, SUSPENDED_TURN_STATUS} and not anchor_is_current:
        raise ConversationTurnConflict(
            f"turn {anchor.id} no longer owns this conversation"
        )

    try:
        revision = max(0, int(metadata.get(TURN_REVISION_KEY) or 0)) + 1
    except (TypeError, ValueError):
        revision = 1
    metadata.update(
        {
            TURN_LIFECYCLE_KEY: True,
            TURN_REVISION_KEY: revision,
            TURN_STATUS_KEY: status,
            "turn_anchor_id": str(anchor.id),
        }
    )
    if state_token is not None:
        metadata[TURN_STATE_TOKEN_KEY] = state_token
    anchor.message_meta = metadata
    if anchor_is_current:
        session.im_config = {
            **dict(session.im_config or {}),
            TURN_SESSION_KEY: {
                "turn_anchor_id": str(anchor.id),
                "generation": int(metadata[TURN_GENERATION_KEY]),
                "revision": revision,
                "status": status,
            },
        }
    await db.flush()
    return _snapshot(anchor)


async def get_conversation_turn_snapshot(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID | None = None,
) -> ConversationTurnSnapshot:
    """Return one exact turn, or the newest admitted turn for the conversation.

    Terminal publishers must pass ``turn_anchor_id``.  Reading the session's
    current pointer after commit is only valid for reconnect/current-state
    reads: a newer turn may be admitted before an older terminal event is
    published.
    """

    if turn_anchor_id is not None:
        anchor = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.id == turn_anchor_id,
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.role.in_(("user", "system")),
                    ChatMessage.message_meta[TURN_LIFECYCLE_KEY].as_boolean().is_(True),
                )
            )
        ).scalar_one_or_none()
        return _snapshot(anchor) if anchor is not None else IDLE_TURN_SNAPSHOT

    try:
        session_id = uuid.UUID(conversation_id)
    except (TypeError, ValueError):
        session_id = None
    if session_id is not None:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is not None:
            current = _session_snapshot(session)
            if current is not None:
                return current

    anchor = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.message_meta[TURN_LIFECYCLE_KEY].as_boolean().is_(True),
            )
            .order_by(
                ChatMessage.message_meta[TURN_GENERATION_KEY].as_integer().desc().nullslast(),
                ChatMessage.message_meta[TURN_REVISION_KEY].as_integer().desc().nullslast(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return _snapshot(anchor) if anchor is not None else IDLE_TURN_SNAPSHOT


def with_turn_envelope(
    payload: dict[str, Any],
    snapshot: ConversationTurnSnapshot | None,
    *,
    event_kind: str,
) -> dict[str, Any]:
    """Attach normalized lifecycle identity without changing legacy fields."""

    if snapshot is None:
        return {**payload, "event_kind": event_kind}
    turn_details = payload.get("turn")
    turn_details = dict(turn_details) if isinstance(turn_details, dict) else {}
    return {
        **payload,
        "event_kind": event_kind,
        "turn": {**turn_details, **snapshot.to_client_dict()},
    }


async def publish_conversation_turn_event(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    payload: dict[str, Any],
    snapshot: ConversationTurnSnapshot | None,
    event_kind: str,
) -> None:
    """Publish one normalized event to every viewer of the exact session."""

    try:
        from app.api.websocket import manager

        await manager.send_to_session(
            str(agent_id),
            str(conversation_id),
            with_turn_envelope(payload, snapshot, event_kind=event_kind),
        )
    except Exception:  # noqa: BLE001 - observer transport must not affect durable state
        # A web observer is optional for IM/background turns. Durable state is
        # authoritative and reconnecting clients recover it from the snapshot.
        return


async def publish_committed_turn_terminal(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    message_id: uuid.UUID | None,
    content: str,
) -> None:
    """Publish terminal state after a caller-owned transaction has committed."""

    from app.database import async_session

    try:
        async with async_session() as db:
            snapshot = await get_conversation_turn_snapshot(
                db,
                agent_id=agent_id,
                conversation_id=conversation_id,
                turn_anchor_id=turn_anchor_id,
            )
    except Exception:  # noqa: BLE001 - post-commit observer lookup is best-effort
        return
    if snapshot.anchor_id is None:
        return
    await publish_conversation_turn_event(
        agent_id=agent_id,
        conversation_id=conversation_id,
        payload={
            "type": "done",
            "role": "assistant",
            "content": content,
            **({"message_id": str(message_id)} if message_id is not None else {}),
        },
        snapshot=snapshot,
        event_kind="turn_terminal",
    )
