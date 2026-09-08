"""Message-loop orchestration for websocket chat."""

from __future__ import annotations

from app.api.websocket_inbox_ops import publish_inbox_receipt, receive_turn_message
from app.services.llm.failure_outcome import render_message
from app.services.turn_inbox import schedule_durable_turn_resume


async def message_loop_impl(api, self):
    initial_content = (
        str(self.pending_initial_assistant["content"]) if self.pending_initial_assistant else self.welcome_message
    )
    automatic_scene = (self.scene_manifest or {}).get("activation_source") == "automatic"
    if initial_content and not self.history_messages and not self.onboarding_required and not automatic_scene:
        await self.websocket.send_json(
            {
                "type": "done",
                "role": "assistant",
                "content": initial_content,
                "message_id": f"initial-assistant:{self.conv_id}",
            }
        )

    while True:
        data = await receive_turn_message(self.websocket)

        project_writable = False
        if getattr(self, "project_session_access", None) is not None:
            project_writable = await self._project_session_still_writable()
        if getattr(self, "project_session_access", None) is not None and not project_writable:
            self.read_only = True

        if data.get("type") == "abort":
            if self.read_only and not project_writable:
                await self._send_current_turn_event({"type": "error", "content": "只读监看会话,无法终止消息。"})
                continue
            try:
                expected_anchor_id = api.uuid.UUID(str(data["turn_anchor_id"]))
                expected_generation = int(data["generation"])
            except (KeyError, TypeError, ValueError):
                await self._send_current_turn_event(
                    {
                        "type": "error",
                        "code": "stale_abort",
                        "content": "终止请求缺少当前轮次标识，请刷新会话后重试。",
                    }
                )
                continue
            from app.services.turn_control import stop_session_turn_tree

            await stop_session_turn_tree(
                agent_id=self.agent_id,
                session_id=self.conv_id,
                reason=f"Web abort by user {self.user_id}",
                expected_anchor_id=expected_anchor_id,
                expected_generation=expected_generation,
            )
            continue

        if self.read_only:
            await self._send_current_turn_event({"type": "error", "content": "只读监看会话,无法在此发送消息。"})
            continue

        trace_id = str(api.uuid.uuid4())[:12]
        api.set_trace_id(trace_id)

        content = data.get("content", "")
        raw_client_message_id = data.get("message_id") or data.get("client_message_id")
        self.current_client_message_id = str(raw_client_message_id) if raw_client_message_id else None
        display_content = data.get("display_content", "")
        file_name = data.get("file_name", "")
        raw_attachments = data.get("attachments") if "attachments" in data else None
        override_model_id = data.get("model_id")
        reasoning_effort = data.get("reasoning_effort")
        is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
        api.logger.info(f"[WS] Received: {content[:50]}" + (" [onboarding]" if is_onboarding_trigger else ""))

        if not content and not is_onboarding_trigger:
            continue

        if content.strip().lower() == "/continue" and not is_onboarding_trigger:
            from app.api.websocket_continue import handle_continue

            await handle_continue(self, data)
            continue

        validated_attachments = None
        if raw_attachments is not None:
            from app.services.chat_attachments import validate_client_attachments

            try:
                validated_attachments = await validate_client_attachments(self.agent_id, raw_attachments)
            except (TypeError, ValueError) as exc:
                await self._send_current_turn_event({"type": "error", "content": f"附件无效：{exc}"})
                continue

        if await self._enqueue_project_subagent_message(
            content=content,
            display_content=display_content,
            file_name=file_name,
            client_message_id=data.get("message_id") or data.get("client_message_id"),
            attachments=validated_attachments,
        ):
            continue

        await self._load_scene_manifest()
        await self.websocket.send_json({
            "type": "scene_manifest",
            "manifest": api.websocket_scene_ops.public_manifest(self.scene_manifest),
        })
        try:
            effective_llm_model = await self._resolve_effective_model(
                override_model_id,
                reasoning_effort,
            )
        except ValueError as exc:
            await self._send_current_turn_event({"type": "error", "content": str(exc)})
            continue

        if not await self._check_quotas():
            continue

        onboarding_claim = None
        if is_onboarding_trigger:
            if effective_llm_model is None:
                await self.websocket.send_json({"type": "onboarding_skipped", "reason": "model_unavailable"})
                continue
            onboarding_claim = await self._claim_onboarding_trigger()
            if onboarding_claim is None:
                continue
            content = "Please begin the onboarding."
        else:
            await self._wait_for_normal_turn_onboarding()

        self.current_user_text = content
        client_message_id = data.get("message_id") or data.get("client_message_id")

        turn_lease = None
        active_snapshot = await self._load_turn_snapshot()
        if self.agent_type != "openclaw" and active_snapshot.status != "running":
            try:
                turn_lease = await api.get_workload_capacity().acquire(
                    api.WorkloadKind.INTERACTIVE,
                    self.tenant_id or self.user_id or self.conv_id,
                )
            except api.WorkloadOverloadedError:
                await self._send_current_turn_event(
                    {
                        "type": "error",
                        "code": "turn_capacity_busy",
                        "retryable": True,
                        "content": (
                            "当前请求较多，请稍后重试。"
                            if self.lang.lower().startswith("zh")
                            else "The service is busy. Please try again shortly."
                        ),
                    }
                )
                continue

        try:
            (
                turn_anchor_id,
                consumed_by_onmessage,
                persisted_initial_assistant,
                pending_confirmation,
                ignored_confirmation,
                turn_snapshot,
            ) = await self._save_user_message(
                content,
                display_content,
                file_name,
                is_onboarding_trigger,
                client_message_id=client_message_id,
                model_id=(str(effective_llm_model.id) if effective_llm_model is not None else None),
                reasoning_effort=(effective_llm_model.reasoning_effort if effective_llm_model is not None else None),
                attachments=validated_attachments,
            )
        except (api.SessionTurnBusyError, api.ConversationTurnConflict):
            if turn_lease is not None:
                await turn_lease.release()
            await self._send_current_turn_event(
                {
                    "type": "error",
                    "code": "turn_already_running",
                    "retryable": True,
                    "content": "当前会话已有消息正在处理，请稍后重试。",
                }
            )
            continue
        except BaseException:
            if turn_lease is not None:
                await turn_lease.release()
            raise

        ingested = getattr(self, "last_ingest_result", None)
        if ingested is not None and (ingested.queued_to_running_turn or not ingested.created):
            if turn_lease is not None:
                await turn_lease.release()
            await publish_inbox_receipt(api, self, ingested, turn_snapshot)
            continue

        if turn_anchor_id is not None and not is_onboarding_trigger:
            await api.publish_conversation_turn_event(
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
                payload={
                    "type": "user_message_committed",
                    **({"client_message_id": str(client_message_id)} if client_message_id else {}),
                    "message_id": str(turn_anchor_id),
                    "id": str(turn_anchor_id),
                    "role": "user",
                    "content": content,
                    "display_content": display_content,
                    "attachments": validated_attachments or [],
                    "sender_user_id": str(self.user_id),
                },
                snapshot=turn_snapshot,
                event_kind="turn_user_committed",
            )

        if pending_confirmation is not None:
            if turn_lease is not None:
                await turn_lease.release()
            await self._send_current_turn_event(
                {
                    "type": "confirmation_required",
                    "content": render_message("chat.confirmationRequired", self.lang),
                    "message_id": data.get("message_id") or data.get("client_message_id"),
                    "name": "request_confirmation",
                    "call_id": str(pending_confirmation.row_id),
                    "args": pending_confirmation.args,
                    "status": "running",
                }
            )
            continue

        if self.agent_type != "openclaw" and turn_lease is None and ingested is not None and not consumed_by_onmessage:
            await schedule_durable_turn_resume(ingested.message)
            continue

        # Onboarding uses a hidden system anchor, absent from ordinary history.
        # Its initialized context must continue through the existing turn path.
        if self.agent_type != "openclaw" and not is_onboarding_trigger and not consumed_by_onmessage and turn_anchor_id is not None:
            from app.services.chat_history import load_history_prefix_before_anchor

            async with api.async_session() as _history_db:
                refreshed_prefix = await load_history_prefix_before_anchor(
                    _history_db,
                    agent_id=self.agent_id,
                    conversation_id=self.conv_id,
                    turn_anchor_id=turn_anchor_id,
                    ctx_size=self.ctx_size,
                    include_thinking=True,
                )
            if refreshed_prefix is None:
                failed_snapshot = await self._transition_turn(turn_anchor_id, "failed")
                await self._safe_send(
                    api.with_turn_envelope(
                        {
                            "type": "error",
                            "content": render_message("errors.conversationHistoryUnavailable", self.lang),
                        },
                        failed_snapshot,
                        event_kind="turn_terminal",
                    )
                )
                if turn_lease is not None:
                    await turn_lease.release()
                continue
            self.conversation = refreshed_prefix

        if persisted_initial_assistant is not None and (is_onboarding_trigger or self.agent_type == "openclaw"):
            self.conversation.append({"role": "assistant", "content": persisted_initial_assistant.content})

        if consumed_by_onmessage:
            await self._send_current_turn_event(
                {
                    "type": "turn_receipt",
                    "status": "delegated",
                    "message_id": str(turn_anchor_id),
                    **({"client_message_id": str(client_message_id)} if client_message_id else {}),
                },
                event_kind="turn_delegated",
                reject_attempt=False,
            )
            if turn_lease is not None:
                await turn_lease.release()
            continue

        from app.services.chat_attachments import normalize_chat_message_attachments, strip_image_data_markers

        current_source = content
        if validated_attachments is None and file_name and "[image_data:" in content:
            current_source = f"[file:{file_name}]\n{content}"
        if validated_attachments is not None:
            current_content = strip_image_data_markers(current_source)
            current_attachments = validated_attachments
        else:
            current_content, current_attachments = normalize_chat_message_attachments(
                current_source,
                {},
                self.source_channel,
            )
        current_message = {"role": "user", "content": current_content}
        if current_attachments:
            current_message["attachments"] = current_attachments
        self.conversation.append(current_message)

        if self.agent_type == "openclaw":
            try:
                await self._route_openclaw(
                    content,
                    turn_anchor_id=turn_anchor_id,
                    turn_snapshot=turn_snapshot,
                )
            except Exception:
                api.logger.exception("[WS] OpenClaw queue failed")
                failed_snapshot = await self._transition_turn(turn_anchor_id, "failed")
                await self._safe_send(
                    api.with_turn_envelope(
                        {"type": "error", "content": "OpenClaw 消息转发失败，请重试。"},
                        failed_snapshot,
                        event_kind="turn_terminal",
                    )
                )
            continue

        task_match = api.re.search(
            r"(?:创建|新建|添加|建一个|帮我建|create|add)(?:一个|a )?(?:任务|待办|todo|task)[，,：：:\\s]*(.+)",
            content,
            api.re.IGNORECASE,
        )

        turn_task = api.asyncio.create_task(
            self._execute_web_turn(
                effective_llm_model=effective_llm_model,
                is_onboarding_trigger=is_onboarding_trigger,
                onboarding_claim=onboarding_claim,
                turn_anchor_id=turn_anchor_id,
                turn_snapshot=turn_snapshot,
                task_match=task_match,
            )
        )
        try:
            disposition = await turn_task
        finally:
            if turn_lease is not None:
                await turn_lease.release()
        if disposition == "disconnect":
            break
        if disposition == "continue":
            continue
