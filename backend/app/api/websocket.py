"""WebSocket chat endpoint for real-time agent conversations."""

import json
import uuid
from datetime import datetime, timezone as tz

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decode_access_token
from app.utils.sanitize import _is_secrets_file_path, sanitize_tool_args
from app.core.permissions import check_agent_access, is_agent_expired
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.user import User
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.llm import call_llm, call_llm_with_failover
from app.services.realtime import realtime_router

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
            if c == 'n':
                val_chars.append('\n')
            elif c == 't':
                val_chars.append('\t')
            elif c == 'r':
                val_chars.append('\r')
            elif c == 'b':
                val_chars.append('\b')
            elif c == 'f':
                val_chars.append('\f')
            elif c == '"':
                val_chars.append('"')
            elif c == '\\':
                val_chars.append('\\')
            elif c == '/':
                val_chars.append('/')
            elif c == 'u':
                if i + 4 < n:
                    try:
                        hex_val = int(s[i + 1:i + 5], 16)
                        val_chars.append(chr(hex_val))
                        i += 4
                    except ValueError:
                        val_chars.append('\\')
                        val_chars.append('u')
                else:
                    # Incomplete \uXXXX — wait for more data
                    val_chars.append('\\')
                    val_chars.append('u')
            else:
                val_chars.append(c)
            escaped = False
        else:
            if c == '\\':
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
    turn's streaming callbacks must tolerate a dead socket (see ``safe_send``)
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


from fastapi import Depends
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User


@router.get("/api/chat/{agent_id}/history")
async def get_chat_history(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return web chat message history for this user + agent."""
    conv_id = f"web_{current_user.id}"
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == conv_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(200)
    )
    messages = result.scalars().all()
    out = []
    for m in messages:
        entry: dict = {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None}
        if getattr(m, 'thinking', None):
            entry["thinking"] = m.thinking
        if m.role == "tool_call":
            # Parse JSON-encoded tool call data (shared helper tolerates both the
            # canonical name/args schema and legacy Feishu tool_name/arguments).
            from app.services.chat_history import parse_tool_call_for_display
            parsed = parse_tool_call_for_display(m.content)
            if parsed:
                entry["content"] = ""
                entry.update(parsed)
        out.append(entry)
    return out


@router.websocket("/ws/chat/{agent_id}")
async def websocket_chat(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    token: str = Query(...),
    session_id: str = Query(None),
    lang: str = Query("en"),
):
    """WebSocket endpoint for real-time chat with an agent.

    Flow:
    1. Client connects with JWT token + optional session_id as query params
    2. Server accepts immediately so browser onopen fires quickly
    3. Server authenticates and checks agent access
    4. If session_id provided, uses it; otherwise finds/creates the user's latest session
    5. Client sends messages as JSON: {"content": "..."}
    6. Server calls the agent's configured LLM and sends response back
    7. Messages are persisted to chat_messages table under the session
    """
    # Accept immediately so browser sees onopen without waiting for DB setup
    await websocket.accept()

    # Authenticate
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except Exception:
        await websocket.send_json({"type": "error", "content": "Authentication failed"})
        await websocket.close(code=4001)
        return

    # Verify access and load agent + model
    agent_name = ""
    agent_type = ""  # Track agent type for OpenClaw routing
    role_description = ""
    welcome_message = ""
    llm_model = None
    fallback_llm_model = None
    history_messages = []

    try:
        async with async_session() as db:
            logger.info(f"[WS] Looking up user {user_id}")
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                logger.info("[WS] User not found")
                await websocket.send_json({"type": "error", "content": "User not found"})
                await websocket.close(code=4001)
                return

            logger.info(f"[WS] Checking agent access for {agent_id}")
            agent, _ = await check_agent_access(db, user, agent_id)
            # Check agent expiry
            if is_agent_expired(agent):
                await websocket.send_json({"type": "error", "content": "This Agent has expired and is off duty. Please contact your admin to extend its service."})
                await websocket.close(code=4003)
                return
            agent_name = agent.name
            agent_type = agent.agent_type or ""
            role_description = agent.role_description or ""
            welcome_message = agent.welcome_message or ""
            ctx_size = agent.context_window_size or 100
            # Captured for onboarding lookups — the DB-bound `agent` goes out
            # of scope when this session block closes.
            agent_snapshot = agent
            user_display_name = (user.display_name or "").strip() or "there"
            logger.info(f"[WS] Agent: {agent_name}, type: {agent_type}, model_id: {agent.primary_model_id}, ctx: {ctx_size}")

            # Load the agent's primary model
            if agent.primary_model_id:
                model_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id)
                )
                llm_model = model_result.scalar_one_or_none()
                if llm_model and not llm_model.enabled:
                    logger.info(f"[WS] Primary model {llm_model.model} is disabled, skipping")
                    llm_model = None
                else:
                    logger.info(f"[WS] Primary model loaded: {llm_model.model if llm_model else 'None'}")

            # Load fallback model
            if agent.fallback_model_id:
                fb_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.fallback_model_id)
                )
                fallback_llm_model = fb_result.scalar_one_or_none()
                if fallback_llm_model and not fallback_llm_model.enabled:
                    logger.info(f"[WS] Fallback model {fallback_llm_model.model} is disabled, skipping")
                    fallback_llm_model = None
                elif fallback_llm_model:
                    logger.info(f"[WS] Fallback model loaded: {fallback_llm_model.model}")

            # Config-level fallback: primary missing -> use fallback
            if not llm_model and fallback_llm_model:
                llm_model = fallback_llm_model
                fallback_llm_model = None  # No further fallback available
                logger.info(f"[WS] Primary model unavailable, using fallback: {llm_model.model}")

            # Resolve or create chat session
            from app.models.chat_session import ChatSession
            from sqlalchemy import select as _sel
            from datetime import datetime as _dt, timezone as _tz
            conv_id = session_id
            if conv_id:
                # Validate the session belongs to this agent and to this user.
                try:
                    _sid = uuid.UUID(conv_id)
                except (ValueError, TypeError):
                    conv_id = None
                    _existing = None
                else:
                    _sr = await db.execute(
                        _sel(ChatSession).where(
                            ChatSession.id == _sid,
                            ChatSession.agent_id == agent_id,
                        )
                    )
                    _existing = _sr.scalar_one_or_none()
                    if not _existing:
                        conv_id = None
                    elif _existing.source_channel != "agent" and str(_existing.user_id) != str(user_id):
                        await websocket.send_json({"type": "error", "content": "Not authorized for this session"})
                        await websocket.close(code=4003)
                        return
            if not conv_id:
                # Prefer the user's designated primary platform session. This keeps agent-initiated
                # conversations and ongoing long-form context anchored in one stable thread, while
                # user-created side sessions remain temporary.
                _sr = await db.execute(
                    _sel(ChatSession)
                    .where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.user_id == user_id,
                        ChatSession.source_channel == "web",
                        ChatSession.is_group == False,
                        ChatSession.is_primary == True,
                    )
                    .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
                    .limit(1)
                )
                _latest = _sr.scalar_one_or_none()
                if _latest:
                    conv_id = str(_latest.id)
                else:
                    # Lazily elect or create the primary session only when it is actually needed.
                    _new_session = await ensure_primary_platform_session(db, agent_id, user_id)
                    await db.commit()
                    await db.refresh(_new_session)
                    conv_id = str(_new_session.id)
                    logger.info(f"[WS] Selected primary session {conv_id}")

            try:
                from app.services.chat_history import load_messages_for_session
                history_messages = await load_messages_for_session(
                    db,
                    agent_id=agent_id,
                    conversation_id=conv_id,
                    ctx_size=ctx_size,
                )
                logger.info(f"[WS] Loaded {len(history_messages)} history messages for session {conv_id}")
            except Exception as e:
                logger.warning(f"[WS] History load failed (non-fatal): {e}")
    except Exception as e:
        logger.error(f"[WS] Setup error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        await websocket.send_json({"type": "error", "content": "Setup failed"})
        await websocket.close(code=4002)  # Config error — client should NOT retry
        return

    agent_id_str = str(agent_id)
    # Defense-in-depth against reconnect churn: prune dead sockets before
    # registering so a flapping client can't pile up stale tuples (each would get
    # duplicate pushes from send_to_session/send_to_user). Checks BOTH states —
    # client_state catches a client that already dropped (relevant now that a
    # turn runs to completion *after* disconnect, leaving the old handler — and
    # its tuple — lingering for seconds), application_state catches one the
    # server has closed. A live CONNECTED socket has neither flag set, so
    # multi-tab connections are preserved; only genuinely-dead ones are dropped.
    from starlette.websockets import WebSocketState as _WSState

    def _ws_dead(ws) -> bool:
        return (
            getattr(ws, "client_state", None) == _WSState.DISCONNECTED
            or getattr(ws, "application_state", None) == _WSState.DISCONNECTED
        )

    _conns = manager.active_connections.setdefault(agent_id_str, [])
    _conns[:] = [(ws, sid, uid) for ws, sid, uid in _conns if not _ws_dead(ws)]
    # connect() appends the tuple AND registers presence with the realtime
    # router (Redis-backed cross-instance routing for HA) — must go through it,
    # not a bare append, or this socket is invisible to other instances.
    await manager.connect(agent_id_str, websocket, conv_id, str(user_id))
    logger.info(f"[WS] Ready! Agent={agent_name} (live conns for agent: {len(_conns)})")

    # Send session_id to frontend so Take Control can reference the correct session.
    await websocket.send_json({"type": "connected", "session_id": conv_id})

    # Build conversation context from history via the shared row→message builder
    # — the SAME mapping the IM channels use (tool_call rows expand to the
    # assistant(tool_calls)+tool(result) pair; web replays model thinking).
    from app.services.chat_history import build_llm_messages_from_rows
    conversation: list[dict] = build_llm_messages_from_rows(history_messages, include_thinking=True)

    # Re-hydrate historical images for multi-turn LLM context
    from app.services.image_context import rehydrate_image_messages
    conversation = rehydrate_image_messages(conversation, agent_id, max_images=3)

    try:
        # Send welcome message on new session (no history)
        if welcome_message and not history_messages:
            await websocket.send_json({"type": "done", "role": "assistant", "content": welcome_message})

        while True:
            logger.info(f"[WS] Waiting for message from {agent_name}...")
            data = await websocket.receive_json()

            # Set a unique trace ID for this specific message processing.
            from app.core.logging_config import set_trace_id
            import uuid as _trace_uuid
            trace_id = str(_trace_uuid.uuid4())[:12]
            set_trace_id(trace_id)

            content = data.get("content", "")
            display_content = data.get("display_content", "")  # User-facing display text
            file_name = data.get("file_name", "")  # Original file name for attachment display
            override_model_id = data.get("model_id")  # Optional per-turn model switcher
            # When the frontend fires an onboarding trigger for a (user, agent)
            # pair that hasn't met before, it tags the message so the server can
            # (a) skip persisting a user-side turn and (b) not echo any user
            # bubble — the agent opens the conversation itself.
            is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
            logger.info(f"[WS] Received: {content[:50]}" + (" [onboarding]" if is_onboarding_trigger else ""))

            if not content and not is_onboarding_trigger:
                continue
            if is_onboarding_trigger:
                # Guard against stale triggers. A frontend with a cached
                # agent query from before the ritual completed can fire an
                # onboarding_trigger on a new session even though the pair
                # is already locked. In that case the resolver would return
                # no prompt, but the placeholder "Please begin the
                # onboarding" would still reach the LLM and the agent would
                # dutifully restart the ritual. Short-circuit here, emit an
                # event so the frontend refreshes its cache, and move on.
                from app.services.onboarding import is_onboarded as _is_onboarded
                async with async_session() as _gdb:
                    if await _is_onboarded(_gdb, agent_id, user_id):
                        logger.info("[WS] Onboarding trigger ignored — pair already onboarded")
                        await websocket.send_json({
                            "type": "onboarded",
                            "agent_id": str(agent_id),
                        })
                        continue
                # Minimal placeholder so the LLM has a valid user turn to anchor
                # its greeting. The onboarding system prompt is what actually
                # drives the reply; this text is never shown or saved.
                content = "Please begin the onboarding."

            # Per-message model override — the chat dropdown lets users pick a
            # different tenant-scoped model for this session. Override only the
            # current turn; nothing is persisted, and it resets when Chat.tsx
            # remounts.
            effective_llm_model = llm_model
            if override_model_id:
                try:
                    _ovr_uuid = uuid.UUID(str(override_model_id))
                    async with async_session() as _mdb:
                        _mr = await _mdb.execute(select(LLMModel).where(LLMModel.id == _ovr_uuid))
                        _ovr = _mr.scalar_one_or_none()
                        if _ovr and _ovr.enabled and _ovr.tenant_id and (
                            not llm_model or _ovr.tenant_id == llm_model.tenant_id
                        ):
                            effective_llm_model = _ovr
                        else:
                            logger.warning(f"[WS] model override {override_model_id} rejected (missing/disabled/tenant mismatch)")
                except (ValueError, TypeError):
                    logger.warning(f"[WS] model override {override_model_id!r} is not a valid UUID")

            # ── Quota checks ──
            try:
                from app.services.quota_guard import (
                    check_conversation_quota, increment_conversation_usage,
                    check_agent_expired, check_agent_llm_quota, increment_agent_llm_usage,
                    QuotaExceeded, AgentExpired,
                )
                await check_conversation_quota(user_id)
                await check_agent_expired(agent_id)
            except QuotaExceeded as qe:
                await websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {qe.message}"})
                continue
            except AgentExpired as ae:
                await websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {ae.message}"})
                continue

            # Add user message to conversation (full LLM context)
            conversation.append({"role": "user", "content": content})

            # Save user message to DB.
            #
            # Bootstrap trigger: the user never sent anything — the frontend
            # fired a synthetic turn so the agent could greet first. Don't
            # persist and don't title the session from it.
            #
            # If the LLM content contains [image_data:...] markers, persist the full
            # payload so subsequent turns can still forward the image to the model.
            has_image_marker = "[image_data:" in content
            if has_image_marker:
                saved_content = f"[file:{file_name}]\n{content}" if file_name else content
            else:
                saved_content = display_content if display_content else content
                if file_name:
                    saved_content = f"[file:{file_name}]\n{saved_content}"
            if is_onboarding_trigger:
                logger.info("[WS] Onboarding trigger — skipping user-message persistence")
                # Title this session "Onboarding" up front so it's identifiable
                # in the session list even before the user has typed anything.
                # The auto-title logic in the normal path only overwrites titles
                # that start with "Session ", so this stays sticky.
                async with async_session() as _sdb:
                    from app.models.chat_session import ChatSession as _CS
                    _sr = await _sdb.execute(
                        select(_CS).where(_CS.id == uuid.UUID(conv_id))
                    )
                    _s = _sr.scalar_one_or_none()
                    if _s and _s.title.startswith("Session "):
                        _s.title = "Onboarding"
                        await _sdb.commit()
            else:
                async with async_session() as db:
                    user_msg = ChatMessage(
                        agent_id=agent_id,
                        user_id=user_id,
                        role="user",
                        content=saved_content,
                        conversation_id=conv_id,
                    )
                    db.add(user_msg)
                    # Update session last_message_at + auto-title on first message
                    from app.models.chat_session import ChatSession as _CS
                    from datetime import datetime as _dt2, timezone as _tz2
                    _now = _dt2.now(_tz2.utc)
                    _sess_r = await db.execute(
                        select(_CS).where(_CS.id == uuid.UUID(conv_id))
                    )
                    _sess = _sess_r.scalar_one_or_none()
                    if _sess:
                        _sess.last_message_at = _now
                        if not history_messages and (_sess.title.startswith("Session ") or _sess.title == "New Session"):
                            # Use display_content for title (avoids raw base64/markers)
                            title_src = display_content if display_content else content
                            # Clean up common prefixes from image/file messages
                            clean_title = title_src.replace("[图片] ", "📷 ").replace("[image_data:", "").strip()
                            if file_name and not clean_title:
                                clean_title = f"📎 {file_name}"
                            _sess.title = clean_title[:40] if clean_title else content[:40]
                    await db.commit()
                logger.info("[WS] User message saved")

            # ── OpenClaw routing: insert into gateway_messages instead of LLM ──
            if agent_type == "openclaw":
                from app.models.gateway_message import GatewayMessage as GwMsg
                async with async_session() as db:
                    gw_msg = GwMsg(
                        agent_id=agent_id,
                        sender_user_id=user_id,
                        conversation_id=conv_id,
                        content=content,
                        status="pending",
                    )
                    db.add(gw_msg)
                    await db.commit()
                logger.info("[WS] OpenClaw: message queued for gateway poll")
                await websocket.send_json({
                    "type": "done",
                    "role": "assistant",
                    "content": "Message forwarded to OpenClaw agent. Waiting for response..."
                })
                continue

            # Detect task creation intent
            import re
            task_match = re.search(
                r'(?:创建|新建|添加|建一个|帮我建|create|add)(?:一个|a )?(?:任务|待办|todo|task)[，,：：:\\s]*(.+)',
                content, re.IGNORECASE
            )

            # Track thinking content for storage (initialize before condition)
            thinking_content = []
            queued_messages: list[dict] = []

            # Reload model config on every message so Settings changes take effect
            # immediately without requiring a page refresh / WebSocket reconnect.
            async with async_session() as _mdb:
                _agent_r = await _mdb.execute(select(Agent).where(Agent.id == agent_id))
                _agent_cur = _agent_r.scalar_one_or_none()
                if _agent_cur:
                    if _agent_cur.primary_model_id:
                        _m_r = await _mdb.execute(select(LLMModel).where(LLMModel.id == _agent_cur.primary_model_id))
                        _m = _m_r.scalar_one_or_none()
                        llm_model = _m if (_m and _m.enabled) else None
                    else:
                        llm_model = None
                    if _agent_cur.fallback_model_id:
                        _fb_r = await _mdb.execute(select(LLMModel).where(LLMModel.id == _agent_cur.fallback_model_id))
                        _fb = _fb_r.scalar_one_or_none()
                        fallback_llm_model = _fb if (_fb and _fb.enabled) else None
                    else:
                        fallback_llm_model = None
                    # Config-level fallback: primary missing → use fallback immediately
                    if not llm_model and fallback_llm_model:
                        llm_model = fallback_llm_model
                        fallback_llm_model = None

            # Set true when the browser dropped mid-turn: the turn still runs to
            # completion + persists, then we tear the handler down cleanly.
            client_disconnected = False

            # Broadcast turn output to EVERY live connection viewing this session
            # — not just the socket that started the turn — so other tabs and
            # freshly-opened views of an in-flight session see the stream live
            # (the "connection = subscriber" model). send_to_session is
            # best-effort per connection and tolerates zero live connections, so:
            #   (a) the turn still runs to completion + persists after the driver
            #       drops mid-turn (the disconnect fix), and
            #   (b) a single-tab session behaves exactly as before — the lone
            #       driving socket is the only subscriber.
            # Defined outside the model branch so the final `done` always uses it.
            async def safe_send(payload: dict):
                try:
                    await manager.send_to_session(str(agent_id), conv_id, payload)
                except Exception:
                    pass

            # Call LLM with streaming
            if effective_llm_model:
                try:
                    logger.info(f"[WS] Calling LLM {effective_llm_model.model} (streaming)...")
                    
                    # Accumulate partial content for abort handling
                    partial_chunks: list[str] = []
                    # Track how many characters of finish-tool content have been streamed
                    finish_content_sent_len = 0

                    # Set inside _call_with_failover when an onboarding prompt
                    # was injected for this turn. The first streamed chunk then
                    # writes the target phase: "greeted" after the hidden
                    # greeting trigger, "completed" after the first real
                    # calibration reply.
                    needs_onboarding_mark = False
                    onboarding_target_phase = "completed"
                    onboarding_mark_done = False

                    async def maybe_mark_onboarding_progress():
                        nonlocal onboarding_mark_done
                        if needs_onboarding_mark and not onboarding_mark_done:
                            onboarding_mark_done = True
                            try:
                                from app.services.onboarding import mark_onboarding_phase
                                async with async_session() as _ob_db:
                                    await mark_onboarding_phase(
                                        _ob_db,
                                        agent_id,
                                        user_id,
                                        onboarding_target_phase,
                                    )
                                # Tell the frontend to refresh its cached agent
                                # record so subsequent sessions (or other open
                                # tabs) see onboarded_for_me=true and skip the
                                # kickoff effect.
                                await safe_send({
                                    "type": "onboarded",
                                    "agent_id": str(agent_id),
                                })
                            except Exception as _ob_err:
                                logger.warning(f"[WS] mark_onboarded failed (non-fatal): {_ob_err}")

                    async def stream_to_ws(text: str):
                        """Send each chunk to client in real-time."""
                        partial_chunks.append(text)
                        await safe_send({"type": "chunk", "content": text})
                        await maybe_mark_onboarding_progress()
                    
                    async def tool_call_to_ws(data: dict):
                        """Send tool call info to client and persist completed ones."""
                        if data.get("status") in {"running", "done"}:
                            await maybe_mark_onboarding_progress()
                        if data.get("status") == "done":
                            try:
                                from app.services.agentbay_live import detect_agentbay_env, get_desktop_screenshot, get_browser_snapshot

                                tool_name = data.get("name", "")
                                env = detect_agentbay_env(tool_name)
                                if env == "desktop":
                                    b64_url = await get_desktop_screenshot(agent_id, session_id=conv_id)
                                    if b64_url:
                                        data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                                        logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
                                elif env == "browser":
                                    b64_url = await get_browser_snapshot(agent_id, session_id=conv_id)
                                    if b64_url:
                                        data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                                        logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
                                elif env == "code":
                                    tool_result = data.get("result", "") or ""
                                    data["live_preview"] = {"env": "code", "output": tool_result[:5000]}
                            except Exception as _lp_err:
                                logger.warning(f"[WS][LivePreview] Embed failed: {_lp_err}")

                            # Attach workspace_activity so the frontend WorkspaceOperationPanel
                            # auto-opens when the agent writes, edits, or converts a file.
                            # PR #419 added the frontend logic but the backend never emitted
                            # this event — this is the missing piece.
                            _WORKSPACE_TOOL_ACTIONS: dict[str, str] = {
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
                            if _done_tool_name in _WORKSPACE_TOOL_ACTIONS:
                                _ws_args = data.get("args") or {}
                                if isinstance(_ws_args, str):
                                    try:
                                        import json as _json_wsa
                                        _ws_args = _json_wsa.loads(_ws_args)
                                    except Exception:
                                        _ws_args = {}
                                _ws_path = _ws_args.get("output_path") or _ws_args.get("destination_path") or _ws_args.get("path", "")
                                _ws_result = str(data.get("result") or "")
                                _pending_approval = "requires approval" in _ws_result.lower()
                                data["workspace_activity"] = {
                                    "action": _WORKSPACE_TOOL_ACTIONS[_done_tool_name],
                                    "path": _ws_path,
                                    "tool": _done_tool_name,
                                    "ok": not _pending_approval,
                                    "pendingApproval": _pending_approval,
                                }
                                logger.info(f"[WS][Workspace] activity: {_done_tool_name} → {_ws_path}")

                        # Output boundary: mask secrets for the client. `data` stays
                        # raw below so persist_tool_call stores the real args (the LLM
                        # replays them — masking storage poisons the model).
                        _ws_data = {**data, "args": sanitize_tool_args(data.get("args"))} if "args" in data else data
                        await safe_send({"type": "tool_call", **_ws_data})
                        # Persist completed tool calls via the shared writer — the
                        # SAME canonical schema every IM channel uses (single source
                        # of truth: args stored RAW for LLM replay, masked only at
                        # output boundaries). Then mark the session read (web-only).
                        if data.get("status") == "done":
                            from app.services.chat_history import persist_tool_call
                            await persist_tool_call(
                                async_session,
                                agent_id=agent_id,
                                user_id=user_id,
                                conversation_id=conv_id,
                                evt=data,
                            )
                            try:
                                async with async_session() as _tc_db:
                                    await maybe_mark_session_read_for_active_viewer(
                                        _tc_db,
                                        agent_id=agent_id,
                                        session_id=conv_id,
                                        user_id=user_id,
                                    )
                                    await _tc_db.commit()
                            except Exception as _tc_err:
                                logger.warning(f"[WS] Failed to mark session read: {_tc_err}")
                    
                    # Track thinking content for storage
                    thinking_content = []
                    
                    async def thinking_to_ws(text: str):
                        """Send thinking chunks to client for collapsible display."""
                        thinking_content.append(text)
                        await safe_send({"type": "thinking", "content": text})

                    _workspace_draft_cache: dict[str, str] = {}

                    async def tool_delta_to_ws(data: dict):
                        """Stream workspace file-operation drafts while tool args are still arriving.

                        Also intercepts the 'finish' tool to forward its content
                        argument as real-time chunk packets so the final response
                        streams to the user.
                        """
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

                        if tool_name not in {
                            "write_file",
                            "edit_file",
                            "move_file",
                            "delete_file",
                            "convert_markdown_to_docx",
                            "convert_csv_to_xlsx",
                            "convert_markdown_to_pdf",
                            "convert_html_to_pdf",
                            "convert_html_to_pptx",
                        }:
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

                        await safe_send(
                            {
                                "type": "workspace_draft",
                                "id": draft_id,
                                "index": data.get("index", 0),
                                "name": tool_name,
                                "arguments": raw_args,
                            }
                        )

                    import asyncio as _aio

                    # Run call_llm_with_failover as a cancellable task
                    async def _call_with_failover():
                        nonlocal needs_onboarding_mark, onboarding_target_phase, conversation

                        async def _on_failover(reason: str):
                            await safe_send({"type": "info", "content": f"Primary model error, {reason}"})

                        # Pre-flight compaction: if the about-to-be-sent prompt is
                        # near the model window, compact NOW and rebuild conversation
                        # from DB so THIS request stays under it. The web conversation
                        # lives in memory across a connection (not reloaded per turn),
                        # so without this rebuild a single oversized turn would
                        # overflow before the post-round hook could help. The current
                        # user message is already persisted, so the rebuild includes
                        # it. Best-effort: any failure leaves conversation untouched.
                        # (Historical image re-inlining is skipped on this rare path.)
                        try:
                            from app.services.llm.compactor import maybe_precompact_prompt

                            if await maybe_precompact_prompt(
                                agent_id=agent_id,
                                conversation_id=conv_id,
                                model=effective_llm_model,
                                prompt_messages=conversation[-ctx_size:],
                            ):
                                from app.services.chat_history import (
                                    build_llm_messages_from_rows,
                                    load_messages_for_session,
                                )

                                async with async_session() as _pf_db:
                                    _pf_rows = await load_messages_for_session(
                                        _pf_db, agent_id=agent_id, conversation_id=conv_id, ctx_size=ctx_size
                                    )
                                # Same shared builder + image rehydration as the
                                # initial build above, so vision context survives a
                                # compaction-triggered rebuild.
                                conversation = build_llm_messages_from_rows(_pf_rows, include_thinking=True)
                                from app.services.image_context import rehydrate_image_messages
                                conversation = rehydrate_image_messages(conversation, agent_id, max_images=3)
                        except Exception as _pf_exc:
                            logger.warning(f"[WS] pre-flight compaction skipped (non-fatal): {_pf_exc}")

                        # Drop orphan tool messages left if the ctx_size slice cut a
                        # tool-call pair (shared guard with the IM history path).
                        from app.services.chat_history import strip_leading_orphan_tool_messages
                        _truncated = strip_leading_orphan_tool_messages(conversation[-ctx_size:])

                        # Per-(user, agent) onboarding. With no row, prepend the
                        # greeting prompt and mark the pair as "greeted" once it
                        # starts streaming. With phase="greeted", prepend the
                        # configuration prompt to the user's first real reply
                        # and mark the pair as "completed" once that reply
                        # starts streaming.
                        from app.services.onboarding import resolve_onboarding_prompt
                        skip_tools_for_greeting = False
                        try:
                            async with async_session() as _ob_db:
                                _onb = await resolve_onboarding_prompt(
                                    _ob_db, agent_snapshot, user_id,
                                    user_name=user_display_name,
                                    user_locale=lang,
                                )
                            if _onb:
                                _truncated = [{"role": "system", "content": _onb.prompt}] + _truncated
                                if _onb.lock_on_first_chunk:
                                    needs_onboarding_mark = True
                                    onboarding_target_phase = _onb.target_phase
                                # Greeting turn produces a templated reply that
                                # never calls tools, so suppress the tool list
                                # to cut prompt size by ~50% and improve TTFT.
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
                                        await websocket.send_json({
                                            "type": "agentbay_live",
                                            "env": "code",
                                            "output": LIVE_CODE_TRUNCATED_NOTICE,
                                            "stream": label,
                                        })
                                    return

                                output = text[:remaining]
                                live_code_chars_sent += len(output)
                                await websocket.send_json({
                                    "type": "agentbay_live",
                                    "env": "code",
                                    "output": output,
                                    "stream": label,
                                })
                            except Exception:
                                pass

                        return await call_llm_with_failover(
                            primary_model=effective_llm_model,
                            fallback_model=fallback_llm_model,
                            messages=_truncated,
                            agent_name=agent_name,
                            role_description=role_description,
                            agent_id=agent_id,
                            user_id=user_id,
                            session_id=conv_id,
                            on_chunk=stream_to_ws,
                            on_tool_call=tool_call_to_ws,
                            on_tool_delta=tool_delta_to_ws,
                            on_thinking=thinking_to_ws,
                            supports_vision=getattr(effective_llm_model, 'supports_vision', False),
                            on_failover=_on_failover,
                            skip_tools=skip_tools_for_greeting,
                            on_code_output=code_output_to_ws,
                        )

                    llm_task = _aio.create_task(_call_with_failover())

                    # Drive the turn to completion while listening for abort /
                    # disconnect. A disconnect does NOT cancel the turn — it runs
                    # to completion and is persisted below, so a reconnecting
                    # client sees the reply via history replay (parity with the
                    # connection-independent IM / trigger channels). Only an
                    # explicit user abort cancels.
                    assistant_response, _turn_outcome = await _await_turn_with_abort(
                        llm_task, websocket.receive_json, partial_chunks
                    )
                    aborted = _turn_outcome == "aborted"
                    client_disconnected = _turn_outcome == "disconnected"
                    if client_disconnected:
                        logger.info(
                            f"[WS] Client disconnected mid-turn — turn finished detached, "
                            f"persisting reply: {str(assistant_response)[:80]}"
                        )
                    elif aborted:
                        logger.info(f"[WS] LLM aborted, partial: {str(assistant_response)[:80]}")
                    else:
                        logger.info(f"[WS] LLM response: {str(assistant_response)[:80]}")

                    # call_llm returns error strings instead of raising — detect and
                    # re-raise so the fallback model logic below can trigger correctly.
                    _LLM_ERROR_PREFIXES = ("[LLM Error]", "[LLM call error]", "[Error]")
                    if not aborted and assistant_response and any(
                        assistant_response.startswith(p) for p in _LLM_ERROR_PREFIXES
                    ):
                        raise RuntimeError(assistant_response)

                    # Update last_active_at. The onboarding lock is handled
                    # earlier in stream_to_ws on the first streamed chunk, so
                    # there's nothing to reconcile here anymore.
                    from datetime import datetime, timezone as tz
                    async with async_session() as _db:
                        from app.models.agent import Agent as AgentModel
                        _ar = await _db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                        _agent = _ar.scalar_one_or_none()
                        if _agent:
                            _agent.last_active_at = datetime.now(tz.utc)
                            await _db.commit()

                    # Increment quota usage
                    try:
                        await increment_conversation_usage(user_id)
                        await increment_agent_llm_usage(agent_id)
                    except Exception:
                        pass

                    # Log activity
                    from app.services.activity_logger import log_activity
                    await log_activity(agent_id, "chat_reply", f"Replied to web chat: {assistant_response[:80]}", detail={"channel": "web", "user_text": content[:200], "reply": assistant_response[:500]})
                except WebSocketDisconnect:
                    raise
                except Exception as e:
                    logger.error(f"[WS] LLM error: {e}")
                    import traceback
                    traceback.print_exc()
                    assistant_response = f"[LLM call error] {str(e)[:200]}"
            else:
                assistant_response = f"⚠️ {agent_name} has no LLM model configured. Please select a model in the agent's Settings tab."

            # If task creation detected, create a real Task record
            if task_match:
                task_title = task_match.group(1).strip()
                if task_title:
                    try:
                        from app.models.task import Task
                        from app.services.task_executor import execute_task
                        import asyncio as _asyncio
                        async with async_session() as db:
                            task = Task(
                                agent_id=agent_id,
                                title=task_title,
                                created_by=user_id,
                                status="pending",
                                priority="medium",
                            )
                            db.add(task)
                            await db.commit()
                            await db.refresh(task)
                            logger.info(f"[WS] Task created: {task.id}")
                            # Trigger background execution
                            task_id = task.id
                        _asyncio.create_task(execute_task(task_id, agent_id))
                        assistant_response += f"\n\n📋 Task synced to task board: [{task_title}]"
                    except Exception as te:
                        logger.error(f"[WS] Task creation failed: {te}")

            # Add assistant response to in-memory conversation for subsequent turns.
            conversation.append({"role": "assistant", "content": assistant_response})

            # Save assistant reply
            async with async_session() as db:
                assistant_msg = ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content=assistant_response,
                    conversation_id=conv_id,
                    thinking="".join(thinking_content) if thinking_content else None,
                )
                db.add(assistant_msg)
                await maybe_mark_session_read_for_active_viewer(
                    db,
                    agent_id=agent_id,
                    session_id=conv_id,
                    user_id=user_id,
                )
                await db.commit()
            logger.info("[WS] Assistant message saved")

            # Final 'done' packet — best-effort; a client that dropped mid-turn
            # gets the reply via history replay on reconnect instead.
            await safe_send({"type": "done", "role": "assistant", "content": assistant_response})

            # The browser dropped mid-turn: we finished + persisted the reply
            # detached. Tear down cleanly instead of looping back into receive
            # (which would raise) — same cleanup as the WebSocketDisconnect path.
            if client_disconnected:
                logger.info(f"[WS] Detached turn complete after disconnect; closing handler for {user_id}")
                manager.disconnect(str(agent_id), websocket)
                break

    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected: {user_id}")
        await manager.disconnect(str(agent_id), websocket)
    except Exception as e:
        logger.error(f"[WS] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        await manager.disconnect(str(agent_id), websocket)
