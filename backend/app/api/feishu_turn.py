"""Feishu text-turn execution inside the serialized channel lock."""

from app.api.feishu_shared import *  # noqa: F401,F403


async def _process_feishu_text_turn(
    *,
    db,
    agent_id,
    agent_obj,
    chat_id,
    chat_type,
    config,
    ctx_size,
    event_id,
    message,
    normalized_attachments,
    platform_user,
    platform_user_id,
    reactions,
    sender_name,
    sender_open_id,
    sender_user_id_feishu,
    session_conv_id,
    task_match,
    user_text,
    _dt,
    _sess,
    _tz,
) -> str:
    # ── User-row write (full turn starts here, inside the session lock) ──
    from app.services.chat_history import ingest_incoming_chat_message

    ingested = await ingest_incoming_chat_message(
        db,
        session=_sess,
        agent_id=agent_id,
        user_id=platform_user_id,
        content=user_text,
        source_channel="feishu",
        provider_event_id=event_id or message.get("message_id") or None,
        channel_config_id=config.id,
        actor_ref=sender_user_id_feishu or sender_open_id,
        message_meta={
            "actor_ref_type": "user_id" if sender_user_id_feishu else "open_id",
            "attachments": normalized_attachments,
        },
    )
    _sess.last_message_at = _dt.now(_tz.utc)
    await db.commit()

    # Mirror this inbound message to anyone viewing the session on web
    # in real time — the agent reply already streams there; this makes
    # the user's own message show up live too, not only on reload.
    from app.services.channel_llm import broadcast_channel_user_message
    await broadcast_channel_user_message(
        agent_id, session_conv_id, message=ingested.message,
        sender_name=sender_name or None, user_id=platform_user_id,
    )

    if ingested.consumed_by_onmessage:
        logger.info(
            "[Feishu] Inbound event %s routed to %d on_message execution(s)",
            event_id,
            len(ingested.execution_ids),
        )
        return ""

    # Load history inside the lock so concurrent turns cannot observe
    # each other's not-yet-committed rows (race condition fix).
    from app.services.chat_history import load_history_for_llm
    history = await load_history_for_llm(
        db,
        agent_id=agent_id,
        conversation_id=session_conv_id,
        ctx_size=ctx_size,
        is_group=(chat_type == "group"),
    )

    # Build the message we'll send to the LLM. Group chats get a
    # platform-injected <sender> prefix (see spec §4.0/§4.1); P2P keeps
    # the legacy `[发送者: ...]` plain prefix (its history is single-
    # speaker so message-level tagging would be redundant).
    from app.services.sender_attribution import wrap_with_sender

    llm_user_text = user_text
    if chat_type == "group":
        llm_user_text = wrap_with_sender(
            user_text,
            platform_user_id,
            sender_name or platform_user.display_name,
        )
    elif sender_name:
        llm_user_text = f"[发送者: {sender_name}] {user_text}"

    # ── Inject recent uploaded file context ──────────────────────────
    # Use only a durable file from this exact session and sender.
    # Agent-wide directory scans can cross-wire files between users.
    try:
        from datetime import timedelta as _td

        from app.models.audit import ChatMessage as _ChatMessage

        _storage = get_storage_backend()
        _recent_file_path = None
        if "uploads/" not in user_text and "workspace/" not in user_text:
            _recent_rows = await db.execute(
                select(_ChatMessage.message_meta)
                .where(
                    _ChatMessage.agent_id == agent_id,
                    _ChatMessage.conversation_id == session_conv_id,
                    _ChatMessage.user_id == platform_user_id,
                    _ChatMessage.role == "user",
                    _ChatMessage.created_at >= _dt.now(_tz.utc) - _td(minutes=30),
                )
                .order_by(_ChatMessage.created_at.desc())
                .limit(20)
            )
            for _meta in _recent_rows.scalars():
                for _attachment in normalize_attachment_metadata(
                    _meta.get("attachments") if isinstance(_meta, dict) else None
                ):
                    if _attachment.get("kind") != "file":
                        continue
                    _candidate_path = _attachment["path"]
                    _candidate_key = agent_storage_key(agent_id, _candidate_path)
                    if (
                        await _storage.exists(_candidate_key)
                        and await _storage.is_file(_candidate_key)
                    ):
                        _recent_file_path = _candidate_path
                        break
                if _recent_file_path:
                    break
        if _recent_file_path:
            llm_user_text = (
                llm_user_text
                + f"\n\n[系统提示：用户刚上传了文件，路径为工作区 `{_recent_file_path}`。"
                f"如果用户的指令涉及这篇文章、这个文件、这份文档等，"
                f"请立即调用 read_document(path=\"{_recent_file_path}\") 读取内容，不要先用 list_files 验证，直接读取即可。]"
            )
            logger.info(f"[Feishu] Injected recent file hint: {_recent_file_path}")
    except Exception as _fe:
        logger.error(f"[Feishu] File injection error: {_fe}")

    # Set sender open_id contextvar so calendar tool can auto-invite the requester
    from app.services.agent_tools import channel_feishu_sender_open_id as _cfso
    _cfso_token = _cfso.set(sender_open_id)

    # Set channel_file_sender contextvar so the agent can send files back via Feishu
    from app.services.agent_tools import channel_file_sender as _cfs
    _reply_to_id = chat_id if chat_type == "group" else sender_open_id
    _rid_type = "chat_id" if chat_type == "group" else "open_id"

    async def _feishu_file_sender(file_path, msg: str = ""):
        from app.services.im_delivery import (
            DeliveryReceiptPersistenceError,
            IMDeliveryPart,
            IMDeliveryResult,
        )
        from app.services.agent_tools import record_channel_file_part

        _delivery_parts = []

        async def _record_file_result(artifact_role: str, provider_result: dict):
            _provider_id = str(
                (provider_result.get("data") or {}).get("message_id")
                or provider_result.get("message_id")
                or ""
            ) or None
            part = IMDeliveryPart(
                transport="feishu_message",
                provider_message_id=_provider_id,
                conversation_ref=_reply_to_id,
                artifact_role=artifact_role,
                recallable=bool(_provider_id),
            )
            _delivery_parts.append(part)
            await record_channel_file_part(part)
        try:
            await feishu_service.upload_and_send_file(
                config.app_id, config.app_secret,
                _reply_to_id, file_path,
                receive_id_type=_rid_type,
                accompany_msg=msg,
                on_result=_record_file_result,
            )
            if not _delivery_parts:
                part = IMDeliveryPart(
                    transport="feishu_message",
                    conversation_ref=_reply_to_id,
                    artifact_role="channel_file",
                    recallable=False,
                )
                _delivery_parts.append(part)
                await record_channel_file_part(part)
            return IMDeliveryResult.sent("feishu", *_delivery_parts)
        except DeliveryReceiptPersistenceError:
            raise
        except Exception as _upload_err:
            # Fallback: send a download link when upload permission is not granted
            from pathlib import Path as _P
            from app.config import get_settings as _gs_fallback
            from app.services.user_output import sanitize_user_visible_text
            _fs = _gs_fallback()
            _safe_upload_error = sanitize_user_visible_text(
                str(_upload_err)
            ).strip()[:200] or type(_upload_err).__name__
            _base_url = getattr(_fs, 'BASE_URL', '').rstrip('/') or ''
            _fp = _P(file_path)
            # Resolve a workspace-relative path for the download link.
            # Prefer the "workspace/" anchor (robust across storage
            # backends); fall back to STORAGE_LOCAL_ROOT / AGENT_DATA_DIR.
            _parts = list(_fp.parts)
            try:
                _workspace_idx = _parts.index("workspace")
                _rel = "/".join(_parts[_workspace_idx:])
            except ValueError:
                _ws_root = _P(getattr(_fs, "STORAGE_LOCAL_ROOT", "") or _fs.AGENT_DATA_DIR)
                try:
                    _rel = str(_fp.relative_to(_ws_root / str(agent_id)))
                except ValueError:
                    _rel = _fp.name
            _fallback_parts = []
            if msg:
                _fallback_parts.append(msg)
            if _base_url:
                _dl_url = f"{_base_url}/api/agents/{agent_id}/files/download?path={_rel}"
                _fallback_parts.append(f"📎 {_fp.name}\n🔗 {_dl_url}")
            _fallback_parts.append(
                f"⚠️ 文件直接发送失败（{_safe_upload_error}）\n"
                "如需 Agent 直接发飞书文件，请在飞书开放平台为应用开启 "
                "`im:resource`（即 `im:resource:upload`）权限并发布版本。"
            )
            _fallback_result = await feishu_service.send_message(
                config.app_id, config.app_secret,
                _reply_to_id, "text",
                _json.dumps({"text": "\n\n".join(_fallback_parts)}),
                receive_id_type=_rid_type,
            )
            _provider_id = str(
                (_fallback_result.get("data") or {}).get("message_id")
                or _fallback_result.get("message_id")
                or ""
            ) or None
            part = IMDeliveryPart(
                transport="feishu_message",
                provider_message_id=_provider_id,
                conversation_ref=_reply_to_id,
                artifact_role="file_fallback",
                recallable=bool(_provider_id),
            )
            await record_channel_file_part(part)
            return IMDeliveryResult.sent("feishu", part)

    _cfs_token = _cfs.set(_feishu_file_sender)

    _reply_target = chat_id if chat_type == "group" and chat_id else sender_open_id
    _reply_rid_type = "chat_id" if chat_type == "group" and chat_id else "open_id"
    # Quote the user's original message in groups so the agent's reply
    # threads under it in the Feishu client (Phase 2 #3). Outside group
    # chats we don't need quoting — P2P already has a single thread.
    _reply_to_user_msg_id = (
        message.get("message_id") or "" if chat_type == "group" else ""
    )

    # ── Streaming card state (intra-turn, orthogonal to the per-session lock) ──
    _stream_buffer: list[str] = []
    _thinking_buffer: list[str] = []
    _thinking_output_enabled = resolve_im_thinking_enabled(agent_obj, _sess)
    _agent_name = agent_obj.name if agent_obj else "AI 回复"
    _tool_errors: list[str] = []
    _tool_status_running: dict[str, str] = {}
    _tool_status_done: list[str] = []
    _patch_queue = _SerialPatchQueue()
    _heartbeat_task: asyncio.Task | None = None
    _llm_done = False
    _last_flushed_hash: int = 0
    _last_flush_time = 0.0
    _flush_interval = 1.0
    _patch_msg_id: str | None = None
    _flush_lock = asyncio.Lock()

    from app.services.im_delivery import persist_delivery_anchor

    assistant_message_id = await persist_delivery_anchor(
        agent_id=agent_id,
        user_id=platform_user_id,
        conversation_id=session_conv_id,
        channel="feishu",
        message="正在处理…",
        turn_anchor_id=ingested.message.id,
        artifact_role="streaming_card",
    )

    def _visible_tool_status_lines() -> list[str]:
        done_visible = _tool_status_done[-_TOOL_STATUS_KEEP_LINES:]
        running_visible = list(_tool_status_running.values())
        return done_visible + running_visible

    async def _queue_patch_card(card: dict, stage: str) -> None:
        if not _patch_msg_id:
            return
        payload = _json.dumps(card)

        async def _job():
            try:
                await feishu_service.patch_message(
                    config.app_id,
                    config.app_secret,
                    _patch_msg_id,
                    payload,
                    stage=stage,
                )
            except Exception as e:
                logger.warning(f"[Feishu] Patch failed (stage={stage}, message_id={_patch_msg_id}): {e}")

        _patch_queue.enqueue(_job)

    _init_card = _build_card(
        answer_text="",
        streaming=True,
        agent_name=_agent_name,
    )
    from app.services.im_delivery import (
        DeliveryReceiptPersistenceError,
        IMDeliveryPart,
        append_delivery_part,
    )

    try:
        _init_resp = await feishu_service.send_message(
            config.app_id,
            config.app_secret,
            _reply_target,
            "interactive",
            _json.dumps(_init_card),
            receive_id_type=_reply_rid_type,
            stage="stream_init_card",
            reply_to_message_id=_reply_to_user_msg_id or None,
        )
        _patch_msg_id = _init_resp.get("data", {}).get("message_id")
        if _patch_msg_id:
            await append_delivery_part(
                assistant_message_id,
                IMDeliveryPart(
                    transport="feishu_message",
                    provider_message_id=_patch_msg_id,
                    conversation_ref=_reply_target,
                    artifact_role="card",
                ),
            )
    except DeliveryReceiptPersistenceError:
        raise
    except Exception as e:
        logger.error(f"[Feishu] Failed to send init streaming card: {e}")

    async def _flush_stream(reason: str, force: bool = False):
        nonlocal _last_flushed_hash, _last_flush_time
        if not _patch_msg_id:
            return
        async with _flush_lock:
            now = time.time()
            if not force and now - _last_flush_time < _flush_interval:
                return
            accumulated = "".join(_stream_buffer)
            thinking_text = "".join(_thinking_buffer) if _thinking_output_enabled else ""
            tool_status_lines = _visible_tool_status_lines()
            current_hash = hash(accumulated + thinking_text + "\n".join(tool_status_lines))
            if reason == "heartbeat" and current_hash == _last_flushed_hash:
                return
            _last_flushed_hash = current_hash
            card = _build_card(
                answer_text=accumulated,
                thinking_text=thinking_text,
                streaming=True,
                tool_status_lines=tool_status_lines,
                agent_name=_agent_name,
            )
            await _queue_patch_card(card, stage=f"stream_{reason}")
            _last_flush_time = now

    # ── Loop-internal streaming callbacks ──────────────────────────────
    # These are the ChannelReactions loop hooks threaded into _call_agent_llm.
    # Defined here (inside _work) so they close over the per-turn state above.
    async def _ws_on_chunk(text: str):
        _stream_buffer.append(text)
        if _patch_msg_id:
            try:
                await _flush_stream("chunk")
            except Exception as _e:
                logger.warning(f"[Feishu] chunk flush failed (ignored): {_e}")

    async def _ws_on_thinking(text: str):
        _thinking_buffer.append(text)
        if _patch_msg_id:
            try:
                await _flush_stream("thinking")
            except Exception as _e:
                logger.warning(f"[Feishu] thinking flush failed (ignored): {_e}")

    async def _ws_on_tool_call(evt: dict):
        tool_name = evt.get("name") or "unknown_tool"
        call_id = evt.get("call_id") or tool_name
        status = (evt.get("status") or "").lower()
        result = evt.get("result")
        if status == "running":
            _tool_status_running[call_id] = f"⏳ Tool running: `{tool_name}`"
        elif status == "done":
            _tool_status_running.pop(call_id, None)
            normalized_error = _normalize_tool_error(tool_name, result)
            if normalized_error:
                _tool_errors.append(normalized_error)
                _tool_status_done.append(f"❌ Tool failed: `{tool_name}`")
            else:
                _tool_status_done.append(f"✅ Tool done: `{tool_name}`")
        elif status and status not in {"running", "done"}:
            _tool_status_running.pop(call_id, None)
            _tool_errors.append(f"`{tool_name}`: tool status `{status}`")
            _tool_status_done.append(f"ℹ️ Tool update: `{tool_name}` ({status})")

        # Persistence is centralized in _call_agent_llm via the shared
        # persist_tool_call (one canonical schema for every channel).
        # This callback only drives live IM progress hints — no DB write.
        if _patch_msg_id:
            try:
                await _flush_stream("tool", force=True)
            except Exception as _flush_err:
                logger.warning(f"[Feishu] tool-status flush failed (ignored): {_flush_err}")

    # Register callbacks into the ChannelReactions bundle (single source of truth)
    reactions.on_chunk = _ws_on_chunk
    reactions.on_thinking = _ws_on_thinking
    reactions.on_tool_call = _ws_on_tool_call

    async def _heartbeat():
        while not _llm_done:
            await asyncio.sleep(_flush_interval)
            if _patch_msg_id:
                await _flush_stream("heartbeat")

    if _patch_msg_id:
        _heartbeat_task = asyncio.create_task(_heartbeat())

    # Call LLM — pass loop hooks via the reactions bundle (single source of truth)
    try:
        reply_text = await _call_agent_llm(
            db,
            agent_id,
            llm_user_text,
            history=history,
            user_id=platform_user_id,
            session_id=session_conv_id,
            on_chunk=reactions.on_chunk,
            on_thinking=reactions.on_thinking,
            on_tool_call=reactions.on_tool_call,
            is_group=(chat_type == "group"),
            turn_anchor_id=ingested.message.id,
        )
    finally:
        _llm_done = True
        if _heartbeat_task:
            _heartbeat_task.cancel()
            try:
                await _heartbeat_task
            except (Exception, asyncio.CancelledError):
                pass
        _cfs.reset(_cfs_token)
        _cfso.reset(_cfso_token)
    logger.info(f"[Feishu] LLM reply: {reply_text[:100]}")

    # If task creation detected, create a real Task record
    if task_match:
        task_title = task_match.group(1).strip()
        if task_title:
            try:
                from app.models.task import Task as TaskModel
                from app.services.task_executor import execute_task
                import asyncio as _asyncio

                task_obj = TaskModel(
                    agent_id=agent_id,
                    title=task_title,
                    created_by=platform_user_id,
                    execution_user_id=platform_user_id,
                    status="pending",
                    priority="medium",
                )
                db.add(task_obj)
                await db.commit()
                await db.refresh(task_obj)
                _asyncio.create_task(
                    execute_task(task_obj.id, agent_id, task_obj.execution_user_id)
                )
                reply_text += f"\n\n📋 已同步创建任务到任务面板：【{task_title}】"
                logger.info(f"[Feishu] Created task: {task_title}")
            except Exception as e:
                logger.error(f"[Feishu] Failed to create task: {e}")
                reply_text += f"\n\n⚠️ 任务已识别，但写入任务面板失败：{str(e)[:150]}"

    final_reply_text = _append_error_details(reply_text, _tool_errors)
    final_card = _build_card(
        answer_text=final_reply_text or "...",
        thinking_text="",
        streaming=False,
        tool_status_lines=_visible_tool_status_lines(),
        agent_name=_agent_name,
    )

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
        content=final_reply_text or "…",
        thinking="".join(_thinking_buffer) or None,
        complete_turn=True,
    ):
        raise RuntimeError("feishu_stream_anchor_missing")
    delivery_parts: list[IMDeliveryPart] = []
    if _patch_msg_id:
        card_part = IMDeliveryPart(
            transport="feishu_message",
            provider_message_id=_patch_msg_id,
            conversation_ref=_reply_target,
            artifact_role="card",
        )
        delivery_parts.append(card_part)

    if _patch_msg_id:
        try:
            await _patch_queue.drain()
        except Exception as e:
            logger.warning(f"[Feishu] Drain patch queue failed before final patch: {e}")
        try:
            await feishu_service.patch_message(
                config.app_id,
                config.app_secret,
                _patch_msg_id,
                _json.dumps(final_card),
                stage="stream_final",
            )
        except Exception as e:
            logger.error(f"[Feishu] Failed to patch final interactive reply: {e}")
            try:
                fallback_response = await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    _reply_target,
                    "text",
                    _json.dumps({"text": final_reply_text}),
                    receive_id_type=_reply_rid_type,
                    stage="final_after_task_fallback_text",
                )
                fallback_message_id = str(
                    ((fallback_response.get("data") or {}).get("message_id")) or ""
                )
                if fallback_message_id:
                    fallback_part = IMDeliveryPart(
                        transport="feishu_message",
                        provider_message_id=fallback_message_id,
                        conversation_ref=_reply_target,
                        artifact_role="fallback",
                    )
                    delivery_parts.append(fallback_part)
                    await append_delivery_part(assistant_message_id, fallback_part)
            except DeliveryReceiptPersistenceError:
                raise
            except Exception as e2:
                logger.error(f"[Feishu] Failed to send fallback text reply: {e2}")
    else:
        try:
            final_response = await feishu_service.send_message(
                config.app_id,
                config.app_secret,
                _reply_target,
                "interactive",
                _json.dumps(final_card),
                receive_id_type=_reply_rid_type,
                stage="final_after_task",
            )
            final_message_id = str(
                ((final_response.get("data") or {}).get("message_id")) or ""
            )
            if final_message_id:
                final_part = IMDeliveryPart(
                    transport="feishu_message",
                    provider_message_id=final_message_id,
                    conversation_ref=_reply_target,
                    artifact_role="card",
                )
                delivery_parts.append(final_part)
                await append_delivery_part(assistant_message_id, final_part)
        except DeliveryReceiptPersistenceError:
            raise
        except Exception as e:
            logger.error(f"[Feishu] Failed to send final interactive reply: {e}")
            try:
                fallback_response = await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    _reply_target,
                    "text",
                    _json.dumps({"text": final_reply_text}),
                    receive_id_type=_reply_rid_type,
                    stage="final_after_task_fallback_text",
                )
                fallback_message_id = str(
                    ((fallback_response.get("data") or {}).get("message_id")) or ""
                )
                if fallback_message_id:
                    fallback_part = IMDeliveryPart(
                        transport="feishu_message",
                        provider_message_id=fallback_message_id,
                        conversation_ref=_reply_target,
                        artifact_role="fallback",
                    )
                    delivery_parts.append(fallback_part)
                    await append_delivery_part(assistant_message_id, fallback_part)
            except DeliveryReceiptPersistenceError:
                raise
            except Exception as e2:
                logger.error(f"[Feishu] Failed to send fallback text reply: {e2}")

    if assistant_message_id is not None:
        delivery_result = (
            IMDeliveryResult.sent("feishu", *delivery_parts)
            if delivery_parts
            else IMDeliveryResult.failed("feishu", "send_failed")
        )
        await register_delivery(assistant_message_id, delivery_result)

    # Log activity
    from app.services.activity_logger import log_activity
    await log_activity(agent_id, "chat_reply", f"回复了飞书消息: {final_reply_text[:80]}", detail={"channel": "feishu", "user_text": user_text[:200], "reply": final_reply_text[:500]})

    _sess.last_message_at = _dt.now(_tz.utc)
    await db.commit()

    return final_reply_text


__all__ = [name for name in globals() if not name.startswith("__")]

