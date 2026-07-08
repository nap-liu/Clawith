"""Runtime delivery adapters for restart-resumed turns.

Startup recovery resumes a turn loop; it should not know channel-specific
transport details. This module rebuilds the original runtime from the durable
ChatSession, then delivers the final assistant reply through that runtime.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import httpx
from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession


@dataclass(frozen=True)
class TurnRuntime:
    source_channel: str
    conversation_id: str
    external_conv_id: str | None
    is_group: bool


async def load_turn_runtime(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> TurnRuntime:
    """Rebuild the runtime envelope for a persisted turn."""
    session: ChatSession | None = None
    try:
        session_id = uuid.UUID(str(conversation_id))
    except (TypeError, ValueError):
        session_id = None

    if session_id is not None:
        async with async_session() as db:
            session = (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.agent_id == agent_id,
                    )
                )
            ).scalar_one_or_none()

    source_channel = str((session.source_channel if session else None) or "web")
    return TurnRuntime(
        source_channel=source_channel,
        conversation_id=str(conversation_id),
        external_conv_id=(session.external_conv_id if session else None),
        is_group=bool(session.is_group if session else False),
    )


async def deliver_recovered_reply_to_origin(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    reply: str,
) -> bool:
    """Deliver a recovered final reply through the original channel runtime."""
    if not (reply or "").strip():
        return True
    runtime = await load_turn_runtime(
        agent_id=agent_id,
        conversation_id=conversation_id,
    )
    channel = runtime.source_channel
    if channel == "web":
        return await _deliver_web(agent_id, runtime, reply)
    if channel == "dingtalk":
        return await _deliver_dingtalk(agent_id, runtime, reply)
    if channel in {"agent", "trigger"}:
        return await _deliver_web(agent_id, runtime, reply)

    logger.warning(
        "[turn_runtime] no restart delivery adapter for channel=%s conversation=%s; "
        "DB history remains authoritative",
        channel,
        conversation_id,
    )
    return True


async def _deliver_web(agent_id: uuid.UUID, runtime: TurnRuntime, reply: str) -> bool:
    """Mirror the recovered reply to live web viewers; DB history is authoritative."""
    try:
        from app.api.websocket import manager

        await manager.send_to_session(
            str(agent_id),
            runtime.conversation_id,
            {"type": "done", "role": "assistant", "content": reply},
        )
    except Exception:
        logger.opt(exception=True).warning(
            "[turn_runtime] web live delivery failed; DB history remains authoritative"
        )
    return True


async def _load_channel_config(agent_id: uuid.UUID, channel_type: str) -> ChannelConfig | None:
    async with async_session() as db:
        return (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == channel_type,
                )
            )
        ).scalar_one_or_none()


async def _deliver_dingtalk(agent_id: uuid.UUID, runtime: TurnRuntime, reply: str) -> bool:
    if not runtime.external_conv_id:
        logger.warning("[turn_runtime] DingTalk runtime missing external_conv_id: %s", runtime.conversation_id)
        return False
    cfg = await _load_channel_config(agent_id, "dingtalk")
    if cfg is None or not cfg.app_id or not cfg.app_secret:
        logger.warning("[turn_runtime] DingTalk channel config missing for agent=%s", agent_id)
        return False

    from app.services.dingtalk_card import _parse_target
    from app.services.dingtalk_service import send_dingtalk_v1_robot_oto_message

    space_type, space_id = _parse_target(runtime.external_conv_id, runtime.is_group)
    if not space_id:
        logger.warning("[turn_runtime] DingTalk runtime has empty target: %s", runtime.external_conv_id)
        return False

    if space_type == "IM_ROBOT":
        result = await send_dingtalk_v1_robot_oto_message(
            cfg.app_id,
            cfg.app_secret,
            [space_id],
            reply,
            msg_type="markdown",
            robot_code=cfg.app_id,
        )
    else:
        result = await _send_dingtalk_group_markdown(
            app_id=cfg.app_id,
            app_secret=cfg.app_secret,
            open_conversation_id=space_id,
            message=reply,
        )
    ok = result.get("errcode") == 0
    if not ok:
        logger.warning("[turn_runtime] DingTalk recovered reply delivery failed: %s", result)
    return ok


async def _send_dingtalk_group_markdown(
    *,
    app_id: str,
    app_secret: str,
    open_conversation_id: str,
    message: str,
) -> dict:
    from app.services.dingtalk_token import dingtalk_token_manager

    access_token = await dingtalk_token_manager.get_token(app_id, app_secret)
    if not access_token:
        return {"errcode": -1, "errmsg": "missing access token"}

    headers = {
        "x-acs-dingtalk-access-token": access_token,
        "Content-Type": "application/json",
    }
    payload = {
        "robotCode": app_id,
        "openConversationId": open_conversation_id,
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": "Notification", "text": message}, ensure_ascii=False),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                headers=headers,
                json=payload,
            )
            data = resp.json()
        except Exception as exc:
            return {"errcode": -1, "errmsg": str(exc)}
    if resp.status_code >= 400 or data.get("errcode"):
        return {"errcode": data.get("errcode", resp.status_code), "errmsg": data.get("errmsg") or str(data)}
    return {"errcode": 0, "processQueryKey": data.get("processQueryKey")}
