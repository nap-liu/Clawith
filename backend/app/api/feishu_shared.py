"""Feishu OAuth and Channel API routes."""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user, set_access_token_cookie
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigCreate, TokenResponse, UserOut
from app.schemas.channel_config import ChannelConfigPublic as ChannelConfigOut
# Shared, channel-agnostic LLM entry point now lives in services/channel_llm.
# Re-exported here for backwards compatibility (older code does
# `from app.api.feishu import _call_agent_llm`); new code imports it directly.
from app.services.channel_llm import _call_agent_llm  # noqa: F401
from app.services.channel_dispatch import (
    ChannelReactions,
    channel_session_lock_key,
    run_channel_message,
)
from app.services.channel_commands import is_channel_command
from app.services.chat_attachments import (
    attachment_from_workspace_path,
    normalize_attachment_metadata,
)
from app.services.feishu_service import feishu_service
from app.services.im_thinking_output import resolve_im_thinking_enabled
from app.services.storage import agent_storage_key, get_storage_backend, store_agent_upload

router = APIRouter(tags=["feishu"])

# Number of tool status lines to keep visible in the Feishu card.
# Shows the last N non-running lines plus any active "running" entry.
_TOOL_STATUS_KEEP_LINES = 20

_USER_RESOLUTION_ERROR_TIP = (
    "抱歉，我暂时无法稳定识别你的飞书账号，已停止本次处理以避免重复创建账号。"
    "请稍后重试，或联系管理员检查飞书 Contact API 权限。"
)


async def _persist_feishu_control_reply(
    *,
    agent_id: uuid.UUID,
    config: ChannelConfig,
    sender_open_id: str,
    chat_type: str,
    chat_id: str,
    message: str,
    artifact_role: str,
) -> None:
    """Persist and deliver an early Feishu ACK before canonical identity exists."""
    from app.database import async_session
    from app.models.agent import Agent
    from app.services.channel_session import find_or_create_channel_session
    from app.services.im_delivery import (
        IMDeliveryPart,
        IMDeliveryResult,
        persist_and_deliver_message,
    )

    is_group = chat_type == "group" and bool(chat_id)
    receive_id = chat_id if is_group else sender_open_id
    receive_id_type = "chat_id" if is_group else "open_id"
    if not receive_id:
        raise RuntimeError("feishu_control_recipient_missing")
    control_route = f"__control_feishu_{'group' if is_group else 'p2p'}_{receive_id}"
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        if agent is None:
            raise RuntimeError("feishu_control_agent_not_found")
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=agent.creator_id,
            external_conv_id=control_route,
            source_channel="feishu",
            first_message_title=message,
            is_group=is_group,
            group_name=f"Feishu Group {chat_id[:12]}" if is_group else None,
        )
        session_id = str(session.id)
        await db.commit()

    async def _deliver(delivery_message: str, on_part) -> IMDeliveryResult:
        import json

        response = await feishu_service.send_message(
            config.app_id,
            config.app_secret,
            receive_id,
            "text",
            json.dumps({"text": delivery_message}, ensure_ascii=False),
            receive_id_type=receive_id_type,
        )
        message_id = str(
            ((response.get("data") or {}).get("message_id"))
            or response.get("message_id")
            or ""
        )
        part = IMDeliveryPart(
            transport="feishu_message",
            provider_message_id=message_id or None,
            conversation_ref=receive_id,
            artifact_role=artifact_role,
            recallable=bool(message_id),
        )
        await on_part(part)
        return IMDeliveryResult.sent("feishu", part)

    await persist_and_deliver_message(
        agent_id=agent_id,
        user_id=agent.creator_id,
        conversation_id=session_id,
        channel="feishu",
        message=message,
        artifact_role=artifact_role,
        deliver=_deliver,
    )


def _build_card(
    answer_text: str,
    thinking_text: str = "",
    streaming: bool = False,
    tool_status_lines: list[str] | None = None,
    agent_name: str = "AI 回复",
) -> dict:
    """Build a Feishu interactive card for streaming replies."""
    from app.services.user_output import sanitize_user_visible_text

    answer_text = sanitize_user_visible_text(answer_text or "")
    thinking_text = sanitize_user_visible_text(thinking_text or "")
    agent_name = sanitize_user_visible_text(agent_name or "") or "AI"
    sanitized_status_lines = [
        sanitize_user_visible_text(line) for line in (tool_status_lines or [])
    ]
    tool_status_lines = [line for line in sanitized_status_lines if line.strip()]
    elements = []

    if tool_status_lines:
        elements.append({
            "tag": "markdown",
            "content": "\n".join(tool_status_lines[-_TOOL_STATUS_KEEP_LINES:]),
        })
        elements.append({"tag": "hr"})

    if thinking_text:
        think_preview = thinking_text[:200].replace("\n", " ")
        elements.append({
            "tag": "markdown",
            "content": f"<font color='grey'>💭 **Thinking**\n{think_preview}{'...' if len(thinking_text) > 200 else ''}</font>",
        })
        elements.append({"tag": "hr"})

    body = answer_text + ("▌" if streaming and answer_text else ("..." if streaming else ""))
    elements.append({"tag": "markdown", "content": body or "..."})
    return {
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"content": agent_name, "tag": "plain_text"},
        },
        "elements": elements,
    }


async def _build_projected_stream_card(
    agent_id: uuid.UUID,
    answer_text: str,
    *,
    thinking_text: str = "",
    tool_status_lines: list[str] | None = None,
    agent_name: str = "AI 回复",
) -> dict:
    """Build an intermediate card with transient IM image URLs."""
    from app.services.im_markdown_media import project_agent_images_for_im

    projected = await project_agent_images_for_im(agent_id, answer_text)
    return _build_card(
        answer_text=projected,
        thinking_text=thinking_text,
        streaming=True,
        tool_status_lines=tool_status_lines,
        agent_name=agent_name,
    )


def _looks_like_error_text(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    markers = (
        "⚠️",
        "[error]",
        "error:",
        "error ",
        "failed",
        "failure",
        "timeout",
        "timed out",
        "调用模型出错",
        "未配置 llm 模型",
        "数字员工未找到",
    )
    return any(marker in normalized for marker in markers)


def _normalize_tool_error(tool_name: str, result: object) -> str | None:
    text = "" if result is None else str(result).strip()
    if not text or not _looks_like_error_text(text):
        return None
    compact = " ".join(text.split())
    if len(compact) > 240:
        compact = compact[:240].rstrip() + "..."
    return f"`{tool_name}`: {compact}"


def _append_error_details(reply_text: str, tool_errors: list[str]) -> str:
    base = (reply_text or "").strip()
    unique_errors: list[str] = []
    seen: set[str] = set()
    for item in tool_errors:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_errors.append(normalized)
    if not unique_errors:
        return base
    details = "\n".join(f"- {item}" for item in unique_errors)
    if not base:
        return f"执行失败，错误信息如下：\n{details}"
    if _looks_like_error_text(base):
        return f"{base}\n\n执行过程中出现以下错误：\n{details}"
    return base


class _SerialPatchQueue:
    """Serialize patch requests for one Feishu message to prevent out-of-order overwrite."""

    def __init__(self):
        self._tail: asyncio.Task | None = None

    def enqueue(self, job_factory: Callable[[], Awaitable[None]]) -> None:
        prev = self._tail

        async def _runner():
            if prev:
                try:
                    await prev
                except Exception as e:
                    logger.warning(f"[Feishu] Previous patch job failed before next job: {e}")
            await job_factory()

        self._tail = asyncio.create_task(_runner())

    async def drain(self) -> None:
        if self._tail:
            await self._tail


# ─── OAuth ──────────────────────────────────────────────

from fastapi.responses import RedirectResponse

__all__ = [name for name in globals() if not name.startswith("__")]
