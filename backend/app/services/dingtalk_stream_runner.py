"""SDK callback runner for the DingTalk Stream adapter."""

import asyncio
import json
import threading
import uuid

from loguru import logger

from app.services.channel_dispatch import ChannelReactions
from app.services.group_policy import group_ingress_allowed
from app.services.group_policy_sender import current_sender


def _stream_facade():
    from app.services import dingtalk_stream

    return dingtalk_stream


def _dingtalk_lock_key(*args, **kwargs):
    return _stream_facade()._dingtalk_lock_key(*args, **kwargs)


def _make_dingtalk_reactions(*args, **kwargs):
    return _stream_facade()._make_dingtalk_reactions(*args, **kwargs)


def _fire_and_forget(*args, **kwargs):
    return _stream_facade()._fire_and_forget(*args, **kwargs)


def run_channel_message(*args, **kwargs):
    return _stream_facade().run_channel_message(*args, **kwargs)


async def _parse_dingtalk_quoted_message(*args, **kwargs):
    return await _stream_facade()._parse_dingtalk_quoted_message(*args, **kwargs)


async def _process_media_message(*args, **kwargs):
    return await _stream_facade()._process_media_message(*args, **kwargs)


class DingTalkStreamRunnerMixin:
    def _run_client_thread(
        self,
        agent_id: uuid.UUID,
        app_key: str,
        app_secret: str,
        stop_event: threading.Event,
        generation: int,
        fingerprint: str,
    ):
        """Run the DingTalk Stream client with auto-reconnect."""
        main_loop = self._main_loop
        try:
            import dingtalk_stream
        except ImportError:
            logger.warning(
                "[DingTalk Stream] dingtalk-stream package not installed. "
                "Install with: pip install dingtalk-stream"
            )
            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._handle_runner_exit(agent_id, generation, fingerprint),
                    main_loop,
                )
            return

        RETRY_DELAYS = [2, 5, 15, 30, 60]  # exponential backoff, seconds

        class ClawithChatbotHandler(dingtalk_stream.ChatbotHandler):
            """Custom handler that dispatches messages to the shared LLM pipeline."""

            async def process(self, callback: dingtalk_stream.CallbackMessage):
                """Handle incoming bot message from DingTalk Stream.

                NOTE: The SDK invokes this method in the thread's own asyncio loop,
                so we must dispatch to the main FastAPI loop for DB + LLM work.
                """
                try:
                    # Parse the raw data
                    incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
                    msg_data = callback.data if isinstance(callback.data, dict) else json.loads(callback.data)

                    msgtype = msg_data.get("msgtype", "text")
                    sender_staff_id = incoming.sender_staff_id or ""
                    sender_id = incoming.sender_id or ""
                    if not sender_staff_id and sender_id:
                        sender_staff_id = sender_id  # fallback
                    sender_nick = incoming.sender_nick or ""
                    chatbot_user_id = incoming.chatbot_user_id or ""
                    message_id = incoming.message_id or ""
                    conversation_id = incoming.conversation_id or ""
                    conversation_type = incoming.conversation_type or "1"
                    conversation_title = (incoming.conversation_title or "").strip()

                    logger.info(
                        f"[DingTalk Stream] Received {msgtype} message from {sender_staff_id}"
                    )

                    if msgtype == "text":
                        # Plain text — use existing logic
                        text_list = incoming.get_text_list()
                        user_text = " ".join(text_list).strip() if text_list else ""
                        if not user_text:
                            return dingtalk_stream.AckMessage.STATUS_OK, "empty message"

                        logger.info(
                            f"[DingTalk Stream] Text from {sender_staff_id}: {user_text[:80]}"
                        )

                        from app.api.dingtalk import process_dingtalk_message
                        from app.services.channel_commands import is_channel_command

                        if main_loop and main_loop.is_running():
                            lock_key = _dingtalk_lock_key(
                                agent_id,
                                conversation_type,
                                conversation_id,
                                sender_staff_id,
                            )
                            is_cmd = is_channel_command(user_text)
                            reactions = _make_dingtalk_reactions(
                                app_key, app_secret, message_id, conversation_id
                            )

                            async def _work(_text=user_text, _md=msg_data,
                                            _is_cmd=is_cmd,
                                            _ssid=sender_staff_id,
                                            _cid=conversation_id, _ctype=conversation_type,
                                            _nick=sender_nick,
                                            _mid=message_id, _sid=sender_id,
                                            _bot_uid=chatbot_user_id,
                                            _title=conversation_title, _reactions=reactions):
                                from app.api.dingtalk import _check_message_dedup

                                if await _check_message_dedup(_mid):
                                    return ""
                                if not await group_ingress_allowed(agent_id, "dingtalk", _cid,
                                    is_group=_ctype == "2", name=_title, sender_id=_sid or _ssid,
                                    sender_name=_nick, sender_type="sender_id" if _sid else "staff_id",
                                    sender_info={"staff_id": _ssid}):
                                    return ""
                                prepared = current_sender()
                                quoted_message = None
                                if not _is_cmd:
                                    quoted_message = await _parse_dingtalk_quoted_message(
                                        _md,
                                        app_key,
                                        app_secret,
                                        agent_id,
                                    )
                                await process_dingtalk_message(
                                    agent_id=agent_id,
                                    sender_staff_id=_ssid,
                                    user_text=_text,
                                    conversation_id=_cid,
                                    conversation_type=_ctype,
                                    sender_nick=_nick,
                                    message_id=_mid,
                                    sender_id=_sid,
                                    chatbot_user_id=_bot_uid,
                                    conversation_title=_title,
                                    channel_reactions=_reactions,
                                    quoted_message=quoted_message,
                                    prepared_sender=prepared,
                                )
                                return ""

                            _fire_and_forget(
                                main_loop,
                                run_channel_message(
                                    lock_key, is_command=is_cmd, reactions=reactions, work=_work
                                ),
                            )
                            # ACK immediately; serialization+LLM run on main_loop
                        else:
                            logger.warning("[DingTalk Stream] Main loop not available")

                    else:
                        # Non-text message: process media in the main loop
                        if main_loop and main_loop.is_running():
                            lock_key = _dingtalk_lock_key(
                                agent_id,
                                conversation_type,
                                conversation_id,
                                sender_staff_id,
                            )
                            reactions = _make_dingtalk_reactions(
                                app_key, app_secret, message_id, conversation_id
                            )

                            async def _work_media(_md=msg_data, _ak=app_key, _as=app_secret,
                                                  _ssid=sender_staff_id, _cid=conversation_id,
                                                  _ctype=conversation_type,
                                                  _nick=sender_nick, _mid=message_id,
                                                  _sid=sender_id, _bot_uid=chatbot_user_id,
                                                  _title=conversation_title,
                                                  _reactions=reactions):
                                from app.api.dingtalk import _check_message_dedup

                                if await _check_message_dedup(_mid):
                                    return ""
                                await self._handle_media_and_dispatch(
                                    msg_data=_md,
                                    app_key=_ak,
                                    app_secret=_as,
                                    agent_id=agent_id,
                                    sender_staff_id=_ssid,
                                    conversation_id=_cid,
                                    conversation_type=_ctype,
                                    sender_nick=_nick,
                                    message_id=_mid,
                                    sender_id=_sid,
                                    chatbot_user_id=_bot_uid,
                                    conversation_title=_title,
                                    channel_reactions=_reactions,
                                )
                                return ""

                            _fire_and_forget(
                                main_loop,
                                run_channel_message(
                                    lock_key,
                                    is_command=False,  # 媒体消息不会是命令
                                    reactions=reactions,
                                    work=_work_media,
                                ),
                            )
                            # ACK immediately; serialization+LLM run on main_loop
                        else:
                            logger.warning("[DingTalk Stream] Main loop not available")

                    return dingtalk_stream.AckMessage.STATUS_OK, "ok"
                except Exception as e:
                    # Keep channel credentials and provider payload details out of logs.
                    logger.error(
                        f"[DingTalk Stream] Error in message handler: {type(e).__name__}"
                    )
                    import traceback
                    traceback.print_exc()
                    return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, str(e)

            @staticmethod
            async def _handle_media_and_dispatch(
                msg_data: dict,
                app_key: str,
                app_secret: str,
                agent_id: uuid.UUID,
                sender_staff_id: str,
                conversation_id: str,
                conversation_type: str,
                sender_nick: str = "",
                message_id: str = "",
                sender_id: str = "",
                chatbot_user_id: str = "",
                conversation_title: str = "",
                channel_reactions: ChannelReactions | None = None,
            ):
                """Download media, then dispatch to process_dingtalk_message."""
                from app.api.dingtalk import process_dingtalk_message
                from app.services.confirmation_service import (
                    find_dingtalk_pending_confirmation,
                    redeliver_pending_confirmation,
                )

                if not await group_ingress_allowed(agent_id, "dingtalk", conversation_id,
                    is_group=conversation_type == "2", name=conversation_title,
                    sender_id=sender_id or sender_staff_id, sender_name=sender_nick,
                    sender_type="sender_id" if sender_id else "staff_id", sender_info={"staff_id": sender_staff_id}):
                    return
                prepared = current_sender()
                external_conv_id = (
                    f"dingtalk_group_{conversation_id}"
                    if conversation_type == "2"
                    else f"dingtalk_p2p_{sender_staff_id}"
                )
                pending = await find_dingtalk_pending_confirmation(
                    agent_id=agent_id,
                    external_conv_id=external_conv_id,
                )
                if pending is not None and pending.force_confirmation:
                    await redeliver_pending_confirmation(pending)
                    logger.info(
                        "[DingTalk Stream] Discarded media before download because "
                        "confirmation %s is required",
                        pending.row_id,
                    )
                    return

                user_text, saved_file_paths = await _process_media_message(
                    msg_data=msg_data,
                    app_key=app_key,
                    app_secret=app_secret,
                    agent_id=agent_id,
                )

                if not user_text:
                    logger.info("[DingTalk Stream] Empty content after media processing, skipping")
                    return

                quoted_message = await _parse_dingtalk_quoted_message(
                    msg_data,
                    app_key,
                    app_secret,
                    agent_id,
                )

                await process_dingtalk_message(
                    agent_id=agent_id,
                    sender_staff_id=sender_staff_id,
                    user_text=user_text,
                    conversation_id=conversation_id,
                    conversation_type=conversation_type,
                    saved_file_paths=saved_file_paths,
                    sender_nick=sender_nick,
                    message_id=message_id,
                    sender_id=sender_id,
                    chatbot_user_id=chatbot_user_id,
                    conversation_title=conversation_title,
                    channel_reactions=channel_reactions,
                    quoted_message=quoted_message,
                    prepared_sender=prepared,
                )

        class ClawithCardCallbackHandler(dingtalk_stream.CallbackHandler):
            """Relays interactive-card button clicks to the agent — generic, no coupling."""

            @staticmethod
            def _extract_button(content) -> "tuple[str, str]":
                # Faithfully pull the clicked button's value + display label.
                # value: cardPrivateData.actionIds[0]; label: cardPrivateData.params.text.
                value, label = "", ""
                if isinstance(content, dict):
                    cpd = content.get("cardPrivateData") or {}
                    if isinstance(cpd, dict):
                        ids = cpd.get("actionIds")
                        if isinstance(ids, list) and ids:
                            value = str(ids[0] or "")
                        params = cpd.get("params") or {}
                        if isinstance(params, dict):
                            label = str(params.get("text") or "")
                            if not value:
                                value = str(params.get("action") or params.get("value") or "")
                if not label:
                    label = value
                if not value:
                    value = label
                return value, label

            async def process(self, callback: dingtalk_stream.CallbackMessage):
                try:
                    cb = dingtalk_stream.CardCallbackMessage.from_dict(callback.data)
                    out_track_id = cb.card_instance_id or ""
                    staff_id = cb.user_id or ""
                    content = cb.content or {}
                    logger.info(
                        f"[DingTalk Stream] card callback outTrackId={out_track_id} "
                        f"user={staff_id} content={content}"
                    )
                    value, label = self._extract_button(content)
                    if (
                        out_track_id
                        and (value or label)
                        and main_loop is not None
                        and not main_loop.is_closed()
                    ):
                        from app.services.confirmation_service import resolve_confirmation_via_dingtalk

                        _fire_and_forget(
                            main_loop,
                            resolve_confirmation_via_dingtalk(out_track_id, staff_id, value, label),
                        )
                    return dingtalk_stream.AckMessage.STATUS_OK, "ok"
                except Exception as e:
                    logger.error(f"[DingTalk Stream] card callback error: {e}")
                    return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, str(e)

        try:
            credential = dingtalk_stream.Credential(client_id=app_key, client_secret=app_secret)
            client = dingtalk_stream.DingTalkStreamClient(credential=credential)
            client.register_callback_handler(
                dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                ClawithChatbotHandler(),
            )
            client.register_callback_handler(
                dingtalk_stream.Card_Callback_Router_Topic,
                ClawithCardCallbackHandler(),
            )
            logger.info(
                f"[DingTalk Stream] registered callback topics for agent {agent_id}: "
                f"{list(client.callback_handler_map.keys())}"
            )
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async_stop_event = asyncio.Event()
            runtime = self._runtimes.get(agent_id)
            if runtime is not None and runtime.generation == generation:
                runtime.loop = loop
                runtime.async_stop_event = async_stop_event
            if stop_event.is_set():
                async_stop_event.set()
            loop.run_until_complete(
                self._run_managed_client(
                    agent_id=agent_id,
                    generation=generation,
                    client=client,
                    async_stop_event=async_stop_event,
                    retry_delays=RETRY_DELAYS,
                )
            )
        except Exception as exc:
            logger.exception(f"[DingTalk Stream] Client runner failed for {agent_id}: {exc}")
        finally:
            try:
                pending = asyncio.all_tasks(loop) if "loop" in locals() and not loop.is_closed() else set()
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                if "loop" in locals() and not loop.is_closed():
                    loop.close()
            except Exception:
                logger.exception(f"[DingTalk Stream] Failed to close runner loop for {agent_id}")
            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._handle_runner_exit(agent_id, generation, fingerprint),
                    main_loop,
                )
            logger.info(
                f"[DingTalk Stream] Client runner exited for agent {agent_id} "
                f"(generation={generation})"
            )
