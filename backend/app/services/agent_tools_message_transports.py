"""Feishu, DingTalk, WeCom, and Slack message transports."""

import json
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.channel_config import ChannelConfig
from app.services.agent_tools_outbound_core import (
    _duplicate_outbound_claim_result,
    _persist_outbound_channel_message,
)
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import get_platform_user_by_org_member
from app.services.im_delivery import (
    IMDeliveryPart,
    IMDeliveryResult,
    register_delivery,
)
from app.services.im_markdown_media import project_agent_images_for_im
from app.services.recipient_resolver import (
    RecipientResolutionError,
    resolve_human_channel_recipient,
)
from app.services.user_output import sanitize_user_visible_text


async def _send_feishu_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send Feishu IM by canonical platform user_id."""
    canonical_user_id = (args.get("user_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()
    if not canonical_user_id or not message_text:
        return "❌ Please provide canonical user_id and message content"
    try:
        from app.services.feishu_service import FeishuAPIError, feishu_service

        async with async_session() as db:
            try:
                route = await resolve_human_channel_recipient(db, agent_id, canonical_user_id, channel="feishu")
            except RecipientResolutionError as exc:
                return exc.as_json()
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured.is_(True),
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no Feishu channel configured"
            receive_id = (route.member.external_id or route.member.open_id or "").strip()
            receive_id_type = "user_id" if route.member.external_id else "open_id"
            if not receive_id:
                return RecipientResolutionError(
                    "recipient_unreachable", "Canonical user has no usable Feishu endpoint"
                ).as_json()
            session = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=route.user.id,
                external_conv_id=f"feishu_p2p_{receive_id}",
                source_channel="feishu",
                first_message_title=f"[Agent → {route.user.display_name}]",
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=route.user.id,
                session=session,
                content=message_text,
                source_channel="feishu",
                actor_ref=receive_id,
                target_name=route.user.display_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("feishu"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)
            delivery_text = await project_agent_images_for_im(agent_id, message_text)
            try:
                resp = await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    receive_id=receive_id,
                    msg_type="text",
                    content=json.dumps({"text": delivery_text}, ensure_ascii=False),
                    receive_id_type=receive_id_type,
                )
            except FeishuAPIError as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("feishu", exc),
                )
                return f"❌ Feishu send failed: {exc.user_message}"
            if resp.get("code") != 0:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.failed("feishu", str(resp.get("code") or "send_failed")),
                )
                return f"❌ Feishu send failed: {resp.get('msg')} (code {resp.get('code')})"

            external_message_id = str(
                ((resp.get("data") or {}).get("message_id"))
                or resp.get("message_id")
                or ""
            ) or None
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "feishu",
                    IMDeliveryPart(
                        transport="feishu_message",
                        provider_message_id=external_message_id,
                        conversation_ref=receive_id,
                        recallable=bool(external_message_id),
                    ),
                ),
            )
            return (
                f"✅ Message sent to {route.user.display_name} via Feishu\n"
                f"message_id: {receipt_id}"
            )
    except Exception as e:
        logger.exception("[Feishu] canonical send failed")
        return f"❌ Message send error: {str(e)[:200]}"


async def _send_dingtalk_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send message via DingTalk channel using Open API."""
    from app.services.dingtalk_service import send_dingtalk_message

    try:
        async with async_session() as db:
            # 1. Get DingTalk channel config
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "dingtalk",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no DingTalk channel configured"

            # 2. Get recipient's user_id (external_id)
            user_id = target_member.external_id
            if not user_id:
                # Try to use unionid or openid as fallback
                user_id = target_member.unionid or target_member.open_id
                if not user_id:
                    return f"❌ {member_name} has no DingTalk user_id"

            logger.info(f"[DingTalk] Sending to user_id: {user_id}")

            # Get agent_id from extra_config (required for DingTalk API)
            agent_id_dingtalk = config.extra_config.get("agent_id") if config.extra_config else None

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent_obj.tenant_id if agent_obj else None,
            )
            conv_id = f"dingtalk_p2p_{user_id}"
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user.id,
                external_conv_id=conv_id,
                source_channel="dingtalk",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="dingtalk",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("dingtalk"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)
            delivery_text = await project_agent_images_for_im(agent_id, message_text)

            # 3. Send message via DingTalk service
            result = await send_dingtalk_message(
                app_id=config.app_id,
                app_secret=config.app_secret,
                user_id=user_id,
                message=delivery_text,
                agent_id=agent_id_dingtalk,
            )

            if result.get("errcode") == 0:
                process_key = str(result.get("processQueryKey") or "") or None
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.sent(
                        "dingtalk",
                        IMDeliveryPart(
                            transport="dingtalk_openapi_oto",
                            provider_message_id=process_key,
                            conversation_ref=str(user_id),
                            recallable=bool(process_key),
                        ),
                    ),
                )
                logger.info(f"[DingTalk] Proactive message saved to session {sess.id}")
                return f"✅ Message sent to {member_name} via DingTalk\nmessage_id: {receipt_id}"
            else:
                errmsg = result.get("errmsg", "Unknown error")
                logger.error(f"[DingTalk] Send failed: {result}")
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.failed("dingtalk", str(result.get("errcode") or errmsg)),
                )
                return f"❌ DingTalk send failed: {errmsg}"

    except Exception as e:
        logger.exception("[DingTalk] Error")
        return f"❌ DingTalk message error: {str(e)[:200]}"


async def _send_wecom_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send message via WeCom channel using Open API."""
    from app.services.wecom_service import send_wecom_message

    try:
        async with async_session() as db:
            # 1. Get WeCom channel config
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "wecom",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no WeCom channel configured"

            # 2. Get recipient's user_id
            user_id = target_member.external_id
            if not user_id:
                user_id = target_member.open_id
                if not user_id:
                    return f"❌ {member_name} has no WeCom user_id"

            logger.info(f"[WeCom] Sending to user_id: {user_id}")

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent.tenant_id if agent else None,
            )
            conv_id = f"wecom_p2p_{user_id}"
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user.id,
                external_conv_id=conv_id,
                source_channel="wecom",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="wecom",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("wecom"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)
            delivery_text = await project_agent_images_for_im(agent_id, message_text)

            # 3. Send message via WeCom service
            result = await send_wecom_message(
                config.app_id,
                config.app_secret,
                user_id,
                delivery_text,
                agent_id=str((config.extra_config or {}).get("wecom_agent_id") or "") or None,
            )

            if result.get("errcode") == 0:
                msgid = str(result.get("msgid") or "") or None
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.sent(
                        "wecom",
                        IMDeliveryPart(
                            transport="wecom_app",
                            provider_message_id=msgid,
                            conversation_ref=str(user_id),
                            recallable=bool(msgid),
                        ),
                    ),
                )
                logger.info(f"[WeCom] Proactive message saved to session {sess.id}")
                return f"✅ Message sent to {member_name} via WeCom\nmessage_id: {receipt_id}"
            else:
                errmsg = result.get("errmsg", "Unknown error")
                logger.error(f"[WeCom] Send failed: {result}")
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.failed("wecom", str(result.get("errcode") or errmsg)),
                )
                return f"❌ WeCom send failed: {errmsg}"

    except Exception as e:
        logger.exception("[WeCom] Error")
        return f"❌ WeCom message error: {str(e)[:200]}"


async def _send_slack_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send proactive Slack DM via conversations.open + chat.postMessage."""
    import httpx

    from app.api.slack import _send_slack_messages

    try:
        async with async_session() as db:
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "slack",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no Slack channel configured"

            user_id = (target_member.external_id or "").strip()
            if not user_id:
                return f"❌ {member_name} has no Slack user_id"

            bot_token = (config.app_secret or "").strip()
            if not bot_token:
                return "❌ Slack bot token is missing"

            async with httpx.AsyncClient(timeout=10) as client:
                open_resp = await client.post(
                    "https://slack.com/api/conversations.open",
                    headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                    json={"users": user_id},
                )
                data = open_resp.json()
                if open_resp.status_code >= 400 or not data.get("ok"):
                    err = data.get("error") or open_resp.text[:200]
                    return f"❌ Slack conversations.open failed: {err}"
                channel_id = ((data.get("channel") or {}).get("id") or "").strip()

            if not channel_id:
                return f"❌ Slack DM channel unavailable for {member_name}"

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent_obj.tenant_id if agent_obj else None,
            )
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user.id,
                external_conv_id=f"slack_{channel_id}",
                source_channel="slack",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="slack",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("slack"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)
            delivery_text = await project_agent_images_for_im(agent_id, message_text)

            async def _record_slack_part(response: dict) -> None:
                part = IMDeliveryPart(
                    transport="slack",
                    provider_message_id=str(response.get("ts") or "") or None,
                    conversation_ref=str(response.get("channel") or channel_id),
                    artifact_role="chunk",
                    recallable=bool(response.get("ts")),
                )
                await append_delivery_part(receipt_id, part)

            try:
                slack_responses = await _send_slack_messages(
                    bot_token,
                    channel_id,
                    delivery_text,
                    on_result=_record_slack_part,
                )
            except Exception as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("slack", exc),
                )
                raise
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "slack",
                    *(
                        IMDeliveryPart(
                            transport="slack",
                            provider_message_id=str(response.get("ts") or "") or None,
                            conversation_ref=str(response.get("channel") or channel_id),
                            artifact_role="chunk",
                            recallable=bool(response.get("ts")),
                        )
                        for response in slack_responses
                    ),
                ),
            )
            logger.info(f"[Slack] Proactive message saved to session {sess.id}")
            return f"✅ Message sent to {member_name} via Slack\nmessage_id: {receipt_id}"
    except Exception as e:
        logger.exception("[Slack] Error")
        return f"❌ Slack message error: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
