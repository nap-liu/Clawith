"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone as tz
from time import perf_counter
from typing import Any


from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import set_trace_id
from app.core.permissions import (
    can_view_all_agent_chat_sessions,
    check_agent_access,
    is_agent_expired,
    require_current_agent_tenant,
    require_tenant_safe_chat_session,
)
from app.core.security import decode_access_token
from app.database import async_session
from app.models.agent import Agent, AgentUserOnboarding
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.task import Task
from app.models.user import User
from app.services.activity_logger import log_activity
from app.services.agentbay_live import detect_agentbay_env, get_browser_snapshot, get_desktop_screenshot
from app.services.auth_code_exchange import validate_platform_login_channel
from app.services.chat_history import persist_initial_assistant_message_if_pristine
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.confirmation_service import PendingConfirmation
from app.services.llm.runtime_model import RuntimeLLMModel
from app.services.llm import call_llm_with_failover
from app.services.onboarding import (
    PHASE_COMPLETED,
    PHASE_GREETED,
    PHASE_PENDING,
    OnboardingClaim,
    claim_fixed_welcome_slot,
    claim_normal_first_turn,
    claim_onboarding_greeting,
    mark_onboarding_phase,
    onboarding_claim_is_current,
    release_onboarding_claim,
    resolve_onboarding_eligibility,
    resolve_onboarding_prompt,
)
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
ONBOARDING_LEASE_REFRESH_SECONDS = 30.0
LLM_FAILURE_PREFIXES = (
    "[LLM Error]",
    "[LLM call error]",
    "[Error]",
    "[LLM returned empty content]",
)


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
    channel: str = Query("web"),
    scene: str | None = Query(None),
):
    """WebSocket endpoint for real-time chat with an agent."""
    handler = WebSocketChatHandler(websocket, agent_id, token, session_id, lang, channel, scene)
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
        channel: str = "web",
        scene: str | None = None,
    ):
        self.websocket = websocket
        self.agent_id = agent_id
        self.token = token
        self.session_id_param = session_id
        self.lang = lang
        self.source_channel = validate_platform_login_channel(channel)
        self.scene_key = scene
        self.scene_manifest: dict | None = None
        self.pending_initial_assistant: dict | None = None

        # State fields initialized during setup
        self.user_id: uuid.UUID | None = None
        self.agent_name: str = ""
        self.agent_type: str = ""
        self.role_description: str = ""
        self.welcome_message: str = ""
        self.ctx_size: int = 100
        self.user_display_name: str = ""
        self.llm_model: RuntimeLLMModel | None = None
        self.fallback_llm_model: RuntimeLLMModel | None = None
        self.conv_id: str | None = None
        # Read-only monitor: viewer is watching a session they do NOT own but are
        # allowed to see (admins / agent creator). They subscribe to live
        # broadcasts but may never drive a turn — enforced in ``message_loop``.
        self.read_only: bool = False
        self.history_messages: list[ChatMessage] = []
        self.conversation: list[dict] = []
        self.current_user_text: str = ""
        self.onboarding_required: bool = False
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
            logger.info(f"[WS] Client disconnected: {self.user_id or 'unknown'}")
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
            self.user_id = user_id
        except Exception:
            await self.websocket.send_json({"type": "error", "content": "Authentication failed"})
            await self.websocket.close(code=4001)
            return False

        try:
            async with async_session() as db:
                result = await db.execute(select(User).where(User.id == user_id))
                user = result.scalar_one_or_none()
                if not user:
                    logger.error("[WS] User not found")
                    await self.websocket.send_json({"type": "error", "content": "User not found"})
                    await self.websocket.close(code=4001)
                    return False

                logger.info(f"[WS] Checking agent access for {self.agent_id}")
                agent, _ = await check_agent_access(db, user, self.agent_id)
                require_current_agent_tenant(user, agent)
                if is_agent_expired(agent):
                    await self.websocket.send_json(
                        {
                            "type": "error",
                            "content": "This Agent has expired and is off duty. Please contact your admin to extend its service.",
                        }
                    )
                    await self.websocket.close(code=4003)
                    return False

                self.agent_name = agent.name
                self.agent_type = agent.agent_type or ""
                self.role_description = agent.role_description or ""
                self.welcome_message = agent.welcome_message or ""
                self.ctx_size = agent.context_window_size or 100
                self.user_display_name = (user.display_name or "").strip() or "there"
                await self._load_scene_manifest(db)
                logger.info(
                    f"[WS] Agent: {self.agent_name}, type: {self.agent_type}, model_id: {agent.primary_model_id}, ctx: {self.ctx_size}"
                )

                # Load models
                await self._load_models(db, agent)

                # Resolve or create chat session
                self.conv_id = await self._resolve_chat_session(
                    db,
                    user_id,
                    viewer=user,
                    agent=agent,
                )
                if not self.conv_id:
                    return False

                # Load history messages
                await self._load_history(db)
                await self._prepare_initial_greeting(db, user_id)
                onboarding_eligibility = await resolve_onboarding_eligibility(
                    db,
                    self.agent_id,
                    user_id,
                    uuid.UUID(self.conv_id),
                )
                # A published scene welcome is already the authored first reply.
                # Prefer it over the generated onboarding greeting so opening the
                # scene does not spend tokens or create an extra model turn.
                self.onboarding_required = self._resolve_onboarding_required(
                    onboarding_eligibility.required
                )
                # setup owns the complete initialization transaction. Helpers
                # may flush/query but must never commit or roll it back.
                await db.commit()

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
            {
                "type": "connected",
                "session_id": self.conv_id,
                "read_only": self.read_only,
                "source_channel": self.source_channel,
                "onboarding_required": self.onboarding_required,
            }
        )

        # Build conversation context
        self.conversation = self._build_conversation_context()

        return True

    async def _load_models(self, db: AsyncSession, agent: Agent):
        """Loads primary and fallback models for the agent."""
        from app.services.chat_model_selection import resolve_runtime_models

        resolved = await resolve_runtime_models(db, agent=agent)
        self.llm_model = resolved.primary_model
        self.fallback_llm_model = resolved.fallback_model
        logger.info(
            f"[WS] Models loaded: primary="
            f"{self.llm_model.model if self.llm_model else 'None'}, fallback="
            f"{self.fallback_llm_model.model if self.fallback_llm_model else 'None'}"
        )

    async def _resolve_chat_session(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        viewer: User,
        agent: Agent,
    ) -> str | None:
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
                else:
                    if hasattr(agent, "tenant_id"):
                        try:
                            await require_tenant_safe_chat_session(
                                db, _existing, agent.tenant_id
                            )
                        except HTTPException:
                            await self.websocket.send_json(
                                {"type": "error", "content": "Session not found"}
                            )
                            await self.websocket.close(code=4003)
                            return None
                    self.source_channel = _existing.source_channel or self.source_channel
                if _existing and _existing.source_channel != "agent" and str(_existing.user_id) != str(user_id):
                    # Not the owner. Allow a READ-ONLY monitor connection if the
                    # viewer may see others' sessions (same gate as the REST
                    # session/message APIs: admins + the agent creator) — so any
                    # session visible in the web UI also updates live. They only
                    # subscribe to broadcasts; sending is blocked in message_loop.
                    if can_view_all_agent_chat_sessions(viewer, agent):
                        self.read_only = True
                    else:
                        await self.websocket.send_json({"type": "error", "content": "Not authorized for this session"})
                        await self.websocket.close(code=4003)
                        return None
        if not conv_id:
            _latest = await ensure_primary_platform_session(
                db,
                self.agent_id,
                user_id,
                source_channel=self.source_channel,
            )
            conv_id = str(_latest.id)
            logger.info(f"[WS] Selected primary session {conv_id}")
        return conv_id

    async def _load_scene_manifest(self, db: AsyncSession | None = None) -> None:
        """Load the current scene revision without binding it to the session."""
        self.scene_manifest = None
        if not self.scene_key:
            return
        try:
            from app.schemas.scene import validate_scene_key
            from app.services.scene_service import (
                SCENE_STATUS_OK,
                resolve_scene_for_activation,
            )

            self.scene_key = validate_scene_key(self.scene_key)

            async def _load(active_db: AsyncSession):
                resolved = await resolve_scene_for_activation(
                    active_db,
                    self.agent_id,
                    self.scene_key,
                )
                return resolved.manifest if resolved.status == SCENE_STATUS_OK else None

            if db is not None:
                self.scene_manifest = await _load(db)
            else:
                async with async_session() as active_db:
                    self.scene_manifest = await _load(active_db)
        except (TypeError, ValueError):
            logger.warning(f"[WS] Ignoring invalid scene key: {self.scene_key!r}")
            self.scene_key = None
        except Exception as exc:
            logger.warning(f"[WS] Scene load failed (non-fatal): {exc}")

    def _has_configured_scene_welcome(self) -> bool:
        """Whether the published scene provides a fixed, user-visible greeting."""
        if not self.scene_manifest:
            return False
        return bool(str(self.scene_manifest.get("welcome_message") or "").strip())

    def _resolve_onboarding_required(self, onboarding_required: bool) -> bool:
        """Make fixed scene greetings and generated onboarding strictly exclusive."""
        return bool(onboarding_required and not self._has_configured_scene_welcome())

    async def _prepare_initial_greeting(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
    ) -> None:
        self.pending_initial_assistant = None
        if self.history_messages or not self._has_configured_scene_welcome():
            return
        if not await claim_fixed_welcome_slot(db, self.agent_id, user_id):
            logger.info(
                "[WS] Fixed scene welcome skipped because onboarding already "
                "published visible output"
            )
            return
        self.pending_initial_assistant = {
            "content": str(self.scene_manifest["welcome_message"]),
            "message_meta": {
                **self._scene_message_meta(),
                "scene_welcome": True,
            },
        }
        # The authored greeting owns the shared first-contact slot. The message
        # itself is still written only with the first real user message through
        # the standard ChatMessage persistence path.

    def _scene_message_meta(self) -> dict:
        from app.services.scene_service import scene_message_meta

        return scene_message_meta(self.scene_manifest)

    def _channel_context(self) -> dict:
        from app.services.scene_service import build_scene_channel_context

        if self.source_channel in {"miniprogram", "wechat_miniprogram"}:
            display_name = "小程序"
            client_surface = "mini-program web-view"
        else:
            display_name = "Web"
            client_surface = "desktop web"
        return build_scene_channel_context(
            self.scene_manifest,
            source_channel=self.source_channel,
            display_name=display_name,
            client_surface=client_surface,
        )

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
        model thinking). Image attachments stay structured until the actual
        model attempt is known."""
        from app.services.chat_history import build_llm_messages_from_rows

        conversation: list[dict] = build_llm_messages_from_rows(self.history_messages, include_thinking=True)
        return conversation

    async def message_loop(self):
        """Core message processing loop."""
        # Send welcome message on new session (no history)
        initial_content = (
            str(self.pending_initial_assistant["content"])
            if self.pending_initial_assistant
            else self.welcome_message
        )
        if initial_content and not self.history_messages and not self.onboarding_required:
            await self.websocket.send_json(
                {
                    "type": "done",
                    "role": "assistant",
                    "content": initial_content,
                    "message_id": f"initial-assistant:{self.conv_id}",
                }
            )

        while True:
            data = await self.websocket.receive_json()

            # Set a unique trace ID for this specific message processing.
            trace_id = str(uuid.uuid4())[:12]
            set_trace_id(trace_id)

            content = data.get("content", "")
            display_content = data.get("display_content", "")
            file_name = data.get("file_name", "")
            raw_attachments = data.get("attachments") if "attachments" in data else None
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

            validated_attachments = None
            if raw_attachments is not None:
                from app.services.chat_attachments import validate_client_attachments

                try:
                    validated_attachments = await validate_client_attachments(
                        self.agent_id,
                        raw_attachments,
                    )
                except (TypeError, ValueError) as exc:
                    await self.websocket.send_json(
                        {"type": "error", "content": f"附件无效：{exc}"}
                    )
                    continue

            # Scene changes apply to the next turn. The session itself remains
            # unchanged; the exact scene revision used is recorded on messages.
            await self._load_scene_manifest()

            effective_llm_model = await self._resolve_effective_model(override_model_id)

            # Quota Checks
            if not await self._check_quotas():
                continue

            onboarding_claim: OnboardingClaim | None = None
            if is_onboarding_trigger:
                if effective_llm_model is None:
                    await self.websocket.send_json(
                        {
                            "type": "onboarding_skipped",
                            "reason": "model_unavailable",
                        }
                    )
                    continue
                onboarding_claim = await self._claim_onboarding_trigger()
                if onboarding_claim is None:
                    continue
                content = "Please begin the onboarding."
            else:
                await self._wait_for_normal_turn_onboarding()

            self.current_user_text = content

            client_message_id = data.get("message_id") or data.get("client_message_id")

            # Persist the first fixed greeting, if any, in the same transaction
            # as the first real user message. Opening a session alone never
            # writes the greeting to history.
            (
                turn_anchor_id,
                consumed_by_onmessage,
                persisted_initial_assistant,
                pending_confirmation,
                ignored_confirmation,
            ) = await self._save_user_message(
                content,
                display_content,
                file_name,
                is_onboarding_trigger,
                client_message_id=client_message_id,
                model_id=(
                    str(effective_llm_model.id)
                    if effective_llm_model is not None
                    else None
                ),
                attachments=validated_attachments,
            )

            if turn_anchor_id is not None and client_message_id:
                await self._safe_send(
                    {
                        "type": "user_message_committed",
                        "client_message_id": str(client_message_id),
                        "message_id": str(turn_anchor_id),
                    }
                )

            if pending_confirmation is not None:
                await self._safe_send(
                    {
                        "type": "confirmation_required",
                        "content": "请先完成待确认操作。",
                        "message_id": data.get("message_id")
                        or data.get("client_message_id"),
                        "name": "request_confirmation",
                        "call_id": str(pending_confirmation.row_id),
                        "args": pending_confirmation.args,
                        "status": "running",
                    }
                )
                continue

            if ignored_confirmation:
                # The pending tool call was closed with a real negative result in the
                # same transaction as this new user row. Reload the persisted prefix
                # before the new anchor so the next single turn sees:
                # assistant(tool_call) -> tool(not confirmed) -> user(new input).
                from app.services.chat_history import load_history_prefix_before_anchor

                async with async_session() as _history_db:
                    refreshed_prefix = await load_history_prefix_before_anchor(
                        _history_db,
                        agent_id=self.agent_id,
                        conversation_id=self.conv_id,
                        turn_anchor_id=turn_anchor_id,
                        ctx_size=self.ctx_size,
                        include_thinking=True,
                    )
                if refreshed_prefix is None:
                    raise RuntimeError(
                        "confirmation ignore committed but history prefix could not be rebuilt"
                    )
                self.conversation = refreshed_prefix

            if persisted_initial_assistant is not None:
                self.conversation.append(
                    {
                        "role": "assistant",
                        "content": persisted_initial_assistant.content,
                    }
                )

            if consumed_by_onmessage:
                # The durable inbound event belongs to one or more exact
                # on_message subscriptions.  Their origin sessions will run
                # the corresponding event turns; do not also answer it in this
                # remote session.
                await self._safe_send({"type": "done", "role": "assistant", "content": ""})
                continue

            # Add the real user message after any assistant-first greeting so
            # the in-memory context matches durable history ordering.
            from app.services.chat_attachments import (
                normalize_chat_message_attachments,
                strip_image_data_markers,
            )

            current_source = content
            if validated_attachments is None and file_name and "[image_data:" in content:
                current_source = f"[file:{file_name}]\n{content}"
            if validated_attachments is not None:
                # Structured attachment metadata is already validated. Keep the
                # current-turn document extraction text, while ensuring legacy
                # image transport data can never reach the model adapter.
                current_content = strip_image_data_markers(current_source)
                current_attachments = validated_attachments
            else:
                current_content, current_attachments = normalize_chat_message_attachments(
                    current_source,
                    {},
                    self.source_channel,
                )
            current_message = {"role": "user", "content": current_content}
            if current_attachments:
                current_message["attachments"] = current_attachments
            self.conversation.append(current_message)

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
                (
                    assistant_response,
                    thinking_content,
                    _queued_messages,
                    turn_outcome,
                    produced_output,
                ) = await self._run_llm_and_stream(
                    effective_llm_model,
                    is_onboarding_trigger,
                    onboarding_claim=onboarding_claim,
                    turn_anchor_id=turn_anchor_id,
                )
            else:
                assistant_response = (
                    f"⚠️ {self.agent_name} has no LLM model configured. "
                    "Please select a model in the agent's Settings tab."
                )
                thinking_content = []
                _queued_messages = []
                turn_outcome = "failed"
                produced_output = False

            if onboarding_claim and onboarding_claim.claimed_at:
                # Successful greeting streams advance pending atomically on the
                # first chunk. Errors/aborts with no output leave it pending; in
                # that case release only this exact claim so reconnect can retry.
                async with async_session() as _release_db:
                    await release_onboarding_claim(
                        _release_db,
                        self.agent_id,
                        self.user_id,
                        onboarding_claim.claimed_at,
                    )

            if (
                is_onboarding_trigger
                and not produced_output
                and turn_outcome in {"failed", "aborted"}
            ):
                # A synthetic greeting that never produced user-visible output
                # must leave the pristine session pristine. Persisting an error
                # or abort marker would make history-based eligibility reject a
                # safe reconnect retry.
                if self.conversation and self.conversation[-1].get("role") == "user":
                    self.conversation.pop()
                await self._safe_send(
                    {
                        "type": "onboarding_skipped",
                        "reason": (
                            "generation_aborted"
                            if turn_outcome == "aborted"
                            else "generation_failed"
                        ),
                        "agent_id": str(self.agent_id),
                    }
                )
                continue

            # request_confirmation suspends the turn inside the unified LLM caller
            # and persists the intro/card rows there. Keep the turn anchor suspended:
            # do not add an empty assistant row and do not mark the anchor completed.
            if assistant_response == "":
                await self._safe_send({"type": "done", "role": "assistant", "content": ""})
                if self.client_disconnected:
                    await manager.disconnect(str(self.agent_id), self.websocket)
                    break
                continue

            # If task creation detected, create a real Task record
            if task_match:
                assistant_response = await self._create_task_record(task_match.group(1).strip(), assistant_response)

            # Add assistant response to in-memory conversation
            self.conversation.append({"role": "assistant", "content": assistant_response})

            # Save assistant reply
            await self._save_assistant_reply(
                assistant_response,
                thinking_content,
                turn_anchor_id=turn_anchor_id,
                complete_onboarding=(
                    is_onboarding_trigger
                    and produced_output
                    and self.source_channel != "web"
                ),
            )

            # Final 'done' packet — best-effort broadcast; a client that dropped
            # mid-turn gets the reply via history replay on reconnect instead.
            await self._safe_send({"type": "done", "role": "assistant", "content": assistant_response})

            # The browser dropped mid-turn: we finished + persisted the reply
            # detached. Tear down cleanly instead of looping back into receive
            # (which would raise) — same cleanup as the WebSocketDisconnect path.
            if self.client_disconnected:
                logger.info(
                    f"[WS] Detached turn complete after disconnect; closing handler for "
                    f"{self.user_id or 'unknown'}"
                )
                await manager.disconnect(str(self.agent_id), self.websocket)
                break

    async def _claim_onboarding_trigger(self) -> OnboardingClaim | None:
        """Revalidate and atomically claim this session's greeting turn."""
        async with async_session() as _gdb:
            claim = await claim_onboarding_greeting(
                _gdb,
                self.agent_id,
                self.user_id,
                uuid.UUID(self.conv_id),
            )
        if claim.acquired:
            return claim
        logger.info(f"[WS] Onboarding trigger skipped: {claim.reason}")
        await self.websocket.send_json(
            {
                "type": "onboarding_skipped",
                "reason": claim.reason,
                "agent_id": str(self.agent_id),
            }
        )
        return None

    async def _wait_for_normal_turn_onboarding(self) -> None:
        """Atomically let a real message win, or wait for a claimed greeting."""

        while True:
            async with async_session() as _normal_db:
                phase = await claim_normal_first_turn(
                    _normal_db,
                    self.agent_id,
                    self.user_id,
                )
            if phase != PHASE_PENDING:
                return
            # ``claim_normal_first_turn`` atomically takes over once the
            # persisted pending claim reaches its TTL. Keep a bounded polling
            # interval even at that boundary so a clock/precision mismatch
            # cannot turn this wait into a busy loop.
            await asyncio.sleep(0.2)

    async def _resolve_effective_model(
        self,
        override_model_id: str | None,
    ) -> RuntimeLLMModel | None:
        """Reloads model config and resolves effective model (taking overrides into account)."""
        from app.services.chat_model_selection import (
            MODEL_OVERRIDE_NONE,
            MODEL_OVERRIDE_OK,
            resolve_runtime_models,
        )

        async with async_session() as _mdb:
            _agent_r = await _mdb.execute(select(Agent).where(Agent.id == self.agent_id))
            _agent_cur = _agent_r.scalar_one_or_none()
            if _agent_cur is None:
                self.llm_model = None
                self.fallback_llm_model = None
                return None
            resolved = await resolve_runtime_models(
                _mdb,
                agent=_agent_cur,
                override_model_id=override_model_id,
            )

        self.llm_model = resolved.primary_model
        self.fallback_llm_model = resolved.fallback_model
        if resolved.override_status not in {MODEL_OVERRIDE_NONE, MODEL_OVERRIDE_OK}:
            logger.warning(
                f"[WS] model override {override_model_id!r} rejected "
                f"({resolved.override_status})"
            )
        return resolved.primary_model

    async def _check_quotas(self) -> bool:
        """Checks conversation and agent LLM quotas. Sends message and returns False if exceeded."""
        try:
            await check_conversation_quota(self.user_id)
            await check_agent_expired(self.agent_id)
            return True
        except QuotaExceeded as qe:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {qe.message}"})
            return False
        except AgentExpired as ae:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {ae.message}"})
            return False

    async def _save_user_message(
        self,
        content: str,
        display_content: str,
        file_name: str,
        is_onboarding_trigger: bool,
        client_message_id: str | None = None,
        model_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[
        uuid.UUID | None,
        bool,
        ChatMessage | None,
        PendingConfirmation | None,
        bool,
    ]:
        """Saves user message to the database and updates session title/time."""
        from app.services.chat_attachments import strip_image_data_markers

        has_image_marker = "[image_data:" in content
        if attachments is not None:
            saved_content = display_content if display_content else strip_image_data_markers(content)
        elif has_image_marker:
            clean_content = strip_image_data_markers(content)
            saved_content = f"[file:{file_name}]\n{clean_content}" if file_name else clean_content
        else:
            saved_content = display_content if display_content else content
            if file_name:
                saved_content = f"[file:{file_name}]\n{saved_content}"

        if is_onboarding_trigger:
            logger.info("[WS] Onboarding trigger — skipping user-message persistence")
            return None, False, None, None, False
        else:
            from app.services.chat_history import ingest_incoming_chat_message

            async with async_session() as db:
                _sess_r = await db.execute(
                    select(ChatSession)
                    .where(ChatSession.id == uuid.UUID(self.conv_id))
                    .with_for_update()
                )
                _sess = _sess_r.scalar_one_or_none()
                if _sess is None:
                    raise RuntimeError("chat session no longer exists")
                initial_assistant = None
                first_user_created_at = None
                if self.pending_initial_assistant is not None:
                    initial_assistant = await persist_initial_assistant_message_if_pristine(
                        db,
                        session=_sess,
                        agent_id=self.agent_id,
                        user_id=self.user_id,
                        content=str(self.pending_initial_assistant["content"]),
                        message_meta=dict(
                            self.pending_initial_assistant.get("message_meta") or {}
                        ),
                    )
                    if initial_assistant is not None:
                        initial_created_at = (
                            initial_assistant.created_at or datetime.now(tz.utc)
                        )
                        first_user_created_at = initial_created_at + timedelta(
                            microseconds=1
                        )
                ingested = await ingest_incoming_chat_message(
                    db,
                    session=_sess,
                    agent_id=self.agent_id,
                    user_id=self.user_id,
                    content=saved_content,
                    source_channel=_sess.source_channel,
                    provider_event_id=str(client_message_id or "") or None,
                    channel_config_id=_sess.id,
                    actor_ref=str(self.user_id),
                    message_meta={
                        **self._scene_message_meta(),
                        **({"model_id": model_id} if model_id else {}),
                        **(
                            {
                                "attachments": attachments,
                                "display_content": display_content,
                            }
                            if attachments is not None
                            else {}
                        ),
                    },
                    created_at=first_user_created_at,
                )
                if ingested.blocked_by_confirmation:
                    # No user row, session timestamp/title change, trigger or turn.  Commit
                    # only releases the session lock acquired by canonical ingestion.
                    await db.commit()
                    logger.info(
                        "[WS] Message blocked by pending confirmation %s",
                        ingested.message.id,
                    )
                    return None, True, None, ingested.pending_confirmation, False
                # Update session
                _now = datetime.now(tz.utc)
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
            if ingested.ignored_confirmation is not None:
                from app.services.chat_history import (
                    finish_ignored_confirmation_ingest,
                )

                await finish_ignored_confirmation_ingest(ingested)
            logger.info("[WS] User message saved")
            return (
                ingested.message.id,
                ingested.consumed_by_onmessage,
                initial_assistant,
                None,
                ingested.ignored_confirmation is not None,
            )

    async def _route_openclaw(self, content: str):
        """Enqueues message for OpenClaw edge node poll."""
        from app.models.gateway_message import GatewayMessage as GwMsg

        async with async_session() as db:
            gw_msg = GwMsg(
                agent_id=self.agent_id,
                sender_user_id=self.user_id,
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
        self,
        effective_llm_model: RuntimeLLMModel,
        is_onboarding_trigger: bool,
        *,
        onboarding_claim: OnboardingClaim | None = None,
        turn_anchor_id: uuid.UUID | None = None,
    ) -> tuple[str, list[str], list[dict], str, bool]:
        """Calls the LLM and streams response chunks to WebSocket."""
        start_gen = perf_counter()
        partial_chunks: list[str] = []
        thinking_content: list[str] = []
        onboarding_lease_task: asyncio.Task | None = None
        onboarding_lease_lost = False

        async def stop_onboarding_lease() -> None:
            nonlocal onboarding_lease_task
            if onboarding_lease_task is None:
                return
            lease_task = onboarding_lease_task
            onboarding_lease_task = None
            lease_task.cancel()
            try:
                await lease_task
            except asyncio.CancelledError:
                pass

        try:
            logger.info(f"[WS] Calling LLM {effective_llm_model.model} (streaming)...")

            # Accumulate partial content for abort handling
            # Set inside _call_with_failover when an onboarding prompt was injected
            onboarding_claimed_at = (
                onboarding_claim.claimed_at if onboarding_claim else None
            )
            needs_onboarding_mark = bool(
                is_onboarding_trigger and onboarding_claimed_at is not None
            )
            onboarding_target_phase = "completed"
            onboarding_expected_phase: str | None = (
                PHASE_PENDING if needs_onboarding_mark else None
            )
            onboarding_mark_done = False
            onboarding_visible_output_started = False
            onboarding_lease_refreshed_at = 0.0

            async def maybe_mark_onboarding_progress() -> bool:
                nonlocal onboarding_mark_done, onboarding_lease_refreshed_at
                nonlocal onboarding_lease_task
                if not needs_onboarding_mark or onboarding_mark_done:
                    return True
                try:
                    async with async_session() as _ob_db:
                        advanced = await mark_onboarding_phase(
                            _ob_db,
                            self.agent_id,
                            self.user_id,
                            onboarding_target_phase,
                            expected_phase=onboarding_expected_phase,
                            expected_onboarded_at=onboarding_claimed_at,
                        )
                    if not advanced:
                        logger.info(
                            "[WS] Onboarding phase changed before this turn "
                            "could publish its first output"
                        )
                        return False
                    onboarding_mark_done = True
                    onboarding_lease_refreshed_at = perf_counter()
                    if onboarding_target_phase == PHASE_GREETED:
                        onboarding_lease_task = asyncio.create_task(
                            maintain_onboarding_lease()
                        )
                    # Tell the frontend to refresh its cached agent record
                    await self._safe_send(
                        {
                            "type": "onboarded",
                            "agent_id": str(self.agent_id),
                        }
                    )
                    return True
                except Exception as _ob_err:
                    logger.warning(f"[WS] mark_onboarded failed: {_ob_err}")
                    return False

            async def maybe_refresh_onboarding_lease(*, force: bool = False) -> bool:
                """Keep an unpublished greeting reserved during a long stream."""
                nonlocal onboarding_lease_refreshed_at, onboarding_lease_lost
                if onboarding_lease_lost:
                    return False
                if (
                    not onboarding_mark_done
                    or onboarding_target_phase != PHASE_GREETED
                    or (
                        not force
                        and perf_counter() - onboarding_lease_refreshed_at
                        < ONBOARDING_LEASE_REFRESH_SECONDS
                    )
                ):
                    return True
                try:
                    async with async_session() as _lease_db:
                        refreshed = await mark_onboarding_phase(
                            _lease_db,
                            self.agent_id,
                            self.user_id,
                            PHASE_GREETED,
                            expected_phase=PHASE_GREETED,
                        )
                    if not refreshed:
                        logger.info(
                            "[WS] Onboarding greeting lease changed before "
                            "assistant persistence"
                        )
                        onboarding_lease_lost = True
                        return False
                    onboarding_lease_refreshed_at = perf_counter()
                    return True
                except Exception as _lease_err:
                    logger.warning(
                        f"[WS] Onboarding greeting lease refresh failed: {_lease_err}"
                    )
                    onboarding_lease_lost = True
                    return False

            async def maintain_onboarding_lease() -> None:
                """Renew independently even while the model stream is silent."""
                while True:
                    await asyncio.sleep(ONBOARDING_LEASE_REFRESH_SECONDS)
                    if not await maybe_refresh_onboarding_lease(force=True):
                        return

            async def reserve_visible_output() -> bool:
                """Reserve first-contact ownership before any visible callback."""
                nonlocal onboarding_visible_output_started
                if not await maybe_mark_onboarding_progress():
                    return False
                if not await maybe_refresh_onboarding_lease():
                    return False
                if is_onboarding_trigger:
                    onboarding_visible_output_started = True
                return True

            async def stream_to_ws(text: str):
                """Send each chunk to client in real-time."""
                if not await reserve_visible_output():
                    raise RuntimeError("Onboarding claim lost before first output")
                partial_chunks.append(text)
                await self._safe_send({"type": "chunk", "content": text})

            async def tool_call_to_ws(data: dict):
                """Send tool call info to client and persist completed ones."""
                public_data = {k: v for k, v in data.items() if not k.startswith("_")}
                if not await reserve_visible_output():
                    raise RuntimeError("Onboarding claim lost before tool output")
                if public_data.get("status") == "done":
                    # Inject Live Preview & Workspace Activities
                    await self._inject_live_preview_and_workspace_metadata(public_data)

                # Output boundary: mask secrets for the client. `data` stays raw
                # below so persist_tool_call stores the real args (the LLM replays
                # them — masking storage poisons the model).
                _ws_data = (
                    {**public_data, "args": sanitize_tool_args(public_data.get("args"))}
                    if "args" in public_data
                    else public_data
                )
                await self._safe_send({"type": "tool_call", **_ws_data})

                # Save tool-call markers to DB before execution and after completion.
                if public_data.get("status") in {"running", "done"} and not data.get("_durable_persisted"):
                    await self._save_tool_call_to_db(public_data, turn_anchor_id=turn_anchor_id)

            # Track thinking content for storage
            async def thinking_to_ws(text: str):
                """Send thinking chunks to client for collapsible display."""
                if not await reserve_visible_output():
                    raise RuntimeError("Onboarding claim lost before thinking output")
                thinking_content.append(text)
                await self._safe_send({"type": "thinking", "content": text})

            _workspace_draft_cache: dict[str, str] = {}

            async def tool_delta_to_ws(data: dict):
                """Stream workspace file-operation drafts while tool args are still arriving."""
                tool_name = data.get("name", "")

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
                if not await reserve_visible_output():
                    raise RuntimeError("Onboarding claim lost before tool draft output")
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
                nonlocal needs_onboarding_mark, onboarding_expected_phase, onboarding_target_phase

                # A synthetic greeting owns one exact pending claim. Another
                # connection may legitimately complete that first-contact slot
                # before this model call begins (for example by selecting a
                # fixed welcome message). Abort before spending a turn in that
                # case; a later race is still stopped by the exact-token CAS
                # before the first output is published.
                if is_onboarding_trigger and onboarding_claimed_at is not None:
                    async with async_session() as _claim_db:
                        claim_is_current = await onboarding_claim_is_current(
                            _claim_db,
                            self.agent_id,
                            self.user_id,
                            onboarding_claimed_at,
                        )
                    if not claim_is_current:
                        raise RuntimeError(
                            "Onboarding claim lost before model invocation"
                        )

                async def _on_failover(reason: str):
                    if not await reserve_visible_output():
                        raise RuntimeError(
                            "Onboarding claim lost before failover output"
                        )
                    await self._safe_send({"type": "info", "content": f"Primary model error, {reason}"})

                # History loading is turn-aware, so do not re-apply a row/message
                # slice here: it could split one tool-heavy protected turn.
                from app.services.chat_history import strip_leading_orphan_tool_messages

                persisted_view = strip_leading_orphan_tool_messages(self.conversation)
                _truncated = list(persisted_view)
                ephemeral_overlays: list[dict] = []

                # Resolve onboarding prompt
                skip_tools_for_greeting = False
                try:
                    async with async_session() as _ob_db:
                        _agent_result = await _ob_db.execute(
                            select(Agent).where(Agent.id == self.agent_id)
                        )
                        _agent = _agent_result.scalar_one_or_none()
                        if _agent is None:
                            raise RuntimeError("Agent no longer exists")
                        _onb = await resolve_onboarding_prompt(
                            _ob_db,
                            _agent,
                            self.user_id,
                            user_name=self.user_display_name,
                            user_locale=self.lang,
                            is_onboarding_trigger=is_onboarding_trigger,
                            complete_after_greeting=self.source_channel != "web",
                        )
                    if _onb:
                        ephemeral_overlays = [{"role": "system", "content": _onb.prompt}]
                        _truncated = ephemeral_overlays + _truncated
                        if _onb.lock_on_first_chunk:
                            needs_onboarding_mark = True
                            onboarding_target_phase = _onb.target_phase
                            onboarding_expected_phase = _onb.expected_phase
                        if _onb.is_greeting_turn:
                            skip_tools_for_greeting = True
                except Exception as _onb_err:
                    logger.warning(f"[WS] Onboarding prompt resolve failed (non-fatal): {_onb_err}")
                    if is_onboarding_trigger:
                        raise RuntimeError(
                            "Onboarding prompt could not be resolved"
                        ) from _onb_err
                if is_onboarding_trigger and _onb is None:
                    raise RuntimeError(
                        "Onboarding claim changed before prompt resolution"
                    )

                context_recovery = None
                if turn_anchor_id is not None and persisted_view:
                    from app.services.llm.turn_partition import effective_keep_recent_turns

                    frozen_current_suffix = [dict(persisted_view[-1])]
                    protected_keep_recent_turns = effective_keep_recent_turns(
                        effective_llm_model,
                        self.fallback_llm_model,
                    )

                    async def _recover_context(_recovery_model, dispatch_budget):
                        from app.services.chat_history import load_history_prefix_before_anchor
                        from app.services.llm.compactor import maybe_compact

                        compacted = await maybe_compact(
                            agent_id=self.agent_id,
                            conversation_id=self.conv_id,
                            model=_recovery_model,
                            pre_flight_estimate=dispatch_budget.estimated_tokens,
                            current_anchor_id=turn_anchor_id,
                            force_required=dispatch_budget.char_overflow,
                            keep_recent_turns_override=protected_keep_recent_turns,
                        )
                        if not compacted.triggered:
                            logger.warning(
                                "[WS] context recovery could not compact session="
                                f"{self.conv_id}: {compacted.skipped_reason}"
                            )
                            return None
                        async with async_session() as recovery_db:
                            prefix = await load_history_prefix_before_anchor(
                                recovery_db,
                                agent_id=self.agent_id,
                                conversation_id=self.conv_id,
                                turn_anchor_id=turn_anchor_id,
                                ctx_size=self.ctx_size,
                                include_thinking=True,
                            )
                        if prefix is None:
                            logger.warning(
                                f"[WS] context recovery lost latest-anchor race session={self.conv_id}"
                            )
                            return None
                        # Keep only the persisted projection on the long-lived WS
                        # state. Onboarding/dynamic overlays remain turn-local.
                        self.conversation = prefix + frozen_current_suffix
                        return ephemeral_overlays + self.conversation

                    context_recovery = _recover_context

                live_code_chars_sent = 0
                live_code_truncated_sent = False

                async def code_output_to_ws(text: str, label: str = "stdout"):
                    """Stream execute_code output chunks to the frontend live panel in real-time."""
                    nonlocal live_code_chars_sent, live_code_truncated_sent
                    if not await reserve_visible_output():
                        raise RuntimeError(
                            "Onboarding claim lost before code output"
                        )
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
                    user_id=self.user_id,
                    session_id=self.conv_id,
                    on_chunk=stream_to_ws,
                    on_tool_call=tool_call_to_ws,
                    on_tool_delta=tool_delta_to_ws,
                    on_thinking=thinking_to_ws,
                    on_failover=_on_failover,
                    skip_tools=skip_tools_for_greeting,
                    on_code_output=code_output_to_ws,
                    channel_context=self._channel_context(),
                    turn_anchor_id=turn_anchor_id,
                    context_recovery=context_recovery,
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
            if (
                not aborted
                and assistant_response
                and any(assistant_response.startswith(p) for p in LLM_FAILURE_PREFIXES)
            ):
                raise RuntimeError(assistant_response)

            # Streaming providers normally advance onboarding on the first
            # chunk. Some compatible APIs return one complete response without
            # invoking the chunk callback, so close that gap at successful
            # completion before the pending claim is released.
            if not aborted and is_onboarding_trigger and assistant_response:
                if not await reserve_visible_output():
                    raise RuntimeError("Onboarding claim lost before completion")

            # Post-success actions (last_active_at, quota usage increments, activity logs)
            await self._update_activity_and_quota(assistant_response)
            await maybe_refresh_onboarding_lease(force=True)
            await stop_onboarding_lease()

            # This must be the final lease check: no await is allowed between
            # it and the return. The renewer has already been stopped above.
            if is_onboarding_trigger and onboarding_lease_lost:
                return "", thinking_content, queued_messages, "aborted", False

            produced_output = (
                bool(partial_chunks)
                or (not aborted and bool(assistant_response))
                or onboarding_visible_output_started
            )
            return assistant_response, thinking_content, queued_messages, _turn_outcome, produced_output

        except WebSocketDisconnect:
            raise
        except Exception as e:
            gen_duration = perf_counter() - start_gen
            logger.exception(f"[WS] LLM error after {gen_duration:.3f}s: {e}")
            await stop_onboarding_lease()
            if is_onboarding_trigger and onboarding_lease_lost:
                return "", thinking_content, [], "aborted", False
            if is_onboarding_trigger and (
                partial_chunks or onboarding_visible_output_started
            ):
                partial_response = "".join(partial_chunks).strip()
                if not partial_response:
                    partial_response = "*[Welcome generation interrupted]*"
                return (
                    (
                        partial_response
                        if partial_response.endswith("*[Welcome generation interrupted]*")
                        else partial_response
                        + "\n\n*[Welcome generation interrupted]*"
                    ),
                    thinking_content,
                    [],
                    "failed",
                    True,
                )
            return f"[LLM call error] {str(e)[:200]}", [], [], "failed", False
        finally:
            await stop_onboarding_lease()

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

    async def _save_tool_call_to_db(self, data: dict, *, turn_anchor_id: uuid.UUID | None = None):
        """Persist tool-call markers via the shared writer — the SAME canonical
        schema every IM channel uses (single source of truth: args stored RAW for
        LLM replay, masked only at output boundaries). Then mark the session read
        (web-only)."""
        from app.services.chat_history import persist_tool_call

        await persist_tool_call(
            async_session,
            agent_id=self.agent_id,
            user_id=self.user_id,
            conversation_id=self.conv_id,
            evt=data,
            turn_anchor_id=turn_anchor_id,
        )
        try:
            async with async_session() as _tc_db:
                await maybe_mark_session_read_for_active_viewer(
                    _tc_db,
                    agent_id=self.agent_id,
                    session_id=self.conv_id,
                    user_id=self.user_id,
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
            await increment_conversation_usage(self.user_id)
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
                    created_by=self.user_id,
                    execution_user_id=self.user_id,
                    status="pending",
                    priority="medium",
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                logger.info(f"[WS] Task created: {task.id}")
                task_id = task.id
            asyncio.create_task(execute_task(task_id, self.agent_id, self.user_id))
            assistant_response += f"\n\n📋 Task synced to task board: [{task_title}]"
        except Exception as te:
            logger.error(f"[WS] Task creation failed: {te}")
        return assistant_response

    async def _save_assistant_reply(
        self,
        assistant_response: str,
        thinking_content: list[str],
        *,
        turn_anchor_id: uuid.UUID | None = None,
        complete_onboarding: bool = False,
    ):
        """Saves assistant reply to DB."""
        async with async_session() as db:
            assistant_msg = ChatMessage(
                agent_id=self.agent_id,
                user_id=self.user_id,
                role="assistant",
                content=assistant_response,
                conversation_id=self.conv_id,
                thinking="".join(thinking_content) if thinking_content else None,
                message_meta=(
                    {
                        "turn_anchor_id": str(turn_anchor_id),
                        "turn_status": "completed",
                        **self._scene_message_meta(),
                    }
                    if turn_anchor_id is not None
                    else self._scene_message_meta()
                ),
            )
            db.add(assistant_msg)
            if complete_onboarding:
                completed = await db.execute(
                    update(AgentUserOnboarding)
                    .where(
                        AgentUserOnboarding.agent_id == self.agent_id,
                        AgentUserOnboarding.user_id == self.user_id,
                        AgentUserOnboarding.phase == PHASE_GREETED,
                    )
                    .values(phase=PHASE_COMPLETED)
                )
                if not completed.rowcount:
                    raise RuntimeError(
                        "Onboarding state changed before assistant persistence"
                    )
            await maybe_mark_session_read_for_active_viewer(
                db,
                agent_id=self.agent_id,
                session_id=self.conv_id,
                user_id=self.user_id,
            )
            await db.commit()
        logger.info("[WS] Assistant message saved")
