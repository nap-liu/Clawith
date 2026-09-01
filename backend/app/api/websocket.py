"""WebSocket chat endpoint for real-time agent conversations."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from datetime import datetime, timedelta
from datetime import timezone as tz
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import String, cast, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api import (
    websocket_message_loop_ops,
    websocket_scene_ops,
    websocket_setup_ops,
    websocket_shared,
    websocket_stream_ops,
    websocket_turn_ops,
)
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
from app.models.subagent_run import SubagentRun
from app.models.task import Task
from app.models.user import User
from app.services.active_turns import (
    active_turn_boundary,
    ensure_active_turn,
    set_active_turn_cancel_task,
)
from app.services.activity_logger import log_activity
from app.services.agentbay_live import detect_agentbay_env, get_browser_snapshot, get_desktop_screenshot
from app.services.auth_code_exchange import validate_platform_login_channel
from app.services.chat_history import persist_initial_assistant_message_if_pristine
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.confirmation_service import PendingConfirmation
from app.services.conversation_turn_lifecycle import (
    ConversationTurnConflict,
    ConversationTurnSnapshot,
    get_conversation_turn_snapshot,
    publish_conversation_turn_event,
    transition_conversation_turn,
    with_turn_envelope,
)
from app.services.llm import call_llm_with_failover
from app.services.llm.runtime_model import RuntimeLLMModel
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
from app.services.workload_capacity import (
    WorkloadKind,
    WorkloadOverloadedError,
    get_workload_capacity,
)
from app.utils.sanitize import sanitize_tool_args

router = APIRouter(tags=["websocket"])
MODULE = sys.modules[__name__]

MAX_LIVE_CODE_STREAM_CHARS = 120_000
LIVE_CODE_TRUNCATED_NOTICE = "\n\n[... live output truncated; execution continues ...]\n"
ONBOARDING_LEASE_REFRESH_SECONDS = 30.0
LLM_FAILURE_PREFIXES = (
    "[LLM Error]",
    "[LLM call error]",
    "[Error]",
    "[LLM returned empty content]",
)

class SessionTurnBusyError(websocket_shared.SessionTurnBusyError):
    """A durable internal wake already owns the next turn in this session."""



async def _has_active_subagent_event_turn(
    db: AsyncSession,
    conversation_id: str,
) -> bool:
    return await websocket_shared.has_active_subagent_event_turn_impl(MODULE, db, conversation_id)


class ConnectionManager(websocket_shared.ConnectionManager):
    """Manage WebSocket connections per agent."""


manager = ConnectionManager()


async def maybe_mark_session_read_for_active_viewer(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: uuid.UUID,
) -> bool:
    return await websocket_shared.maybe_mark_session_read_for_active_viewer_impl(
        MODULE,
        db,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
    )


async def _await_turn_with_abort(
    llm_task,
    recv_json,
    partial_chunks: list[str],
    *,
    on_abort=None,
):
    return await websocket_shared.await_turn_with_abort_impl(
        MODULE,
        llm_task,
        recv_json,
        partial_chunks,
        on_abort=on_abort,
    )


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
        self.user_id: uuid.UUID | None = None
        self.tenant_id: uuid.UUID | None = None
        self.agent_name: str = ""
        self.agent_type: str = ""
        self.role_description: str = ""
        self.welcome_message: str = ""
        self.ctx_size: int = 100
        self.user_display_name: str = ""
        self.llm_model: RuntimeLLMModel | None = None
        self.fallback_llm_model: RuntimeLLMModel | None = None
        self.conv_id: str | None = None
        self.read_only: bool = False
        self.project_session_access: str | None = None
        self.history_messages: list[ChatMessage] = []
        self.conversation: list[dict] = []
        self.current_user_text: str = ""
        self.onboarding_required: bool = False
        self.client_disconnected: bool = False
        self.current_client_message_id: str | None = None

    async def _safe_send(self, payload: dict):
        return await websocket_setup_ops.safe_send_impl(MODULE, self, payload)

    async def run(self):
        return await websocket_setup_ops.run_impl(MODULE, self)

    async def setup(self) -> bool:
        return await websocket_setup_ops.setup_impl(MODULE, self)

    async def _load_turn_snapshot(
        self,
        turn_anchor_id: uuid.UUID | None = None,
    ) -> ConversationTurnSnapshot:
        return await websocket_setup_ops.load_turn_snapshot_impl(MODULE, self, turn_anchor_id=turn_anchor_id)

    async def _transition_turn(
        self,
        turn_anchor_id: uuid.UUID | None,
        status: str,
    ) -> ConversationTurnSnapshot | None:
        return await websocket_setup_ops.transition_turn_impl(MODULE, self, turn_anchor_id, status)

    async def _publish_turn_lifecycle(self, snapshot: ConversationTurnSnapshot | None) -> None:
        return await websocket_setup_ops.publish_turn_lifecycle_impl(MODULE, self, snapshot)

    async def _send_current_turn_event(
        self,
        payload: dict[str, Any],
        *,
        event_kind: str = "turn_rejected",
        reject_attempt: bool = True,
    ) -> None:
        return await websocket_setup_ops.send_current_turn_event_impl(
            MODULE,
            self,
            payload,
            event_kind=event_kind,
            reject_attempt=reject_attempt,
        )

    async def _load_models(self, db: AsyncSession, agent: Agent):
        return await websocket_setup_ops.load_models_impl(MODULE, self, db, agent)

    async def _resolve_chat_session(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        *,
        viewer: User,
        agent: Agent,
    ) -> str | None:
        return await websocket_setup_ops.resolve_chat_session_impl(
            MODULE,
            self,
            db,
            user_id,
            viewer=viewer,
            agent=agent,
        )

    async def _project_session_still_writable(self) -> bool:
        return await websocket_setup_ops.project_session_still_writable_impl(MODULE, self)

    async def _enqueue_project_subagent_message(
        self,
        *,
        content: str,
        display_content: str,
        file_name: str,
        client_message_id: str | None,
        attachments: list[dict] | None,
    ) -> bool:
        return await websocket_setup_ops.enqueue_project_subagent_message_impl(
            MODULE,
            self,
            content=content,
            display_content=display_content,
            file_name=file_name,
            client_message_id=client_message_id,
            attachments=attachments,
        )

    async def _load_scene_manifest(self, db: AsyncSession | None = None) -> None:
        return await websocket_scene_ops.load_scene_manifest_impl(MODULE, self, db=db)

    def _has_configured_scene_welcome(self) -> bool:
        return websocket_scene_ops.has_configured_scene_welcome_impl(MODULE, self)

    def _resolve_onboarding_required(self, onboarding_required: bool) -> bool:
        return websocket_scene_ops.resolve_onboarding_required_impl(MODULE, self, onboarding_required)

    async def _prepare_initial_greeting(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
    ) -> None:
        return await websocket_scene_ops.prepare_initial_greeting_impl(MODULE, self, db, user_id)

    def _scene_message_meta(self) -> dict:
        return websocket_scene_ops.scene_message_meta_impl(MODULE, self)

    def _channel_context(self) -> dict:
        return websocket_scene_ops.channel_context_impl(MODULE, self)

    async def _load_history(self, db: AsyncSession):
        return await websocket_scene_ops.load_history_impl(MODULE, self, db)

    def _build_conversation_context(self) -> list[dict]:
        return websocket_scene_ops.build_conversation_context_impl(MODULE, self)

    async def message_loop(self):
        return await websocket_message_loop_ops.message_loop_impl(MODULE, self)

    async def _execute_web_turn(
        self,
        *,
        effective_llm_model: RuntimeLLMModel | None,
        is_onboarding_trigger: bool,
        onboarding_claim: OnboardingClaim | None,
        turn_anchor_id: uuid.UUID | None,
        turn_snapshot: ConversationTurnSnapshot | None,
        task_match,
    ) -> str:
        return await websocket_turn_ops.execute_web_turn_impl(
            MODULE,
            self,
            effective_llm_model=effective_llm_model,
            is_onboarding_trigger=is_onboarding_trigger,
            onboarding_claim=onboarding_claim,
            turn_anchor_id=turn_anchor_id,
            turn_snapshot=turn_snapshot,
            task_match=task_match,
        )

    async def _claim_onboarding_trigger(self) -> OnboardingClaim | None:
        return await websocket_turn_ops.claim_onboarding_trigger_impl(MODULE, self)

    async def _wait_for_normal_turn_onboarding(self) -> None:
        return await websocket_turn_ops.wait_for_normal_turn_onboarding_impl(MODULE, self)

    async def _resolve_effective_model(
        self,
        override_model_id: str | None,
    ) -> RuntimeLLMModel | None:
        return await websocket_turn_ops.resolve_effective_model_impl(MODULE, self, override_model_id)

    async def _check_quotas(self) -> bool:
        return await websocket_turn_ops.check_quotas_impl(MODULE, self)

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
        ConversationTurnSnapshot | None,
    ]:
        return await websocket_turn_ops.save_user_message_impl(
            MODULE,
            self,
            content,
            display_content,
            file_name,
            is_onboarding_trigger,
            client_message_id=client_message_id,
            model_id=model_id,
            attachments=attachments,
        )

    async def _route_openclaw(
        self,
        content: str,
        *,
        turn_anchor_id: uuid.UUID | None,
        turn_snapshot: ConversationTurnSnapshot | None,
    ):
        return await websocket_turn_ops.route_openclaw_impl(
            MODULE,
            self,
            content,
            turn_anchor_id=turn_anchor_id,
            turn_snapshot=turn_snapshot,
        )

    async def _run_llm_and_stream(
        self,
        effective_llm_model: RuntimeLLMModel,
        is_onboarding_trigger: bool,
        *,
        onboarding_claim: OnboardingClaim | None = None,
        turn_anchor_id: uuid.UUID | None = None,
        turn_snapshot: ConversationTurnSnapshot | None = None,
    ) -> tuple[str, list[str], list[dict], str, bool]:
        return await websocket_stream_ops.run_llm_and_stream_impl(
            MODULE,
            self,
            effective_llm_model,
            is_onboarding_trigger,
            onboarding_claim=onboarding_claim,
            turn_anchor_id=turn_anchor_id,
            turn_snapshot=turn_snapshot,
        )

    async def _inject_live_preview_and_workspace_metadata(self, data: dict):
        return await websocket_stream_ops.inject_live_preview_and_workspace_metadata_impl(MODULE, self, data)

    async def _save_tool_call_to_db(self, data: dict, *, turn_anchor_id: uuid.UUID | None = None):
        return await websocket_stream_ops.save_tool_call_to_db_impl(
            MODULE,
            self,
            data,
            turn_anchor_id=turn_anchor_id,
        )

    async def _update_activity_and_quota(self, assistant_response: str):
        return await websocket_stream_ops.update_activity_and_quota_impl(MODULE, self, assistant_response)

    async def _create_task_record(self, task_title: str, assistant_response: str) -> str:
        return await websocket_stream_ops.create_task_record_impl(MODULE, self, task_title, assistant_response)

    async def _save_assistant_reply(
        self,
        assistant_response: str,
        thinking_content: list[str],
        *,
        message_id: uuid.UUID | None = None,
        turn_anchor_id: uuid.UUID | None = None,
        turn_status: str = "completed",
        complete_onboarding: bool = False,
    ) -> bool:
        return await websocket_stream_ops.save_assistant_reply_impl(
            MODULE,
            self,
            assistant_response,
            thinking_content,
            message_id=message_id,
            turn_anchor_id=turn_anchor_id,
            turn_status=turn_status,
            complete_onboarding=complete_onboarding,
        )
