"""Channel delivery and provider-card confirmation handling."""

from app.services.confirmation_shared import *  # noqa: F401,F403
from app.services.confirmation_core import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# DingTalk interactive-card delivery (the card on its origin IM channel)
# ---------------------------------------------------------------------------


async def _deliver_channel_card(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    out_track_id: str,
    args: dict,
    external_conv_id: str | None,
    is_group: bool,
) -> bool:
    """Best-effort: deliver the confirmation as a DingTalk interactive card whose outTrackId
    IS the tool_call row id (so the click callback maps straight back). Never raises."""
    return await run_channel_send(
        _im_send_lock_key(conversation_id),
        lambda: _deliver_channel_card_unlocked(
            agent_id=agent_id,
            out_track_id=out_track_id,
            args=args,
            external_conv_id=external_conv_id,
            is_group=is_group,
        ),
    )


async def _deliver_channel_card_unlocked(
    *,
    agent_id: uuid.UUID,
    out_track_id: str,
    args: dict,
    external_conv_id: str | None,
    is_group: bool,
) -> bool:
    """Send one card while the caller owns the conversation send lock."""
    try:
        if not external_conv_id:
            return False
        from app.models.channel_config import ChannelConfig

        async with async_session() as db:
            cc = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "dingtalk",
                    )
                )
            ).scalar_one_or_none()
        if cc is None or not cc.app_id or not cc.app_secret:
            logger.warning("Confirmation %s: no usable dingtalk ChannelConfig for agent %s", out_track_id, agent_id)
            return False
        # The card template id is a STANDARD tool config field on request_confirmation
        # (agent override → tenant default → tool default), read via the usual tool-config
        # path — no bespoke channel/extra_config concept.
        from app.services.agent_tools import _get_tool_config

        tool_cfg = await _get_tool_config(agent_id, REQUEST_CONFIRMATION_TOOL_NAME) or {}
        template_id = tool_cfg.get("card_template_id")
        if not template_id:
            logger.warning(
                "Confirmation %s: request_confirmation tool has no card_template_id configured for agent %s",
                out_track_id, agent_id,
            )
            return False
        from app.services.dingtalk_card import build_confirmation_card_data, send_confirmation_card

        card_data = build_confirmation_card_data(
            title=args.get("title") or "",
            summary=args.get("summary") or "",
            action_preview=_action_preview(args.get("action")),
            risk_level=args.get("risk_level") or "medium",
            status="pending",
            buttons=args.get("buttons"),
        )
        return bool(await send_confirmation_card(
            app_id=cc.app_id,
            app_secret=cc.app_secret,
            card_template_id=template_id,
            out_track_id=out_track_id,
            card_data=card_data,
            external_conv_id=external_conv_id,
            is_group=is_group,
        ))
    except Exception:
        logger.exception("_deliver_channel_card failed for confirmation %s", out_track_id)
        return False


async def redeliver_pending_confirmation(pending: PendingConfirmation) -> bool:
    """Create and deliver a fresh transport card for the same pending tool call.

    DingTalk does not guarantee that delivering the same instance twice creates a
    second visible message. A fresh transport id represents this delivery only and
    deterministically maps back to the original normalized tool_call row.
    """
    try:
        channel, external_conv_id, is_group = await _resolve_session_channel(
            pending.conversation_id, None, ""
        )
        if channel != "dingtalk" or not external_conv_id:
            logger.info(
                "Pending confirmation %s blocked %s input; card redelivery is unavailable",
                pending.row_id,
                channel,
            )
            return False

        from app.models.audit import ChatMessage
        from app.models.channel_config import ChannelConfig
        from app.models.chat_session import ChatSession

        async with async_session() as db:
            cc = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == pending.agent_id,
                        ChannelConfig.channel_type == "dingtalk",
                    )
                )
            ).scalar_one_or_none()
        if cc is None or not cc.app_id or not cc.app_secret:
            return False

        from app.services.agent_tools import _get_tool_config
        from app.services.dingtalk_card import (
            build_confirmation_card_data,
            send_confirmation_card,
        )

        tool_cfg = await _get_tool_config(
            pending.agent_id,
            REQUEST_CONFIRMATION_TOOL_NAME,
        ) or {}
        template_id = tool_cfg.get("card_template_id")
        if not template_id:
            return False
        delivery_id = f"{pending.row_id.hex}.{uuid.uuid4().hex[:12]}"
        card_data = build_confirmation_card_data(
            title=pending.args.get("title") or "",
            summary=pending.args.get("summary") or "",
            action_preview=_action_preview(pending.args.get("action")),
            risk_level=pending.args.get("risk_level") or "medium",
            status="pending",
            buttons=pending.args.get("buttons"),
        )
        from app.services.im_delivery import (
            IMDeliveryPart,
            IMDeliveryResult,
            append_delivery_part,
            register_delivery,
        )

        pending_part = IMDeliveryPart(
            transport="dingtalk_interactive_card",
            provider_message_id=delivery_id,
            conversation_ref=external_conv_id,
            artifact_role="confirmation_card_redelivery",
            recallable=False,
            send_status="pending",
        )
        if not await append_delivery_part(pending.row_id, pending_part):
            return False
        sent = await run_channel_send(
            _im_send_lock_key(pending.conversation_id),
            lambda: send_confirmation_card(
                app_id=cc.app_id,
                app_secret=cc.app_secret,
                card_template_id=template_id,
                out_track_id=delivery_id,
                card_data=card_data,
                external_conv_id=external_conv_id,
                is_group=is_group,
            ),
        )
        if not sent:
            await register_delivery(
                pending.row_id,
                IMDeliveryResult.failed("dingtalk", "confirmation_card_redelivery_failed"),
            )
            return False
        await register_delivery(
            pending.row_id,
            IMDeliveryResult.sent(
                "dingtalk",
                IMDeliveryPart(
                    transport="dingtalk_interactive_card",
                    provider_message_id=delivery_id,
                    conversation_ref=external_conv_id,
                    artifact_role="confirmation_card_redelivery",
                    recallable=False,
                ),
            ),
        )

        async with async_session() as db:
            # Serialize the post-send state check with resolve_confirmation. If
            # resolution won the race, this fresh card is immediately expired;
            # if redelivery wins, resolve sees and disables this delivery id.
            sid = uuid.UUID(str(pending.conversation_id))
            await db.execute(
                select(ChatSession.id).where(ChatSession.id == sid).with_for_update()
            )
            row = await db.get(ChatMessage, pending.row_id)
            if row is None:
                return True
            meta = dict(row.message_meta or {})
            previous_delivery_id = str(
                meta.get("confirmation_delivery_id") or pending.row_id
            )
            meta["confirmation_delivery_id"] = delivery_id
            row.message_meta = meta
            try:
                payload = json.loads(row.content or "{}")
            except (TypeError, ValueError):
                payload = {}
            still_pending = payload.get("status") == "pending"
            await db.commit()
        if not still_pending:
            await _mark_card_expired(cc, pending.conversation_id, delivery_id, pending.args)
        elif previous_delivery_id != delivery_id:
            # Only the newest transport card remains actionable. This keeps delivery
            # metadata bounded and prevents a trail of visually active stale cards.
            await _mark_card_expired(cc, pending.conversation_id, previous_delivery_id, pending.args)
        return True
    except Exception:
        logger.exception(
            "redeliver_pending_confirmation failed for %s", pending.row_id
        )
        return False


IGNORED_CONFIRMATION_RESULT = (
    "用户没有点击确认卡，而是继续发送了新的消息。本次操作未获得用户确认，"
    "不得将其视为同意，也不得执行确认卡中描述的待确认操作。请根据用户随后发送的新消息继续处理。"
)

STOPPED_CONFIRMATION_RESULT = (
    "当前 turn 已被用户终止；本确认请求已取消，迟到的卡片点击不得执行任何操作。"
)


async def cancel_pending_confirmation_for_stop(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Close the exact pending card inside the caller's STOP transaction."""

    from app.models.audit import ChatMessage

    pending = await find_pending_confirmation(
        db,
        agent_id=agent_id,
        conversation_id=conversation_id,
    )
    if (
        pending is None
        or pending.turn_anchor_id != turn_anchor_id
    ):
        return None
    row = await db.get(ChatMessage, pending.row_id, with_for_update=True)
    if row is None:
        return None
    try:
        payload = json.loads(row.content or "{}")
    except (TypeError, ValueError):
        return None
    if (
        payload.get("name") != REQUEST_CONFIRMATION_TOOL_NAME
        or payload.get("status") != "pending"
    ):
        return None
    payload["status"] = "done"
    payload["result"] = STOPPED_CONFIRMATION_RESULT
    row.content = json.dumps(payload, ensure_ascii=False, default=str)
    await db.flush()
    return row.id


async def ignore_pending_confirmation_for_new_input(
    db: AsyncSession,
    pending: PendingConfirmation,
) -> str | None:
    """Close one non-blocking pending card with a real negative tool result.

    The caller holds the ChatSession row lock and owns the transaction.  This only
    fills the existing normalized tool_call row; it does not start the suspended
    turn.  The new user message that follows drives the next single turn.
    """
    from app.models.audit import ChatMessage

    row = await db.get(ChatMessage, pending.row_id)
    if row is None or row.role != "tool_call":
        return None
    try:
        payload = json.loads(row.content or "{}")
    except (TypeError, ValueError):
        return None
    args = payload.get("args")
    if (
        payload.get("name") != REQUEST_CONFIRMATION_TOOL_NAME
        or payload.get("status") != "pending"
        or not isinstance(args, dict)
        or args.get("force_confirmation") is not False
    ):
        return None
    payload["status"] = "done"
    payload["result"] = IGNORED_CONFIRMATION_RESULT
    row.content = json.dumps(payload, ensure_ascii=False, default=str)
    if pending.turn_anchor_id is not None:
        from app.services.conversation_turn_lifecycle import transition_conversation_turn

        await transition_conversation_turn(
            db,
            agent_id=pending.agent_id,
            conversation_id=pending.conversation_id,
            turn_anchor_id=pending.turn_anchor_id,
            status="cancelled",
        )
    await db.flush()
    return IGNORED_CONFIRMATION_RESULT


async def publish_ignored_confirmation(
    pending: PendingConfirmation,
    result_text: str,
) -> None:
    """Publish the committed non-confirmation result to live/card views."""
    await _broadcast(
        pending.agent_id,
        pending.conversation_id,
        {
            "type": "tool_call",
            "name": REQUEST_CONFIRMATION_TOOL_NAME,
            "call_id": str(pending.row_id),
            "args": pending.args,
            "status": "done",
            "result": result_text,
        },
    )
    await _update_origin_card(
        pending.agent_id,
        pending.conversation_id,
        pending.conversation_id,
        str(pending.row_id),
        pending.args,
        "未确认",
        "ignored",
        status_text="未确认",
    )


_DEFAULT_RESOLVE_BUTTONS = [{"text": "取消", "value": "cancel"}, {"text": "确认", "value": "confirm"}]


def _mark_selected_buttons(buttons: list | None, selected_value: str) -> list:
    """Return the card buttons with a ✅ marker on the chosen one, so a disabled/resolved
    card visibly shows WHICH button was selected (a row of greyed buttons otherwise can't)."""
    spec = buttons if isinstance(buttons, list) and buttons else _DEFAULT_RESOLVE_BUTTONS
    out = []
    for b in spec:
        if not isinstance(b, dict):
            continue
        text = b.get("text") or b.get("value") or ""
        bval = b.get("value") or b.get("text") or ""
        if bval == selected_value and not text.startswith("✅"):
            text = f"✅ {text}"
        out.append({**b, "text": text})
    return out


async def _update_origin_card(
    agent_id: uuid.UUID,
    conversation_id: str,
    runtime_conversation_id: str,
    out_track_id: str,
    args: dict,
    label: str,
    value: str,
    *,
    status_text: str = "已处理",
) -> None:
    """On resolution, flip the card on its origin IM channel (DingTalk) to a disabled, resolved
    state — with a ✅ marker on the selected button so it's clear what was chosen — so it can't
    be clicked again. Best-effort; web-origin cards are a no-op."""
    try:
        channel, ext, _is_group = await _resolve_session_channel(conversation_id, None, "")
        if channel != "dingtalk" or not ext:
            return
        from app.models.channel_config import ChannelConfig

        async with async_session() as db:
            cc = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "dingtalk",
                    )
                )
            ).scalar_one_or_none()
        fields = {**(args or {}), "buttons": _mark_selected_buttons((args or {}).get("buttons"), value)}
        await _push_card_state(
            cc,
            out_track_id,
            fields,
            status_text=status_text,
            buttons_disabled=True,
            conversation_id=runtime_conversation_id,
        )
    except Exception:
        logger.exception("_update_origin_card failed for %s", out_track_id)


async def resolve_confirmation_via_dingtalk(
    out_track_id: str, staff_id: str, button_value: str, button_label: str
) -> None:
    """Bridge a DingTalk card-button click to the durable confirmation transition.

    ``outTrackId`` is either the canonical tool-call UUID or a fresh delivery alias
    whose prefix is that UUID. Maps the clicker to an internal user, updates the
    transport card, then resolves the stored tool call and resumes the loop.
    """
    try:
        from app.models.agent import Agent as AgentModel
        from app.models.audit import ChatMessage
        from app.models.channel_config import ChannelConfig
        from app.models.identity import IdentityProvider
        from app.models.org import OrgMember

        try:
            rid = uuid.UUID(str(out_track_id).split(".", 1)[0])
        except (ValueError, TypeError):
            logger.warning(f"[DingTalkCard] callback outTrackId not a uuid: {out_track_id!r}")
            return

        async with async_session() as db:
            row = await db.get(ChatMessage, rid)
            if row is None or row.role != "tool_call":
                logger.warning(f"[DingTalkCard] callback for unknown confirmation row {out_track_id}")
                return
            agent_id = row.agent_id
            conversation_id = row.conversation_id
            try:
                _payload = json.loads(row.content or "{}")
            except Exception:
                _payload = {}
            args = _payload.get("args") or {}
            card_status = _payload.get("status")
            agent_obj = (
                await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            ).scalar_one_or_none()
            tenant_id = agent_obj.tenant_id if agent_obj else None
            cc = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "dingtalk",
                    )
                )
            ).scalar_one_or_none()
            user_id = None
            prov = (
                await db.execute(
                    select(IdentityProvider).where(
                        IdentityProvider.provider_type == "dingtalk",
                        IdentityProvider.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if prov is not None:
                om = (
                    await db.execute(
                        select(OrgMember).where(
                            OrgMember.provider_id == prov.id,
                            OrgMember.external_id == staff_id,
                            OrgMember.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if om and om.user_id:
                    user_id = om.user_id

        # Stale click: the card was already resolved elsewhere (e.g. on web) or has expired —
        # it's no longer actionable. Don't re-resolve (and don't even need the clicker mapped);
        # disable the card with an 已过期 state and tell the user, so a still-clickable DingTalk
        # card can't drive a duplicate action.
        if card_status != "pending":
            await _mark_card_expired(
                cc,
                conversation_id,
                out_track_id,
                args,
            )
            return

        if user_id is None:
            logger.warning(f"[DingTalkCard] cannot map staff_id {staff_id} to a user (row {out_track_id})")
            return

        # resolve_confirmation flips the origin DingTalk card to its resolved/disabled state
        # itself (via _update_origin_card), so no separate optimistic push is needed here.
        result = await resolve_confirmation(
            agent_id=agent_id,
            call_id=rid,
            button_value=button_value,
            button_label=button_label,
            resolving_user_id=user_id,
            clicked_card_instance_id=out_track_id,
        )
        # Race: another path (web) resolved it between our status read and resolve's lock.
        # resolve returned None (already done) — correct the card to the 已过期 state.
        if result is None:
            await _mark_card_expired(
                cc,
                conversation_id,
                out_track_id,
                args,
            )
    except Exception:
        logger.exception("[DingTalkCard] resolve_confirmation_via_dingtalk failed")


async def _mark_card_expired(
    cc,
    conversation_id: str,
    out_track_id: str,
    args: dict,
) -> None:
    """A stale DingTalk click on an already-resolved/expired card: just replace its buttons
    with a single disabled 已过期 button. Framework-level card update ONLY — no message
    delivery, no agent wake-up (the stale click is a no-op beyond the visual update)."""
    fields = {**(args or {}), "buttons": [{"text": "已过期", "value": "expired", "color": "gray"}]}
    await _push_card_state(
        cc,
        out_track_id,
        fields,
        status_text="",
        buttons_disabled=True,
        conversation_id=conversation_id,
    )


async def _push_card_state(
    cc,
    out_track_id: str,
    fields: dict,
    *,
    status_text: str,
    buttons_disabled: bool,
    conversation_id: str,
) -> None:
    """Best-effort DingTalk card update — status text + button enabled/disabled."""
    try:
        if cc is None or not cc.app_id or not cc.app_secret:
            return
        from app.services.dingtalk_card import build_confirmation_card_data, update_confirmation_card

        card_data = build_confirmation_card_data(
            title=fields.get("title") or "",
            summary=fields.get("summary") or "",
            action_preview=_action_preview(fields.get("action")),
            risk_level=fields.get("risk_level") or "medium",
            status=status_text,
            buttons=fields.get("buttons"),
            buttons_disabled=buttons_disabled,
        )
        await run_channel_send(
            _im_send_lock_key(conversation_id),
            lambda: update_confirmation_card(
                app_id=cc.app_id,
                app_secret=cc.app_secret,
                out_track_id=out_track_id,
                card_data=card_data,
            ),
        )
    except Exception:
        logger.exception("[DingTalkCard] _push_card_state failed")

__all__ = [name for name in globals() if not name.startswith("__")]
