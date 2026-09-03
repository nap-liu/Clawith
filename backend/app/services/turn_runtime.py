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
from app.services import turn_runtime_transports as _transport_helpers
from app.services.channel_dispatch import run_channel_send
from app.services.im_delivery import (
    IMDeliveryPart,
    IMDeliveryResult,
    MentionIntent,
    ProviderResponseUncertainError,
)
from app.services.im_markdown_media import project_agent_images_for_im

DeliveryPartObserver = Callable[[IMDeliveryPart], Awaitable[None]]
HISTORY_ONLY_CHANNELS = frozenset({"agent", "trigger", "subagent", "project"})


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
        if message_id is None and runtime.source_channel in HISTORY_ONLY_CHANNELS:
            return await deliver_message_to_runtime(
                agent_id=agent_id,
                runtime=runtime,
                message=reply,
                require_transport=require_transport,
                origin_actor_ref=origin_actor_ref,
                origin_actor_ref_type=origin_actor_ref_type,
            )
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
    mention: MentionIntent | None = None,
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
        mention=mention,
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
    mention: MentionIntent | None = None,
    dingtalk_lock_held: bool = False,
    content_format: str = "markdown",
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    """Deliver through one exact Session route and preserve provider receipts."""
    if not (message or "").strip():
        return IMDeliveryResult.sent(runtime.source_channel)
    channel = runtime.source_channel
    if mention is not None and not runtime.is_group:
        return IMDeliveryResult.failed(channel, "mention_requires_group")
    if mention is not None and channel != "dingtalk":
        return IMDeliveryResult.failed(channel, "native_mention_not_supported")
    if channel in {"web", "miniprogram", "wechat_miniprogram", "mcp"}:
        return await _deliver_web(agent_id, runtime, message)
    if channel in HISTORY_ONLY_CHANNELS:
        return await _deliver_web(agent_id, runtime, message)
    if content_format == "markdown":
        message = await project_agent_images_for_im(agent_id, message)
    if channel == "dingtalk":
        if content_format not in {"markdown", "plain_text"}:
            return IMDeliveryResult.failed(channel, "unsupported_content_format")
        if dingtalk_lock_held:
            return await _deliver_dingtalk_unlocked(
                agent_id,
                runtime,
                message,
                mention=mention,
                content_format=content_format,
                on_part=on_part,
            )
        return await _deliver_dingtalk(
            agent_id,
            runtime,
            message,
            mention=mention,
            content_format=content_format,
            on_part=on_part,
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
    return await _transport_helpers._deliver_web(agent_id, runtime, reply)


async def _load_channel_config(agent_id: uuid.UUID, channel_type: str) -> ChannelConfig | None:
    return await _transport_helpers._load_channel_config(agent_id, channel_type)


async def _deliver_dingtalk(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    mention: MentionIntent | None = None,
    content_format: str = "markdown",
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    return await run_channel_send(
        f"im-send:{runtime.conversation_id}",
        lambda: _deliver_dingtalk_unlocked(
            agent_id,
            runtime,
            reply,
            mention=mention,
            content_format=content_format,
            on_part=on_part,
        ),
    )


async def _deliver_dingtalk_unlocked(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    mention: MentionIntent | None = None,
    content_format: str = "markdown",
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    """Send one DingTalk message while the caller owns the conversation send lock."""
    if not runtime.external_conv_id:
        logger.warning("[turn_runtime] DingTalk runtime missing external_conv_id: %s", runtime.conversation_id)
        return IMDeliveryResult.failed("dingtalk", "missing_external_conversation")
    cfg = await _load_channel_config(agent_id, "dingtalk")
    if cfg is None or not cfg.app_id or not cfg.app_secret:
        logger.warning("[turn_runtime] DingTalk channel config missing for agent=%s", agent_id)
        return IMDeliveryResult.failed("dingtalk", "channel_config_unavailable")

    from app.services.dingtalk_card import _parse_target, send_message_card

    space_type, space_id = _parse_target(runtime.external_conv_id, runtime.is_group)
    if not space_id:
        logger.warning("[turn_runtime] DingTalk runtime has empty target: %s", runtime.external_conv_id)
        return IMDeliveryResult.failed("dingtalk", "empty_target")

    if mention is not None:
        if space_type == "IM_ROBOT" or not runtime.is_group:
            return IMDeliveryResult.failed("dingtalk", "mention_requires_group")
        # The canonical session-message tool owns this transport configuration.
        # The legacy group-only wrapper consumes the same config through this
        # shared runtime rather than maintaining a second template setting.
        from app.services.agent_tools import _get_tool_config

        tool_config = await _get_tool_config(agent_id, "send_session_message") or {}
        card_template_id = str(tool_config.get("card_template_id") or "").strip()
        if not card_template_id:
            logger.warning(
                "[turn_runtime] DingTalk message-card template missing for agent=%s",
                agent_id,
            )
            return IMDeliveryResult.failed(
                "dingtalk",
                "dingtalk_message_card_template_unavailable",
            )
        at_user_ids = (
            {"@ALL": "@ALL"}
            if mention.scope == "all"
            else dict(zip(mention.target_ids, mention.target_names, strict=True))
        )
        out_track_id = f"message.{uuid.uuid4().hex}"
        delivered_id = await send_message_card(
            app_id=cfg.app_id,
            app_secret=cfg.app_secret,
            card_template_id=card_template_id,
            out_track_id=out_track_id,
            content=reply,
            external_conv_id=runtime.external_conv_id,
            at_user_ids=at_user_ids,
        )
        if not delivered_id:
            return IMDeliveryResult.failed("dingtalk", "dingtalk_message_card_delivery_failed")
        part = IMDeliveryPart(
            transport="dingtalk_interactive_card",
            provider_message_id=delivered_id,
            conversation_ref=space_id,
            recallable=False,
            metadata={"mention_scope": mention.scope},
        )
        if on_part is not None:
            await on_part(part)
        return IMDeliveryResult.sent("dingtalk", part)

    async def _send() -> dict:
        sender = (
            send_dingtalk_proactive_text
            if content_format == "plain_text"
            else send_dingtalk_proactive_markdown
        )
        return await sender(
            app_id=cfg.app_id,
            app_secret=cfg.app_secret,
            target_id=space_id,
            is_group=space_type == "IM_GROUP",
            message=reply,
        )

    result = await _send()
    ok = result.get("errcode") == 0
    if not ok:
        logger.warning("[turn_runtime] DingTalk recovered reply delivery failed: %s", result)
        return IMDeliveryResult.failed("dingtalk", str(result.get("errmsg") or result.get("errcode") or "send_failed"))
    process_key = str(result.get("processQueryKey") or "")
    transport = "dingtalk_openapi_oto" if space_type == "IM_ROBOT" else "dingtalk_openapi_group"
    part = IMDeliveryPart(
        transport=transport,
        provider_message_id=process_key or None,
        conversation_ref=space_id,
        recallable=bool(process_key),
    )
    if on_part is not None:
        await on_part(part)
    return IMDeliveryResult.sent("dingtalk", part)

async def _deliver_feishu(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    origin_actor_ref: str | None = None,
    origin_actor_ref_type: str | None = None,
) -> IMDeliveryResult:
    return await _transport_helpers._deliver_feishu(
        agent_id,
        runtime,
        reply,
        origin_actor_ref=origin_actor_ref,
        origin_actor_ref_type=origin_actor_ref_type,
    )


async def _deliver_slack(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    return await _transport_helpers._deliver_slack(
        agent_id,
        runtime,
        reply,
        on_part=on_part,
    )


async def _deliver_wecom(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    origin_actor_ref: str | None = None,
    allow_group_actor_fallback: bool = True,
) -> IMDeliveryResult:
    return await _transport_helpers._deliver_wecom(
        agent_id,
        runtime,
        reply,
        origin_actor_ref=origin_actor_ref,
        allow_group_actor_fallback=allow_group_actor_fallback,
    )


async def _deliver_teams(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    return await _transport_helpers._deliver_teams(
        agent_id,
        runtime,
        reply,
        on_part=on_part,
    )


async def _deliver_whatsapp(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    return await _transport_helpers._deliver_whatsapp(
        agent_id,
        runtime,
        reply,
        on_part=on_part,
    )


async def _deliver_wechat(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
    *,
    on_part: DeliveryPartObserver | None = None,
) -> IMDeliveryResult:
    return await _transport_helpers._deliver_wechat(
        agent_id,
        runtime,
        reply,
        on_part=on_part,
    )


async def _deliver_discord(
    agent_id: uuid.UUID,
    runtime: TurnRuntime,
    reply: str,
) -> IMDeliveryResult:
    return await _transport_helpers._deliver_discord(agent_id, runtime, reply)


async def _send_dingtalk_group_message(
    *,
    app_id: str,
    app_secret: str,
    open_conversation_id: str,
    message: str,
    msg_type: str,
    raise_on_transport_error: bool = False,
) -> dict:
    from app.services.dingtalk_service import build_dingtalk_markdown_content
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
        "msgKey": "sampleText" if msg_type == "text" else "sampleMarkdown",
        "msgParam": json.dumps(
            {"content": message}
            if msg_type == "text"
            else build_dingtalk_markdown_content(message),
            ensure_ascii=False,
        ),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                headers=headers,
                json=payload,
            )
            try:
                data = resp.json()
            except ValueError as exc:
                raise ProviderResponseUncertainError(
                    "DingTalk group send returned an unreadable response"
                ) from exc
        except Exception as exc:
            if raise_on_transport_error:
                raise
            return {"errcode": -1, "errmsg": str(exc)}
    if resp.status_code >= 400 or data.get("errcode"):
        return {"errcode": data.get("errcode", resp.status_code), "errmsg": data.get("errmsg") or str(data)}
    return {"errcode": 0, "processQueryKey": data.get("processQueryKey")}


async def _send_dingtalk_group_markdown(
    *,
    app_id: str,
    app_secret: str,
    open_conversation_id: str,
    message: str,
    raise_on_transport_error: bool = False,
) -> dict:
    return await _send_dingtalk_group_message(
        app_id=app_id,
        app_secret=app_secret,
        open_conversation_id=open_conversation_id,
        message=message,
        msg_type="markdown",
        raise_on_transport_error=raise_on_transport_error,
    )


async def send_dingtalk_proactive_markdown(
    *,
    app_id: str,
    app_secret: str,
    target_id: str,
    is_group: bool,
    message: str,
) -> dict:
    """Send Markdown through durable robot OpenAPI identifiers only."""
    if is_group:
        return await _send_dingtalk_group_markdown(
            app_id=app_id,
            app_secret=app_secret,
            open_conversation_id=target_id,
            message=message,
            raise_on_transport_error=True,
        )

    from app.services.dingtalk_service import send_dingtalk_v1_robot_oto_message

    return await send_dingtalk_v1_robot_oto_message(
        app_id,
        app_secret,
        [target_id],
        message,
        msg_type="markdown",
        robot_code=app_id,
        raise_on_transport_error=True,
    )


async def send_dingtalk_proactive_text(
    *,
    app_id: str,
    app_secret: str,
    target_id: str,
    is_group: bool,
    message: str,
) -> dict:
    """Send plain text so command newlines retain their exact semantics."""
    if is_group:
        return await _send_dingtalk_group_message(
            app_id=app_id,
            app_secret=app_secret,
            open_conversation_id=target_id,
            message=message,
            msg_type="text",
            raise_on_transport_error=True,
        )

    from app.services.dingtalk_service import send_dingtalk_v1_robot_oto_message

    return await send_dingtalk_v1_robot_oto_message(
        app_id,
        app_secret,
        [target_id],
        message,
        msg_type="text",
        robot_code=app_id,
        raise_on_transport_error=True,
    )
