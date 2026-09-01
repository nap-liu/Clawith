"""Exact-session authorization and channel message dispatch."""

import json
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.agent_tools_message_transports import (
    _send_dingtalk_message,
    _send_feishu_message,
    _send_slack_message,
    _send_wecom_message,
)
from app.services.agent_tools_outbound_core import (
    _GROUP_SESSION_DENIAL,
    _LEGACY_GROUP_SESSION_CHANNELS,
    _PLATFORM_SESSION_CHANNELS,
    _SESSION_MESSAGE_CAPABILITIES,
    _SESSION_MESSAGE_DENIAL,
    _build_outbound_operation_key,
    _lock_outbound_operation,
    _persist_outbound_channel_message,
    _session_message_result,
    _session_receipt_replay_status,
)
from app.services.agent_tools_platform_transports import (
    _send_platform_message,
    _send_teams_channel_message,
    _send_wechat_channel_message,
)
from app.services.dingtalk_group_mentions import prepare_group_user_mentions
from app.services.im_delivery import (
    IMDeliveryPart,
    IMDeliveryResult,
    MentionIntent,
    append_delivery_part,
    register_delivery,
)
from app.services.recipient_resolver import (
    RecipientResolutionError,
    resolve_human_channel_recipient,
    resolve_human_recipient,
)
from app.services.turn_runtime import TurnRuntime, deliver_message_with_receipt
from app.services.user_output import sanitize_user_visible_text


async def _send_exact_session_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
    require_group: bool = False,
    legacy_group_contract: bool = False,
) -> str:
    """Authorize and send text through one exact human ChatSession route."""
    raw_session_id = str(args.get("session_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()
    raw_mention_user_ids = args.get("mention_user_ids")
    if "mention_all" in args:
        raw_mention_all = args["mention_all"]
        if type(raw_mention_all) is not bool:
            return "❌ mention_all must be a boolean"
        mention_all = raw_mention_all
    else:
        mention_all = False
    if not raw_session_id:
        qualifier = "group " if require_group else ""
        return f"❌ Please provide the exact {qualifier}session_id"
    if not message_text:
        return "❌ Please provide message content"
    if raw_mention_user_ids is None:
        mention_user_ids: list[str] = []
    elif not isinstance(raw_mention_user_ids, list):
        return "❌ mention_user_ids must be an array of canonical platform user_ids"
    else:
        mention_user_ids = list(dict.fromkeys(str(value or "").strip() for value in raw_mention_user_ids))
        if not all(mention_user_ids):
            return "❌ mention_user_ids cannot contain empty values"
        if len(mention_user_ids) > 20:
            return "❌ mention_user_ids supports at most 20 people"
    if mention_all and mention_user_ids:
        return "❌ mention_all=true cannot be combined with mention_user_ids"
    try:
        target_session_id = uuid.UUID(raw_session_id)
    except (TypeError, ValueError):
        return _GROUP_SESSION_DENIAL if require_group else _SESSION_MESSAGE_DENIAL

    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    target_name = "group" if require_group else "conversation"
    target_channel = ""
    target_is_group = require_group
    mentioned_names: list[str] = []
    mention_intent: MentionIntent | None = None
    mentions_meta: dict | None = None

    try:
        async with async_session() as db:
            await _lock_outbound_operation(db, operation_key)
            if operation_key:
                existing = (
                    await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == operation_key))
                ).scalar_one_or_none()
                if existing is not None:
                    meta = existing.message_meta if isinstance(existing.message_meta, dict) else {}
                    replay_mentions = meta.get("mentions")
                    if not isinstance(replay_mentions, dict):
                        replay_mentions = None
                    return _session_message_result(
                        status=_session_receipt_replay_status(existing),
                        session_id=str(existing.conversation_id),
                        channel=str(meta.get("source_channel") or ""),
                        target_name=str(meta.get("target_name") or target_name),
                        is_group=bool(meta.get("target_is_group", require_group)),
                        legacy_group_contract=legacy_group_contract,
                        mentioned_users=list(meta.get("mentioned_users") or []),
                        mentions=replay_mentions,
                        message_id=str(existing.id),
                    )

            conditions = [
                ChatSession.id == target_session_id,
                ChatSession.agent_id == agent_id,
            ]
            if require_group:
                conditions.append(ChatSession.is_group.is_(True))
            session = (await db.execute(select(ChatSession).where(*conditions).with_for_update())).scalar_one_or_none()
            if session is None:
                return _GROUP_SESSION_DENIAL if require_group else _SESSION_MESSAGE_DENIAL

            target_channel = str(session.source_channel or "").strip()
            external_conv_id = str(session.external_conv_id or "").strip()
            target_is_group = bool(session.is_group)
            target_kind = "group" if target_is_group else "person"
            if target_channel not in _PLATFORM_SESSION_CHANNELS and (
                "__archived_" in external_conv_id or not external_conv_id
            ):
                qualifier = "群" if target_is_group else ""
                return f"❌ 无法投递：该{qualifier}会话已归档或通道绑定已失效。"
            if legacy_group_contract and target_channel not in _LEGACY_GROUP_SESSION_CHANNELS:
                return f"❌ Group Session delivery is not supported for channel: {target_channel or 'unknown'}"
            capabilities = _SESSION_MESSAGE_CAPABILITIES.get(target_channel, frozenset())
            if target_kind not in capabilities:
                label = "Group Session" if target_is_group else "Session"
                return f"❌ {label} delivery is not supported for channel: {target_channel or 'unknown'}"

            if mention_all or mention_user_ids:
                if not target_is_group or target_channel != "dingtalk":
                    return "❌ 原生 @ 当前仅支持钉钉群 Session。"
                if mention_all:
                    mention_intent = MentionIntent(scope="all")
                    mentions_meta = {"scope": "all"}
                else:
                    try:
                        target_ids, mentioned_names = await prepare_group_user_mentions(
                            db,
                            agent_id=agent_id,
                            canonical_user_ids=mention_user_ids,
                        )
                    except RecipientResolutionError as exc:
                        return exc.as_json()
                    except ValueError as exc:
                        if str(exc) == "dingtalk_staff_id_unavailable":
                            return "❌ 指定用户缺少可用的钉钉 userId，无法 @。"
                        raise
                    mention_intent = MentionIntent(
                        scope="users",
                        target_ids=tuple(target_ids),
                        target_names=tuple(mentioned_names),
                    )
                    mentions_meta = {
                        "scope": "users",
                        "user_ids": mention_user_ids,
                        "display_names": mentioned_names,
                    }

            if target_is_group:
                target_name = str(session.group_name or session.title or "group")
                target_user_id = None
            else:
                if session.user_id is None:
                    return _SESSION_MESSAGE_DENIAL
                try:
                    recipient = await resolve_human_recipient(db, agent_id, session.user_id)
                except RecipientResolutionError as exc:
                    return exc.as_json()
                target_user_id = recipient.user.id
                target_name = str(recipient.user.display_name or session.title or "person")

            runtime = TurnRuntime(
                session_found=True,
                source_channel=target_channel,
                conversation_id=str(session.id),
                external_conv_id=external_conv_id or None,
                is_group=target_is_group,
            )
            if mention_all:
                message_for_history = f"{message_text}\n\n@所有人"
            elif mentioned_names:
                message_for_history = (
                    f"{message_text}\n\n"
                    f"{' '.join(f'@{name}' for name in mentioned_names)}"
                )
            else:
                message_for_history = message_text
            message_for_delivery = message_text if mention_intent is not None else message_for_history
            delivery_kwargs = {
                "agent_id": agent_id,
                "runtime": runtime,
                "message": message_for_delivery,
                "allow_wecom_group_actor_fallback": False,
            }
            if mention_intent is not None:
                delivery_kwargs["mention"] = mention_intent
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=target_user_id,
                session=session,
                content=message_for_history,
                source_channel=target_channel,
                actor_ref=external_conv_id or str(target_user_id or ""),
                target_name=target_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending(target_channel),
            )
            if should_deliver:
                receipt_meta = dict(receipt.message_meta or {})
                receipt_meta["target_is_group"] = target_is_group
                if mentions_meta:
                    receipt_meta["mentions"] = mentions_meta
                if mentioned_names:
                    receipt_meta["mention_user_ids"] = mention_user_ids
                    receipt_meta["mentioned_users"] = mentioned_names
                receipt.message_meta = receipt_meta
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                replay_meta = receipt.message_meta if isinstance(receipt.message_meta, dict) else {}
                replay_mentions = replay_meta.get("mentions")
                if not isinstance(replay_mentions, dict):
                    replay_mentions = None
                return _session_message_result(
                    status=_session_receipt_replay_status(receipt),
                    session_id=str(receipt.conversation_id),
                    channel=target_channel,
                    target_name=target_name,
                    is_group=target_is_group,
                    legacy_group_contract=legacy_group_contract,
                    mentioned_users=mentioned_names,
                    mentions=replay_mentions,
                    message_id=str(receipt.id),
                )

        # The provider call runs outside the row/advisory lock. The pending
        # receipt above is the durable idempotency claim and crash marker.
        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        delivery_kwargs["on_part"] = _record_part
        try:
            delivery_result = await deliver_message_with_receipt(**delivery_kwargs)
        except Exception as exc:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception(target_channel, exc),
            )
            raise
        await register_delivery(receipt_id, delivery_result)
        if not delivery_result.ok:
            label = "Group message" if target_is_group else "Session message"
            return f"❌ {label} delivery failed via {target_channel}; receipt status is failed."

        if target_channel not in _PLATFORM_SESSION_CHANNELS:
            try:
                from app.api.websocket import manager as ws_manager

                await ws_manager.send_to_session(
                    str(agent_id),
                    str(target_session_id),
                    {
                        "type": "assistant_message_committed",
                        "id": str(receipt_id),
                        "role": "assistant",
                        "content": message_for_history,
                        "session_id": str(target_session_id),
                    },
                )
            except Exception:
                logger.opt(exception=True).warning("[SessionMessage] Web live mirror failed after external delivery")

        return _session_message_result(
            status="sent",
            session_id=str(target_session_id),
            channel=target_channel,
            target_name=target_name,
            is_group=target_is_group,
            legacy_group_contract=legacy_group_contract,
            mentioned_users=mentioned_names,
            mentions=mentions_meta,
            message_id=str(receipt_id),
        )
    except Exception as exc:
        logger.opt(exception=True).error(
            "[SessionMessage] delivery failed agent={} session={}",
            agent_id,
            target_session_id,
        )
        label = "Group Session" if require_group else "Session"
        return f"❌ {label} message error: {type(exc).__name__}: {str(exc)[:200]}"


async def _send_session_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send text through an exact person or group Session route."""
    return await _send_exact_session_message(
        agent_id,
        args,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )


async def _send_group_session_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Backward-compatible group-only wrapper around exact Session delivery."""
    return await _send_exact_session_message(
        agent_id,
        args,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
        require_group=True,
        legacy_group_contract=True,
    )


async def _send_channel_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send an external-channel message by canonical platform user_id."""
    canonical_user_id = str(args.get("user_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()
    raw_target_channel = (args.get("channel") or "").strip().lower()
    target_channel = "teams" if raw_target_channel == "microsoft_teams" else raw_target_channel

    if not canonical_user_id:
        return "❌ Please provide canonical user_id"
    if not message_text:
        return "❌ Please provide message content"

    try:
        async with async_session() as db:
            try:
                route = await resolve_human_channel_recipient(
                    db,
                    agent_id,
                    canonical_user_id,
                    channel=target_channel or None,
                )
            except RecipientResolutionError as exc:
                return exc.as_json()
            target_member = route.member
            provider_type = route.channel
            member_name = route.user.display_name
            logger.info(
                "[ChannelMessage] canonical user %s via %s",
                canonical_user_id,
                provider_type,
            )

            if provider_type == "feishu":
                return await _send_feishu_message(
                    agent_id,
                    {
                        "user_id": canonical_user_id,
                        "message": message_text,
                    },
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "dingtalk":
                return await _send_dingtalk_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "wecom":
                return await _send_wecom_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "slack":
                return await _send_slack_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "teams":
                return await _send_teams_channel_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "wechat":
                return await _send_wechat_channel_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            else:
                return f"❌ Unsupported channel type: {provider_type}"

    except Exception as e:
        logger.exception("[ChannelMessage] Error")
        return f"❌ Channel message error: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
