"""Discord Bot Channel API routes (slash command interactions)."""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut
from app.services.channel_commands import is_channel_command

router = APIRouter(tags=["discord"])

DISCORD_MSG_LIMIT = 2000  # Discord message char limit


# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/discord-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_discord_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Discord bot for an agent.

    Gateway mode fields: bot_token (+ connection_mode='gateway').
    Webhook mode fields: application_id, bot_token, public_key.
    """
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    connection_mode = data.get("connection_mode", "webhook").strip()
    bot_token = data.get("bot_token", "").strip()
    application_id = data.get("application_id", "").strip()
    public_key = data.get("public_key", "").strip()

    if not bot_token:
        raise HTTPException(status_code=422, detail="bot_token is required")
    if connection_mode == "webhook" and (not application_id or not public_key):
        raise HTTPException(status_code=422, detail="application_id and public_key are required for webhook mode")

    extra_config = {"connection_mode": connection_mode}

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "discord",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.app_id = application_id or existing.app_id
        existing.app_secret = bot_token
        existing.encrypt_key = public_key or existing.encrypt_key
        existing.extra_config = extra_config
        existing.is_configured = True
        await db.flush()
    else:
        existing = ChannelConfig(
            agent_id=agent_id,
            channel_type="discord",
            app_id=application_id,
            app_secret=bot_token,
            encrypt_key=public_key,
            extra_config=extra_config,
            is_configured=True,
        )
        db.add(existing)
        await db.flush()

    # Mode-specific post-configuration
    if connection_mode == "gateway":
        # Start Gateway bot
        from app.services.discord_gateway import discord_gateway_manager
        await discord_gateway_manager.start_client(agent_id, bot_token)
    else:
        # Register slash commands for webhook mode
        try:
            reg = await _register_slash_commands(application_id, bot_token)
            logger.info(f"[Discord] Slash command registration: {reg['status']}")
        except Exception as e:
            logger.warning(f"[Discord] Could not register slash commands: {e}")

    return ChannelConfigOut.model_validate(existing)


@router.get("/agents/{agent_id}/discord-channel", response_model=ChannelConfigOut)
async def get_discord_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "discord",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Discord not configured")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/discord-channel/webhook-url")
async def get_discord_webhook_url(agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.platform_service import platform_service
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/discord/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/discord-channel", status_code=204)
async def delete_discord_channel(
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
            ChannelConfig.channel_type == "discord",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Discord not configured")
    # Stop Gateway client if running
    try:
        from app.services.discord_gateway import discord_gateway_manager
        await discord_gateway_manager.stop_client(agent_id)
    except Exception:
        pass
    await db.delete(config)


# ─── Slash Command Registration ─────────────────────────

async def _register_slash_commands(application_id: str, bot_token: str) -> dict:
    """Register /ask global slash command with Discord API."""
    import httpx
    import os
    command = {
        "name": "ask",
        "description": "Ask the AI agent a question",
        "options": [
            {
                "name": "message",
                "description": "Your question or message to the agent",
                "type": 3,   # STRING
                "required": True,
            }
        ],
    }
    url = f"https://discord.com/api/v10/applications/{application_id}/commands"
    proxy = os.environ.get("DISCORD_PROXY") or os.environ.get("HTTPS_PROXY") or None
    async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
        resp = await client.put(
            url,
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json=[command],
        )
        return {"status": resp.status_code, "body": resp.text}


# ─── Interactions Webhook ───────────────────────────────

def _verify_discord_signature(public_key: str, body: bytes, headers: dict) -> bool:
    """Verify Discord ed25519 signature."""
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError

        timestamp = headers.get("x-signature-timestamp", "")
        signature = headers.get("x-signature-ed25519", "")
        if not timestamp or not signature:
            return False

        verify_key = VerifyKey(bytes.fromhex(public_key))
        verify_key.verify(f"{timestamp}".encode() + body, bytes.fromhex(signature))
        return True
    except Exception:
        return False


async def _send_discord_followup(application_id: str, bot_token: str, interaction_token: str, text: str) -> None:
    """Send follow-up message(s) to Discord Interactions, chunked at 2000 chars."""
    import httpx
    chunks = [text[i:i + DISCORD_MSG_LIMIT] for i in range(0, len(text), DISCORD_MSG_LIMIT)]
    proxy = os.environ.get("DISCORD_PROXY") or os.environ.get("HTTPS_PROXY") or None
    async with httpx.AsyncClient(timeout=10, proxy=proxy) as client:
        for i, chunk in enumerate(chunks):
            if i == 0:
                # Edit the original deferred response
                await client.patch(
                    f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}/messages/@original",
                    headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                    json={"content": chunk},
                )
            else:
                # Additional chunks as follow-up messages
                await client.post(
                    f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}",
                    headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                    json={"content": chunk},
                )


@router.post("/channel/discord/{agent_id}/webhook")
async def discord_interaction_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Discord Interaction webhooks (PING + slash commands)."""
    body_bytes = await request.body()

    # Get channel config
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "discord",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    # Verify Discord signature
    public_key = config.encrypt_key or ""
    if public_key and not _verify_discord_signature(public_key, body_bytes, dict(request.headers)):
        return Response(content="Invalid signature", status_code=401)

    import json
    import asyncio
    body = json.loads(body_bytes)
    interaction_type = body.get("type", 0)

    # Type 1: PING — Discord URL verification
    if interaction_type == 1:
        return {"type": 1}

    # Type 2: APPLICATION_COMMAND (slash command)
    if interaction_type == 2:
        data_obj = body.get("data", {})
        command_name = data_obj.get("name", "")
        options = data_obj.get("options", [])
        user_text = ""
        for opt in options:
            if opt.get("name") == "message":
                user_text = opt.get("value", "").strip()
                break

        if not user_text:
            return {"type": 4, "data": {"content": "⚠️ 请提供消息内容。Usage: `/ask message:<你的问题>`"}}

        interaction_token = body.get("token", "")
        application_id = config.app_id or ""
        sender_id = body.get("member", {}).get("user", {}).get("id") or body.get("user", {}).get("id", "")
        channel_id = body.get("channel_id", "")
        # Discord: guild interactions are group chats, DM interactions are P2P
        _is_group_discord = bool(body.get("guild_id"))
        conv_id = f"discord_{channel_id}" if channel_id else f"discord_dm_{sender_id}"

        logger.info(f"[Discord] /{command_name} from {sender_id}: {user_text[:80]}")

        # Defer response immediately (Discord requires response within 3 seconds)
        # We return type 5 (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE) and reply later
        async def handle_in_background():
            from app.models.audit import ChatMessage
            from app.models.agent import Agent as AgentModel
            from app.services.channel_llm import _call_agent_llm
            from app.services.channel_session import find_or_create_channel_session
            from app.services.channel_dispatch import ChannelReactions, run_channel_message
            from app.services.channel_commands import handle_channel_command
            from app.services.im_thinking_output import BufferedIMThinkingSender, resolve_im_thinking_enabled
            from app.database import async_session
            from datetime import datetime, timezone

            # Early-return for channel commands (/new, /reset):
            # archive the session and reply via follow-up — no LLM, no lock needed.
            if is_cmd:
                async with async_session() as _cmd_db:
                    cmd_result = await handle_channel_command(
                        db=_cmd_db, command=user_text, agent_id=agent_id,
                        user_id=None, external_conv_id=conv_id,
                        source_channel="discord",
                    )
                    await _cmd_db.commit()
                # Re-read config for bot credentials
                async with async_session() as _cfg_db:
                    from sqlalchemy import select as _sel_cmd
                    _cfg_res = await _cfg_db.execute(_sel_cmd(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "discord",
                    ))
                    _cfg = _cfg_res.scalar_one_or_none()
                    _bot_token_cmd = _cfg.app_secret if _cfg else ""
                    _app_id_cmd = _cfg.app_id if _cfg else ""
                if _bot_token_cmd and interaction_token and _app_id_cmd:
                    try:
                        await _send_discord_followup(_app_id_cmd, _bot_token_cmd, interaction_token, cmd_result["message"])
                    except Exception as _cmd_e:
                        logger.error(f"[Discord] Failed to send command reply: {_cmd_e}")
                return

            async with async_session() as bg_db:
                # 整轮（用户行写入 → LLM → 回复持久化 → 发送）包在 _work 内串行化
                async def _work() -> str:
                    # Load agent
                    agent_r = await bg_db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                    agent_obj = agent_r.scalar_one_or_none()
                    creator_id = agent_obj.creator_id if agent_obj else agent_id
                    from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
                    ctx_size = (agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE) if agent_obj else DEFAULT_CONTEXT_WINDOW_SIZE

                    # Find-or-create platform user for this Discord sender via unified service
                    from app.services.channel_user_service import channel_user_service

                    _discord_username = body.get("member", {}).get("user", {}).get("username") or body.get("user", {}).get("username", "")
                    _display = _discord_username or f"Discord User {sender_id[:8]}"
                    _extra_info = {"name": _display}

                    _platform_user = await channel_user_service.resolve_channel_user(
                        db=bg_db,
                        agent=agent_obj,
                        channel_type="discord",
                        external_user_id=sender_id,
                        extra_info=_extra_info,
                    )

                    # Update display_name if we now have a better name
                    if _discord_username and _platform_user.display_name and _platform_user.display_name.startswith("Discord User ") and _platform_user.display_name != _discord_username:
                        _platform_user.display_name = _discord_username
                        await bg_db.flush()
                    platform_user_id = _platform_user.id

                    # Find-or-create ChatSession for this Discord conversation
                    sess = await find_or_create_channel_session(
                        db=bg_db,
                        agent_id=agent_id,
                        user_id=creator_id if _is_group_discord else platform_user_id,
                        external_conv_id=conv_id,
                        source_channel="discord",
                        first_message_title=user_text,
                        is_group=_is_group_discord,
                        group_name=f"Discord Channel {channel_id[:8]}" if _is_group_discord else None,
                    )
                    session_conv_id = str(sess.id)

                    # Load history from session
                    from app.services.chat_history import load_history_for_llm
                    history = await load_history_for_llm(
                        bg_db,
                        agent_id=agent_id,
                        conversation_id=session_conv_id,
                        ctx_size=ctx_size,
                        is_group=False,  # group-chat sender wrap not enabled for Discord yet
                    )

                    from app.services.chat_history import ingest_incoming_chat_message

                    ingested = await ingest_incoming_chat_message(
                        bg_db,
                        session=sess,
                        agent_id=agent_id,
                        user_id=platform_user_id,
                        content=user_text,
                        source_channel="discord",
                        provider_event_id=str(body.get("id") or "") or None,
                        channel_config_id=config.id,
                        actor_ref=sender_id,
                    )
                    sess.last_message_at = datetime.now(timezone.utc)
                    await bg_db.commit()

                    # Mirror this inbound message to anyone viewing the session on
                    # web in real time — the agent reply already streams there; this
                    # makes the user's own message show up live too, not only on reload.
                    from app.services.channel_llm import broadcast_channel_user_message
                    await broadcast_channel_user_message(
                        agent_id, session_conv_id, content=user_text,
                        sender_name=_discord_username or None, user_id=platform_user_id,
                    )

                    if ingested.consumed_by_onmessage:
                        logger.info(
                            "[Discord] Inbound interaction %s routed to %d on_message execution(s)",
                            body.get("id"),
                            len(ingested.execution_ids),
                        )
                        return ""

                    # Call LLM
                    _thinking_chunks: list[str] = []
                    cfg_r = await bg_db.execute(select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "discord",
                    ))
                    cfg = cfg_r.scalar_one_or_none()
                    bot_token_bg = cfg.app_secret if cfg else ""
                    app_id_bg = cfg.app_id if cfg else ""

                    async def _send_thinking_text(text: str) -> None:
                        if bot_token_bg and interaction_token and app_id_bg:
                            await _send_discord_followup(app_id_bg, bot_token_bg, interaction_token, text)

                    _thinking_sender = BufferedIMThinkingSender(
                        enabled=resolve_im_thinking_enabled(agent_obj, sess),
                        send_text=_send_thinking_text,
                    )

                    async def _collect_thinking(text: str):
                        _thinking_chunks.append(text)
                        await _thinking_sender.push(text)

                    try:
                        reply_text = await _call_agent_llm(
                            bg_db, agent_id, user_text,
                            history=history, user_id=platform_user_id, session_id=session_conv_id,
                            on_thinking=_collect_thinking,
                            turn_anchor_id=ingested.message.id,
                        )
                    finally:
                        await _thinking_sender.flush()
                    logger.info(f"[Discord] LLM reply: {reply_text[:80]}")

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
                    await bg_db.commit()

                    # Send chunked reply via Discord follow-up
                    if bot_token_bg and interaction_token and app_id_bg:
                        try:
                            await _send_discord_followup(app_id_bg, bot_token_bg, interaction_token, reply_text)
                        except Exception as e:
                            logger.error(f"[Discord] Failed to send follow-up: {e}")

                    return reply_text or ""

                # Discord webhook 无 emoji reaction，ChannelReactions 保持空
                await run_channel_message(lock_key, is_command=False, reactions=ChannelReactions(), work=_work)

        # 提前计算 lock_key 和指令标志，供 handle_in_background 内使用
        lock_key = f"discord:{conv_id}"
        is_cmd = is_channel_command(user_text)

        asyncio.create_task(handle_in_background())
        # Return DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE — shows "thinking..." to user
        return {"type": 5}

    # Unsupported interaction type
    return {"type": 1}
