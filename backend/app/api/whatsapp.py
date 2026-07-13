"""WhatsApp Cloud API channel routes."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut


router = APIRouter(tags=["whatsapp"])

WHATSAPP_TEXT_LIMIT = 4096
DEFAULT_WHATSAPP_API_VERSION = "v23.0"
_processed_whatsapp_messages: set[str] = set()


def _split_text(text: str, limit: int = WHATSAPP_TEXT_LIMIT) -> list[str]:
    remaining = text or ""
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        segment = remaining[:limit]
        cut = max(segment.rfind("\n\n"), segment.rfind("\n"), segment.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks or [""]


def _verify_signature(app_secret: str, body: bytes, signature: str | None) -> bool:
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_message_text(message: dict) -> str:
    msg_type = message.get("type")
    if msg_type == "text":
        return str(((message.get("text") or {}).get("body") or "")).strip()
    if msg_type == "button":
        return str(((message.get("button") or {}).get("text") or "")).strip()
    if msg_type == "interactive":
        interactive = message.get("interactive") or {}
        button_reply = interactive.get("button_reply") or {}
        list_reply = interactive.get("list_reply") or {}
        return str(button_reply.get("title") or list_reply.get("title") or "").strip()
    return ""


async def _send_whatsapp_messages(config: ChannelConfig, to_phone: str, text: str) -> None:
    token = (config.app_secret or "").strip()
    phone_number_id = (config.app_id or "").strip()
    if not token or not phone_number_id:
        raise RuntimeError("WhatsApp channel is not fully configured")

    api_version = str((config.extra_config or {}).get("api_version") or DEFAULT_WHATSAPP_API_VERSION).strip()
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"

    async with httpx.AsyncClient(timeout=20) as client:
        for chunk in _split_text(text):
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to_phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": chunk},
                },
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"WhatsApp send failed: {resp.text[:300]}")


@router.post("/agents/{agent_id}/whatsapp-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_whatsapp_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    access_token = str(data.get("access_token") or "").strip()
    phone_number_id = str(data.get("phone_number_id") or "").strip()
    verify_token = str(data.get("verify_token") or "").strip()
    app_secret = str(data.get("app_secret") or "").strip()
    api_version = str(data.get("api_version") or DEFAULT_WHATSAPP_API_VERSION).strip()

    if not access_token or not phone_number_id or not verify_token:
        raise HTTPException(status_code=422, detail="access_token, phone_number_id, and verify_token are required")

    extra_config = {"api_version": api_version}
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.app_id = phone_number_id
        existing.app_secret = access_token
        existing.verification_token = verify_token
        existing.encrypt_key = app_secret or None
        existing.extra_config = extra_config
        existing.is_configured = True
        await db.flush()
        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="whatsapp",
        app_id=phone_number_id,
        app_secret=access_token,
        verification_token=verify_token,
        encrypt_key=app_secret or None,
        extra_config=extra_config,
        is_configured=True,
    )
    db.add(config)
    await db.flush()
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/whatsapp-channel", response_model=ChannelConfigOut)
async def get_whatsapp_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WhatsApp not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/whatsapp-channel/webhook-url")
async def get_whatsapp_webhook_url(agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.platform_service import platform_service

    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/whatsapp/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/whatsapp-channel", status_code=204)
async def delete_whatsapp_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WhatsApp not configured")
    await db.delete(config)


@router.get("/channel/whatsapp/{agent_id}/webhook")
async def whatsapp_verify_webhook(
    agent_id: uuid.UUID,
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    if hub_mode == "subscribe" and hub_verify_token and hmac.compare_digest(hub_verify_token, config.verification_token or ""):
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("/channel/whatsapp/{agent_id}/webhook")
async def whatsapp_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    app_secret = (config.encrypt_key or "").strip()
    signature = request.headers.get("x-hub-signature-256")
    if app_secret and not _verify_signature(app_secret, body, signature):
        return Response(status_code=401)

    payload = await request.json()
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            messages = value.get("messages") or []
            contacts = value.get("contacts") or []
            contact_name = ""
            if contacts:
                contact_name = str(((contacts[0].get("profile") or {}).get("name") or "")).strip()

            for message in messages:
                message_id = str(message.get("id") or "").strip()
                user_text = _extract_message_text(message)
                sender_phone = str(message.get("from") or "").strip()
                if not user_text or not sender_phone:
                    continue

                from app.services.channel_llm import _call_agent_llm
                from app.services.im_thinking_output import BufferedIMThinkingSender, resolve_im_thinking_enabled
                from app.models.agent import Agent as AgentModel, DEFAULT_CONTEXT_WINDOW_SIZE
                from app.models.audit import ChatMessage
                from app.services.channel_session import find_or_create_channel_session
                from app.services.channel_user_service import channel_user_service
                from app.services.channel_dispatch import ChannelReactions, run_channel_message
                from app.services.channel_commands import is_channel_command, handle_channel_command

                agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                agent_obj = agent_r.scalar_one_or_none()
                if not agent_obj:
                    continue

                platform_user = await channel_user_service.resolve_channel_user(
                    db=db,
                    agent=agent_obj,
                    channel_type="whatsapp",
                    external_user_id=sender_phone,
                    extra_info={"name": contact_name or f"WhatsApp User {sender_phone[-6:]}"},
                )
                platform_user_id = platform_user.id
                conv_id = f"whatsapp_{sender_phone}"

                # 同一 WhatsApp 号码（会话）的多轮消息串行化；不同号码各自独立
                lock_key = f"whatsapp:{conv_id}"

                # Early-return for channel commands (/new, /reset):
                # archive the session and send a canned reply — no LLM, no lock needed.
                if is_channel_command(user_text):
                    if message_id and message_id in _processed_whatsapp_messages:
                        continue
                    if message_id:
                        _processed_whatsapp_messages.add(message_id)
                        if len(_processed_whatsapp_messages) > 2000:
                            _processed_whatsapp_messages.clear()
                    cmd_result = await handle_channel_command(
                        db=db, command=user_text, agent_id=agent_id,
                        user_id=None, external_conv_id=conv_id,
                        source_channel="whatsapp",
                    )
                    await db.commit()
                    try:
                        await _send_whatsapp_messages(config, sender_phone, cmd_result["message"])
                    except Exception as _cmd_e:
                        logger.error(f"[WhatsApp] Failed to send command reply: {_cmd_e}")
                    continue

                # 捕获循环变量供闭包使用
                _sender_phone = sender_phone

                async def _work() -> str:
                    # 正常消息轮次：会话解析 → 写入用户行 → LLM → 持久化回复 → 发送
                    sess = await find_or_create_channel_session(
                        db=db,
                        agent_id=agent_id,
                        user_id=platform_user_id,
                        external_conv_id=conv_id,
                        source_channel="whatsapp",
                        first_message_title=user_text,
                    )
                    session_conv_id = str(sess.id)
                    ctx_size = agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
                    history_r = await db.execute(
                        select(ChatMessage)
                        .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == session_conv_id)
                        .order_by(ChatMessage.created_at.desc())
                        .limit(ctx_size)
                    )
                    history = [{"role": m.role, "content": m.content} for m in reversed(history_r.scalars().all())]

                    from app.services.chat_history import ingest_incoming_chat_message

                    ingested = await ingest_incoming_chat_message(
                        db,
                        session=sess,
                        agent_id=agent_id,
                        user_id=platform_user_id,
                        content=user_text,
                        source_channel="whatsapp",
                        provider_event_id=message_id or None,
                        channel_config_id=config.id,
                        actor_ref=_sender_phone,
                        reply_to_external_message_id=(message.get("context") or {}).get("id"),
                    )
                    sess.last_message_at = datetime.now(timezone.utc)
                    await db.commit()

                    # Mirror this inbound message to anyone viewing the session on
                    # web in real time — the agent reply already streams there; this
                    # makes the user's own message show up live too, not only on reload.
                    from app.services.channel_llm import broadcast_channel_user_message
                    await broadcast_channel_user_message(
                        agent_id, session_conv_id, content=user_text,
                        sender_name=contact_name or None, user_id=platform_user_id,
                    )

                    if ingested.consumed_by_onmessage:
                        logger.info(
                            "[WhatsApp] Inbound event %s routed to %d on_message execution(s)",
                            message_id,
                            len(ingested.execution_ids),
                        )
                        return ""

                    _thinking_chunks: list[str] = []
                    _thinking_sender = BufferedIMThinkingSender(
                        enabled=resolve_im_thinking_enabled(agent_obj, sess),
                        send_text=lambda text: _send_whatsapp_messages(config, _sender_phone, text),
                    )

                    async def _collect_thinking(text: str):
                        _thinking_chunks.append(text)
                        await _thinking_sender.push(text)

                    try:
                        reply_text = await _call_agent_llm(
                            db, agent_id, user_text,
                            history=history, user_id=platform_user_id, session_id=session_conv_id,
                            on_thinking=_collect_thinking,
                            turn_anchor_id=ingested.message.id,
                        )
                    except Exception as exc:
                        logger.exception(f"[WhatsApp] LLM failed for agent {agent_id}: {exc}")
                        reply_text = "Sorry, I encountered an error processing your message."
                    finally:
                        await _thinking_sender.flush()

                    try:
                        await _send_whatsapp_messages(config, _sender_phone, reply_text)
                        config.is_connected = True
                        # Save assistant reply via the shared writer. Its own session stamps
                        # created_at at save time (after the tool loop), so the reply orders
                        # AFTER the turn's tool calls instead of being folded into the web UI's
                        # analysis card.
                        from app.services.chat_history import persist_assistant_reply
                        from app.database import async_session as _areply_session
                        await persist_assistant_reply(
                            _areply_session, agent_id=agent_id, user_id=platform_user_id,
                            conversation_id=session_conv_id, content=reply_text,
                            thinking="".join(_thinking_chunks) or None,
                            turn_anchor_id=ingested.message.id,
                        )
                        sess.last_message_at = datetime.now(timezone.utc)
                        await db.commit()
                    except Exception as exc:
                        logger.exception(f"[WhatsApp] Send failed for agent {agent_id}: {exc}")
                        config.is_connected = False
                        await db.commit()

                    return reply_text

                await run_channel_message(lock_key, is_command=False, reactions=ChannelReactions(), work=_work)

    return {"ok": True}
