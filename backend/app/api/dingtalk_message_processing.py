"""DingTalk inbound message processing implementation."""

import uuid

from loguru import logger

from app.models.channel_config import ChannelConfig
from app.services.group_policy import group_ingress_allowed
from app.services.chat_attachments import attachment_from_workspace_path
from app.services.dingtalk_quoted_message import has_trusted_dingtalk_sender_alias
from app.services.quoted_message import resolve_quoted_message_sender


async def process_dingtalk_message(
    agent_id: uuid.UUID,
    sender_staff_id: str,
    user_text: str,
    conversation_id: str,
    conversation_type: str,
    saved_file_paths: list[str] | None = None,
    sender_nick: str = "",
    message_id: str = "",
    sender_id: str = "",
    chatbot_user_id: str = "",
    conversation_title: str = "",
    channel_reactions=None,
    quoted_message: dict | None = None,
    prepared_sender=None,
):
    """Process an incoming DingTalk bot message through durable OpenAPI routes.

    Args:
        saved_file_paths: List of local file paths where media files were saved.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select as _select
    from app.database import async_session
    from app.models.agent import Agent as AgentModel
    from app.services.channel_session import find_or_create_channel_session
    from app.services.channel_llm import _call_agent_llm
    from app.services.im_thinking_output import BufferedIMThinkingSender, resolve_im_thinking_enabled

    if not await group_ingress_allowed(agent_id, "dingtalk", conversation_id,
                                       is_group=conversation_type == "2", name=conversation_title,
                                       sender_id=sender_id or sender_staff_id, sender_name=sender_nick,
                                       sender_type="sender_id" if sender_id else "staff_id",
                                       sender_info={"staff_id": sender_staff_id}, prepared_sender=prepared_sender):
        return
    async with async_session() as db:
        sender_staff_id = (sender_staff_id or "").strip()
        sender_id = (sender_id or "").strip()
        chatbot_user_id = (chatbot_user_id or "").strip()
        # The Stream adapter historically falls back to the opaque senderId
        # when senderStaffId is absent. Keep that actor routable, but never
        # persist the opaque value as DingTalk's corporate staff ID.
        directory_staff_id = (
            sender_staff_id
            if not sender_id or sender_staff_id != sender_id
            else ""
        )

        # Load agent
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        if not agent_obj:
            logger.warning(f"[DingTalk] Agent {agent_id} not found")
            return
        if not sender_staff_id:
            logger.warning("[DingTalk] Skip message attribution because sender_staff_id is empty")
            return
        from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
        ctx_size = (agent_obj.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE) if agent_obj else DEFAULT_CONTEXT_WINDOW_SIZE

        # Determine conv_id for session isolation
        if conversation_type == "2":
            # Group chat
            conv_id = f"dingtalk_group_{conversation_id}"
        else:
            # P2P / single chat
            conv_id = f"dingtalk_p2p_{sender_staff_id}"

        # Load robot credentials for message delivery only.
        _early_cfg_r = await db.execute(
            _select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
        _early_cfg = _early_cfg_r.scalar_one_or_none()

        from app.services.channel_user_service import channel_user_service
        from app.services.group_policy_sender import current_sender, resolve_ingress_user
        from app.services.dingtalk_sender import resolve_dingtalk_sender
        prepared = current_sender()
        if prepared:
            from app.models.identity import IdentityProvider
            platform_user = await resolve_ingress_user(db, agent_obj, "dingtalk", directory_staff_id or None)
            _dingtalk_provider = await db.get(IdentityProvider, prepared.provider_id)
        else:
            platform_user, _dingtalk_provider, directory_staff_id = await resolve_dingtalk_sender(
                db, agent_obj, _early_cfg, sender_staff_id, sender_id, sender_nick)

        platform_user_id = platform_user.id

        # Learn the opaque DingTalk senderId only from a callback that also
        # carried a real corporate staff id.  The Stream adapter falls back to
        # senderId when senderStaffId is absent, so equal values are not trusted
        # identity evidence.
        trusted_sender_alias = has_trusted_dingtalk_sender_alias(
            directory_staff_id,
            sender_id,
        )
        if _dingtalk_provider is None and (trusted_sender_alias or quoted_message):
            _dingtalk_provider = await _get_tenant_dingtalk_provider(
                db,
                agent_obj.tenant_id,
            )

        if trusted_sender_alias and _dingtalk_provider is not None:
            await channel_user_service.remember_provider_user_alias(
                db,
                provider=_dingtalk_provider,
                channel_type="dingtalk",
                id_type="sender_id",
                subject=sender_id,
                user=platform_user,
            )

        quoted_message = await resolve_quoted_message_sender(
            db,
            quoted_message,
            provider=_dingtalk_provider,
            channel_type="dingtalk",
            provider_sender_id_type="sender_id",
            agent=agent_obj,
            agent_provider_ref=chatbot_user_id,
        )

        # Check for channel commands (/new, /reset)
        from app.services.channel_commands import (
            is_channel_command,
            prepare_channel_command_reply,
        )
        if is_channel_command(user_text):
            cmd_result = await prepare_channel_command_reply(
                db=db, command=user_text, agent_id=agent_id,
                user_id=platform_user_id, external_conv_id=conv_id,
                external_user_id=sender_staff_id,
                source_channel="dingtalk",
                provider_event_id=message_id,
                is_group=conversation_type == "2",
                group_name=conversation_title or None,
            )
            if not cmd_result["should_deliver"]:
                await db.commit()
                return
            await db.commit()
            _cmd_delivery = await _deliver_dingtalk_command_reply(
                message_id=cmd_result["message_id"],
                agent_id=agent_id,
                conversation_id=cmd_result["conversation_id"],
                external_conv_id=conv_id,
                is_group=conversation_type == "2",
                message=cmd_result["message"],
            )
            if not _cmd_delivery.ok:
                logger.error(
                    f"[DingTalk] Command reply failed: {_cmd_delivery.error}"
                )
            return

        # Use the real DingTalk group title when the stream event provides one;
        # fall back to a conversation_id-based placeholder otherwise.
        _dt_group_name = None
        if conversation_type == "2":
            _dt_group_name = (
                conversation_title.strip()
                if conversation_title and conversation_title.strip()
                else f"DingTalk Group {conversation_id[:12]}"
            )

        # Find or create session
        sess = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=platform_user_id,
            external_conv_id=conv_id,
            source_channel="dingtalk",
            first_message_title=user_text,
            is_group=(conversation_type == "2"),
            group_name=_dt_group_name,
        )
        session_conv_id = str(sess.id)

        # Load provider-neutral history; the shared caller materializes images
        # only after the concrete model attempt is resolved.
        from app.services.chat_history import load_history_for_llm
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=session_conv_id,
            ctx_size=ctx_size,
            is_group=(conversation_type == "2"),
        )

        # Save user message — use display-friendly format for DB (no base64)
        # Build saved_content: [file:name] prefix for each saved file + clean text
        import re as _re_dt
        _clean_text = _re_dt.sub(
            r'\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]',
            "", user_text,
        ).strip()
        if saved_file_paths:
            from pathlib import Path as _PathDT
            _file_prefixes = "\n".join(
                f"[file:{_PathDT(p).name}]" for p in saved_file_paths
            )
            saved_content = f"{_file_prefixes}\n{_clean_text}".strip() if _clean_text else _file_prefixes
        else:
            saved_content = _clean_text or user_text
        from app.services.chat_history import (
            finish_blocked_confirmation_ingest,
            finish_ignored_confirmation_ingest,
            ingest_incoming_chat_message,
            load_history_prefix_before_anchor,
        )
        from app.services.quoted_message import normalize_quoted_message

        normalized_quote = normalize_quoted_message(quoted_message)
        own_attachments = [
            attachment_from_workspace_path(path)
            for path in (saved_file_paths or [])
        ]
        quoted_attachments = (
            list(normalized_quote.get("attachments") or [])
            if normalized_quote is not None
            else []
        )
        inbound_meta = {
            "sender_display_name": platform_user.display_name,
            "sender_nickname": sender_nick or None,
            "attachments": [*own_attachments, *quoted_attachments],
        }
        if message_id and conversation_id:
            # Reaction callbacks capture these values only in process memory.
            # Persist the minimum provider coordinates needed for bounded stale
            # progress cleanup after a backend restart; never persist secrets.
            inbound_meta["channel_receipt"] = {
                "provider_message_id": message_id,
                "provider_conversation_id": conversation_id,
            }
        if normalized_quote is not None:
            inbound_meta["quoted_message"] = normalized_quote

        ingested = await ingest_incoming_chat_message(
            db,
            session=sess,
            agent_id=agent_id,
            user_id=platform_user_id,
            content=saved_content,
            source_channel="dingtalk",
            provider_event_id=message_id or None,
            channel_config_id=_early_cfg.id if _early_cfg else None,
            actor_ref=sender_staff_id,
            message_meta=inbound_meta,
        )

        async def _delete_unconsumed_uploads() -> None:
            stored_paths = {
                item["path"]
                for item in [*own_attachments, *quoted_attachments]
                if item.get("path")
            }
            if stored_paths:
                from app.services.storage import (
                    agent_storage_key,
                    get_storage_backend,
                )

                storage = get_storage_backend()
                for workspace_path in stored_paths:
                    await storage.delete(
                        agent_storage_key(agent_id, workspace_path)
                    )

        if await finish_blocked_confirmation_ingest(db, ingested):
            await _delete_unconsumed_uploads()
            return
        if not ingested.created:
            await _delete_unconsumed_uploads()
        turn_anchor_id = ingested.message.id
        from app.services.turn_inbox import bind_durable_channel_receipt_anchor

        bind_durable_channel_receipt_anchor(sess, ingested.message.id)
        if channel_reactions and channel_reactions.bind_receipt_context:
            channel_reactions.bind_receipt_context(
                agent_id,
                session_conv_id,
                ingested.message.id,
            )
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()
        if ingested.ignored_confirmation is not None:
            await finish_ignored_confirmation_ingest(ingested)
            refreshed_prefix = await load_history_prefix_before_anchor(
                db,
                agent_id=agent_id,
                conversation_id=session_conv_id,
                turn_anchor_id=ingested.message.id,
                ctx_size=ctx_size,
                is_group=(conversation_type == "2"),
            )
            if refreshed_prefix is None:
                raise RuntimeError(
                    "confirmation ignore committed but DingTalk history prefix could not be rebuilt"
                )
            history = refreshed_prefix

        # Mirror this inbound message to anyone viewing the session on web in real
        # time — the agent reply already streams there; this makes the user's own
        # message show up live too, not only on reload.
        from app.services.channel_llm import broadcast_channel_user_message
        await broadcast_channel_user_message(
            agent_id, session_conv_id, message=ingested.message,
            sender_name=platform_user.display_name or sender_nick or None,
            user_id=platform_user_id,
        )

        if ingested.consumed_by_onmessage:
            logger.info(
                "[DingTalk] Inbound message %s routed to %d on_message execution(s)",
                message_id,
                len(ingested.execution_ids),
            )
            return

        from app.services.quoted_message import render_quoted_message_for_llm
        from app.services.sender_attribution import wrap_with_sender

        # Group chats get a platform-injected <sender> prefix (spec §4.0/§4.1).
        # DingTalk P2P had no prefix before this iteration and we keep it that
        # way — agent_context's "## Current Conversation" handles the single-
        # speaker session-level identity.
        llm_user_text = render_quoted_message_for_llm(user_text, normalized_quote)
        if conversation_type == "2":
            llm_user_text = wrap_with_sender(
                llm_user_text,
                platform_user_id,
                platform_user.display_name or sender_nick,
            )

        # Call LLM
        _thinking_chunks: list[str] = []

        _thinking_sender = BufferedIMThinkingSender.for_runtime(
            enabled=resolve_im_thinking_enabled(agent_obj, sess),
            agent_id=agent_id,
            user_id=platform_user_id,
            conversation_id=session_conv_id,
            turn_anchor_id=turn_anchor_id,
        )

        async def _collect_thinking(text: str):
            _thinking_chunks.append(text)
            if channel_reactions and channel_reactions.on_thinking:
                try:
                    await channel_reactions.on_thinking(text)
                except Exception as exc:  # noqa: BLE001 - reaction feedback is best-effort
                    logger.warning(f"[DingTalk] Thinking reaction update failed: {exc}")
            await _thinking_sender.push(text)

        async def _notify_tool_call(evt: dict):
            if channel_reactions and channel_reactions.on_tool_call:
                try:
                    await channel_reactions.on_tool_call(evt)
                except Exception as exc:  # noqa: BLE001 - reaction feedback is best-effort
                    logger.warning(f"[DingTalk] Tool reaction update failed: {exc}")

        async def _notify_status(status: dict):
            if channel_reactions and channel_reactions.on_status:
                try:
                    await channel_reactions.on_status(status)
                except Exception as exc:
                    logger.warning(f"[DingTalk] Status reaction update failed: {exc}")

        try:
            reply_text = await _call_agent_llm(
                db, agent_id, llm_user_text,
                history=history, user_id=platform_user_id,
                session_id=session_conv_id,
                is_group=(conversation_type == "2"),
                on_thinking=_collect_thinking,
                on_tool_call=_notify_tool_call,
                on_status=_notify_status,
                turn_anchor_id=turn_anchor_id,
            )
        finally:
            await _thinking_sender.flush()

        has_media = bool(own_attachments or quoted_attachments)
        logger.info(
            f"[DingTalk] LLM reply ({('media' if has_media else 'text')} input): "
            f"{reply_text[:100]}"
        )

        if reply_text:
            # Persist the final assistant reply before the external side effect.
            # The assistant row is the durable completion marker for this
            # append-only message turn.
            from app.database import async_session as _reply_session_factory
            from app.services.chat_history import persist_assistant_reply_row
            from app.services.im_delivery import (
                IMDeliveryResult,
                attach_delivery_to_meta,
                register_delivery,
            )

            async with _reply_session_factory() as reply_db:
                assistant_message_id = await persist_assistant_reply_row(
                    reply_db,
                    agent_id=agent_id,
                    user_id=platform_user_id,
                    conversation_id=session_conv_id,
                    content=reply_text,
                    thinking="".join(_thinking_chunks) or None,
                    message_meta=attach_delivery_to_meta(
                        {},
                        IMDeliveryResult.pending("dingtalk"),
                    ),
                    turn_anchor_id=turn_anchor_id,
                )
                await reply_db.commit()
            from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

            await publish_committed_turn_terminal(
                agent_id=agent_id,
                conversation_id=session_conv_id,
                turn_anchor_id=turn_anchor_id,
                message_id=assistant_message_id,
                content=reply_text,
            )
            sess.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            # All final replies use the durable OpenAPI route so delivery is
            # independent of the inbound callback's temporary webhook TTL.
            from app.services.im_delivery import deliver_persisted_message
            from app.services.turn_runtime import TurnRuntime

            try:
                delivery_result = await deliver_persisted_message(
                    message_id=assistant_message_id,
                    agent_id=agent_id,
                    runtime=TurnRuntime(
                        session_found=True,
                        source_channel="dingtalk",
                        conversation_id=session_conv_id,
                        external_conv_id=sess.external_conv_id,
                        is_group=bool(sess.is_group),
                    ),
                    message=reply_text,
                )
            except Exception as e:
                logger.error(f"[DingTalk] OpenAPI reply failed: {type(e).__name__}")
                await register_delivery(
                    assistant_message_id,
                    IMDeliveryResult.from_exception("dingtalk", e),
                )
        # Log activity
        from app.services.activity_logger import log_activity
        await log_activity(
            agent_id, "chat_reply",
            f"Replied to DingTalk message: {reply_text[:80]}",
            detail={"channel": "dingtalk", "user_text": user_text[:200], "reply": reply_text[:500]},
        )
