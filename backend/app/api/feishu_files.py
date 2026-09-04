"""Feishu file-message handling helpers."""

from app.api.feishu_shared import *  # noqa: F401,F403
from app.services.im_markdown_media import project_agent_images_for_im

IMPORT_RE = None  # lazy sentinel
_FILE_ACK_MESSAGES = [
    "收到你的文件，请问有什么需要帮忙的？",
    "文件收到了！你想让我怎么处理它？",
    "好的，我已经收到这份文件，请告诉我你的需求~",
    "已收到文件，随时准备好为你处理！",
    "收到！请问希望我对这份文件做什么？",
]


async def _handle_feishu_file(
    db,
    agent_id,
    config,
    message,
    sender_open_id,
    sender_user_id_from_event,
    chat_type,
    chat_id,
):
    """Handle an incoming Feishu file/image before acknowledging the event."""
    import asyncio, random, json
    from app.models.agent import Agent as AgentModel
    from app.models.user import User as UserModel
    from app.services.channel_session import find_or_create_channel_session
    from app.core.security import hash_password
    from app.database import async_session as _async_session
    from datetime import datetime as _dt, timezone as _tz
    import uuid as _uuid
    from sqlalchemy import select as _select

    msg_type = message.get("message_type", "file")
    message_id = message.get("message_id", "")
    content = json.loads(message.get("content", "{}"))

    # Extract file key and name
    if msg_type == "image":
        file_key = content.get("image_key", "")
        filename = f"image_{file_key[-8:]}.jpg" if file_key else "image.jpg"
        res_type = "image"
    else:
        file_key = content.get("file_key", "")
        filename = content.get("file_name") or f"file_{file_key[-8:]}.bin"
        res_type = "file"

    if not file_key:
        logger.warning(f"[Feishu] No file_key in {msg_type} message")
        return

    # Resolve workspace upload dir
    # Download the file
    try:
        file_bytes = await feishu_service.download_message_resource(
            config.app_id, config.app_secret, message_id, file_key, res_type
        )
        _, workspace_path, save_path = await store_agent_upload(
            agent_id,
            filename,
            file_bytes,
            content_type="image/jpeg" if msg_type == "image" else None,
        )
        logger.info(f"[Feishu] Saved {msg_type} to {workspace_path} ({len(file_bytes)} bytes)")
    except Exception as e:
        logger.error(f"[Feishu] Failed to download {msg_type}: {e}")
        err_tip = "抱歉，文件下载失败。可能原因：机器人缺少 `im:resource` 权限（文件读取）。\n请在飞书开放平台 → 权限管理 → 批量导入权限 JSON → 重新发布机器人版本后重试。"
        try:
            await _persist_feishu_control_reply(
                agent_id=agent_id,
                config=config,
                sender_open_id=sender_open_id,
                chat_type=chat_type,
                chat_id=chat_id,
                message=err_tip,
                artifact_role="download_error_ack",
            )
        except Exception as e2:
            logger.error(f"[Feishu] Also failed to send error tip: {e2}")
        raise RuntimeError(f"Feishu {msg_type} event was not durably ingested") from e

    # Resolve platform user and session using a fresh db session
    async with _async_session() as db:
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()

        # Resolve sender's Feishu user_id (more stable than open_id)
        sender_user_id_feishu = sender_user_id_from_event or ""
        extra_info: dict | None = {
            "open_id": sender_open_id,
            "external_id": sender_user_id_feishu or None,
        }
        try:
            import httpx as _hx
            async with _hx.AsyncClient() as _fc:
                _tr = await _fc.post(
                    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                    json={"app_id": config.app_id, "app_secret": config.app_secret},
                )
                _at = _tr.json().get("app_access_token", "")
                if _at:
                    _ur = await _fc.get(
                        f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_open_id}",
                        params={"user_id_type": "open_id"},
                        headers={"Authorization": f"Bearer {_at}"},
                    )
                    _ud = _ur.json()
                    if _ud.get("code") == 0:
                        _user_info = _ud.get("data", {}).get("user", {})
                        sender_user_id_feishu = _user_info.get("user_id", "")
                        # Feishu contact API returns 'avatar' as a dict
                        # (keys: avatar_240, avatar_640, avatar_origin), NOT a plain URL.
                        _raw_avatar = _user_info.get("avatar")
                        if isinstance(_raw_avatar, dict):
                            _avatar_url = (
                                _raw_avatar.get("avatar_240")
                                or _raw_avatar.get("avatar_640")
                                or _raw_avatar.get("avatar_origin")
                                or ""
                            )
                        else:
                            _avatar_url = _raw_avatar or ""
                        extra_info = {
                            "name": _user_info.get("name"),
                            "avatar_url": _avatar_url,
                            "email": _user_info.get("email"),
                            "mobile": _user_info.get("mobile"),
                            "external_id": _user_info.get("user_id"),
                            "unionid": _user_info.get("union_id"),
                            "open_id": sender_open_id,
                        }
        except Exception:
            pass

        # Resolve channel user via unified service (uses OrgMember + SSO patterns)
        from app.services.channel_user_service import channel_user_service
        try:
            platform_user = await channel_user_service.resolve_channel_user(
                db=db,
                agent=agent_obj,
                channel_type="feishu",
                # For Feishu, external_user_id is strictly user_id (tenant-stable).
                external_user_id=sender_user_id_feishu or None,
                extra_info=extra_info,
            )
        except Exception as e:
            from app.services.channel_user_service import ChannelUserResolutionError

            if isinstance(e, ChannelUserResolutionError):
                logger.warning(f"[Feishu] File sender resolution refused: {e}")
                await _persist_feishu_control_reply(
                    agent_id=agent_id,
                    config=config,
                    sender_open_id=sender_open_id,
                    chat_type=chat_type,
                    chat_id=chat_id,
                    message=_USER_RESOLUTION_ERROR_TIP,
                    artifact_role="identity_error_ack",
                )
                return
            raise
        platform_user_id = platform_user.id

        # Conv ID — prefer user_id for session continuity.
        # NOTE: sender_user_id_feishu may have been refined by the API call above;
        # use the refined value (falls back to sender_user_id_from_event or open_id).
        if chat_type == "group" and chat_id:
            conv_id = f"feishu_group_{chat_id}"
        else:
            conv_id = f"feishu_p2p_{sender_user_id_feishu or sender_open_id}"

        _is_group_file = (chat_type == "group")
        sender_name_file = extra_info.get("name", "") if extra_info else ""

    # Per-session lock key (same formula as text path, 1-to-1 with DB session)
    lock_key = channel_session_lock_key(agent_id, "feishu", conv_id)

    # For images: call LLM so vision models can actually see the image.
    # _work covers user-row write → LLM → reply persistence (full turn, inside lock).
    if msg_type == "image":
        import json as _json_card_img

        async def _image_work() -> str:
            # ── User-row write + session setup (inside the session lock) ──
            async with _async_session() as _db_setup:
                _ag_r = await _db_setup.execute(_select(AgentModel).where(AgentModel.id == agent_id))
                _ag_obj = _ag_r.scalar_one_or_none()
                _file_user_id_img = _ag_obj.creator_id if (_is_group_file and _ag_obj) else platform_user_id
                _agent_name_img = _ag_obj.name if _ag_obj else "AI"
                ctx_size_img = (_ag_obj.context_window_size or 20) if _ag_obj else 20

                # Find/create session (so we have session_conv_id before writing user row)
                _fs_file_group_name = None
                if _is_group_file:
                    try:
                        _chat_info = await feishu_service.get_chat_info(config.app_id, config.app_secret, chat_id)
                        _real_name = (_chat_info or {}).get("name") if _chat_info else None
                        _fs_file_group_name = (
                            _real_name.strip() if _real_name and _real_name.strip()
                            else f"Feishu Group {chat_id[:12]}"
                        )
                    except Exception as _gci_err:
                        logger.warning(f"[Feishu] chat-info lookup failed (image path): {_gci_err}")
                        _fs_file_group_name = f"Feishu Group {chat_id[:12]}"

                _sess_img = await find_or_create_channel_session(
                    db=_db_setup, agent_id=agent_id, user_id=_file_user_id_img,
                    external_conv_id=conv_id, source_channel="feishu",
                    first_message_title=f"[图片] {filename}",
                    is_group=_is_group_file,
                    group_name=_fs_file_group_name,
                )
                session_conv_id_img = str(_sess_img.id)

                user_msg_content_img = "[用户发送了图片]"
                from app.services.chat_history import ingest_incoming_chat_message

                _image_ingested = await ingest_incoming_chat_message(
                    _db_setup,
                    session=_sess_img,
                    agent_id=agent_id,
                    user_id=platform_user_id,
                    content=f"[file:{filename}]",
                    source_channel="feishu",
                    provider_event_id=message_id or None,
                    channel_config_id=config.id,
                    actor_ref=sender_user_id_feishu or sender_open_id,
                    message_meta={
                        "message_type": "image",
                        "workspace_path": workspace_path,
                        "attachments": [attachment_from_workspace_path(workspace_path)],
                        "display_content": "",
                        "actor_ref_type": "user_id" if sender_user_id_feishu else "open_id",
                    },
                )
                _sess_img.last_message_at = _dt.now(_tz.utc)

                from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
                ctx_size_img = ctx_size_img if ctx_size_img > 0 else DEFAULT_CONTEXT_WINDOW_SIZE
                from app.services.chat_history import load_history_for_llm as _load_hist_llm
                _history_img = await _load_hist_llm(
                    _db_setup,
                    agent_id=agent_id,
                    conversation_id=session_conv_id_img,
                    ctx_size=ctx_size_img,
                    is_group=_is_group_file,
                )
                await _db_setup.commit()

            # Mirror this inbound file message to anyone viewing the session on web
            # in real time (matches what a reload renders: the [file:...] row).
            from app.services.channel_llm import broadcast_channel_user_message
            await broadcast_channel_user_message(
                agent_id, session_conv_id_img, message=_image_ingested.message,
                sender_name=sender_name_file or None, user_id=platform_user_id,
            )

            if _image_ingested.consumed_by_onmessage:
                logger.info("[Feishu] Image event %s routed to on_message", message_id)
                return ""

            # ── Streaming card setup ──
            _reply_to = chat_id if chat_type == "group" else sender_open_id
            _rid_type_img = "chat_id" if chat_type == "group" else "open_id"
            _img_reply_to_user_msg_id = message_id if chat_type == "group" else ""
            _init_card_img = {
                "config": {"update_multi": True},
                "header": {"template": "blue", "title": {"content": "识别图片中...", "tag": "plain_text"}},
                "elements": [{"tag": "markdown", "content": "..."}]
            }
            from app.services.im_delivery import persist_delivery_anchor

            assistant_message_id = await persist_delivery_anchor(
                agent_id=agent_id,
                user_id=platform_user_id,
                conversation_id=session_conv_id_img,
                channel="feishu",
                message="识别图片中…",
                turn_anchor_id=_image_ingested.message.id,
                artifact_role="streaming_card",
            )
            _patch_msg_id = None
            from app.services.im_delivery import (
                DeliveryReceiptPersistenceError,
                IMDeliveryPart,
                append_delivery_part,
            )

            try:
                _init_resp = await feishu_service.send_message(
                    config.app_id, config.app_secret, _reply_to, "interactive",
                    _json_card_img.dumps(_init_card_img), receive_id_type=_rid_type_img,
                    stage="image_stream_init_card",
                    reply_to_message_id=_img_reply_to_user_msg_id or None,
                )
                _patch_msg_id = _init_resp.get("data", {}).get("message_id")
                if _patch_msg_id:
                    await append_delivery_part(
                        assistant_message_id,
                        IMDeliveryPart(
                            transport="feishu_message",
                            provider_message_id=_patch_msg_id,
                            conversation_ref=_reply_to,
                            artifact_role="card",
                        ),
                    )
            except DeliveryReceiptPersistenceError:
                raise
            except Exception as _e_init:
                logger.error(f"[Feishu] Failed to send init card for image: {_e_init}")

            _img_stream_buf: list[str] = []
            _img_last_flush = time.time()
            _img_flush_interval = 1.0
            _img_patch_queue = _SerialPatchQueue()
            _img_heartbeat_task: asyncio.Task | None = None
            _img_llm_done = False
            _img_last_flushed_hash: int = 0
            _img_thinking_enabled = resolve_im_thinking_enabled(_ag_obj, _sess_img)

            async def _queue_image_patch(_card: dict, _stage: str):
                if not _patch_msg_id:
                    return
                _payload = _json_card_img.dumps(_card)
                async def _job():
                    try:
                        await feishu_service.patch_message(
                            config.app_id, config.app_secret, _patch_msg_id, _payload, stage=_stage,
                        )
                    except Exception as _e_patch:
                        logger.warning(f"[Feishu] Image patch failed (stage={_stage}): {_e_patch}")
                _img_patch_queue.enqueue(_job)

            async def _flush_image_stream(reason: str, force: bool = False):
                nonlocal _img_last_flush, _img_last_flushed_hash
                now = time.time()
                if not force and now - _img_last_flush < _img_flush_interval:
                    return
                _answer_text = "".join(_img_stream_buf)
                _thinking_text = "".join(_img_thinking_chunks) if _img_thinking_enabled else ""
                _card = await _build_projected_stream_card(
                    agent_id,
                    _answer_text,
                    thinking_text=_thinking_text,
                    agent_name=_agent_name_img,
                )
                current_hash = hash(_answer_text + _thinking_text)
                if reason == "heartbeat" and current_hash == _img_last_flushed_hash:
                    return
                _img_last_flushed_hash = current_hash
                await _queue_image_patch(_card, _stage=f"image_stream_{reason}")
                _img_last_flush = now

            _img_thinking_chunks: list[str] = []

            async def _img_on_chunk(text: str):
                _img_stream_buf.append(text)
                if _patch_msg_id:
                    await _flush_image_stream("chunk")

            async def _img_on_thinking(text: str):
                _img_thinking_chunks.append(text)
                if _patch_msg_id:
                    await _flush_image_stream("thinking")

            async def _img_heartbeat():
                while not _img_llm_done:
                    await asyncio.sleep(_img_flush_interval)
                    if _patch_msg_id:
                        await _flush_image_stream("heartbeat")

            if _patch_msg_id:
                _img_heartbeat_task = asyncio.create_task(_img_heartbeat())

            # Group chats get a platform-injected <sender> prefix (spec §4.0/§4.1).
            from app.services.sender_attribution import wrap_with_sender

            llm_user_msg_content = user_msg_content_img
            if chat_type == "group":
                llm_user_msg_content = wrap_with_sender(
                    user_msg_content_img,
                    platform_user_id,
                    sender_name_file or platform_user.display_name,
                )
            elif sender_name_file:
                llm_user_msg_content = f"[发送者: {sender_name_file}] {user_msg_content_img}"

            # ── LLM call ──
            async with _async_session() as _db_img:
                try:
                    reply_text = await _call_agent_llm(
                        _db_img, agent_id, llm_user_msg_content, history=_history_img,
                        user_id=platform_user_id, session_id=session_conv_id_img,
                        on_chunk=_img_on_chunk,
                        on_thinking=_img_on_thinking,
                        is_group=_is_group_file,
                        turn_anchor_id=_image_ingested.message.id,
                    )
                finally:
                    _img_llm_done = True
                    if _img_heartbeat_task:
                        _img_heartbeat_task.cancel()
                        try:
                            await _img_heartbeat_task
                        except Exception:
                            pass

            logger.info(f"[Feishu] Image LLM reply: {reply_text[:100]}")

            from app.services.im_delivery import (
                DeliveryReceiptPersistenceError,
                IMDeliveryPart,
                IMDeliveryResult,
                append_delivery_part,
                register_delivery,
                update_delivery_message_content,
            )

            if not await update_delivery_message_content(
                assistant_message_id,
                agent_id=agent_id,
                content=reply_text or "…",
                thinking="".join(_img_thinking_chunks) or None,
                complete_turn=True,
            ):
                raise RuntimeError("feishu_image_stream_anchor_missing")
            delivery_reply_text = await project_agent_images_for_im(agent_id, reply_text)

            # ── Send final card / fallback ──
            delivery_result = IMDeliveryResult.failed("feishu", "send_failed")
            if _patch_msg_id:
                card_part = IMDeliveryPart(
                    transport="feishu_message",
                    provider_message_id=_patch_msg_id,
                    conversation_ref=_reply_to,
                    artifact_role="card",
                )
                try:
                    await _img_patch_queue.drain()
                except Exception as _e_drain:
                    logger.warning(f"[Feishu] Image patch queue drain failed: {_e_drain}")
                _final_card = _build_card(delivery_reply_text or "...", streaming=False, agent_name=_agent_name_img)
                try:
                    await feishu_service.patch_message(
                        config.app_id, config.app_secret, _patch_msg_id,
                        _json_card_img.dumps(_final_card), stage="image_stream_final"
                    )
                    delivery_result = IMDeliveryResult.sent(
                        "feishu",
                        card_part,
                    )
                except Exception as _e_final_patch:
                    logger.error(f"[Feishu] Failed to patch final image reply: {_e_final_patch}")
            else:
                try:
                    fallback_response = await feishu_service.send_message(
                        config.app_id, config.app_secret, _reply_to, "text",
                        json.dumps({"text": delivery_reply_text}), receive_id_type=_rid_type_img,
                        stage="image_stream_fallback_text",
                    )
                    fallback_message_id = str(
                        ((fallback_response.get("data") or {}).get("message_id")) or ""
                    )
                    if fallback_message_id:
                        fallback_part = IMDeliveryPart(
                            transport="feishu_message",
                            provider_message_id=fallback_message_id,
                            conversation_ref=_reply_to,
                            artifact_role="fallback",
                        )
                        await append_delivery_part(assistant_message_id, fallback_part)
                        delivery_result = IMDeliveryResult.sent(
                            "feishu",
                            fallback_part,
                        )
                except DeliveryReceiptPersistenceError:
                    raise
                except Exception as _e_fb:
                    logger.error(f"[Feishu] Failed to send image reply: {_e_fb}")

            if assistant_message_id is not None:
                await register_delivery(assistant_message_id, delivery_result)

            # ── Log ──
            from app.services.activity_logger import log_activity
            await log_activity(agent_id, "chat_reply", f"回复了飞书图片消息: {reply_text[:80]}",
                               detail={"channel": "feishu", "type": "image"})
            return reply_text

        await run_channel_message(lock_key, is_command=False, reactions=ChannelReactions(), work=_image_work)
        return

    # For non-image files: send simple ack and persist
    # Set up session (needed for persist_assistant_reply)
    # 群聊文件 ack 也需要获取群名称，避免会话标题退化为 "[文件] filename"
    _ack_group_name = None
    if _is_group_file and chat_id:
        try:
            _ack_chat_info = await feishu_service.get_chat_info(config.app_id, config.app_secret, chat_id)
            _ack_real_name = (_ack_chat_info or {}).get("name") if _ack_chat_info else None
            _ack_group_name = (
                _ack_real_name.strip() if _ack_real_name and _ack_real_name.strip()
                else f"Feishu Group {chat_id[:12]}"
            )
        except Exception as _ack_gci_err:
            logger.warning(f"[Feishu] chat-info lookup failed (file ack path): {_ack_gci_err}")
            _ack_group_name = f"Feishu Group {chat_id[:12]}"

    async with _async_session() as _db_ack:
        _ag_r_ack = await _db_ack.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        _ag_obj_ack = _ag_r_ack.scalar_one_or_none()
        _file_user_id_ack = _ag_obj_ack.creator_id if (_is_group_file and _ag_obj_ack) else platform_user_id
        _sess_ack = await find_or_create_channel_session(
            db=_db_ack, agent_id=agent_id, user_id=_file_user_id_ack,
            external_conv_id=conv_id, source_channel="feishu",
            first_message_title=f"[文件] {filename}",
            is_group=_is_group_file,
            group_name=_ack_group_name,
        )
        session_conv_id_ack = str(_sess_ack.id)
        from app.services.chat_history import ingest_incoming_chat_message

        _file_ingested = await ingest_incoming_chat_message(
            _db_ack,
            session=_sess_ack,
            agent_id=agent_id,
            user_id=platform_user_id,
            content=f"[file:{filename}]",
            source_channel="feishu",
            provider_event_id=message_id or None,
            channel_config_id=config.id,
            actor_ref=sender_user_id_feishu or sender_open_id,
            message_meta={
                "message_type": "file",
                "workspace_path": workspace_path,
                "attachments": [attachment_from_workspace_path(workspace_path)],
                "display_content": "",
                "actor_ref_type": "user_id" if sender_user_id_feishu else "open_id",
            },
        )
        _sess_ack.last_message_at = _dt.now(_tz.utc)
        await _db_ack.commit()

    # Mirror this inbound file message to anyone viewing the session on web in
    # real time (matches what a reload renders: the [file:...] row).
    from app.services.channel_llm import broadcast_channel_user_message
    await broadcast_channel_user_message(
        agent_id, session_conv_id_ack, message=_file_ingested.message,
        sender_name=sender_name_file or None, user_id=platform_user_id,
    )

    if _file_ingested.consumed_by_onmessage:
        logger.info("[Feishu] File event %s routed to on_message", message_id)
        return

    await asyncio.sleep(random.uniform(1.0, 2.0))

    ack = random.choice(_FILE_ACK_MESSAGES)
    from app.services.im_delivery import persist_and_deliver_runtime_message
    from app.services.turn_runtime import TurnRuntime

    _ack_id, _ack_result = await persist_and_deliver_runtime_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        runtime=TurnRuntime(
            session_found=True,
            source_channel="feishu",
            conversation_id=session_conv_id_ack,
            external_conv_id=conv_id,
            is_group=_is_group_file,
        ),
        message=ack,
        turn_anchor_id=_file_ingested.message.id,
        artifact_role="file_ack",
        origin_actor_ref=sender_open_id if not _is_group_file else None,
        origin_actor_ref_type="open_id" if not _is_group_file else None,
    )
    if not _ack_result.ok:
        logger.error(f"[Feishu] Failed to send ack: {_ack_result.error}")



async def _download_post_images(agent_id, config, message_id, image_keys):
    """Download images embedded in a Feishu post message to the agent's workspace."""
    for ik in image_keys:
        try:
            file_bytes = await feishu_service.download_message_resource(
                config.app_id, config.app_secret, message_id, ik, "image"
            )
            _, workspace_path, _ = await store_agent_upload(
                agent_id,
                f"image_{ik[-8:]}.jpg",
                file_bytes,
                content_type="image/jpeg",
            )
            logger.info(f"[Feishu] Saved post image to {workspace_path} ({len(file_bytes)} bytes)")
        except Exception as e:
                logger.error(f"[Feishu] Failed to download post image {ik}: {e}")

__all__ = [name for name in globals() if not name.startswith("__")]
