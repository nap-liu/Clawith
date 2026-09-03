"""WeCom (企业微信) Channel API routes.

Provides Config CRUD and webhook-based message handling with AES encryption.
"""

import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import create_access_token, get_current_user
from app.database import async_session, get_db
from app.models.agent import Agent as AgentModel
from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
from app.models.channel_config import ChannelConfig
from app.models.identity import SSOScanSession
from app.models.user import User
from app.services.activity_logger import log_activity
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service
from app.services.platform_service import platform_service
from app.services.channel_llm import _call_agent_llm
from app.services.im_thinking_output import BufferedIMThinkingSender, resolve_im_thinking_enabled
from app.schemas.channel_config import ChannelConfigPublic as ChannelConfigOut
from app.services.wecom_stream import wecom_stream_manager
from app.services.im_markdown_media import project_agent_images_for_im
from app.api.wecom_support import (
    _decrypt_msg,
    _encrypt_msg,
    _get_wecom_token_cached,
    _pad,
    _unpad,
    _verify_signature,
)
from app.api.wecom_verification import (
    router as verification_router,
    serve_wecom_verify_file,
)

router = APIRouter(tags=["wecom"])
serve_wecom_verify_file.__module__ = __name__
router.include_router(verification_router)


# ─── WeCom Domain Verification File Hosting ────────────

# WeCom requires that each self-built app's trusted domain host a
# verification file at: https://domain/WW_verify_<token>.txt
# The file content is just the token string (plain text).
#
# For multi-tenant SaaS, we don't want every tenant to have their own server.
# Instead, tenants paste their verification token into the enterprise settings,
# and this endpoint serves the correct file content for any known token.
#
# Nginx config required to route requests at the root path:
#   location ~ ^/(WW_verify_[A-Za-z0-9_.-]{1,64}\.txt)$ {
#       proxy_pass http://backend:8000/api/wecom-verify/$1;
#   }

# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_wecom_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure WeCom bot for an agent.

    Supports two modes:
    - WebSocket (AI Bot): bot_id + bot_secret (no callback URL needed)
    - Webhook (legacy): corp_id, secret, token, encoding_aes_key
    """
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    # WebSocket mode fields (AI Bot)
    bot_id = data.get("bot_id", "").strip()
    bot_secret = data.get("bot_secret", "").strip()

    # Legacy webhook mode fields
    corp_id = data.get("corp_id", "").strip()
    wecom_agent_id = data.get("wecom_agent_id", "").strip()
    secret = data.get("secret", "").strip()
    token = data.get("token", "").strip()
    encoding_aes_key = data.get("encoding_aes_key", "").strip()

    # At least one mode must be configured
    has_ws_mode = bool(bot_id and bot_secret)
    has_webhook_mode = bool(corp_id and secret and token and encoding_aes_key)
    if not has_ws_mode and not has_webhook_mode:
        raise HTTPException(
            status_code=422,
            detail="Either bot_id+bot_secret (WebSocket) or corp_id+secret+token+encoding_aes_key (Webhook) required"
        )

    extra_config = {
        "wecom_agent_id": wecom_agent_id,
        "bot_id": bot_id,
        "bot_secret": bot_secret,
        "connection_mode": "websocket" if has_ws_mode else "webhook",
    }

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.app_id = corp_id
        existing.app_secret = secret
        existing.encrypt_key = encoding_aes_key
        existing.verification_token = token
        existing.extra_config = extra_config
        existing.is_configured = True
        existing.is_connected = False
        await db.flush()
        config_out = ChannelConfigOut.model_validate(existing)
    else:
        config = ChannelConfig(
            agent_id=agent_id,
            channel_type="wecom",
            app_id=corp_id,
            app_secret=secret,
            encrypt_key=encoding_aes_key,
            verification_token=token,
            extra_config=extra_config,
            is_configured=True,
            is_connected=False,
        )
        db.add(config)
        await db.flush()
        config_out = ChannelConfigOut.model_validate(config)

    try:
        if has_ws_mode:
            asyncio.create_task(
                wecom_stream_manager.start_client(agent_id, bot_id, bot_secret)
            )
            logger.info(f"[WeCom] WebSocket client start triggered for agent {agent_id}")
        else:
            asyncio.create_task(wecom_stream_manager.stop_client(agent_id))
            logger.info(f"[WeCom] WebSocket client stop triggered for agent {agent_id}")
    except Exception as e:
        logger.error(f"[WeCom] Failed to update WebSocket client state: {e}")

    return config_out


@router.get("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut)
async def get_wecom_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")

    config_out = ChannelConfigOut.model_validate(config)
    if (config.extra_config or {}).get("connection_mode") == "websocket":
        config_out.is_connected = wecom_stream_manager.status().get(str(agent_id), False)
    else:
        config_out.is_connected = False
    return config_out


@router.get("/agents/{agent_id}/wecom-channel/webhook-url")
async def get_wecom_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/wecom/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/wecom-channel", status_code=204)
async def delete_wecom_channel(
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
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")
    await wecom_stream_manager.stop_client(agent_id)
    await db.delete(config)


# ─── Event Webhook ──────────────────────────────────────

_processed_wecom_events: set[str] = set()
_processed_kf_msgids: set[str] = set()



@router.get("/channel/wecom/{agent_id}/webhook")
async def wecom_verify_webhook(
    agent_id: uuid.UUID,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle WeCom callback URL verification (GET request)."""
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning(f"[WeCom] Signature mismatch: expected={expected_sig}, got={msg_signature}")
        return Response(status_code=403)

    # Decrypt echostr and return plaintext
    try:
        decrypted, _ = _decrypt_msg(encoding_aes_key, echostr)
        return Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt echostr: {e}")
        return Response(status_code=500)


@router.post("/channel/wecom/{agent_id}/webhook")
async def wecom_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle WeCom message callback (POST request with encrypted XML)."""
    body_bytes = await request.body()

    # Get channel config
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""
    # Parse encrypted XML body
    try:
        root = ET.fromstring(body_bytes)
        encrypt_text = root.findtext("Encrypt", "")
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse XML body: {e}")
        return Response(content="success", media_type="text/plain")

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, encrypt_text)
    if expected_sig != msg_signature:
        logger.warning("[WeCom] Signature mismatch on POST")
        return Response(status_code=403)

    # Decrypt message
    try:
        decrypted_xml, recv_corp_id = _decrypt_msg(encoding_aes_key, encrypt_text)
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt message: {e}")
        return Response(content="success", media_type="text/plain")

    logger.info(f"[WeCom] Decrypted event for {agent_id}")

    # Parse decrypted message XML
    try:
        msg_root = ET.fromstring(decrypted_xml)
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse decrypted XML: {e}")
        return Response(content="success", media_type="text/plain")

    msg_type = msg_root.findtext("MsgType", "")
    from_user = msg_root.findtext("FromUserName", "")  # WeCom userid
    msg_id = msg_root.findtext("MsgId", "")
    open_kfid = msg_root.findtext("OpenKfId", "")
    token = msg_root.findtext("Token", "")
    # Group chat ID — present when message comes from a WeCom group
    chat_id = msg_root.findtext("ChatId", "")

    dedup_key = msg_id if msg_id else token
    logger.info(f"[WeCom] Message type={msg_type}, from={from_user}, msg_id={msg_id}, chat_id={chat_id or 'N/A'}")

    if msg_type == "text":
        user_text = msg_root.findtext("Content", "").strip()
        if not user_text:
            return Response(content="success", media_type="text/plain")

        # Process in background task
        asyncio.create_task(
            _process_wecom_text(
                agent_id,
                config,
                from_user,
                user_text,
                chat_id=chat_id,
                provider_event_id=dedup_key or None,
            )
        )

    elif msg_type == "event":
        if dedup_key and dedup_key in _processed_wecom_events:
            return Response(content="success", media_type="text/plain")
        if dedup_key:
            _processed_wecom_events.add(dedup_key)
            if len(_processed_wecom_events) > 1000:
                _processed_wecom_events.clear()
        event = msg_root.findtext("Event", "")
        if event == "kf_msg_or_event":
            asyncio.create_task(
                _process_wecom_kf_event(agent_id, config, token, open_kfid)
            )
        else:
            logger.info(f"[WeCom] Received event: {event} (not handled)")

    elif msg_type in ("image", "file"):
        # TODO: Handle image/file messages in future
        logger.info(f"[WeCom] Received {msg_type} message (not yet handled)")

    return Response(content="success", media_type="text/plain")


async def _process_wecom_kf_event(agent_id: uuid.UUID, config_obj: ChannelConfig, token: str, open_kfid: str = None):
    """Sync WeCom Customer Service (KF) messages in background."""
    try:
        async with async_session() as session:
            r = await session.execute(
                select(ChannelConfig).where(ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "wecom")
            )
            config = r.scalar_one_or_none()
            if not config:
                return

            access_token = await _get_wecom_token_cached(config.app_id, config.app_secret)
            if not access_token:
                return

            async with httpx.AsyncClient(timeout=10) as client:
                current_cursor = token
                has_more = 1
                current_ts = int(time.time())

                while has_more:
                    payload = {"limit": 20}
                    if open_kfid:
                        payload["open_kfid"] = open_kfid

                    if current_cursor.startswith("ENC"):
                        payload["token"] = current_cursor
                    else:
                        payload["cursor"] = current_cursor
                    
                    logger.info(f"[WeCom KF] Calling sync_msg with payload: {payload}")
                    sync_resp = await client.post(f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}", json=payload)
                    sync_data = sync_resp.json()
                    if sync_data.get("errcode") != 0:
                        logger.error(f"[WeCom KF] sync_msg error: {sync_data}")
                        break
                    
                    has_more = sync_data.get("has_more", 0)
                    current_cursor = sync_data.get("next_cursor", "")
                    
                    for msg in sync_data.get("msg_list", []):
                        if msg.get("origin") == 3 and msg.get("msgtype") == "text":
                            mid = msg.get("msgid")
                            if mid in _processed_kf_msgids:
                                continue
                            if msg.get("send_time", 0) > 0 and (current_ts - msg.get("send_time", 0) > 86400):
                                continue
                            _processed_kf_msgids.add(mid)
                            text = msg.get("text", {}).get("content", "").strip()
                            if text:
                                logger.info(f"[WeCom KF] Found msg from {msg.get('external_userid')}: {text[:20]}...")
                                # Call the local process text with extra KF info
                                await _process_wecom_text(
                                    agent_id, config,
                                    msg.get("external_userid"), text,
                                    is_kf=True, open_kfid=msg.get("open_kfid"), kf_msg_id=mid
                                )
                    if not has_more:
                        break
    except Exception as e: 
        logger.error(f"[WeCom KF] Error in background task: {e}")


async def _process_wecom_text(
    agent_id: uuid.UUID,
    config: ChannelConfig,
    from_user: str,
    user_text: str,
    is_kf: bool = False,
    open_kfid: str = None,
    kf_msg_id: str = None,
    chat_id: str = "",
    provider_event_id: str | None = None,
):
    """Process an incoming WeCom text message and reply."""
    from app.services.channel_commands import is_channel_command, prepare_channel_command_reply
    from app.services.channel_dispatch import (
        ChannelReactions,
        channel_session_lock_key,
        run_channel_message,
    )

    # conv_id 与 find_or_create_channel_session 传入的 external_conv_id 完全一致:
    #   群聊 → wecom_group_{chat_id}  (不含 from_user,避免不同成员开多会话)
    #   P2P  → wecom_p2p_{from_user}
    _is_group = bool(chat_id)
    conv_id = f"wecom_group_{chat_id}" if _is_group else f"wecom_p2p_{from_user}"
    lock_key = channel_session_lock_key(agent_id, "wecom", conv_id)

    # Early-return for channel commands (/new, /reset):
    # archive the session and send a canned reply — no LLM, no lock needed.
    if is_channel_command(user_text):
        async with async_session() as _cmd_db:
            cmd_result = await prepare_channel_command_reply(
                db=_cmd_db, command=user_text, agent_id=agent_id,
                user_id=None, external_conv_id=conv_id,
                external_user_id=from_user,
                source_channel="wecom",
                provider_event_id=provider_event_id or kf_msg_id,
                is_group=_is_group,
            )
            await _cmd_db.commit()
        if not cmd_result["should_deliver"]:
            return
        wecom_agent_id_cmd = (config.extra_config or {}).get("wecom_agent_id", "")
        from app.services.im_delivery import (
            IMDeliveryPart,
            IMDeliveryResult,
            register_delivery,
        )
        try:
            access_token_cmd = await _get_wecom_token_cached(config.app_id, config.app_secret)
            async with httpx.AsyncClient(timeout=10) as _cl_cmd:
                if access_token_cmd:
                    _cmd_response = await _cl_cmd.post(
                        f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token_cmd}",
                        json={
                            "touser": from_user,
                            "msgtype": "text",
                            "agentid": int(wecom_agent_id_cmd) if wecom_agent_id_cmd else 0,
                            "text": {"content": cmd_result["message"]},
                        },
                    )
                    _cmd_response.raise_for_status()
                    _cmd_data = _cmd_response.json()
                    if _cmd_data.get("errcode") not in (0, "0"):
                        raise RuntimeError(str(_cmd_data.get("errmsg") or _cmd_data.get("errcode")))
                    _cmd_provider_id = str(_cmd_data.get("msgid") or "") or None
                    await register_delivery(
                        cmd_result["message_id"],
                        IMDeliveryResult.sent(
                            "wecom",
                            IMDeliveryPart(
                                transport="wecom_app",
                                provider_message_id=_cmd_provider_id,
                                conversation_ref=from_user,
                                artifact_role="command_reply",
                                recallable=bool(_cmd_provider_id),
                            ),
                        ),
                    )
                else:
                    raise RuntimeError("access_token_unavailable")
        except Exception as _cmd_e:
            await register_delivery(
                cmd_result["message_id"],
                IMDeliveryResult.from_exception("wecom", _cmd_e),
            )
            logger.error(f"[WeCom] Failed to send command reply: {_cmd_e}")
        return

    async def _work() -> str:
        async with async_session() as db:
            # 加载 Agent
            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            if not agent_obj:
                logger.warning(f"[WeCom] Agent {agent_id} not found")
                return ""
            creator_id = agent_obj.creator_id
            ctx_size = (agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE) if agent_obj else DEFAULT_CONTEXT_WINDOW_SIZE

            extra_info = {"unionid": from_user}

            # 通过统一服务解析渠道用户(OrgMember + SSO)
            platform_user = await channel_user_service.resolve_channel_user(
                db=db,
                agent=agent_obj,
                channel_type="wecom",
                external_user_id=from_user,
                extra_info=extra_info,
            )
            platform_user_id = platform_user.id

            # 查找或创建会话,external_conv_id 与上方 conv_id 一致
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=creator_id if _is_group else platform_user_id,
                external_conv_id=conv_id,
                source_channel="wecom",
                first_message_title=user_text,
                is_group=_is_group,
                group_name=f"WeCom Group {chat_id[:8]}" if _is_group else None,
            )
            session_conv_id = str(sess.id)

            # 加载历史消息
            from app.services.chat_history import load_history_for_llm
            history = await load_history_for_llm(
                db,
                agent_id=agent_id,
                conversation_id=session_conv_id,
                ctx_size=ctx_size,
                is_group=False,  # 企微群聊暂不启用 sender wrap
            )

            # 写入用户消息行并在同一事务中匹配精确 on_message 订阅。
            from app.services.chat_history import ingest_incoming_chat_message

            ingested = await ingest_incoming_chat_message(
                db,
                session=sess,
                agent_id=agent_id,
                user_id=platform_user_id,
                content=user_text,
                source_channel="wecom",
                provider_event_id=provider_event_id or kf_msg_id,
                channel_config_id=config.id,
                actor_ref=from_user,
            )
            sess.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            # 实时镜像入站用户消息到正在 web 端查看该会话的客户端 —— agent 回复已经
            # 在那边流式渲染,这一步让用户自己的消息也实时出现,而非刷新后才看到。
            from app.services.channel_llm import broadcast_channel_user_message
            await broadcast_channel_user_message(
                agent_id, session_conv_id, message=ingested.message,
                sender_name=None, user_id=platform_user_id,
            )

            if ingested.consumed_by_onmessage:
                logger.info(
                    "[WeCom] Inbound event %s routed to %d on_message execution(s)",
                    provider_event_id or kf_msg_id,
                    len(ingested.execution_ids),
                )
                return ""

            wecom_agent_id = (config.extra_config or {}).get("wecom_agent_id", "")

            async def _send_wecom_text(text: str) -> dict:
                access_token = await _get_wecom_token_cached(config.app_id, config.app_secret)
                if not access_token:
                    raise RuntimeError("WeCom access token unavailable")
                async with httpx.AsyncClient(timeout=10) as client:
                    if is_kf and open_kfid:
                        # KF 消息需先转接状态再发送
                        res_state = await client.post(
                            f"https://qyapi.weixin.qq.com/cgi-bin/kf/service_state/trans?access_token={access_token}",
                            json={"open_kfid": open_kfid, "external_userid": from_user, "service_state": 1},
                        )
                        logger.info(f"[WeCom KF] trans state result: {res_state.json()}")
                        res_send = await client.post(
                            f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={access_token}",
                            json={"touser": from_user, "open_kfid": open_kfid, "msgtype": "text", "text": {"content": text}},
                        )
                        data = res_send.json()
                        logger.info(f"[WeCom KF] send_msg result: {data}")
                    else:
                        # 默认发送文本消息
                        response = await client.post(
                            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}",
                            json={
                                "touser": from_user,
                                "msgtype": "text",
                                "agentid": int(wecom_agent_id) if wecom_agent_id else 0,
                                "text": {"content": text},
                            },
                        )
                        data = response.json()
                if data.get("errcode") != 0:
                    raise RuntimeError(str(data.get("errmsg") or data.get("errcode") or "WeCom send failed"))
                return data

            # 调用 LLM
            _thinking_chunks: list[str] = []
            _thinking_sender = BufferedIMThinkingSender.for_runtime(
                enabled=resolve_im_thinking_enabled(agent_obj, sess),
                agent_id=agent_id,
                user_id=platform_user_id,
                conversation_id=session_conv_id,
                turn_anchor_id=ingested.message.id,
                delivery_kwargs={
                    "origin_actor_ref": from_user,
                    "allow_wecom_group_actor_fallback": True,
                },
            )

            async def _collect_thinking(text: str):
                _thinking_chunks.append(text)
                await _thinking_sender.push(text)

            try:
                reply_text = await _call_agent_llm(
                    db, agent_id, user_text,
                    history=history, user_id=platform_user_id,
                    session_id=session_conv_id,
                    on_thinking=_collect_thinking,
                    turn_anchor_id=ingested.message.id,
                )
            finally:
                await _thinking_sender.flush()
            logger.info(f"[WeCom] LLM reply: {reply_text[:100]}")

            # Persist the local lifecycle anchor before the external side effect.
            from app.services.chat_history import persist_assistant_reply
            from app.database import async_session as _areply_session
            from app.services.im_delivery import (
                IMDeliveryPart,
                IMDeliveryResult,
                attach_delivery_to_meta,
                register_delivery,
            )

            assistant_message_id = await persist_assistant_reply(
                _areply_session, agent_id=agent_id, user_id=platform_user_id,
                conversation_id=session_conv_id, content=reply_text,
                thinking="".join(_thinking_chunks) or None,
                message_meta=attach_delivery_to_meta({}, IMDeliveryResult.pending("wecom")),
                turn_anchor_id=ingested.message.id,
                required=True,
            )
            delivery_reply_text = await project_agent_images_for_im(agent_id, reply_text)

            # 通过企微 API 发送回复
            try:
                send_result = await _send_wecom_text(delivery_reply_text)
                msgid = str(send_result.get("msgid") or "")
                transport = "wecom_kf" if is_kf else "wecom_app"
                delivery_result = IMDeliveryResult.sent(
                    "wecom",
                    IMDeliveryPart(
                        transport=transport,
                        provider_message_id=msgid or None,
                        conversation_ref=from_user,
                        recallable=bool(msgid) and not is_kf,
                    ),
                )
            except Exception as e:
                logger.error(f"[WeCom] Failed to send reply: {e}")
                delivery_result = IMDeliveryResult.from_exception("wecom", e)
            if assistant_message_id is not None:
                await register_delivery(assistant_message_id, delivery_result)
            sess.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            # 记录活动日志
            await log_activity(
                agent_id, "chat_reply",
                f"Replied to WeCom message: {reply_text[:80]}",
                detail={"channel": "wecom", "user_text": user_text[:200], "reply": reply_text[:500]},
            )

            return reply_text or ""

    # WeCom webhook 无 emoji reaction,ChannelReactions 保持空
    await run_channel_message(lock_key, is_command=False, reactions=ChannelReactions(), work=_work)


# ─── OAuth Callback (SSO) ──────────────────────────────

@router.get("/auth/wecom/callback")
async def wecom_callback(
    code: str,
    request: Request,
    state: str = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.auth_provider import WeComAuthProvider
    from app.services.sso_login_state import (
        get_enabled_sso_provider,
        parse_sso_or_legacy_state,
        sso_browser_cookie_name,
        sso_completion_url,
        sso_error_url,
        verify_sso_browser_binding,
    )
    # 1. Resolve session to get tenant context
    sid, provider_id, login_query = parse_sso_or_legacy_state(state)
    if sid is None or provider_id is None:
        return RedirectResponse(sso_error_url("invalid_state", login_query), status_code=302)
    if not verify_sso_browser_binding(sid, request.cookies.get(sso_browser_cookie_name(sid))):
        return RedirectResponse(sso_error_url("browser_mismatch", login_query), status_code=302)
    tenant_id = None
    if sid:
        s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
        session = s_res.scalar_one_or_none()
        if session and session.expires_at >= datetime.now(timezone.utc) and session.status in {"pending", "scanned"}:
            tenant_id = session.tenant_id
        else:
            return RedirectResponse(sso_error_url("invalid_session", login_query), status_code=302)

    # 1. Get WeCom provider config
    provider = await get_enabled_sso_provider(db, provider_id, "wecom", tenant_id)
    if not provider:
        return RedirectResponse(sso_error_url("provider_unavailable", login_query), status_code=302)

    # 2. Extract user info and login/register via RegistrationService
    try:
        auth_provider = WeComAuthProvider(provider=provider, config=provider.config or {})
        
        token_data = await auth_provider.exchange_code_for_token(code)
        access_token_str = token_data.get("access_token")
        if not access_token_str:
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)
            
        user_info = await auth_provider.get_user_info(access_token_str)
        if not user_info.provider_user_id:
            return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)
            
        # Find or Create User (handles Identity and OrgMember linking)
        user, _is_new = await auth_provider.find_or_create_user(
            db, user_info, tenant_id=tenant_id or provider.tenant_id
        )
    except Exception as e:
        logger.exception(f"WeCom login/register error: {e}")
        return RedirectResponse(sso_error_url("authentication_failed", login_query), status_code=302)


    # Standard login
    token = create_access_token(str(user.id), user.role)

    if sid:
        try:
            s_res = await db.execute(select(SSOScanSession).where(SSOScanSession.id == sid))
            session = s_res.scalar_one_or_none()
            if session:
                session.status = "authorized"
                session.provider_type = "wecom"
                session.user_id = user.id
                session.access_token = token
                session.error_msg = None
                await db.commit()
                return RedirectResponse(sso_completion_url(sid, login_query), status_code=302)
        except Exception as e:
            logger.exception("Failed to update SSO session (wecom) %s", e)

    return RedirectResponse(sso_error_url("session_update_failed", login_query), status_code=302)
