"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone as tz
from time import perf_counter


from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import set_trace_id
from app.core.permissions import can_view_all_agent_chat_sessions, check_agent_access, is_agent_expired
from app.core.security import decode_access_token
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.task import Task
from app.models.user import User
from app.services.activity_logger import log_activity
from app.services.agentbay_live import detect_agentbay_env, get_browser_snapshot, get_desktop_screenshot
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.llm import call_llm_with_failover
from app.services.onboarding import is_onboarded, mark_onboarding_phase, resolve_onboarding_prompt
from app.services.quota_guard import (
    AgentExpired,
    QuotaExceeded,
    check_agent_expired,
    check_conversation_quota,
    increment_agent_llm_usage,
    increment_conversation_usage,
)
from app.services.realtime import realtime_router
from app.services.task_executor import execute_task
from app.utils.sanitize import sanitize_tool_args

router = APIRouter(tags=["websocket"])

MAX_LIVE_CODE_STREAM_CHARS = 120_000
LIVE_CODE_TRUNCATED_NOTICE = "\n\n[... live output truncated; execution continues ...]\n"


def extract_partial_content(args_str: str) -> str:
    """Extract the string value of the 'content' field from a partial JSON tool-arguments string.

    When the LLM streams the finish tool call, arguments arrive as an
    incrementally-growing JSON fragment like '{"content": "hello \\\\n wor'.
    This function parses what is available so far, correctly handling JSON
    escape sequences (\\n, \\", \\\\, \\\\uXXXX, etc.) even when the string is
    truncated mid-escape.
    """
    import re as _re

    s = args_str.strip()
    match = _re.search(r'"content"\s*:\s*"', s)
    if not match:
        return ""

    start_idx = match.end()
    val_chars: list[str] = []
    escaped = False
    i = start_idx
    n = len(s)
    while i < n:
        c = s[i]
        if escaped:
            if c == "n":
                val_chars.append("\n")
            elif c == "t":
                val_chars.append("\t")
            elif c == "r":
                val_chars.append("\r")
            elif c == "b":
                val_chars.append("\b")
            elif c == "f":
                val_chars.append("\f")
            elif c == '"':
                val_chars.append('"')
            elif c == "\\":
                val_chars.append("\\")
            elif c == "/":
                val_chars.append("/")
            elif c == "u":
                if i + 4 < n:
                    try:
                        hex_val = int(s[i + 1 : i + 5], 16)
                        val_chars.append(chr(hex_val))
                        i += 4
                    except ValueError:
                        val_chars.append("\\")
                        val_chars.append("u")
                else:
                    # Incomplete \uXXXX — wait for more data
                    val_chars.append("\\")
                    val_chars.append("u")
            else:
                val_chars.append(c)
            escaped = False
        else:
            if c == "\\":
                escaped = True
            elif c == '"':
                # End of the JSON string value
                break
            else:
                val_chars.append(c)
        i += 1
    return "".join(val_chars)


class ConnectionManager:
    """Manage WebSocket connections per agent."""

    def __init__(self):
        # agent_id_str -> list of (WebSocket, session_id_str | None, user_id_str | None)
        self.active_connections: dict[str, list[tuple]] = {}

    async def connect(self, agent_id: str, websocket: WebSocket, session_id: str = None, user_id: str | None = None):
        if agent_id not in self.active_connections:
            self.active_connections[agent_id] = []
        self.active_connections[agent_id].append((websocket, session_id, user_id))
        await realtime_router.register_connection(
            agent_id=agent_id,
            websocket=websocket,
            session_id=session_id,
            user_id=user_id,
        )

    async def disconnect(self, agent_id: str, websocket: WebSocket):
        if agent_id in self.active_connections:
            self.active_connections[agent_id] = [
                (ws, sid, uid) for ws, sid, uid in self.active_connections[agent_id] if ws != websocket
            ]
        await realtime_router.unregister_connection(agent_id=agent_id, websocket=websocket)

    def _local_connections(self, agent_id: str) -> list[tuple[WebSocket, str | None, str | None]]:
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
        """Send message only to WebSocket connections matching the given session_id."""
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            session_id=session_id,
        )

    async def send_to_user(self, agent_id: str, user_id: str, message: dict):
        """Send message to all live WebSocket sessions of a given platform user for an agent."""
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            user_id=user_id,
        )

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        """Return distinct session IDs for all active WS connections of an agent."""
        return await realtime_router.get_active_session_ids(agent_id)

    async def is_user_viewing_session(self, agent_id: str, session_id: str, user_id: str) -> bool:
        """Return True if the given platform user currently has this exact session open."""
        return await realtime_router.is_user_viewing_session(
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
        )


manager = ConnectionManager()


async def maybe_mark_session_read_for_active_viewer(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: uuid.UUID,
) -> bool:
    """Advance last_read_at_by_user if the owner is actively viewing this exact session."""
    if not await manager.is_user_viewing_session(str(agent_id), session_id, str(user_id)):
        return False

    session = await db.get(ChatSession, uuid.UUID(session_id))
    if not session:
        return False

    session.last_read_at_by_user = datetime.now(tz.utc)
    return True


async def _await_turn_with_abort(llm_task, recv_json, partial_chunks: list[str]):
    """Drive a running web turn while listening for abort / disconnect on the socket.

    Returns ``(assistant_response, outcome)`` where ``outcome`` is one of
    ``"completed" | "aborted" | "disconnected"``.

    Invariant (the "agent gave no reply" fix): a client **disconnect** never
    cancels the turn. A turn is a unit of work that must run to completion and be
    persisted, so a reconnecting client sees the reply via history replay — parity
    with the IM / trigger channels, which are connection-independent. Only an
    explicit user **abort** cancels the in-flight task.

    ``recv_json`` is a 0-arg coroutine factory (e.g. ``websocket.receive_json``)
    polled with a short timeout so the loop can notice the task finishing. The
    turn's streaming callbacks must tolerate a dead socket (see ``_safe_send``)
    so that, after a disconnect, awaiting the task yields the real reply rather
    than a send error.
    """
    import asyncio as _aio

    aborted = False
    disconnected = False
    while not llm_task.done():
        try:
            msg = await _aio.wait_for(recv_json(), timeout=0.5)
            if isinstance(msg, dict) and msg.get("type") == "abort":
                logger.info("[WS] Abort received, cancelling LLM task")
                llm_task.cancel()
                aborted = True
                break
            # Non-abort messages during generation are ignored; the client should
            # wait for `done` before sending the next turn.
        except _aio.TimeoutError:
            continue
        except WebSocketDisconnect:
            # Connection dropped mid-turn — stop listening but DO NOT cancel.
            disconnected = True
            break

    if aborted:
        try:
            await llm_task
        except (_aio.CancelledError, Exception):
            pass
        partial_text = "".join(partial_chunks).strip()
        resp = (partial_text + "\n\n*[Generation stopped]*") if partial_text else "*[Generation stopped]*"
        return resp, "aborted"

    # completed OR disconnected: the task was never cancelled, so it runs to
    # completion. Await its real result for the caller to persist.
    resp = await llm_task
    return resp, ("disconnected" if disconnected else "completed")


@router.websocket("/ws/chat/{agent_id}")
async def websocket_chat(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    token: str = Query(...),
    session_id: str = Query(None),
    lang: str = Query("en"),
):
    """WebSocket endpoint for real-time chat with an agent."""
    handler = WebSocketChatHandler(websocket, agent_id, token, session_id, lang)
    await handler.run()


class WebSocketChatHandler:
    """Manages connection lifecycle, message polling, LLM orchestration, and persistence for a single user-agent session."""

    def __init__(
        self,
        websocket: WebSocket,
        agent_id: uuid.UUID,
        token: str,
        session_id: str | None = None,
        lang: str = "en",
    ):
        self.websocket = websocket
        self.agent_id = agent_id
        self.token = token
        self.session_id_param = session_id
        self.lang = lang

        # State fields initialized during setup
        self.user: User | None = None
        self.agent: Agent | None = None
        self.agent_name: str = ""
        self.agent_type: str = ""
        self.role_description: str = ""
        self.welcome_message: str = ""
        self.ctx_size: int = 100
        self.user_display_name: str = ""
        self.llm_model: LLMModel | None = None
        self.fallback_llm_model: LLMModel | None = None
        self.conv_id: str | None = None
        # Read-only monitor: viewer is watching a session they do NOT own but are
        # allowed to see (admins / agent creator). They subscribe to live
        # broadcasts but may never drive a turn — enforced in ``message_loop``.
        self.read_only: bool = False
        self.history_messages: list[ChatMessage] = []
        self.conversation: list[dict] = []
        self.current_user_text: str = ""
        # Set true when the browser drops mid-turn: the turn still runs to
        # completion + persists, then we tear the handler down cleanly.
        self.client_disconnected: bool = False

    async def _safe_send(self, payload: dict):
        """Broadcast turn output to EVERY live connection viewing this session —
        not just the socket that started the turn — so other tabs and freshly
        opened views of an in-flight session see the stream live (the
        "connection = subscriber" model). ``send_to_session`` is best-effort per
        connection and tolerates zero live connections, so:
          (a) the turn still runs to completion + persists after the driver drops
              mid-turn (the disconnect fix), and
          (b) a single-tab session behaves exactly as before — the lone driving
              socket is the only subscriber.
        """
        try:
            await manager.send_to_session(str(self.agent_id), self.conv_id, payload)
        except Exception:
            pass

    async def run(self):
        """Main entry point for handling the lifecycle of the WebSocket connection."""
        try:
            # 1. Setup session (Authentication, permissions, loading models, history, etc.)
            success = await self.setup()
            if not success:
                return

            # 2. Start the message receiving and processing loop
            await self.message_loop()

        except WebSocketDisconnect:
            logger.info(f"[WS] Client disconnected: {getattr(self.user, 'id', 'unknown')}")
            await manager.disconnect(str(self.agent_id), self.websocket)
        except Exception as e:
            logger.exception(f"[WS] Unexpected error: {e}")
            await manager.disconnect(str(self.agent_id), self.websocket)

    async def setup(self) -> bool:
        """Accepts connection, authenticates user, verifies agent access, loads models, resolves session & history."""
        # Accept immediately so browser sees onopen without waiting for DB setup
        await self.websocket.accept()

        # Authenticate
        try:
            payload = decode_access_token(self.token)
            user_id = uuid.UUID(payload["sub"])
        except Exception:
            await self.websocket.send_json({"type": "error", "content": "Authentication failed"})
            await self.websocket.close(code=4001)
            return False

        try:
            async with async_session() as db:
                result = await db.execute(select(User).where(User.id == user_id))
                self.user = result.scalar_one_or_none()
                if not self.user:
                    logger.error("[WS] User not found")
                    await self.websocket.send_json({"type": "error", "content": "User not found"})
                    await self.websocket.close(code=4001)
                    return False

                logger.info(f"[WS] Checking agent access for {self.agent_id}")
                self.agent, _ = await check_agent_access(db, self.user, self.agent_id)
                if is_agent_expired(self.agent):
                    await self.websocket.send_json(
                        {
                            "type": "error",
                            "content": "This Agent has expired and is off duty. Please contact your admin to extend its service.",
                        }
                    )
                    await self.websocket.close(code=4003)
                    return False

                self.agent_name = self.agent.name
                self.agent_type = self.agent.agent_type or ""
                self.role_description = self.agent.role_description or ""
                self.welcome_message = self.agent.welcome_message or ""
                self.ctx_size = self.agent.context_window_size or 100
                self.user_display_name = (self.user.display_name or "").strip() or "there"
                logger.info(
                    f"[WS] Agent: {self.agent_name}, type: {self.agent_type}, model_id: {self.agent.primary_model_id}, ctx: {self.ctx_size}"
                )

                # Load models
                await self._load_models(db)

                # Resolve or create chat session
                self.conv_id = await self._resolve_chat_session(db, user_id)
                if not self.conv_id:
                    return False

                # Load history messages
                await self._load_history(db)

        except Exception as e:
            logger.exception(f"[WS] Setup error: {e}")
            await self.websocket.send_json({"type": "error", "content": "Setup failed"})
            await self.websocket.close(code=4002)
            return False

        # Connect connection manager.
        # Defense-in-depth against reconnect churn: prune dead sockets before
        # registering so a flapping client can't pile up stale tuples (each would
        # get duplicate pushes from send_to_session/send_to_user). Checks BOTH
        # states — client_state catches a client that already dropped (relevant
        # now that a turn runs to completion *after* disconnect, leaving the old
        # handler — and its tuple — lingering for seconds), application_state
        # catches one the server has closed. A live CONNECTED socket has neither
        # flag set, so multi-tab connections are preserved; only genuinely-dead
        # ones are dropped.
        from starlette.websockets import WebSocketState as _WSState

        def _ws_dead(ws) -> bool:
            return (
                getattr(ws, "client_state", None) == _WSState.DISCONNECTED
                or getattr(ws, "application_state", None) == _WSState.DISCONNECTED
            )

        agent_id_str = str(self.agent_id)
        _conns = manager.active_connections.setdefault(agent_id_str, [])
        _conns[:] = [(ws, sid, uid) for ws, sid, uid in _conns if not _ws_dead(ws)]
        # connect() appends the tuple AND registers presence with the realtime
        # router (Redis-backed cross-instance routing for HA) — must go through
        # it, not a bare append, or this socket is invisible to other instances.
        await manager.connect(agent_id_str, self.websocket, self.conv_id, str(user_id))
        logger.info(f"[WS] Ready! Agent={self.agent_name} (live conns for agent: {len(_conns)})")

        # Send session_id to frontend
        await self.websocket.send_json(
            {"type": "connected", "session_id": self.conv_id, "read_only": self.read_only}
        )

        # Build conversation context
        self.conversation = self._build_conversation_context()

        return True

    async def _load_models(self, db: AsyncSession):
        """Loads primary and fallback models for the agent."""
        if self.agent.primary_model_id:
            model_result = await db.execute(select(LLMModel).where(LLMModel.id == self.agent.primary_model_id))
            self.llm_model = model_result.scalar_one_or_none()
            if self.llm_model and not self.llm_model.enabled:
                logger.info(f"[WS] Primary model {self.llm_model.model} is disabled, skipping")
                self.llm_model = None
            else:
                logger.info(f"[WS] Primary model loaded: {self.llm_model.model if self.llm_model else 'None'}")

        if self.agent.fallback_model_id:
            fb_result = await db.execute(select(LLMModel).where(LLMModel.id == self.agent.fallback_model_id))
            self.fallback_llm_model = fb_result.scalar_one_or_none()
            if self.fallback_llm_model and not self.fallback_llm_model.enabled:
                logger.info(f"[WS] Fallback model {self.fallback_llm_model.model} is disabled, skipping")
                self.fallback_llm_model = None
            elif self.fallback_llm_model:
                logger.info(f"[WS] Fallback model loaded: {self.fallback_llm_model.model}")

        if not self.llm_model and self.fallback_llm_model:
            self.llm_model = self.fallback_llm_model
            self.fallback_llm_model = None
            logger.info(f"[WS] Primary model unavailable, using fallback: {self.llm_model.model}")

    async def _resolve_chat_session(self, db: AsyncSession, user_id: uuid.UUID) -> str | None:
        """Resolves existing session or creates a new one."""
        conv_id = self.session_id_param
        if conv_id:
            try:
                _sid = uuid.UUID(conv_id)
            except (ValueError, TypeError):
                conv_id = None
                _existing = None
            else:
                _sr = await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == _sid,
                        ChatSession.agent_id == self.agent_id,
                    )
                )
                _existing = _sr.scalar_one_or_none()
                if not _existing:
                    conv_id = None
                elif _existing.source_channel != "agent" and str(_existing.user_id) != str(user_id):
                    # Not the owner. Allow a READ-ONLY monitor connection if the
                    # viewer may see others' sessions (same gate as the REST
                    # session/message APIs: admins + the agent creator) — so any
                    # session visible in the web UI also updates live. They only
                    # subscribe to broadcasts; sending is blocked in message_loop.
                    if can_view_all_agent_chat_sessions(self.user, self.agent):
                        self.read_only = True
                    else:
                        await self.websocket.send_json({"type": "error", "content": "Not authorized for this session"})
                        await self.websocket.close(code=4003)
                        return None
        if not conv_id:
            _sr = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == self.agent_id,
                    ChatSession.user_id == user_id,
                    ChatSession.source_channel == "web",
                    not ChatSession.is_group,
                    ChatSession.is_primary,
                )
                .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
                .limit(1)
            )
            _latest = _sr.scalar_one_or_none()
            if _latest:
                conv_id = str(_latest.id)
            else:
                _new_session = await ensure_primary_platform_session(db, self.agent_id, user_id)
                await db.commit()
                await db.refresh(_new_session)
                conv_id = str(_new_session.id)
                logger.info(f"[WS] Selected primary session {conv_id}")
        return conv_id

    async def _load_history(self, db: AsyncSession):
        """Loads and prepares history messages for the conversation via the shared
        loader — the SAME row source the IM channels use, so web and IM replay
        history identically."""
        try:
            from app.services.chat_history import load_messages_for_session

            self.history_messages = await load_messages_for_session(
                db,
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
                ctx_size=self.ctx_size,
            )
            logger.info(f"[WS] Loaded {len(self.history_messages)} history messages for session {self.conv_id}")
        except Exception as e:
            logger.warning(f"[WS] History load failed (non-fatal): {e}")

    def _build_conversation_context(self) -> list[dict]:
        """Translates historical ChatMessages to LLM inputs via the shared
        row→message builder — the SAME mapping the IM channels use (tool_call
        rows expand to the assistant(tool_calls)+tool(result) pair; web replays
        model thinking) — then re-hydrates historical images for multi-turn
        vision context."""
        from app.services.chat_history import build_llm_messages_from_rows
        from app.services.image_context import rehydrate_image_messages

        conversation: list[dict] = build_llm_messages_from_rows(self.history_messages, include_thinking=True)
        conversation = rehydrate_image_messages(conversation, self.agent_id, max_images=3)
        return conversation

    async def message_loop(self):
        """Core message processing loop."""
        # Send welcome message on new session (no history)
        if self.welcome_message and not self.history_messages:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": self.welcome_message})

        while True:
            data = await self.websocket.receive_json()

            # Set a unique trace ID for this specific message processing.
            trace_id = str(uuid.uuid4())[:12]
            set_trace_id(trace_id)

            content = data.get("content", "")
            display_content = data.get("display_content", "")
            file_name = data.get("file_name", "")
            override_model_id = data.get("model_id")
            is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
            logger.info(f"[WS] Received: {content[:50]}" + (" [onboarding]" if is_onboarding_trigger else ""))

            if not content and not is_onboarding_trigger:
                continue

            # Read-only monitor: this viewer is watching a session they do not
            # own (admin/creator with view rights). They receive live broadcasts
            # but must never drive a turn or post as the session owner. The UI
            # also disables the composer, but THIS is the authoritative guard.
            if self.read_only:
                await self.websocket.send_json(
                    {"type": "error", "content": "只读监看会话,无法在此发送消息。"}
                )
                continue

            if is_onboarding_trigger:
                if await self._handle_onboarding_trigger_guard():
                    continue
                content = "Please begin the onboarding."

            self.current_user_text = content
            effective_llm_model = await self._resolve_effective_model(override_model_id)

            # Quota Checks
            if not await self._check_quotas():
                continue

            # Add user message to in-memory context
            self.conversation.append({"role": "user", "content": content})

            # Save user message to DB
            await self._save_user_message(content, display_content, file_name, is_onboarding_trigger)

            # OpenClaw routing check
            if self.agent_type == "openclaw":
                await self._route_openclaw(content)
                continue

            # Detect task creation intent
            task_match = re.search(
                r"(?:创建|新建|添加|建一个|帮我建|create|add)(?:一个|a )?(?:任务|待办|todo|task)[，,：：:\\s]*(.+)",
                content,
                re.IGNORECASE,
            )

            # Invoke LLM and stream response
            self.client_disconnected = False
            if effective_llm_model:
                assistant_response, thinking_content, queued_messages = await self._run_llm_and_stream(
                    effective_llm_model, is_onboarding_trigger
                )
            else:
                assistant_response = (
                    f"⚠️ {self.agent_name} has no LLM model configured. "
                    "Please select a model in the agent's Settings tab."
                )
                thinking_content = []
                queued_messages = []

            # If task creation detected, create a real Task record
            if task_match:
                assistant_response = await self._create_task_record(task_match.group(1).strip(), assistant_response)

            # Add assistant response to in-memory conversation
            self.conversation.append({"role": "assistant", "content": assistant_response})

            # Save assistant reply
            await self._save_assistant_reply(assistant_response, thinking_content)

            # Final 'done' packet — best-effort broadcast; a client that dropped
            # mid-turn gets the reply via history replay on reconnect instead.
            await self._safe_send({"type": "done", "role": "assistant", "content": assistant_response})

            # The browser dropped mid-turn: we finished + persisted the reply
            # detached. Tear down cleanly instead of looping back into receive
            # (which would raise) — same cleanup as the WebSocketDisconnect path.
            if self.client_disconnected:
                logger.info(
                    f"[WS] Detached turn complete after disconnect; closing handler for "
                    f"{getattr(self.user, 'id', 'unknown')}"
                )
                await manager.disconnect(str(self.agent_id), self.websocket)
                break

    async def _handle_onboarding_trigger_guard(self) -> bool:
        """Returns True if the onboarding trigger was ignored (already onboarded)."""
        async with async_session() as _gdb:
            if await is_onboarded(_gdb, self.agent_id, self.user.id):
                logger.info("[WS] Onboarding trigger ignored — pair already onboarded")
                await self.websocket.send_json(
                    {
                        "type": "onboarded",
                        "agent_id": str(self.agent_id),
                    }
                )
                return True
        return False

    async def _resolve_effective_model(self, override_model_id: str | None) -> LLMModel | None:
        """Reloads model config and resolves effective model (taking overrides into account)."""
        async with async_session() as _mdb:
            _agent_r = await _mdb.execute(select(Agent).where(Agent.id == self.agent_id))
            _agent_cur = _agent_r.scalar_one_or_none()
            if _agent_cur:
                if _agent_cur.primary_model_id:
                    _m_r = await _mdb.execute(select(LLMModel).where(LLMModel.id == _agent_cur.primary_model_id))
                    _m = _m_r.scalar_one_or_none()
                    self.llm_model = _m if (_m and _m.enabled) else None
                else:
                    self.llm_model = None

                if _agent_cur.fallback_model_id:
                    _fb_r = await _mdb.execute(select(LLMModel).where(LLMModel.id == _agent_cur.fallback_model_id))
                    _fb = _fb_r.scalar_one_or_none()
                    self.fallback_llm_model = _fb if (_fb and _fb.enabled) else None
                else:
                    self.fallback_llm_model = None

                if not self.llm_model and self.fallback_llm_model:
                    self.llm_model = self.fallback_llm_model
                    self.fallback_llm_model = None

        effective_llm_model = self.llm_model
        if override_model_id:
            try:
                _ovr_uuid = uuid.UUID(str(override_model_id))
                async with async_session() as _mdb:
                    _mr = await _mdb.execute(select(LLMModel).where(LLMModel.id == _ovr_uuid))
                    _ovr = _mr.scalar_one_or_none()
                    if (
                        _ovr
                        and _ovr.enabled
                        and _ovr.tenant_id
                        and (not self.llm_model or _ovr.tenant_id == self.llm_model.tenant_id)
                    ):
                        effective_llm_model = _ovr
                    else:
                        logger.warning(
                            f"[WS] model override {override_model_id} rejected (missing/disabled/tenant mismatch)"
                        )
            except (ValueError, TypeError):
                logger.warning(f"[WS] model override {override_model_id!r} is not a valid UUID")

        return effective_llm_model

    async def _check_quotas(self) -> bool:
        """Checks conversation and agent LLM quotas. Sends message and returns False if exceeded."""
        try:
            await check_conversation_quota(self.user.id)
            await check_agent_expired(self.agent_id)
            return True
        except QuotaExceeded as qe:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {qe.message}"})
            return False
        except AgentExpired as ae:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {ae.message}"})
            return False

    async def _save_user_message(self, content: str, display_content: str, file_name: str, is_onboarding_trigger: bool):
        """Saves user message to the database and updates session title/time."""
        has_image_marker = "[image_data:" in content
        if has_image_marker:
            saved_content = f"[file:{file_name}]\n{content}" if file_name else content
        else:
            saved_content = display_content if display_content else content
            if file_name:
                saved_content = f"[file:{file_name}]\n{saved_content}"

        if is_onboarding_trigger:
            logger.info("[WS] Onboarding trigger — skipping user-message persistence")
            async with async_session() as _sdb:
                _sr = await _sdb.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(self.conv_id)))
                _s = _sr.scalar_one_or_none()
                if _s and _s.title.startswith("Session "):
                    _s.title = "Onboarding"
                    await _sdb.commit()
        else:
            async with async_session() as db:
                user_msg = ChatMessage(
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    role="user",
                    content=saved_content,
                    conversation_id=self.conv_id,
                )
                db.add(user_msg)
                # Update session
                _now = datetime.now(tz.utc)
                _sess_r = await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(self.conv_id)))
                _sess = _sess_r.scalar_one_or_none()
                if _sess:
                    _sess.last_message_at = _now
                    if not self.history_messages and (
                        _sess.title.startswith("Session ") or _sess.title == "New Session"
                    ):
                        title_src = display_content if display_content else content
                        clean_title = title_src.replace("[图片] ", "📷 ").replace("[image_data:", "").strip()
                        if file_name and not clean_title:
                            clean_title = f"📎 {file_name}"
                        _sess.title = clean_title[:40] if clean_title else content[:40]
                await db.commit()
            logger.info("[WS] User message saved")

    async def _route_openclaw(self, content: str):
        """Enqueues message for OpenClaw edge node poll."""
        from app.models.gateway_message import GatewayMessage as GwMsg

        async with async_session() as db:
            gw_msg = GwMsg(
                agent_id=self.agent_id,
                sender_user_id=self.user.id,
                conversation_id=self.conv_id,
                content=content,
                status="pending",
            )
            db.add(gw_msg)
            await db.commit()
        logger.info("[WS] OpenClaw: message queued for gateway poll")
        await self.websocket.send_json(
            {
                "type": "done",
                "role": "assistant",
                "content": "Message forwarded to OpenClaw agent. Waiting for response...",
            }
        )

    async def _run_llm_and_stream(
        self, effective_llm_model: LLMModel, is_onboarding_trigger: bool
    ) -> tuple[str, list[str], list[dict]]:
        """Calls the LLM and streams response chunks to WebSocket."""
        start_gen = perf_counter()
        try:
            logger.info(f"[WS] Calling LLM {effective_llm_model.model} (streaming)...")

            # Accumulate partial content for abort handling
            partial_chunks: list[str] = []
            # Track how many characters of finish-tool content have been streamed
            finish_content_sent_len = 0

            # Set inside _call_with_failover when an onboarding prompt was injected
            needs_onboarding_mark = False
            onboarding_target_phase = "completed"
            onboarding_mark_done = False

            async def maybe_mark_onboarding_progress():
                nonlocal onboarding_mark_done
                if needs_onboarding_mark and not onboarding_mark_done:
                    onboarding_mark_done = True
                    try:
                        async with async_session() as _ob_db:
                            await mark_onboarding_phase(
                                _ob_db,
                                self.agent_id,
                                self.user.id,
                                onboarding_target_phase,
                            )
                        # Tell the frontend to refresh its cached agent record
                        await self._safe_send(
                            {
                                "type": "onboarded",
                                "agent_id": str(self.agent_id),
                            }
                        )
                    except Exception as _ob_err:
                        logger.warning(f"[WS] mark_onboarded failed (non-fatal): {_ob_err}")

            async def stream_to_ws(text: str):
                """Send each chunk to client in real-time."""
                partial_chunks.append(text)
                await self._safe_send({"type": "chunk", "content": text})
                await maybe_mark_onboarding_progress()

            async def tool_call_to_ws(data: dict):
                """Send tool call info to client and persist completed ones."""
                if data.get("status") in {"running", "done"}:
                    await maybe_mark_onboarding_progress()
                if data.get("status") == "done":
                    # Inject Live Preview & Workspace Activities
                    await self._inject_live_preview_and_workspace_metadata(data)

                # Output boundary: mask secrets for the client. `data` stays raw
                # below so persist_tool_call stores the real args (the LLM replays
                # them — masking storage poisons the model).
                _ws_data = {**data, "args": sanitize_tool_args(data.get("args"))} if "args" in data else data
                await self._safe_send({"type": "tool_call", **_ws_data})

                # Save completed tool calls to DB so they persist in chat history
                if data.get("status") == "done":
                    await self._save_completed_tool_call_to_db(data)

            # Track thinking content for storage
            thinking_content = []

            async def thinking_to_ws(text: str):
                """Send thinking chunks to client for collapsible display."""
                thinking_content.append(text)
                await self._safe_send({"type": "thinking", "content": text})

            _workspace_draft_cache: dict[str, str] = {}

            async def tool_delta_to_ws(data: dict):
                """Stream workspace file-operation drafts while tool args are still arriving."""
                nonlocal finish_content_sent_len
                tool_name = data.get("name", "")

                # Stream finish tool content as real-time chunks
                if tool_name == "finish":
                    raw_args = data.get("arguments", "")
                    if isinstance(raw_args, str) and raw_args:
                        current_content = extract_partial_content(raw_args)
                        if len(current_content) > finish_content_sent_len:
                            delta = current_content[finish_content_sent_len:]
                            finish_content_sent_len = len(current_content)
                            await stream_to_ws(delta)
                    return

                _ws_tools = {
                    "write_file",
                    "edit_file",
                    "move_file",
                    "delete_file",
                    "convert_markdown_to_docx",
                    "convert_csv_to_xlsx",
                    "convert_markdown_to_pdf",
                    "convert_html_to_pdf",
                    "convert_html_to_pptx",
                }
                if tool_name not in _ws_tools:
                    return

                raw_args = data.get("arguments", "")
                if isinstance(raw_args, (dict, list)):
                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                elif raw_args is None:
                    raw_args = ""
                else:
                    raw_args = str(raw_args)

                draft_id = str(data.get("id") or f"draft-{data.get('index', 0)}")
                if _workspace_draft_cache.get(draft_id) == raw_args:
                    return
                _workspace_draft_cache[draft_id] = raw_args

                await self._safe_send(
                    {
                        "type": "workspace_draft",
                        "id": draft_id,
                        "index": data.get("index", 0),
                        "name": tool_name,
                        "arguments": raw_args,
                    }
                )

            # Run call_llm_with_failover as a cancellable task
            async def _call_with_failover():
                nonlocal needs_onboarding_mark, onboarding_target_phase

                async def _on_failover(reason: str):
                    await self._safe_send({"type": "info", "content": f"Primary model error, {reason}"})

                # Pre-flight compaction: if the about-to-be-sent prompt is near the
                # model window, compact NOW and rebuild conversation from DB so THIS
                # request stays under it. The web conversation lives in memory across
                # a connection (not reloaded per turn), so without this rebuild a
                # single oversized turn would overflow before the post-round hook
                # could help. The current user message is already persisted, so the
                # rebuild includes it. Best-effort: failure leaves it untouched.
                try:
                    from app.services.llm.compactor import maybe_precompact_prompt

                    if await maybe_precompact_prompt(
                        agent_id=self.agent_id,
                        conversation_id=self.conv_id,
                        model=effective_llm_model,
                        prompt_messages=self.conversation[-self.ctx_size :],
                    ):
                        from app.services.chat_history import (
                            build_llm_messages_from_rows,
                            load_messages_for_session,
                        )
                        from app.services.image_context import rehydrate_image_messages

                        async with async_session() as _pf_db:
                            _pf_rows = await load_messages_for_session(
                                _pf_db, agent_id=self.agent_id, conversation_id=self.conv_id, ctx_size=self.ctx_size
                            )
                        # Same shared builder + image rehydration as the initial
                        # build, so vision context survives a compaction rebuild.
                        self.conversation = build_llm_messages_from_rows(_pf_rows, include_thinking=True)
                        self.conversation = rehydrate_image_messages(self.conversation, self.agent_id, max_images=3)
                except Exception as _pf_exc:
                    logger.warning(f"[WS] pre-flight compaction skipped (non-fatal): {_pf_exc}")

                # Drop orphan tool messages left if the ctx_size slice cut a
                # tool-call pair (shared guard with the IM history path).
                from app.services.chat_history import strip_leading_orphan_tool_messages

                _truncated = strip_leading_orphan_tool_messages(self.conversation[-self.ctx_size :])

                # Resolve onboarding prompt
                skip_tools_for_greeting = False
                try:
                    async with async_session() as _ob_db:
                        _onb = await resolve_onboarding_prompt(
                            _ob_db,
                            self.agent,
                            self.user.id,
                            user_name=self.user_display_name,
                            user_locale=self.lang,
                        )
                    if _onb:
                        _truncated = [{"role": "system", "content": _onb.prompt}] + _truncated
                        if _onb.lock_on_first_chunk:
                            needs_onboarding_mark = True
                            onboarding_target_phase = _onb.target_phase
                        if _onb.is_greeting_turn:
                            skip_tools_for_greeting = True
                except Exception as _onb_err:
                    logger.warning(f"[WS] Onboarding prompt resolve failed (non-fatal): {_onb_err}")

                live_code_chars_sent = 0
                live_code_truncated_sent = False

                async def code_output_to_ws(text: str, label: str = "stdout"):
                    """Stream execute_code output chunks to the frontend live panel in real-time."""
                    nonlocal live_code_chars_sent, live_code_truncated_sent
                    try:
                        remaining = MAX_LIVE_CODE_STREAM_CHARS - live_code_chars_sent
                        if remaining <= 0:
                            if not live_code_truncated_sent:
                                live_code_truncated_sent = True
                                await self.websocket.send_json(
                                    {
                                        "type": "agentbay_live",
                                        "env": "code",
                                        "output": LIVE_CODE_TRUNCATED_NOTICE,
                                        "stream": label,
                                    }
                                )
                            return

                        output = text[:remaining]
                        live_code_chars_sent += len(output)
                        await self.websocket.send_json(
                            {
                                "type": "agentbay_live",
                                "env": "code",
                                "output": output,
                                "stream": label,
                            }
                        )
                    except Exception:
                        pass

                return await call_llm_with_failover(
                    primary_model=effective_llm_model,
                    fallback_model=self.fallback_llm_model,
                    messages=_truncated,
                    agent_name=self.agent_name,
                    role_description=self.role_description,
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    session_id=self.conv_id,
                    on_chunk=stream_to_ws,
                    on_tool_call=tool_call_to_ws,
                    on_tool_delta=tool_delta_to_ws,
                    on_thinking=thinking_to_ws,
                    supports_vision=getattr(effective_llm_model, "supports_vision", False),
                    on_failover=_on_failover,
                    skip_tools=skip_tools_for_greeting,
                    on_code_output=code_output_to_ws,
                )

            llm_task = asyncio.create_task(_call_with_failover())

            # Drive the turn to completion while listening for abort / disconnect.
            # A disconnect does NOT cancel the turn — it runs to completion and is
            # persisted below, so a reconnecting client sees the reply via history
            # replay (parity with the connection-independent IM / trigger
            # channels). Only an explicit user abort cancels.
            queued_messages: list[dict] = []
            assistant_response, _turn_outcome = await _await_turn_with_abort(
                llm_task, self.websocket.receive_json, partial_chunks
            )
            aborted = _turn_outcome == "aborted"
            self.client_disconnected = _turn_outcome == "disconnected"
            if self.client_disconnected:
                logger.info(
                    f"[WS] Client disconnected mid-turn — turn finished detached, "
                    f"persisting reply: {str(assistant_response)[:80]}"
                )
            elif aborted:
                logger.info(f"[WS] LLM aborted, partial: {str(assistant_response)[:80]}")
            else:
                logger.info(f"[WS] LLM response: {str(assistant_response)[:80]}")

            # Raise error on prefix for failover matching
            _llm_error_prefixes = ("[LLM Error]", "[LLM call error]", "[Error]")
            if (
                not aborted
                and assistant_response
                and any(assistant_response.startswith(p) for p in _llm_error_prefixes)
            ):
                raise RuntimeError(assistant_response)

            # Post-success actions (last_active_at, quota usage increments, activity logs)
            await self._update_activity_and_quota(assistant_response)

            return assistant_response, thinking_content, queued_messages

        except WebSocketDisconnect:
            raise
        except Exception as e:
            gen_duration = perf_counter() - start_gen
            logger.exception(f"[WS] LLM error after {gen_duration:.3f}s: {e}")
            return f"[LLM call error] {str(e)[:200]}", [], []

    async def _inject_live_preview_and_workspace_metadata(self, data: dict):
        """Injects live previews and workspace panel activity tracking into tool results."""
        try:
            tool_name = data.get("name", "")
            env = detect_agentbay_env(tool_name)
            if env == "desktop":
                b64_url = await get_desktop_screenshot(self.agent_id, session_id=self.conv_id)
                if b64_url:
                    data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                    logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
            elif env == "browser":
                b64_url = await get_browser_snapshot(self.agent_id, session_id=self.conv_id)
                if b64_url:
                    data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                    logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
            elif env == "code":
                tool_result = data.get("result", "") or ""
                data["live_preview"] = {"env": "code", "output": tool_result[:5000]}
        except Exception as _lp_err:
            logger.warning(f"[WS][LivePreview] Embed failed: {_lp_err}")

        _workspace_tool_actions = {
            "write_file": "write",
            "edit_file": "edit",
            "move_file": "move",
            "delete_file": "delete",
            "convert_markdown_to_docx": "convert",
            "convert_csv_to_xlsx": "convert",
            "convert_markdown_to_pdf": "convert",
            "convert_html_to_pdf": "convert",
            "convert_html_to_pptx": "convert",
        }
        _done_tool_name = data.get("name", "")
        if _done_tool_name in _workspace_tool_actions:
            _ws_args = data.get("args") or {}
            if isinstance(_ws_args, str):
                try:
                    _ws_args = json.loads(_ws_args)
                except Exception:
                    _ws_args = {}
            _ws_path = _ws_args.get("output_path") or _ws_args.get("destination_path") or _ws_args.get("path", "")
            _ws_result = str(data.get("result") or "")
            _pending_approval = "requires approval" in _ws_result.lower()
            data["workspace_activity"] = {
                "action": _workspace_tool_actions[_done_tool_name],
                "path": _ws_path,
                "tool": _done_tool_name,
                "ok": not _pending_approval,
                "pendingApproval": _pending_approval,
            }
            logger.info(f"[WS][Workspace] activity: {_done_tool_name} → {_ws_path}")

    async def _save_completed_tool_call_to_db(self, data: dict):
        """Persist completed tool calls via the shared writer — the SAME canonical
        schema every IM channel uses (single source of truth: args stored RAW for
        LLM replay, masked only at output boundaries). Then mark the session read
        (web-only)."""
        from app.services.chat_history import persist_tool_call

        await persist_tool_call(
            async_session,
            agent_id=self.agent_id,
            user_id=self.user.id,
            conversation_id=self.conv_id,
            evt=data,
        )
        try:
            from app.services.chat_session_service import save_tool_call_log
            await save_tool_call_log(
                agent_id=self.agent_id,
                user_id=self.user.id,
                conversation_id=self.conv_id,
                tool_name=data.get("name", ""),
                arguments=data.get("args"),
                result=(data.get("result") or "")[:500],
                status="done",
                tool_call_id=data.get("call_id"),
                reasoning_content=data.get("reasoning_content"),
            )
            async with async_session() as _tc_db:
                await maybe_mark_session_read_for_active_viewer(
                    _tc_db,
                    agent_id=self.agent_id,
                    session_id=self.conv_id,
                    user_id=self.user.id,
                )
                await _tc_db.commit()
        except Exception as _tc_err:
            logger.warning(f"[WS] Failed to mark session read: {_tc_err}")

    async def _update_activity_and_quota(self, assistant_response: str):
        """Update last_active_at, conversation/agent LLM usage, and log activity."""
        try:
            async with async_session() as _db:
                _ar = await _db.execute(select(Agent).where(Agent.id == self.agent_id))
                _agent = _ar.scalar_one_or_none()
                if _agent:
                    _agent.last_active_at = datetime.now(tz.utc)
                    await _db.commit()
        except Exception as e:
            logger.warning(f"[WS] Failed to update last_active_at: {e}")

        try:
            await increment_conversation_usage(self.user.id)
            await increment_agent_llm_usage(self.agent_id)
        except Exception:
            pass

        try:
            user_text = getattr(self, "current_user_text", "")
            await log_activity(
                self.agent_id,
                "chat_reply",
                f"Replied to web chat: {assistant_response[:80]}",
                detail={"channel": "web", "user_text": user_text[:200], "reply": assistant_response[:500]},
            )
        except Exception as e:
            logger.warning(f"[WS] Failed to log activity: {e}")

    async def _create_task_record(self, task_title: str, assistant_response: str) -> str:
        """Creates a background execution task from task matching."""
        if not task_title:
            return assistant_response
        try:
            async with async_session() as db:
                task = Task(
                    agent_id=self.agent_id,
                    title=task_title,
                    created_by=self.user.id,
                    status="pending",
                    priority="medium",
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                logger.info(f"[WS] Task created: {task.id}")
                task_id = task.id
            asyncio.create_task(execute_task(task_id, self.agent_id))
            assistant_response += f"\n\n📋 Task synced to task board: [{task_title}]"
        except Exception as te:
            logger.error(f"[WS] Task creation failed: {te}")
        return assistant_response

    async def _save_assistant_reply(self, assistant_response: str, thinking_content: list[str]):
        """Saves assistant reply to DB."""
        async with async_session() as db:
            assistant_msg = ChatMessage(
                agent_id=self.agent_id,
                user_id=self.user.id,
                role="assistant",
                content=assistant_response,
                conversation_id=self.conv_id,
                thinking="".join(thinking_content) if thinking_content else None,
            )
            db.add(assistant_msg)
            await maybe_mark_session_read_for_active_viewer(
                db,
                agent_id=self.agent_id,
                session_id=self.conv_id,
                user_id=self.user.id,
            )
            await db.commit()
        logger.info("[WS] Assistant message saved")
