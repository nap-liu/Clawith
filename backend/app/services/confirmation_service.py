"""Confirmation service — confirmation card as a normal, suspended tool_call.

A confirmation card is NOT a parallel system. It is one ordinary ``request_confirmation``
tool_call whose execution simply SUSPENDS the turn until the user clicks:

- ``suspend_for_confirmation``: persist the agent's intro text + a PENDING tool_call row
  (``status="pending"``, empty result) and deliver the card to the originating channel.
  The row id is the canonical confirmation handle. Web uses it as ``call_id``;
  DingTalk delivery instances use it as the stable prefix of ``outTrackId``.
- ``resolve_confirmation``: the user's click FILLS that row's tool result and flips status
  to ``done``, then RESUMES the loop from the now-complete history — no synthetic user
  message, no separate table/event/role.

The card's content is the tool_call's ``args``; its state is the tool_call's status/result
(``expand_tool_call_row`` replays the assistant(tool_call)+tool(result) pair). The platform
executes nothing — it faithfully relays the clicked button; the agent decides what to do.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME
from app.services.turn_runtime import (
    _deliver_dingtalk_unlocked,
    deliver_reply_to_origin,
    load_turn_runtime,
)
from app.services.channel_dispatch import run_channel_send

logger = logging.getLogger(__name__)

CONFIRMATION_EXPIRY_HOURS = 24


def _im_send_lock_key(conversation_id: str) -> str:
    return f"im-send:{conversation_id}"


class ConfirmationActorMismatch(PermissionError):
    """The resolving user is not the human for whom this card was created."""


@dataclass(frozen=True)
class PendingConfirmation:
    """The durable suspended ``request_confirmation`` tool call for one session."""

    row_id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: str
    user_id: uuid.UUID | None
    args: dict
    created_at: datetime | None
    force_confirmation: bool = True


async def find_pending_confirmation(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> PendingConfirmation | None:
    """Return the session's normalized pending confirmation, if any.

    Confirmation remains part of the normalized message stream. The latest active
    tool call is read through the existing ``ix_chat_messages_active`` session
    history index and interpreted in Python. This avoids casting historical message
    text inside PostgreSQL while keeping the ordinary message row as the only truth.
    """
    from app.models.audit import ChatMessage

    row = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == str(conversation_id),
                ChatMessage.role == "tool_call",
                ChatMessage.compacted_into.is_(None),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        payload = json.loads(row.content or "{}")
    except (TypeError, ValueError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME
        and payload.get("status") == "pending"
    ):
        args = payload.get("args")
        normalized_args = args if isinstance(args, dict) else {}
        return PendingConfirmation(
            row_id=row.id,
            agent_id=row.agent_id,
            conversation_id=row.conversation_id,
            user_id=row.user_id,
            args=normalized_args,
            created_at=row.created_at,
            force_confirmation=normalized_args.get("force_confirmation") is not False,
        )
    return None


async def find_dingtalk_pending_confirmation(
    *,
    agent_id: uuid.UUID,
    external_conv_id: str,
) -> PendingConfirmation | None:
    """Read the pending card for an existing DingTalk session, if any."""
    from app.models.chat_session import ChatSession

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel == "dingtalk",
                    ChatSession.external_conv_id == external_conv_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return None
        return await find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )


def _assert_confirmation_actor(row, pending_meta: dict, resolving_user_id: uuid.UUID) -> None:
    intended_user_id = pending_meta.get("intended_user_id") or row.user_id
    if intended_user_id is None or str(intended_user_id) != str(resolving_user_id):
        raise ConfirmationActorMismatch("This confirmation belongs to another user")


def _action_preview(action: dict | None) -> str:
    """One-line, concrete preview of the action the agent intends to run (display only)."""
    if not action:
        return ""
    tool = str(action.get("tool") or "")
    args = action.get("args")
    if isinstance(args, dict) and args:
        parts = []
        for k, v in args.items():
            vs = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            parts.append(f"{k}={vs}")
        preview = f"{tool}({', '.join(parts)})"
    else:
        preview = tool
    return preview if len(preview) <= 300 else preview[:300] + "…"


def _build_card_args(
    title: str,
    summary: str,
    action: dict | None,
    risk_level: str,
    buttons: list | None,
    force_confirmation: bool,
) -> dict:
    """The tool_call args that ARE the card — what the agent passed to request_confirmation.
    Stored verbatim on the tool_call row; both renderers (web + DingTalk) read these and
    derive the action preview themselves via _action_preview (web mirrors it in TS)."""
    return {
        "title": title or "",
        "summary": summary or "",
        "action": action,
        "risk_level": risk_level or "medium",
        "buttons": buttons,
        "force_confirmation": force_confirmation,
    }


def _ago(created: datetime | None, now: datetime) -> str:
    """Human '约 N 分钟/小时前发出' suffix — cards are time-sensitive (24h)."""
    if not created:
        return ""
    mins = int((now - created).total_seconds() // 60)
    if mins >= 60:
        return f",约 {mins // 60} 小时前发出"
    if mins >= 1:
        return f",约 {mins} 分钟前发出"
    return ""


async def _broadcast(agent_id: uuid.UUID, conversation_id: str, payload: dict) -> None:
    """Best-effort broadcast to web WebSocket clients viewing this session. Never raises."""
    try:
        from app.api.websocket import manager  # lazy import — avoids services→api circular dependency

        await manager.send_to_session(str(agent_id), str(conversation_id), payload)
    except Exception:  # best-effort; broadcast failure must not affect DB write
        pass


async def _resolve_session_channel(
    conversation_id: str, chat_session_id: uuid.UUID | None, source_channel: str
) -> tuple[str, str | None, bool]:
    """Derive (channel, external_conv_id, is_group) from the ChatSession so a card is
    delivered to its TRUE origin (a DingTalk-originated turn → DingTalk, not web).
    conversation_id == ChatSession.id by convention."""
    resolved = source_channel or "web"
    ext: str | None = None
    is_group = False
    try:
        from app.models.chat_session import ChatSession

        sid = chat_session_id
        if sid is None:
            try:
                sid = uuid.UUID(str(conversation_id))
            except (ValueError, TypeError):
                sid = None
        if sid is not None:
            async with async_session() as db:
                sess = (
                    await db.execute(select(ChatSession).where(ChatSession.id == sid))
                ).scalar_one_or_none()
            if sess is not None:
                if sess.source_channel:
                    resolved = sess.source_channel
                ext = sess.external_conv_id
                is_group = bool(getattr(sess, "is_group", False))
    except Exception:
        logger.exception("_resolve_session_channel failed for conv %s", conversation_id)
    return resolved, ext, is_group


async def suspend_for_confirmation(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    chat_session_id: uuid.UUID | None,
    source_channel: str,
    user_id: uuid.UUID | None,
    intro_text: str | None,
    title: str,
    summary: str,
    action: dict | None,
    risk_level: str,
    buttons: list | None = None,
    force_confirmation: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Suspend the turn on a request_confirmation tool_call and return the row id.

    Persists the agent's intro text (if any) BEFORE the pending tool_call row so a reload
    renders text-before-card, then delivers the card to the originating channel. The CALLER
    ends the turn (returns ""); the loop resumes later in resolve_confirmation.
    """
    from app.models.audit import ChatMessage
    from app.services.chat_history import persist_pending_confirmation_row

    args = _build_card_args(
        title,
        summary,
        action,
        risk_level,
        buttons,
        force_confirmation,
    )
    resolved_channel, ext_conv_id, is_group = await _resolve_session_channel(
        conversation_id, chat_session_id, source_channel
    )

    # Intro text first (live web already streamed it; persisted here so it precedes the
    # card on reload). The pending tool_call row is the durable suspended state;
    # startup recovery sees it and leaves the turn waiting for the user's click.
    has_intro = bool(intro_text and intro_text.strip())
    created_at = datetime.now(timezone.utc)
    async with async_session() as db:
        # The session row is the cross-process serialization boundary shared with
        # inbound message ingestion and confirmation resolution.  Either the user
        # message wins first and belongs to this turn, or the pending card wins and
        # every later message is rejected until the card is clicked.
        from app.models.chat_session import ChatSession

        try:
            sid = uuid.UUID(str(conversation_id))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "confirmation requires a normalized chat session"
            ) from exc
        locked_session_id = (
            await db.execute(
                select(ChatSession.id)
                .where(
                    ChatSession.id == sid,
                    ChatSession.agent_id == agent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_session_id is None:
            raise RuntimeError("confirmation chat session no longer exists")
        existing = await find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(conversation_id),
        )
        if existing is not None:
            logger.warning(
                "Confirmation %s already pending for conversation %s; "
                "reusing the existing suspended tool call",
                existing.row_id,
                conversation_id,
            )
            return existing.row_id
        if has_intro:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content=intro_text,
                    conversation_id=str(conversation_id),
                    created_at=created_at,
                )
            )
        row_id = await persist_pending_confirmation_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(conversation_id),
            name=REQUEST_CONFIRMATION_TOOL_NAME,
            args=args,
            turn_anchor_id=turn_anchor_id,
            created_at=(
                created_at + timedelta(microseconds=1)
                if has_intro
                else None
            ),
        )
        await db.commit()

    # ALWAYS mirror the card to live web viewers — the card IS a tool_call, so broadcast it
    # as one regardless of origin channel (a web viewer watching a DingTalk session sees it
    # live, keyed on the row id so the later resolve flips this same card in place).
    await _broadcast(
        agent_id,
        conversation_id,
        {
            "type": "tool_call",
            "name": REQUEST_CONFIRMATION_TOOL_NAME,
            "call_id": str(row_id),
            "args": args,
            "status": "running",
        },
    )
    # Additionally deliver to the originating IM channel. DingTalk doesn't stream, so send
    # the intro text as a message first, then the interactive card.
    if resolved_channel == "dingtalk":
        runtime = await load_turn_runtime(
            agent_id=agent_id,
            conversation_id=str(conversation_id),
        )

        async def _send_intro_then_card() -> bool:
            if has_intro:
                intro_delivered = await _deliver_dingtalk_unlocked(
                    agent_id,
                    runtime,
                    intro_text or "",
                )
                if not intro_delivered:
                    logger.warning(
                        "Confirmation %s: intro delivery failed; card suppressed",
                        row_id,
                    )
                    return False
            return await _deliver_channel_card_unlocked(
                agent_id=agent_id,
                out_track_id=str(row_id),
                args=args,
                external_conv_id=ext_conv_id,
                is_group=is_group,
            )

        await run_channel_send(
            _im_send_lock_key(str(conversation_id)),
            _send_intro_then_card,
        )
    logger.info("Confirmation suspended: row %s on channel %s (conv %s)", row_id, resolved_channel, conversation_id)
    return row_id


async def resolve_confirmation(
    *,
    agent_id: uuid.UUID,
    call_id: uuid.UUID,
    button_value: str,
    button_label: str | None,
    resolving_user_id: uuid.UUID,
    clicked_card_instance_id: str | None = None,
) -> str | None:
    """Record the clicked button as the tool_call's result, then resume the loop.

    ``call_id`` IS the pending request_confirmation tool_call row id. GENERIC: the platform
    executes nothing — it fills the tool result with a faithful relay of which button was
    pressed (+ validity), flips the row to ``done``, and re-enters the LLM loop so the agent
    reads the result and decides the next step itself. Idempotent; serialized per-session so
    a double-click resolves exactly once. Returns the result text (or None if already
    resolved / unknown), so the REST caller can surface the committed outcome.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    now = datetime.now(timezone.utc)
    result_text: str | None = None

    async with async_session() as db:
        # The row's conversation_id is immutable — read it first (unlocked) so we know which
        # session to lock; the authoritative pending-check happens under the lock below.
        row = await db.get(ChatMessage, call_id)
        if row is None or row.role != "tool_call" or row.agent_id != agent_id:
            logger.warning("resolve_confirmation: no matching tool_call row %s", call_id)
            return None
        conversation_id = row.conversation_id

        # Lock the session row FOR UPDATE so concurrent resolves of the same card serialize:
        # the second blocks here, then (after refresh) re-reads a 'done' row and returns early.
        try:
            sid = uuid.UUID(str(conversation_id))
            await db.execute(select(ChatSession.id).where(ChatSession.id == sid).with_for_update())
            await db.refresh(row)
        except (ValueError, TypeError):
            pass  # non-session conversation — best-effort, no lock

        try:
            payload = json.loads(row.content or "{}")
        except Exception:
            return None
        if payload.get("name") != REQUEST_CONFIRMATION_TOOL_NAME or payload.get("status") != "pending":
            return None  # idempotent — already resolved (or not a confirmation)

        pending_meta = row.message_meta if isinstance(row.message_meta, dict) else {}
        latest_delivery_id = pending_meta.get("confirmation_delivery_id")
        try:
            _assert_confirmation_actor(row, pending_meta, resolving_user_id)
        except ConfirmationActorMismatch:
            logger.warning(
                "resolve_confirmation: actor %s rejected for row %s intended for %s",
                resolving_user_id,
                call_id,
                pending_meta.get("intended_user_id") or row.user_id,
            )
            raise
        try:
            turn_anchor_id = uuid.UUID(str(pending_meta.get("turn_anchor_id")))
        except (TypeError, ValueError):
            turn_anchor_id = None

        label = button_label or button_value or "按钮"
        ago = _ago(row.created_at, now)
        expired = bool(row.created_at and (now - row.created_at) > timedelta(hours=CONFIRMATION_EXPIRY_HOURS))
        if expired:
            result_text = (
                f"用户点了「{label}」(value={button_value}){ago},但确认卡已超过 {CONFIRMATION_EXPIRY_HOURS} "
                f"小时有效期,此响应不可靠——请勿据此执行不可逆/危险操作;如仍需要请重新发卡确认。"
            )
        else:
            result_text = f"用户点了「{label}」(value={button_value}){ago}、在有效期内。"

        payload["status"] = "done"
        payload["result"] = result_text
        row.content = json.dumps(payload, ensure_ascii=False, default=str)
        await db.commit()

    # Flip the card for any live web viewer (the click optimistically updates the clicker's
    # own card; this updates other watchers + is the authoritative live signal).
    await _broadcast(
        agent_id,
        conversation_id,
        {
            "type": "tool_call",
            "name": REQUEST_CONFIRMATION_TOOL_NAME,
            "call_id": str(call_id),
            # Carry args so the live merge keeps the card's content (the web merge spreads
            # the event and would otherwise overwrite toolArgs with undefined).
            "args": payload.get("args"),
            "status": "done",
            "result": result_text,
        },
    )

    # Keep the origin IM card in sync: a card delivered to DingTalk — resolved here on web OR
    # via its own button — flips to a disabled, resolved state so it can't be clicked again
    # (no stale clicks). Best-effort; web-origin cards are a no-op.
    card_instance_ids = [str(call_id)]
    if latest_delivery_id:
        card_instance_ids.append(str(latest_delivery_id))
    if clicked_card_instance_id:
        card_instance_ids.append(clicked_card_instance_id)
    for card_instance_id in dict.fromkeys(card_instance_ids):
        await _update_origin_card(
            agent_id,
            conversation_id,
            conversation_id,
            card_instance_id,
            payload.get("args") or {},
            label,
            button_value,
        )

    # Resume the agent's loop from the now-complete tool result. Best-effort — its failure
    # must not mask the resolution (the REST caller still gets the committed outcome).
    try:
        await _reenter_loop(
            agent_id,
            str(conversation_id),
            resolving_user_id,
            turn_anchor_id=turn_anchor_id,
        )
    except Exception:
        logger.exception("Reenter after resolving confirmation %s failed; resolution stands.", call_id)

    return result_text


async def _reenter_loop(
    agent_id: uuid.UUID,
    conversation_id: str,
    resolving_user_id: uuid.UUID,
    *,
    turn_anchor_id: uuid.UUID | None = None,
) -> None:
    """Resume the agent's LLM loop from existing history (which now ends with the filled
    request_confirmation tool result) WITHOUT injecting a user message. Per-session lock via
    run_channel_message; persists + delivers the follow-up reply to the originating channel."""
    from app.models.agent import Agent as AgentModel, DEFAULT_CONTEXT_WINDOW_SIZE
    from app.models.chat_session import ChatSession
    from app.services.channel_dispatch import (
        ChannelReactions,
        chat_session_lock_key,
        run_channel_message,
    )
    from app.services.channel_llm import _call_agent_llm
    from app.services.chat_history import (
        load_history_for_llm,
        persist_assistant_reply,
        persist_assistant_reply_and_complete_turn,
    )

    async def _work() -> str:
        async with async_session() as db:
            agent_obj = (
                await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            ).scalar_one_or_none()
            ctx_size = (agent_obj.context_window_size if agent_obj else None) or DEFAULT_CONTEXT_WINDOW_SIZE
            history = await load_history_for_llm(
                db, agent_id=agent_id, conversation_id=conversation_id, ctx_size=ctx_size
            )
            reply = await _call_agent_llm(
                db,
                agent_id,
                "",
                session_id=conversation_id,
                user_id=resolving_user_id,
                history=history,
                recovery_hint=None,
                continue_turn=True,
                turn_anchor_id=turn_anchor_id,
            )
        # An empty reply means the agent suspended AGAIN (chained confirmation) and the new
        # suspend already persisted/delivered everything — nothing to add here.
        if reply and reply.strip():
            if turn_anchor_id is not None:
                await persist_assistant_reply_and_complete_turn(
                    async_session,
                    agent_id=agent_id,
                    user_id=resolving_user_id,
                    conversation_id=conversation_id,
                    content=reply,
                    turn_anchor_id=turn_anchor_id,
                )
            else:
                await persist_assistant_reply(
                    async_session,
                    agent_id=agent_id,
                    user_id=resolving_user_id,
                    conversation_id=conversation_id,
                    content=reply,
                )
            delivered = await deliver_reply_to_origin(
                agent_id=agent_id,
                conversation_id=conversation_id,
                reply=reply,
                require_transport=True,
            )
            if not delivered:
                logger.warning(
                    "Confirmation reply persisted but origin delivery failed: "
                    "agent=%s conversation=%s",
                    agent_id,
                    conversation_id,
                )
        return reply

    async with async_session() as db:
        session = await db.get(ChatSession, uuid.UUID(str(conversation_id)))
        if session is not None and session.agent_id != agent_id:
            raise RuntimeError("Confirmation session changed owner")
        # Legacy confirmation rows can outlive a deleted/missing ChatSession.
        # Preserve their UUID lock identity; durable sessions use the same
        # canonical external-session key as ordinary channel messages.
        lock_key = chat_session_lock_key(session) if session is not None else str(conversation_id)

    await run_channel_message(
        lock_key,
        is_command=False,
        reactions=ChannelReactions(),
        work=_work,
    )


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
            return False

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
