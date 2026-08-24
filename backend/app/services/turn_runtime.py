"""Unified delivery adapters for replies bound to a durable ChatSession.

Callers rebuild the original runtime from the persisted session and deliver through
the same channel-specific transport, regardless of whether the reply came from a
normal turn, a confirmation continuation, or startup recovery.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from loguru import logger
from sqlalchemy import or_, select

from app.database import async_session
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.services.channel_dispatch import run_channel_send
from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult

DeliveryPartObserver = Callable[[IMDeliveryPart], Awaitable[None]]


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


async def deliver_reply_to_origin(
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
    message_id: uuid.UUID | str | None = None,
) -> bool:
    """Deliver a reply through the channel runtime persisted on its ChatSession."""
    if not (reply or "").strip():
        return True
    try:
        runtime = await load_turn_runtime(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        if require_transport and not runtime.session_found:
            logger.warning(
                "[turn_runtime] origin session disappeared before delivery: {}",
                conversation_id,
            )
            return False
        if expected_source_channel and runtime.source_channel != expected_source_channel:
            logger.warning(
                "[turn_runtime] origin channel changed before delivery: expected={} actual={}",
                expected_source_channel,
                runtime.source_channel,
            )
            return False
        if validate_external_conv_id and runtime.external_conv_id != expected_external_conv_id:
            logger.warning(
                "[turn_runtime] origin conversation generation changed before delivery: "
                "expected={} actual={}",
                expected_external_conv_id,
                runtime.external_conv_id,
            )
            return False
        if message_id is None:
            from app.models.audit import ChatMessage

            async with async_session() as db:
                candidates = (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(conversation_id),
                            ChatMessage.role == "assistant",
                        )
                        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                        .limit(10)
                    )
                ).scalars().all()
            for candidate in candidates:
                meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
                delivery = meta.get("delivery") if isinstance(meta.get("delivery"), dict) else {}
                if delivery.get("status") == "pending":
                    message_id = candidate.id
                    break
        if message_id is not None:
            if not runtime.session_found:
                if require_transport:
                    return False
                from app.services.im_delivery import register_delivery

                await register_delivery(
                    message_id,
                    IMDeliveryResult.unsupported_delivery(
                        runtime.source_channel or "web",
                        "websocket",
                        conversation_ref=str(conversation_id),
                    ),
                )
                return True
            from app.services.im_delivery import deliver_persisted_message

            result = await deliver_persisted_message(
                message_id=message_id,
                agent_id=agent_id,
                runtime=runtime,
                message=reply,
                origin_actor_ref=origin_actor_ref,
                origin_actor_ref_type=origin_actor_ref_type,
            )
            return result.ok or not require_transport
        if require_transport:
            logger.warning(
                "[turn_runtime] persisted pending reply not found before required delivery: {}",
                conversation_id,
            )
            return False
        return await deliver_message_to_runtime(
            agent_id=agent_id,
            runtime=runtime,
            message=reply,
            require_transport=require_transport,
            origin_actor_ref=origin_actor_ref,
            origin_actor_ref_type=origin_actor_ref_type,
        )
    except Exception:
        logger.opt(exception=True).warning(
            "[turn_runtime] origin delivery raised for conversation={}; "
            "DB history remains authoritative",
            conversation_id,
        )
        return False


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
    message_id: uuid.UUID | str | None = None,
) -> bool:
    """Backward-compatible recovery entry point for the unified origin delivery."""
    return await deliver_reply_to_origin(
        agent_id=agent_id,
        conversation_id=conversation_id,
        reply=reply,
        origin_actor_ref=origin_actor_ref,
        origin_actor_ref_type=origin_actor_ref_type,
        require_transport=require_transport,
        expected_source_channel=expected_source_channel,
        expected_external_conv_id=expected_external_conv_id,
        validate_external_conv_id=validate_external_conv_id,
        message_id=message_id,
    )


async def deliver_message_to_runtime(
    *,
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    message: str,
    require_transport: bool = True,
    origin_actor_ref: str | None = None,
    origin_actor_ref_type: str | None = None,
    allow_wecom_group_actor_fallback: bool = True,
    dingtalk_at_user_ids: list[str] | None = None,
    dingtalk_session_webhook: str | None = None,
) -> bool:
    """Deliver text through the exact transport bound to a loaded Session runtime.

    This is the shared transport layer for restart recovery and explicit group
    Session delivery. Callers remain responsible for authorizing the Session;
    this function never selects a different Session or channel.
    """
    result = await deliver_message_with_receipt(
        agent_id=agent_id,
        runtime=runtime,
        message=message,
        origin_actor_ref=origin_actor_ref,
        origin_actor_ref_type=origin_actor_ref_type,
        allow_wecom_group_actor_fallback=allow_wecom_group_actor_fallback,
        dingtalk_at_user_ids=dingtalk_at_user_ids,
        dingtalk_session_webhook=dingtalk_session_webhook,
    )
    if result.ok:
        return True
    if not require_transport:
        logger.warning(
            "[turn_runtime] channel redelivery failed for %s; DB history remains authoritative",
            runtime.source_channel,
        )
        return True
    return False


async def deliver_message_with_receipt(
    *,
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    message: str,
    origin_actor_ref: str | None = None,
    origin_actor_ref_type: str | None = None,
    allow_wecom_group_actor_fallback: bool = True,
    dingtalk_at_user_ids: list[str] | None = None,
    dingtalk_session_webhook: str | None = None,
    dingtalk_lock_held: bool = False,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    """Deliver through one exact Session route and preserve provider receipts."""
    if not (message or "").strip():
        return IMDeliveryResult.sent(runtime.source_channel)
    channel = runtime.source_channel
    if channel in {"web", "miniprogram", "wechat_miniprogram", "mcp"}:
        return await _deliver_web(agent_id, runtime, message)
    if channel == "dingtalk":
        if dingtalk_lock_held:
            return await _deliver_dingtalk_unlocked(
                agent_id,
                runtime,
                message,
                at_user_ids=dingtalk_at_user_ids,
                session_webhook=dingtalk_session_webhook,
            )
        return await _deliver_dingtalk(
            agent_id,
            runtime,
            message,
            at_user_ids=dingtalk_at_user_ids,
            session_webhook=dingtalk_session_webhook,
        )

    delivered: IMDeliveryResult | None = None
    if channel == "feishu":
        delivered = await _deliver_feishu(
            agent_id,
            runtime,
            message,
            origin_actor_ref=origin_actor_ref,
            origin_actor_ref_type=origin_actor_ref_type,
        )
    elif channel == "slack":
        delivered = await _deliver_slack(agent_id, runtime, message, on_part=on_part)
    elif channel == "wecom":
        delivered = await _deliver_wecom(
            agent_id,
            runtime,
            message,
            origin_actor_ref=origin_actor_ref,
            allow_group_actor_fallback=allow_wecom_group_actor_fallback,
        )
    elif channel in {"teams", "microsoft_teams"}:
        delivered = await _deliver_teams(agent_id, runtime, message, on_part=on_part)
    elif channel == "whatsapp":
        delivered = await _deliver_whatsapp(agent_id, runtime, message, on_part=on_part)
    elif channel == "wechat":
        delivered = await _deliver_wechat(agent_id, runtime, message, on_part=on_part)
    elif channel == "discord":
        delivered = await _deliver_discord(agent_id, runtime, message)
    elif channel in {"agent", "trigger"}:
        return await _deliver_web(agent_id, runtime, message)

    if delivered is not None:
        return delivered

    logger.warning(
        "[turn_runtime] no restart delivery adapter for channel=%s conversation=%s; "
        "DB history remains authoritative",
        channel,
        runtime.conversation_id,
    )
    return IMDeliveryResult.failed(channel, "transport_unavailable")


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


async def _deliver_dingtalk(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    at_user_ids: list[str] | None = None,
    session_webhook: str | None = None,
) -> IMDeliveryResult:
    return await run_channel_send(
        f"im-send:{runtime.conversation_id}",
        lambda: _deliver_dingtalk_unlocked(
            agent_id,
            runtime,
            reply,
            at_user_ids=at_user_ids,
            session_webhook=session_webhook,
        ),
    )


async def _deliver_dingtalk_unlocked(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    at_user_ids: list[str] | None = None,
    session_webhook: str | None = None,
) -> IMDeliveryResult:
    """Send one DingTalk message while the caller owns the conversation send lock."""
    if not runtime.external_conv_id:
        logger.warning("[turn_runtime] DingTalk runtime missing external_conv_id: %s", runtime.conversation_id)
        return IMDeliveryResult.failed("dingtalk", "missing_external_conversation")
    cfg = await _load_channel_config(agent_id, "dingtalk")
    if cfg is None or not cfg.app_id or not cfg.app_secret:
        logger.warning("[turn_runtime] DingTalk channel config missing for agent=%s", agent_id)
        return IMDeliveryResult.failed("dingtalk", "channel_config_unavailable")

    from app.services.dingtalk_card import _parse_target
    from app.services.dingtalk_service import send_dingtalk_v1_robot_oto_message

    space_type, space_id = _parse_target(runtime.external_conv_id, runtime.is_group)
    if not space_id:
        logger.warning("[turn_runtime] DingTalk runtime has empty target: %s", runtime.external_conv_id)
        return IMDeliveryResult.failed("dingtalk", "empty_target")

    async def _send() -> dict:
        if space_type == "IM_ROBOT":
            return await send_dingtalk_v1_robot_oto_message(
                cfg.app_id,
                cfg.app_secret,
                [space_id],
                reply,
                msg_type="markdown",
                robot_code=cfg.app_id,
            )
        if at_user_ids:
            if not session_webhook:
                logger.warning(
                    "[turn_runtime] DingTalk group mention missing temporary session webhook"
                )
                return {"errcode": -1, "errmsg": "missing session webhook"}
            return await _send_dingtalk_group_mention(
                session_webhook=session_webhook,
                message=reply,
                at_user_ids=at_user_ids,
            )
        return await _send_dingtalk_group_markdown(
            app_id=cfg.app_id,
            app_secret=cfg.app_secret,
            open_conversation_id=space_id,
            message=reply,
        )

    result = await _send()
    ok = result.get("errcode") == 0
    if not ok:
        logger.warning("[turn_runtime] DingTalk recovered reply delivery failed: %s", result)
        return IMDeliveryResult.failed("dingtalk", str(result.get("errmsg") or result.get("errcode") or "send_failed"))
    if at_user_ids:
        return IMDeliveryResult.unsupported_delivery(
            "dingtalk",
            "dingtalk_session_webhook",
            conversation_ref=space_id,
        )
    process_key = str(result.get("processQueryKey") or "")
    transport = "dingtalk_openapi_oto" if space_type == "IM_ROBOT" else "dingtalk_openapi_group"
    return IMDeliveryResult.sent(
        "dingtalk",
        IMDeliveryPart(
            transport=transport,
            provider_message_id=process_key or None,
            conversation_ref=space_id,
            recallable=bool(process_key),
        ),
    )


async def _send_dingtalk_group_mention(
    *,
    session_webhook: str,
    message: str,
    at_user_ids: list[str],
) -> dict:
    """Reply to one DingTalk group with native @ metadata."""
    payload = {
        "msgtype": "text",
        "text": {"content": message},
        "at": {"atUserIds": at_user_ids, "isAtAll": False},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(session_webhook, json=payload)
        try:
            data = response.json()
        except ValueError:
            data = {}
    except Exception as exc:
        # httpx exception strings may contain the signed webhook URL. Keep that
        # credential out of logs and tool-visible errors.
        return {
            "errcode": -1,
            "errmsg": f"temporary webhook request failed ({type(exc).__name__})",
        }
    if response.status_code >= 400 or data.get("errcode"):
        return {
            "errcode": data.get("errcode", response.status_code),
            "errmsg": data.get("errmsg") or response.text[:200],
        }
    return {"errcode": 0}


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

        # Some inbound group transports cannot be addressed by appchat/send.
        # Preserve the current platform behavior by falling back to the exact
        # human actor who created the originating turn, never an arbitrary user.
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
