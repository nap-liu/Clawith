"""Shared helpers for the WebSocket chat API."""

from __future__ import annotations

import asyncio

from loguru import logger
from sqlalchemy import String, cast, exists, or_, select
from sqlalchemy.orm import aliased
from starlette.websockets import WebSocketDisconnect

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.realtime import realtime_router


class SessionTurnBusyError(RuntimeError):
    """A durable internal wake already owns the next turn in this session."""


async def has_active_subagent_event_turn_impl(
    _api,
    db,
    conversation_id: str,
) -> bool:
    """Return whether an unfinished durable parent wake owns this session."""
    anchor = aliased(ChatMessage)
    final = aliased(ChatMessage)
    final_exists = exists(
        select(final.id).where(
            final.conversation_id == anchor.conversation_id,
            final.role == "assistant",
            final.message_meta["turn_anchor_id"].as_string() == cast(anchor.id, String),
            or_(
                final.message_meta["artifact_role"].as_string().is_(None),
                final.message_meta["artifact_role"].as_string() != "intermediate_assistant",
            ),
        )
    )
    active = (
        await db.execute(
            select(anchor.id)
            .where(
                anchor.conversation_id == conversation_id,
                anchor.message_meta["kind"].as_string() == "subagent_event",
                anchor.message_meta["turn_status"].as_string() == "running",
                ~final_exists,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return active is not None


class ConnectionManager:
    """Manage WebSocket connections per agent."""

    def __init__(self):
        self.active_connections: dict[str, list[tuple]] = {}

    async def connect(self, agent_id: str, websocket, session_id: str = None, user_id: str | None = None):
        if agent_id not in self.active_connections:
            self.active_connections[agent_id] = []
        self.active_connections[agent_id].append((websocket, session_id, user_id))
        await realtime_router.register_connection(
            agent_id=agent_id,
            websocket=websocket,
            session_id=session_id,
            user_id=user_id,
        )

    async def disconnect(self, agent_id: str, websocket):
        if agent_id in self.active_connections:
            self.active_connections[agent_id] = [
                (ws, sid, uid) for ws, sid, uid in self.active_connections[agent_id] if ws != websocket
            ]
        await realtime_router.unregister_connection(agent_id=agent_id, websocket=websocket)

    def _local_connections(self, agent_id: str) -> list[tuple]:
        return self.active_connections.get(agent_id, [])

    async def deliver_pubsub_message(
        self,
        *,
        agent_id: str,
        payload: dict,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if agent_id not in self.active_connections:
            return
        for ws, sid, uid in list(self.active_connections[agent_id]):
            if session_id is not None and sid != session_id:
                continue
            if user_id is not None and uid != user_id:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                pass

    async def send_message(self, agent_id: str, message: dict):
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
        )

    async def send_to_session(self, agent_id: str, session_id: str, message: dict):
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            session_id=session_id,
        )

    async def send_to_user(self, agent_id: str, user_id: str, message: dict):
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            user_id=user_id,
        )

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        return await realtime_router.get_active_session_ids(agent_id)

    async def is_user_viewing_session(self, agent_id: str, session_id: str, user_id: str) -> bool:
        return await realtime_router.is_user_viewing_session(
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
        )


async def maybe_mark_session_read_for_active_viewer_impl(
    api,
    db,
    *,
    agent_id,
    session_id: str,
    user_id,
) -> bool:
    if not await api.manager.is_user_viewing_session(str(agent_id), session_id, str(user_id)):
        return False

    session = await db.get(ChatSession, api.uuid.UUID(session_id))
    if not session:
        return False

    session.last_read_at_by_user = api.datetime.now(api.tz.utc)
    return True


async def await_turn_with_abort_impl(
    _api,
    llm_task,
    recv_json,
    partial_chunks: list[str],
    *,
    on_abort=None,
    on_message=None,
):
    """Drive a running web turn while listening for abort / disconnect on the socket."""
    aborted = False
    disconnected = False
    while not llm_task.done():
        try:
            msg = await asyncio.wait_for(recv_json(), timeout=0.5)
            if isinstance(msg, dict) and msg.get("type") == "abort":
                logger.info("[WS] Abort received, stopping durable turn tree")
                accepted = True
                if on_abort is not None:
                    accepted = bool(await on_abort(msg))
                if not accepted:
                    logger.info("[WS] Ignoring stale abort for a different turn")
                    continue
                if not llm_task.done():
                    llm_task.cancel()
                aborted = True
                break
            elif isinstance(msg, dict) and on_message is not None:
                await on_message(msg)
        except asyncio.TimeoutError:
            continue
        except WebSocketDisconnect:
            disconnected = True
            break
        except RuntimeError as exc:
            if not _is_closed_websocket_receive_error(exc, recv_json):
                raise
            disconnected = True
            break

    # Only an accepted STOP is an abort. Supervisor shutdown cancellation
    # propagates to the root, which checks the durable STOP before finalizing.
    if aborted:
        try:
            await llm_task
        except (asyncio.CancelledError, Exception):
            pass
        partial_text = "".join(partial_chunks).strip()
        resp = (partial_text + "\n\n*[Generation stopped]*") if partial_text else "*[Generation stopped]*"
        return resp, "aborted"

    resp = await llm_task
    return resp, ("disconnected" if disconnected else "completed")


_CLOSED_WEBSOCKET_RECEIVE_ERRORS = frozenset(
    {
        'WebSocket is not connected. Need to call "accept" first.',
        'Cannot call "receive" once a disconnect message has been received.',
    }
)


def _is_closed_websocket_receive_error(exc: RuntimeError, recv_json) -> bool:
    """Recognize only Starlette's closed-socket receive errors."""
    if str(exc) not in _CLOSED_WEBSOCKET_RECEIVE_ERRORS:
        return False
    websocket = getattr(recv_json, "__self__", None)
    states = [
        getattr(websocket, name, None)
        for name in ("client_state", "application_state")
        if hasattr(websocket, name)
    ]
    if not states:
        return True
    return any(getattr(state, "name", str(state)).upper().endswith("DISCONNECTED") for state in states)
