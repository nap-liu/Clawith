from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import httpx
from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult

if TYPE_CHECKING:
    from app.services.turn_runtime import DeliveryPartObserver, TurnRuntime


async def _deliver_web(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
) -> IMDeliveryResult:
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
    return IMDeliveryResult.unsupported_delivery(
        runtime.source_channel,
        "websocket",
        conversation_ref=runtime.conversation_id,
    )


async def _load_channel_config(agent_id: uuid.UUID, channel_type: str) -> ChannelConfig | None:
    async with async_session() as db:
        return (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == channel_type,
                    ChannelConfig.is_configured.is_(True),
                )
            )
        ).scalar_one_or_none()


async def _deliver_feishu(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    origin_actor_ref: str | None = None,
    origin_actor_ref_type: str | None = None,
) -> IMDeliveryResult:
    target = str(runtime.external_conv_id or "")
    if not target:
        return IMDeliveryResult.failed("feishu", "missing_target")
    cfg = await _load_channel_config(agent_id, "feishu")
    if cfg is None or not cfg.app_id or not cfg.app_secret:
        return IMDeliveryResult.failed("feishu", "channel_config_unavailable")

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
        return IMDeliveryResult.failed("feishu", "invalid_target")

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
    except Exception as exc:
        logger.opt(exception=True).warning("[turn_runtime] Feishu origin delivery failed")
        return IMDeliveryResult.from_exception("feishu", exc)
    if result.get("code") != 0:
        return IMDeliveryResult.failed("feishu", str(result.get("msg") or result.get("code") or "send_failed"))
    message_id = str(((result.get("data") or {}).get("message_id")) or result.get("message_id") or "")
    return IMDeliveryResult.sent(
        "feishu",
        IMDeliveryPart(
            transport="feishu_message",
            provider_message_id=message_id or None,
            conversation_ref=receive_id,
            recallable=bool(message_id),
        ),
    )


async def _deliver_slack(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    target = str(runtime.external_conv_id or "")
    channel_id = target.removeprefix("slack_") if target.startswith("slack_") else ""
    cfg = await _load_channel_config(agent_id, "slack")
    if not channel_id or cfg is None or not cfg.app_secret:
        return IMDeliveryResult.failed("slack", "channel_config_unavailable")
    try:
        from app.api.slack import _send_slack_messages

        async def _observe(response: dict) -> None:
            if on_part is not None:
                await on_part(
                    IMDeliveryPart(
                        transport="slack",
                        provider_message_id=str(response.get("ts") or "") or None,
                        conversation_ref=str(response.get("channel") or channel_id),
                        artifact_role="chunk",
                        recallable=bool(response.get("ts")),
                    )
                )

        responses = await _send_slack_messages(
            cfg.app_secret,
            channel_id,
            reply,
            on_result=_observe,
        )
        parts = tuple(
            IMDeliveryPart(
                transport="slack",
                provider_message_id=str(response.get("ts") or "") or None,
                conversation_ref=str(response.get("channel") or channel_id),
                artifact_role="chunk",
                recallable=bool(response.get("ts")),
            )
            for response in responses
        )
        return IMDeliveryResult.sent("slack", *parts)
    except Exception as exc:
        logger.opt(exception=True).warning("[turn_runtime] Slack origin delivery failed")
        return IMDeliveryResult.from_exception("slack", exc)


async def _deliver_wecom(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    origin_actor_ref: str | None = None,
    allow_group_actor_fallback: bool = True,
) -> IMDeliveryResult:
    target = str(runtime.external_conv_id or "")
    cfg = await _load_channel_config(agent_id, "wecom")
    if cfg is None or not cfg.app_id or not cfg.app_secret:
        return IMDeliveryResult.failed("wecom", "channel_config_unavailable")
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
                    return IMDeliveryResult.unsupported_delivery(
                        "wecom",
                        "wecom_appchat",
                        conversation_ref=chat_id,
                    )
                logger.warning("[turn_runtime] WeCom group delivery failed: %s", data)
            except Exception:
                logger.opt(exception=True).warning("[turn_runtime] WeCom group origin delivery failed")

        if not allow_group_actor_fallback:
            return IMDeliveryResult.failed("wecom", "group_send_failed")
        user_id = str(origin_actor_ref or "").strip()
    elif target.startswith("wecom_p2p_"):
        user_id = target.removeprefix("wecom_p2p_")
    else:
        return IMDeliveryResult.failed("wecom", "invalid_target")
    if not user_id:
        return IMDeliveryResult.failed("wecom", "missing_user")
    try:
        from app.services.wecom_service import send_wecom_message

        result = await send_wecom_message(
            cfg.app_id,
            cfg.app_secret,
            user_id,
            reply,
            agent_id=wecom_agent_id,
        )
    except Exception as exc:
        logger.opt(exception=True).warning("[turn_runtime] WeCom origin delivery failed")
        return IMDeliveryResult.from_exception("wecom", exc)
    if result.get("errcode") != 0:
        return IMDeliveryResult.failed("wecom", str(result.get("errmsg") or result.get("errcode") or "send_failed"))
    msgid = str(result.get("msgid") or "")
    return IMDeliveryResult.sent(
        "wecom",
        IMDeliveryPart(
            transport="wecom_app",
            provider_message_id=msgid or None,
            conversation_ref=user_id,
            recallable=bool(msgid),
        ),
    )


async def _deliver_teams(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    conversation_id = str(runtime.external_conv_id or "")
    cfg = await _load_channel_config(agent_id, "microsoft_teams")
    if not conversation_id or cfg is None:
        return IMDeliveryResult.failed("microsoft_teams", "channel_config_unavailable")
    try:
        from app.api.teams import _send_teams_message

        service_url = str((cfg.extra_config or {}).get("service_url") or "")

        async def _observe(response: dict) -> None:
            if on_part is not None:
                await on_part(
                    IMDeliveryPart(
                        transport="microsoft_teams",
                        provider_message_id=str(response.get("id") or "") or None,
                        conversation_ref=conversation_id,
                        artifact_role="chunk",
                        recallable=bool(response.get("id")),
                        metadata={"service_url": service_url},
                    )
                )

        responses = await _send_teams_message(
            cfg,
            conversation_id,
            {
                "type": "message",
                "text": reply,
                "conversation": {"id": conversation_id},
            },
            on_result=_observe,
        )
        parts = tuple(
            IMDeliveryPart(
                transport="microsoft_teams",
                provider_message_id=str(response.get("id") or "") or None,
                conversation_ref=conversation_id,
                artifact_role="chunk",
                recallable=bool(response.get("id")),
                metadata={"service_url": service_url},
            )
            for response in responses
        )
        return IMDeliveryResult.sent("microsoft_teams", *parts)
    except Exception as exc:
        logger.opt(exception=True).warning("[turn_runtime] Teams origin delivery failed")
        return IMDeliveryResult.from_exception("microsoft_teams", exc)


async def _deliver_whatsapp(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    target = str(runtime.external_conv_id or "")
    phone = target.removeprefix("whatsapp_") if target.startswith("whatsapp_") else ""
    cfg = await _load_channel_config(agent_id, "whatsapp")
    if not phone or cfg is None:
        return IMDeliveryResult.failed("whatsapp", "channel_config_unavailable")
    try:
        from app.api.whatsapp import _send_whatsapp_messages

        async def _observe(response: dict) -> None:
            message_rows = response.get("messages") or []
            wamid = str((message_rows[0] if message_rows else {}).get("id") or "")
            if on_part is not None:
                await on_part(
                    IMDeliveryPart(
                        transport="whatsapp_cloud",
                        provider_message_id=wamid or None,
                        conversation_ref=phone,
                        artifact_role="chunk",
                        recallable=False,
                    )
                )

        responses = await _send_whatsapp_messages(cfg, phone, reply, on_result=_observe)
        parts = []
        for response in responses:
            message_rows = response.get("messages") or []
            wamid = str((message_rows[0] if message_rows else {}).get("id") or "")
            parts.append(
                IMDeliveryPart(
                    transport="whatsapp_cloud",
                    provider_message_id=wamid or None,
                    conversation_ref=phone,
                    artifact_role="chunk",
                    recallable=False,
                )
            )
        return IMDeliveryResult.sent("whatsapp", *parts)
    except Exception as exc:
        logger.opt(exception=True).warning("[turn_runtime] WhatsApp origin delivery failed")
        return IMDeliveryResult.from_exception("whatsapp", exc)


async def _deliver_wechat(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    target = str(runtime.external_conv_id or "")
    user_id = target.removeprefix("wechat_") if target.startswith("wechat_") else ""
    cfg = await _load_channel_config(agent_id, "wechat")
    if not user_id or cfg is None:
        return IMDeliveryResult.failed("wechat", "channel_config_unavailable")
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
            return IMDeliveryResult.failed("wechat", "context_unavailable")

        async def _observe(response: dict) -> None:
            if on_part is not None:
                await on_part(
                    IMDeliveryPart(
                        transport="wechat_ilink",
                        provider_message_id=str(response.get("client_id") or "") or None,
                        conversation_ref=user_id,
                        artifact_role="chunk",
                        recallable=False,
                    )
                )

        responses = await send_wechat_text_message(
            token=token,
            base_url=str((cfg.extra_config or {}).get("baseurl") or WECHAT_ILINK_BASE_URL),
            to_user_id=user_id,
            context_token=context_token,
            text=reply,
            route_tag=str((cfg.extra_config or {}).get("route_tag") or "") or None,
            on_result=_observe,
        )
        return IMDeliveryResult.sent(
            "wechat",
            *(
                IMDeliveryPart(
                    transport="wechat_ilink",
                    provider_message_id=str(response.get("client_id") or "") or None,
                    conversation_ref=user_id,
                    artifact_role="chunk",
                    recallable=False,
                )
                for response in responses
            ),
        )
    except Exception as exc:
        logger.opt(exception=True).warning("[turn_runtime] WeChat origin delivery failed")
        return IMDeliveryResult.from_exception("wechat", exc)


async def _deliver_discord(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
) -> IMDeliveryResult:
    target = str(runtime.external_conv_id or "")
    if target.startswith("discord_dm_"):
        channel_id = ""
        user_id = target.removeprefix("discord_dm_")
    elif target.startswith("discord_"):
        tail = target.removeprefix("discord_")
        channel_id = tail.split("_", 1)[0]
        user_id = ""
    else:
        return IMDeliveryResult.failed("discord", "invalid_target")
    cfg = await _load_channel_config(agent_id, "discord")
    if cfg is None or not cfg.app_secret:
        return IMDeliveryResult.failed("discord", "channel_config_unavailable")
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
                    return IMDeliveryResult.failed("discord", "dm_open_failed")
                channel_id = str(dm.json().get("id") or "")
            response = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": reply[:2000]},
            )
        if response.status_code >= 400:
            return IMDeliveryResult.failed("discord", str(response.status_code))
        data = response.json()
        message_id = str(data.get("id") or "")
        return IMDeliveryResult.sent(
            "discord",
            IMDeliveryPart(
                transport="discord_gateway",
                provider_message_id=message_id or None,
                conversation_ref=channel_id,
                recallable=bool(message_id),
            ),
        )
    except Exception as exc:
        logger.opt(exception=True).warning("[turn_runtime] Discord origin delivery failed")
        return IMDeliveryResult.from_exception("discord", exc)
