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
from sqlalchemy import or_, select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession


@dataclass(frozen=True)
class TurnRuntime:
    session_found: bool
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
                        or_(
                            ChatSession.agent_id == agent_id,
                            (
                                (ChatSession.source_channel == "agent")
                                & (ChatSession.peer_agent_id == agent_id)
                            ),
                        ),
                    )
                )
            ).scalar_one_or_none()

    source_channel = str((session.source_channel if session else None) or "web")
    return TurnRuntime(
        session_found=session is not None,
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
    origin_actor_ref: str | None = None,
    origin_actor_ref_type: str | None = None,
    require_transport: bool = False,
    expected_source_channel: str | None = None,
    expected_external_conv_id: str | None = None,
    validate_external_conv_id: bool = False,
) -> bool:
    """Deliver a recovered final reply through the original channel runtime."""
    if not (reply or "").strip():
        return True
    runtime = await load_turn_runtime(
        agent_id=agent_id,
        conversation_id=conversation_id,
    )
    if require_transport and not runtime.session_found:
        logger.warning(
            "[turn_runtime] origin session disappeared before delivery: %s",
            conversation_id,
        )
        return False
    if expected_source_channel and runtime.source_channel != expected_source_channel:
        logger.warning(
            "[turn_runtime] origin channel changed before delivery: expected=%s actual=%s",
            expected_source_channel,
            runtime.source_channel,
        )
        return False
    if validate_external_conv_id and runtime.external_conv_id != expected_external_conv_id:
        logger.warning(
            "[turn_runtime] origin conversation generation changed before delivery: "
            "expected=%s actual=%s",
            expected_external_conv_id,
            runtime.external_conv_id,
        )
        return False
    channel = runtime.source_channel
    if channel in {"web", "wechat_miniprogram", "mcp"}:
        return await _deliver_web(agent_id, runtime, reply)
    if channel == "dingtalk":
        return await _deliver_dingtalk(agent_id, runtime, reply)

    delivered: bool | None = None
    if channel == "feishu":
        delivered = await _deliver_feishu(
            agent_id,
            runtime,
            reply,
            origin_actor_ref=origin_actor_ref,
            origin_actor_ref_type=origin_actor_ref_type,
        )
    elif channel == "slack":
        delivered = await _deliver_slack(agent_id, runtime, reply)
    elif channel == "wecom":
        delivered = await _deliver_wecom(
            agent_id,
            runtime,
            reply,
            origin_actor_ref=origin_actor_ref,
        )
    elif channel in {"teams", "microsoft_teams"}:
        delivered = await _deliver_teams(agent_id, runtime, reply)
    elif channel == "whatsapp":
        delivered = await _deliver_whatsapp(agent_id, runtime, reply)
    elif channel == "wechat":
        delivered = await _deliver_wechat(agent_id, runtime, reply)
    elif channel == "discord":
        delivered = await _deliver_discord(agent_id, runtime, reply)
    elif channel in {"agent", "trigger"}:
        return await _deliver_web(agent_id, runtime, reply)

    if delivered is not None:
        if delivered or require_transport:
            return delivered
        logger.warning(
            "[turn_runtime] channel redelivery failed for %s; DB history remains authoritative",
            channel,
        )
        return True

    logger.warning(
        "[turn_runtime] no restart delivery adapter for channel=%s conversation=%s; "
        "DB history remains authoritative",
        channel,
        conversation_id,
    )
    return not require_transport


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


async def _deliver_feishu(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    origin_actor_ref: str | None = None,
    origin_actor_ref_type: str | None = None,
) -> bool:
    target = str(runtime.external_conv_id or "")
    if not target:
        return False
    cfg = await _load_channel_config(agent_id, "feishu")
    if cfg is None or not cfg.app_id or not cfg.app_secret:
        return False

    if target.startswith("feishu_group_"):
        receive_id = target.removeprefix("feishu_group_")
        receive_id_type = "chat_id"
    elif target.startswith("feishu_p2p_"):
        receive_id = origin_actor_ref or target.removeprefix("feishu_p2p_")
        receive_id_type = (
            origin_actor_ref_type
            if origin_actor_ref_type in {"user_id", "open_id"}
            else "user_id"
        )
    else:
        return False

    from app.services.feishu_service import feishu_service

    try:
        result = await feishu_service.send_message(
            cfg.app_id,
            cfg.app_secret,
            receive_id,
            "text",
            json.dumps({"text": reply}, ensure_ascii=False),
            receive_id_type=receive_id_type,
        )
    except Exception:
        logger.opt(exception=True).warning("[turn_runtime] Feishu origin delivery failed")
        return False
    return result.get("code") == 0


async def _deliver_slack(agent_id: uuid.UUID, runtime: TurnRuntime, reply: str) -> bool:
    target = str(runtime.external_conv_id or "")
    channel_id = target.removeprefix("slack_") if target.startswith("slack_") else ""
    cfg = await _load_channel_config(agent_id, "slack")
    if not channel_id or cfg is None or not cfg.app_secret:
        return False
    try:
        from app.api.slack import _send_slack_messages

        await _send_slack_messages(cfg.app_secret, channel_id, reply)
        return True
    except Exception:
        logger.opt(exception=True).warning("[turn_runtime] Slack origin delivery failed")
        return False


async def _deliver_wecom(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    origin_actor_ref: str | None = None,
) -> bool:
    target = str(runtime.external_conv_id or "")
    cfg = await _load_channel_config(agent_id, "wecom")
    if cfg is None or not cfg.app_id or not cfg.app_secret:
        return False
    wecom_agent_id = str((cfg.extra_config or {}).get("wecom_agent_id") or "") or None

    if target.startswith("wecom_group_"):
        chat_id = target.removeprefix("wecom_group_")
        from app.services.wecom_service import get_wecom_access_token

        token_result = await get_wecom_access_token(cfg.app_id, cfg.app_secret)
        access_token = token_result.get("access_token")
        if access_token and chat_id:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(
                        "https://qyapi.weixin.qq.com/cgi-bin/appchat/send",
                        params={"access_token": access_token},
                        json={
                            "chatid": chat_id,
                            "msgtype": "text",
                            "text": {"content": reply},
                            "safe": 0,
                        },
                    )
                data = response.json()
                if data.get("errcode") == 0:
                    return True
                logger.warning("[turn_runtime] WeCom group delivery failed: %s", data)
            except Exception:
                logger.opt(exception=True).warning("[turn_runtime] WeCom group origin delivery failed")

        # Some inbound group transports cannot be addressed by appchat/send.
        # Preserve the current platform behavior by falling back to the exact
        # human actor who created the originating turn, never an arbitrary user.
        user_id = str(origin_actor_ref or "").strip()
    elif target.startswith("wecom_p2p_"):
        user_id = target.removeprefix("wecom_p2p_")
    else:
        return False
    if not user_id:
        return False
    try:
        from app.services.wecom_service import send_wecom_message

        result = await send_wecom_message(
            cfg.app_id,
            cfg.app_secret,
            user_id,
            reply,
            agent_id=wecom_agent_id,
        )
    except Exception:
        logger.opt(exception=True).warning("[turn_runtime] WeCom origin delivery failed")
        return False
    return result.get("errcode") == 0


async def _deliver_teams(agent_id: uuid.UUID, runtime: TurnRuntime, reply: str) -> bool:
    conversation_id = str(runtime.external_conv_id or "")
    cfg = await _load_channel_config(agent_id, "microsoft_teams")
    if cfg is None:
        cfg = await _load_channel_config(agent_id, "teams")
    if not conversation_id or cfg is None:
        return False
    try:
        from app.api.teams import _send_teams_message

        await _send_teams_message(
            cfg,
            conversation_id,
            {
                "type": "message",
                "text": reply,
                "conversation": {"id": conversation_id},
            },
        )
        return True
    except Exception:
        logger.opt(exception=True).warning("[turn_runtime] Teams origin delivery failed")
        return False


async def _deliver_whatsapp(agent_id: uuid.UUID, runtime: TurnRuntime, reply: str) -> bool:
    target = str(runtime.external_conv_id or "")
    phone = target.removeprefix("whatsapp_") if target.startswith("whatsapp_") else ""
    cfg = await _load_channel_config(agent_id, "whatsapp")
    if not phone or cfg is None:
        return False
    try:
        from app.api.whatsapp import _send_whatsapp_messages

        await _send_whatsapp_messages(cfg, phone, reply)
        return True
    except Exception:
        logger.opt(exception=True).warning("[turn_runtime] WhatsApp origin delivery failed")
        return False


async def _deliver_wechat(agent_id: uuid.UUID, runtime: TurnRuntime, reply: str) -> bool:
    target = str(runtime.external_conv_id or "")
    user_id = target.removeprefix("wechat_") if target.startswith("wechat_") else ""
    cfg = await _load_channel_config(agent_id, "wechat")
    if not user_id or cfg is None:
        return False
    try:
        from app.services.wechat_channel import (
            WECHAT_ILINK_BASE_URL,
            get_wechat_context_entry,
            send_wechat_text_message,
        )

        entry = get_wechat_context_entry(cfg.extra_config, from_user_id=user_id)
        context_token = str((entry or {}).get("context_token") or "")
        token = str((cfg.extra_config or {}).get("bot_token") or "")
        if not context_token or not token:
            return False
        await send_wechat_text_message(
            token=token,
            base_url=str((cfg.extra_config or {}).get("baseurl") or WECHAT_ILINK_BASE_URL),
            to_user_id=user_id,
            context_token=context_token,
            text=reply,
            route_tag=str((cfg.extra_config or {}).get("route_tag") or "") or None,
        )
        return True
    except Exception:
        logger.opt(exception=True).warning("[turn_runtime] WeChat origin delivery failed")
        return False


async def _deliver_discord(agent_id: uuid.UUID, runtime: TurnRuntime, reply: str) -> bool:
    target = str(runtime.external_conv_id or "")
    if target.startswith("discord_dm_"):
        channel_id = ""
        user_id = target.removeprefix("discord_dm_")
    elif target.startswith("discord_"):
        tail = target.removeprefix("discord_")
        channel_id = tail.split("_", 1)[0]
        user_id = ""
    else:
        return False
    cfg = await _load_channel_config(agent_id, "discord")
    if cfg is None or not cfg.app_secret:
        return False
    headers = {
        "Authorization": f"Bot {cfg.app_secret}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if not channel_id:
                dm = await client.post(
                    "https://discord.com/api/v10/users/@me/channels",
                    headers=headers,
                    json={"recipient_id": user_id},
                )
                if dm.status_code >= 400:
                    return False
                channel_id = str(dm.json().get("id") or "")
            response = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": reply[:2000]},
            )
        return response.status_code < 400
    except Exception:
        logger.opt(exception=True).warning("[turn_runtime] Discord origin delivery failed")
        return False


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
