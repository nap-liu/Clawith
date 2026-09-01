"""Feishu event decoding and message dispatch."""

from app.api.feishu_routes import *  # noqa: F401,F403
from app.api.feishu_files import *  # noqa: F401,F403
from app.api.feishu_turn import *  # noqa: F401,F403

async def process_feishu_event(agent_id: uuid.UUID, body: dict, db: AsyncSession):
    """Core logic to process feishu events from both webhook and WS client."""
    import json as _json
    logger.info(f"[Feishu] Event processing for {agent_id}: event_type={body.get('header', {}).get('event_type', 'N/A')}")

    event_id = body.get("header", {}).get("event_id", "")

    # Get channel config — filter by feishu since an agent can have multiple channels
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "feishu",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return {"code": 1, "msg": "Channel not found"}

    # Handle events
    event = body.get("event", {})
    event_type = body.get("header", {}).get("event_type", "")

    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        sender = event.get("sender", {}).get("sender_id", {})
        sender_open_id = sender.get("open_id", "")
        sender_user_id_from_event = sender.get("user_id", "")  # tenant-stable ID, available directly in event body
        msg_type = message.get("message_type", "text")
        chat_type = message.get("chat_type", "p2p")  # p2p or group
        chat_id = message.get("chat_id", "")
        normalized_attachments = []

        logger.info(f"[Feishu] Received {msg_type} message, chat_type={chat_type}, open_id={sender_open_id!r}, user_id_from_event={sender_user_id_from_event!r}")

        # ── Normalize post (rich text) → extract text + schedule image downloads ──
        if msg_type == "post":
            import json as _json_post
            _post_body = _json_post.loads(message.get("content", "{}"))
            # Feishu post content: {"title": "...", "content": [[{"tag":"text","text":"..."},...],...]}
            # The content may be nested under a locale key like "zh_cn"
            _paragraphs = _post_body.get("content", [])
            if not _paragraphs:
                # Try locale keys (zh_cn, en_us, etc.)
                for _locale_key, _locale_val in _post_body.items():
                    if isinstance(_locale_val, dict) and "content" in _locale_val:
                        _paragraphs = _locale_val["content"]
                        break
            _text_parts = []
            _post_image_keys = []
            for _para in _paragraphs:
                _line_parts = []
                for _elem in _para:
                    _tag = _elem.get("tag")
                    if _tag == "text":
                        _line_parts.append(_elem.get("text", ""))
                    elif _tag == "a":
                        _href = _elem.get("href", "")
                        _link_text = _elem.get("text", "")
                        _line_parts.append(f"{_link_text} ({_href})" if _href else _link_text)
                    elif _tag == "img":
                        _ik = _elem.get("image_key", "")
                        if _ik:
                            _post_image_keys.append(_ik)
                if _line_parts:
                    _text_parts.append("".join(_line_parts))
            _extracted_text = "\n".join(_text_parts).strip()
            # Download images into the agent workspace. The shared LLM caller
            # decides whether the actual model attempt receives image blocks.
            if _post_image_keys:
                _msg_id = message.get("message_id", "")
                for _ik in _post_image_keys:
                    try:
                        _img_bytes = await feishu_service.download_message_resource(
                            config.app_id, config.app_secret, _msg_id, _ik, "image"
                        )
                        _, _workspace_path, _save_path = await store_agent_upload(
                            agent_id,
                            f"image_{_ik[-8:]}.jpg",
                            _img_bytes,
                            content_type="image/jpeg",
                        )
                        logger.info(f"[Feishu] Saved post image to {_workspace_path} ({len(_img_bytes)} bytes)")
                        normalized_attachments.append(
                            attachment_from_workspace_path(
                                _workspace_path,
                                mime_type="image/jpeg",
                                size_bytes=len(_img_bytes),
                            )
                        )
                    except Exception as _dl_err:
                        logger.error(f"[Feishu] Failed to download post image {_ik}: {_dl_err}")
            if not _extracted_text and normalized_attachments:
                _extracted_text = "[用户发送了图片，请看图片内容]"
            # Rewrite as text message so existing handler processes it
            message["content"] = _json_post.dumps({"text": _extracted_text})
            msg_type = "text"
            logger.info(
                f"[Feishu] Normalized post → text='{_extracted_text[:100]}', "
                f"images={len(normalized_attachments)}"
            )

        if msg_type in ("file", "image"):
            # Do not acknowledge the provider before the durable ingest/match
            # transaction has completed. Provider retries are deduplicated by
            # ChatMessage.external_event_key, not by process-local memory.
            await _handle_feishu_file(
                db,
                agent_id,
                config,
                message,
                sender_open_id,
                sender_user_id_from_event,
                chat_type,
                chat_id,
            )
            return {"code": 0, "msg": "ok"}

        if msg_type == "text":
            import json
            import re
            content = json.loads(message.get("content", "{}"))
            user_text = content.get("text", "")

            # Strip @mention tags (e.g. @_user_1) from group messages
            user_text = re.sub(r'@_user_\d+', '', user_text).strip()

            if not user_text:
                return {"code": 0, "msg": "empty message after stripping mentions"}

            # Detect task creation intent
            task_match = re.search(
                r'(?:创建|新建|添加|建一个|帮我建)(?:一个)?(?:任务|待办|todo)[，,：:\s]*(.+)',
                user_text, re.IGNORECASE
            )

            # Determine conversation_id for history isolation
            # Group chats: use chat_id; P2P chats: prefer user_id (tenant-stable)
            if chat_type == "group" and chat_id:
                conv_id = f"feishu_group_{chat_id}"
            else:
                conv_id = f"feishu_p2p_{sender_user_id_from_event or sender_open_id}"

            # Early-return for channel commands (/new, /reset):
            # Must run BEFORE find_or_create_channel_session to avoid creating a
            # ghost session that is immediately archived.
            if is_channel_command(user_text):
                command_event_id = event_id or message.get("message_id") or None
                from app.database import async_session as _async_session
                async with _async_session() as _cmd_db:
                    from app.services.channel_commands import prepare_channel_command_reply
                    cmd_result = await prepare_channel_command_reply(
                        db=_cmd_db, command=user_text, agent_id=agent_id,
                        user_id=None, external_conv_id=conv_id,
                        external_user_id=sender_user_id_from_event or sender_open_id,
                        source_channel="feishu",
                        provider_event_id=command_event_id,
                        is_group=chat_type == "group",
                        external_user_info={"open_id": sender_open_id},
                    )
                    await _cmd_db.commit()
                if not cmd_result["should_deliver"]:
                    return {"code": 0, "msg": "duplicate"}
                _cmd_reply_to = chat_id if chat_type == "group" and chat_id else sender_open_id
                _cmd_rid_type = "chat_id" if chat_type == "group" and chat_id else "open_id"
                from app.services.im_delivery import (
                    DeliveryReceiptPersistenceError,
                    IMDeliveryPart,
                    IMDeliveryResult,
                    register_delivery,
                )
                try:
                    _cmd_send_result = await feishu_service.send_message(
                        config.app_id, config.app_secret,
                        _cmd_reply_to, "text",
                        json.dumps({"text": cmd_result["message"]}),
                        receive_id_type=_cmd_rid_type,
                    )
                    _cmd_provider_id = str(
                        (_cmd_send_result.get("data") or {}).get("message_id")
                        or _cmd_send_result.get("message_id")
                        or ""
                    ) or None
                    await register_delivery(
                        cmd_result["message_id"],
                        IMDeliveryResult.sent(
                            "feishu",
                            IMDeliveryPart(
                                transport="feishu_message",
                                provider_message_id=_cmd_provider_id,
                                conversation_ref=_cmd_reply_to,
                                artifact_role="command_reply",
                                recallable=bool(_cmd_provider_id),
                            ),
                        ),
                    )
                except DeliveryReceiptPersistenceError:
                    raise
                except Exception as _cmd_e:
                    await register_delivery(
                        cmd_result["message_id"],
                        IMDeliveryResult.from_exception("feishu", _cmd_e),
                    )
                    logger.error(f"[Feishu] Failed to send command reply: {_cmd_e}")
                return {"code": 0, "msg": "ok"}

            # Load recent conversation history via session (session UUID may already exist)
            from app.models.agent import Agent as AgentModel
            from app.services.channel_session import find_or_create_channel_session
            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            creator_id = agent_obj.creator_id if agent_obj else agent_id
            from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
            ctx_size = (agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE) if agent_obj else DEFAULT_CONTEXT_WINDOW_SIZE

            # --- Resolve Feishu sender identity & find/create platform user ---
            import uuid as _uuid
            import httpx as _httpx

            sender_name = ""
            sender_user_id_feishu = sender_user_id_from_event  # tenant-level user_id, pre-filled from event body
            extra_info: dict | None = {
                "open_id": sender_open_id,
                "external_id": sender_user_id_feishu or None,
            }

            try:
                async with _httpx.AsyncClient() as _client:
                    _tok_resp = await _client.post(
                        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                        json={"app_id": config.app_id, "app_secret": config.app_secret},
                    )
                    _app_token = _tok_resp.json().get("app_access_token", "")
                    if _app_token:
                        _user_resp = await _client.get(
                            f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_open_id}",
                            params={"user_id_type": "open_id"},
                            headers={"Authorization": f"Bearer {_app_token}"},
                        )
                        _user_data = _user_resp.json()
                        logger.info(f"[Feishu] Sender resolve: code={_user_data.get('code')}, msg={_user_data.get('msg', '')}")
                        if _user_data.get("code") == 0:
                            _user_info = _user_data.get("data", {}).get("user", {})
                            sender_name = _user_info.get("name", "")
                            sender_user_id_feishu = _user_info.get("user_id", "")
                            sender_email = _user_info.get("email", "") or _user_info.get("enterprise_email", "")
                            # Feishu contact API returns 'avatar' as a dict
                            # (keys: avatar_240, avatar_640, avatar_origin), NOT a plain URL.
                            # We must extract a string to avoid a DataError when writing to the DB.
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
                                "name": sender_name,
                                "email": sender_email,
                                "mobile": _user_info.get("mobile"),
                                "avatar_url": _avatar_url,
                                "external_id": _user_info.get("user_id"),
                                "unionid": _user_info.get("union_id"),
                                "open_id": sender_open_id,
                            }
                            logger.info(f"[Feishu] Resolved sender: {sender_name} (user_id={sender_user_id_feishu})")
            except Exception as e:
                logger.error(f"[Feishu] Failed to resolve sender: {e}")

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
                    logger.warning(f"[Feishu] Sender resolution refused: {e}")
                    await _persist_feishu_control_reply(
                        agent_id=agent_id,
                        config=config,
                        sender_open_id=sender_open_id,
                        chat_type=chat_type,
                        chat_id=chat_id,
                        message=_USER_RESOLUTION_ERROR_TIP,
                        artifact_role="identity_error_ack",
                    )
                    return {"code": 0, "msg": "user_resolution_skipped"}
                raise
            platform_user_id = platform_user.id

            # ── Find-or-create a ChatSession via external_conv_id (DB-based, no cache needed) ──
            from datetime import datetime as _dt, timezone as _tz
            _is_group = (chat_type == "group")

            # For group chats, fetch the real chat title from Feishu so the
            # session shows e.g. "产品讨论组" instead of "Feishu Group ou_xxx".
            # API failure falls back to the conversation_id-based placeholder
            # so a chat-info hiccup never blocks message processing.
            _fs_group_name = None
            if _is_group:
                try:
                    _chat_info = await feishu_service.get_chat_info(
                        config.app_id, config.app_secret, chat_id,
                    )
                    _real_name = (_chat_info or {}).get("name") if _chat_info else None
                    _fs_group_name = (
                        _real_name.strip() if _real_name and _real_name.strip()
                        else f"Feishu Group {chat_id[:12]}"
                    )
                except Exception as _gci_err:
                    logger.warning(f"[Feishu] chat-info lookup failed: {_gci_err}")
                    _fs_group_name = f"Feishu Group {chat_id[:12]}"

            _sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user_id if not _is_group else creator_id,
                external_conv_id=conv_id,
                source_channel="feishu",
                first_message_title=user_text,
                is_group=_is_group,
                group_name=_fs_group_name,
            )
            session_conv_id = str(_sess.id)

            # Match the DB session identity, including this agent/bot.
            lock_key = channel_session_lock_key(agent_id, "feishu", conv_id)

            # Feishu has no emoji "thinking" reaction (unlike DingTalk), so the
            # boundary hooks (on_consume / on_complete / on_error) stay None.
            # The loop-internal hooks are threaded directly through _work below.
            reactions = ChannelReactions()

            async def _work() -> str:
                return await _process_feishu_text_turn(
                    db=db,
                    agent_id=agent_id,
                    agent_obj=agent_obj,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    config=config,
                    ctx_size=ctx_size,
                    event_id=event_id,
                    message=message,
                    normalized_attachments=normalized_attachments,
                    platform_user=platform_user,
                    platform_user_id=platform_user_id,
                    reactions=reactions,
                    sender_name=sender_name,
                    sender_open_id=sender_open_id,
                    sender_user_id_feishu=sender_user_id_feishu,
                    session_conv_id=session_conv_id,
                    task_match=task_match,
                    user_text=user_text,
                    _dt=_dt,
                    _sess=_sess,
                    _tz=_tz,
                )
            await run_channel_message(lock_key, is_command=False, reactions=reactions, work=_work)

    return {"code": 0, "msg": "ok"}

__all__ = [name for name in globals() if not name.startswith("__")]
