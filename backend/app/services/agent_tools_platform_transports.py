"""Teams, WeChat, and platform-user message transports."""

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.services.agent_tools_outbound_core import (
    _duplicate_outbound_claim_result,
    _persist_outbound_channel_message,
)
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import get_platform_user_by_org_member
from app.services.im_delivery import IMDeliveryResult, register_delivery
from app.services.recipient_resolver import (
    RecipientResolutionError,
    resolve_platform_user_recipient,
)
from app.services.user_output import sanitize_user_visible_text


async def _send_teams_channel_message(
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
    """Send proactive Teams message using the latest known conversation context."""
    from app.api.teams import _send_teams_message

    try:
        async with async_session() as db:
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "microsoft_teams",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no Teams channel configured"

            service_url = str((config.extra_config or {}).get("service_url") or "").strip()
            if not service_url:
                return "❌ Teams proactive send requires an existing inbound conversation to capture service_url"

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent_obj.tenant_id if agent_obj else None,
            )

            session_result = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.user_id == platform_user.id,
                    ChatSession.source_channel == "microsoft_teams",
                    ChatSession.is_group == False,
                )
                .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
                .limit(1)
            )
            session = session_result.scalar_one_or_none()
            conversation_id = str(session.external_conv_id or "").strip() if session else ""
            if not conversation_id:
                return f"❌ Teams proactive send to {member_name} requires them to message the bot first"

            actor_ref = str(target_member.external_id or target_member.open_id or platform_user.id)
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=session,
                content=message_text,
                source_channel="microsoft_teams",
                actor_ref=actor_ref,
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("microsoft_teams"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            async def _record_teams_part(response: dict) -> None:
                part = IMDeliveryPart(
                    transport="microsoft_teams",
                    provider_message_id=str(response.get("id") or "") or None,
                    conversation_ref=conversation_id,
                    artifact_role="chunk",
                    recallable=bool(response.get("id")),
                    metadata={"service_url": service_url},
                )
                await append_delivery_part(receipt_id, part)

            try:
                teams_responses = await _send_teams_message(
                    config,
                    conversation_id,
                    {
                        "type": "message",
                        "text": message_text,
                        "conversation": {"id": conversation_id},
                    },
                    on_result=_record_teams_part,
                )
            except Exception as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("microsoft_teams", exc),
                )
                raise
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "microsoft_teams",
                    *(
                        IMDeliveryPart(
                            transport="microsoft_teams",
                            provider_message_id=str(response.get("id") or "") or None,
                            conversation_ref=conversation_id,
                            artifact_role="chunk",
                            recallable=bool(response.get("id")),
                            metadata={"service_url": service_url},
                        )
                        for response in teams_responses
                    ),
                ),
            )
            logger.info(f"[Teams] Proactive message saved to session {session.id}")
            return f"✅ Message sent to {member_name} via Teams\nmessage_id: {receipt_id}"
    except Exception as e:
        logger.exception("[Teams] Error")
        return f"❌ Teams message error: {str(e)[:200]}"


async def _send_wechat_channel_message(
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
    """Send proactive WeChat message using the latest cached context_token."""
    from app.services.wechat_channel import (
        WECHAT_ILINK_BASE_URL,
        get_wechat_context_entry,
        send_wechat_text_message,
    )

    try:
        async with async_session() as db:
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "wechat",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no WeChat channel configured"

            user_id = (target_member.external_id or "").strip()
            if not user_id:
                return f"❌ {member_name} has no WeChat user_id"

            ctx_entry = get_wechat_context_entry(config.extra_config, from_user_id=user_id)
            context_token = str((ctx_entry or {}).get("context_token") or "").strip()
            conv_id = str((ctx_entry or {}).get("conv_id") or f"wechat_{user_id}").strip()
            if not context_token:
                return f"❌ WeChat proactive send to {member_name} requires them to message the bot first"

            token = str((config.extra_config or {}).get("bot_token") or "").strip()
            base_url = str((config.extra_config or {}).get("baseurl") or WECHAT_ILINK_BASE_URL).strip()
            route_tag = str((config.extra_config or {}).get("route_tag") or "").strip() or None
            if not token:
                return "❌ WeChat bot token is missing"

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
                external_conv_id=conv_id,
                source_channel="wechat",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="wechat",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("wechat"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            async def _record_wechat_part(response: dict) -> None:
                part = IMDeliveryPart(
                    transport="wechat_ilink",
                    provider_message_id=str(response.get("client_id") or "") or None,
                    conversation_ref=user_id,
                    artifact_role="chunk",
                    recallable=False,
                )
                await append_delivery_part(receipt_id, part)

            try:
                wechat_responses = await send_wechat_text_message(
                    token=token,
                    base_url=base_url,
                    to_user_id=user_id,
                    context_token=context_token,
                    text=message_text,
                    route_tag=route_tag,
                    on_result=_record_wechat_part,
                )
            except Exception as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("wechat", exc),
                )
                raise
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "wechat",
                    *(
                        IMDeliveryPart(
                            transport="wechat_ilink",
                            provider_message_id=str(response.get("client_id") or "") or None,
                            conversation_ref=user_id,
                            artifact_role="chunk",
                            recallable=False,
                        )
                        for response in wechat_responses
                    ),
                ),
            )
            logger.info(f"[WeChat] Proactive message saved to session {sess.id}")
            return f"✅ Message sent to {member_name} via WeChat\nmessage_id: {receipt_id}"
    except Exception as e:
        logger.exception("[WeChat] Error")
        return f"❌ WeChat message error: {str(e)[:200]}"


async def _send_platform_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a proactive message to a first-party platform user."""
    canonical_user_id = str(args.get("user_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()

    if not canonical_user_id or not message_text:
        return "❌ Please provide canonical user_id and message content"

    try:
        async with async_session() as db:
            try:
                recipient = await resolve_platform_user_recipient(db, agent_id, canonical_user_id)
            except RecipientResolutionError as exc:
                return exc.as_json()
            target_user = recipient.user

            # Agent-initiated platform messages should always go to the long-lived primary session
            # for this agent+user pair, so trigger-driven outreach does not fragment into dozens of
            # tiny one-off web sessions.
            from app.services.chat_session_service import ensure_primary_platform_session

            session = await ensure_primary_platform_session(db, agent_id, target_user.id)

            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=target_user.id,
                session=session,
                content=message_text,
                source_channel=session.source_channel,
                actor_ref=str(target_user.id),
                target_name=target_user.display_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
            )
            if should_deliver:
                try:
                    from app.api.websocket import maybe_mark_session_read_for_active_viewer

                    await maybe_mark_session_read_for_active_viewer(
                        db,
                        agent_id=agent_id,
                        session_id=str(session.id),
                        user_id=target_user.id,
                    )
                except Exception:
                    pass
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            try:
                # Push via WebSocket if user has an active connection
                from app.api.websocket import manager as ws_manager

                await ws_manager.send_to_user(
                    str(agent_id),
                    str(target_user.id),
                    {
                        "type": "trigger_notification",
                        "content": message_text,
                        "triggers": ["web_message"],
                        "session_id": str(session.id),
                    },
                )
            except Exception:
                pass

            display = target_user.display_name
            return f"✅ Message sent to {display} on web platform. It has been saved to their chat history."

    except Exception as e:
        logger.exception("[PlatformMessage] Error")
        return f"❌ Web message send error: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
