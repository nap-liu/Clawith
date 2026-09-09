"""Turn lifecycle helpers for websocket chat."""

from __future__ import annotations

from app.services.conversation_turn_lifecycle import conversation_turn_snapshot_for_session


async def execute_web_turn_impl(
    api,
    self,
    *,
    effective_llm_model,
    is_onboarding_trigger: bool,
    onboarding_claim,
    turn_anchor_id,
    turn_snapshot,
    task_match,
) -> str:
    async with api.active_turn_boundary():
        await self._publish_turn_lifecycle(turn_snapshot)
        current_user_content = next(
            (str(item.get("content") or "") for item in reversed(self.conversation) if item.get("role") == "user"),
            "",
        )
        await api.ensure_active_turn(
            owner_user_id=self.user_id,
            agent_id=self.agent_id,
            session_id=self.conv_id,
            turn_type="web",
            turn_anchor_id=turn_anchor_id,
            title=current_user_content.strip()[:40] or None,
        )
        terminal_message_id = api.uuid.uuid4()
        try:
            self.client_disconnected = False
            if effective_llm_model:
                (
                    assistant_response,
                    thinking_content,
                    _queued_messages,
                    turn_outcome,
                    produced_output,
                ) = await self._run_llm_and_stream(
                    effective_llm_model,
                    is_onboarding_trigger,
                    onboarding_claim=onboarding_claim,
                    turn_anchor_id=turn_anchor_id,
                    turn_snapshot=turn_snapshot,
                )
            else:
                assistant_response = (
                    f"⚠️ {self.agent_name} has no LLM model configured. "
                    "Please select a model in the agent's Settings tab."
                )
                thinking_content = []
                _queued_messages = []
                turn_outcome = "failed"
                produced_output = False

            if onboarding_claim and onboarding_claim.claimed_at:
                async with api.async_session() as _release_db:
                    await api.release_onboarding_claim(
                        _release_db,
                        self.agent_id,
                        self.user_id,
                        onboarding_claim.claimed_at,
                    )

            if is_onboarding_trigger and not produced_output and turn_outcome in {"failed", "aborted"}:
                if self.conversation and self.conversation[-1].get("role") == "user":
                    self.conversation.pop()
                terminal_snapshot = await self._transition_turn(
                    turn_anchor_id,
                    "cancelled" if turn_outcome == "aborted" else "failed",
                )
                await self._safe_send(
                    api.with_turn_envelope(
                        {
                            "type": "onboarding_skipped",
                            "reason": "generation_aborted" if turn_outcome == "aborted" else "generation_failed",
                            "agent_id": str(self.agent_id),
                        },
                        terminal_snapshot,
                        event_kind="turn_terminal",
                    )
                )
                return "continue"

            if assistant_response == "":
                # The shared tool loop persisted suspension before exposing the
                # confirmation. Its response may already have resumed this anchor
                # while the old execution was finishing. Project durable state;
                # never write a second suspension from this late finalizer.
                current_snapshot = await self._load_turn_snapshot()
                if (
                    turn_snapshot is not None
                    and current_snapshot.anchor_id == turn_anchor_id
                    and current_snapshot.generation == turn_snapshot.generation
                    and current_snapshot.status == "suspended"
                ):
                    await self._publish_turn_lifecycle(current_snapshot)
                    await self._safe_send(
                        api.with_turn_envelope(
                            {"type": "done", "role": "assistant", "content": ""},
                            current_snapshot,
                            event_kind="turn_suspended",
                        )
                    )
                if self.client_disconnected:
                    await api.manager.disconnect(str(self.agent_id), self.websocket)
                    return "disconnect"
                return "continue"

            if task_match and turn_outcome != "failed":
                assistant_response = await self._create_task_record(
                    task_match.group(1).strip(),
                    assistant_response,
                )

            self.conversation.append({"role": "assistant", "content": assistant_response})
            terminal_status = "cancelled" if turn_outcome == "aborted" else "failed" if turn_outcome == "failed" else "completed"
            await self._save_assistant_reply(
                assistant_response,
                thinking_content,
                message_id=terminal_message_id,
                turn_anchor_id=turn_anchor_id,
                turn_status=terminal_status,
                complete_onboarding=(
                    is_onboarding_trigger
                    and produced_output
                    and turn_outcome not in {"failed", "aborted"}
                    and self.source_channel != "web"
                ),
            )
            turn_snapshot = await self._load_turn_snapshot(turn_anchor_id)
            await self._safe_send(
                api.with_turn_envelope(
                    {
                        "type": "done",
                        "role": "assistant",
                        "content": assistant_response,
                        "message_id": str(terminal_message_id),
                    },
                    turn_snapshot,
                    event_kind="turn_terminal",
                )
            )

            if self.client_disconnected:
                api.logger.info(
                    f"[WS] Detached turn complete after disconnect; closing handler for {self.user_id or 'unknown'}"
                )
                await api.manager.disconnect(str(self.agent_id), self.websocket)
                return "disconnect"
            return "done"
        except api.asyncio.CancelledError:
            stopped = "*[Generation stopped]*"
            saved_cancelled = False
            try:
                saved_cancelled = await self._save_assistant_reply(
                    stopped,
                    [],
                    message_id=terminal_message_id,
                    turn_anchor_id=turn_anchor_id,
                    turn_status="cancelled",
                )
            except Exception:
                api.logger.exception("[WS] Failed to persist externally cancelled turn")
            if saved_cancelled:
                if self.conversation and self.conversation[-1].get("role") == "assistant":
                    self.conversation.pop()
                self.conversation.append({"role": "assistant", "content": stopped})
                turn_snapshot = await self._load_turn_snapshot(turn_anchor_id)
                await self._safe_send(
                    api.with_turn_envelope(
                        {
                            "type": "done",
                            "role": "assistant",
                            "content": stopped,
                            "message_id": str(terminal_message_id),
                        },
                        turn_snapshot,
                        event_kind="turn_terminal",
                    )
                )
            elif self.conversation and self.conversation[-1].get("role") == "assistant":
                turn_snapshot = await self._load_turn_snapshot(turn_anchor_id)
                await self._safe_send(
                    api.with_turn_envelope(
                        {
                            "type": "done",
                            "role": "assistant",
                            "content": self.conversation[-1].get("content", ""),
                            "message_id": str(terminal_message_id),
                        },
                        turn_snapshot,
                        event_kind="turn_terminal",
                    )
                )
            return "continue"


async def claim_onboarding_trigger_impl(api, self):
    async with api.async_session() as _gdb:
        claim = await api.claim_onboarding_greeting(
            _gdb,
            self.agent_id,
            self.user_id,
            api.uuid.UUID(self.conv_id),
        )
    if claim.acquired:
        return claim
    api.logger.info(f"[WS] Onboarding trigger skipped: {claim.reason}")
    await self.websocket.send_json(
        {
            "type": "onboarding_skipped",
            "reason": claim.reason,
            "agent_id": str(self.agent_id),
        }
    )
    return None


async def wait_for_normal_turn_onboarding_impl(api, self) -> None:
    while True:
        async with api.async_session() as _normal_db:
            phase = await api.claim_normal_first_turn(_normal_db, self.agent_id, self.user_id)
        if phase != api.PHASE_PENDING:
            return
        await api.asyncio.sleep(0.2)


async def resolve_effective_model_impl(
    api,
    self,
    override_model_id: str | None,
    reasoning_effort: str | None = None,
):
    from app.services.chat_model_selection import MODEL_OVERRIDE_NONE, MODEL_OVERRIDE_OK, resolve_runtime_models

    async with api.async_session() as _mdb:
        _agent_r = await _mdb.execute(api.select(api.Agent).where(api.Agent.id == self.agent_id))
        _agent_cur = _agent_r.scalar_one_or_none()
        if _agent_cur is None:
            self.llm_model = None
            self.fallback_llm_model = None
            return None
        resolved = await resolve_runtime_models(
            _mdb,
            agent=_agent_cur,
            override_model_id=override_model_id,
            override_reasoning_effort=reasoning_effort,
        )

    self.llm_model = resolved.primary_model
    self.fallback_llm_model = resolved.fallback_model
    if resolved.override_status not in {MODEL_OVERRIDE_NONE, MODEL_OVERRIDE_OK}:
        api.logger.warning(f"[WS] model override {override_model_id!r} rejected ({resolved.override_status})")
    return resolved.primary_model


async def check_quotas_impl(api, self) -> bool:
    try:
        await api.check_conversation_quota(self.user_id)
        await api.check_agent_expired(self.agent_id)
        return True
    except api.QuotaExceeded as qe:
        await self._send_current_turn_event({"type": "error", "content": f"⚠️ {qe.message}"})
        return False
    except api.AgentExpired as ae:
        await self._send_current_turn_event({"type": "error", "content": f"⚠️ {ae.message}"})
        return False


async def save_user_message_impl(
    api,
    self,
    content: str,
    display_content: str,
    file_name: str,
    is_onboarding_trigger: bool,
    *,
    client_message_id: str | None = None,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
    attachments: list[dict] | None = None,
):
    from app.services.chat_attachments import strip_image_data_markers

    self.last_ingest_result = None

    has_image_marker = "[image_data:" in content
    if attachments is not None:
        saved_content = display_content if display_content else strip_image_data_markers(content)
    elif has_image_marker:
        clean_content = strip_image_data_markers(content)
        saved_content = f"[file:{file_name}]\n{clean_content}" if file_name else clean_content
    else:
        saved_content = display_content if display_content else content
        if file_name:
            saved_content = f"[file:{file_name}]\n{saved_content}"

    if is_onboarding_trigger:
        from app.services.chat_history import HIDDEN_ONBOARDING_ANCHOR_KIND

        async with api.async_session() as db:
            session = (
                await db.execute(
                    api.select(api.ChatSession).where(api.ChatSession.id == api.uuid.UUID(self.conv_id)).with_for_update()
                )
            ).scalar_one_or_none()
            if session is None:
                raise RuntimeError("chat session no longer exists")
            anchor = api.ChatMessage(
                agent_id=self.agent_id,
                user_id=self.user_id,
                role="system",
                content=content,
                conversation_id=self.conv_id,
                message_meta={
                    "kind": HIDDEN_ONBOARDING_ANCHOR_KIND,
                    **self._scene_message_meta(),
                    **({"model_id": model_id} if model_id else {}),
                    **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
                },
            )
            db.add(anchor)
            await db.flush()
            turn_snapshot = await api.transition_conversation_turn(
                db,
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
                turn_anchor_id=anchor.id,
                status="running",
            )
            session.last_message_at = api.datetime.now(api.tz.utc)
            await db.commit()
        api.logger.info("[WS] Onboarding trigger anchored as a hidden system turn")
        return anchor.id, False, None, None, False, turn_snapshot

    from app.services.chat_history import ingest_incoming_chat_message

    async with api.async_session() as db:
        _sess_r = await db.execute(
            api.select(api.ChatSession).where(api.ChatSession.id == api.uuid.UUID(self.conv_id)).with_for_update()
        )
        _sess = _sess_r.scalar_one_or_none()
        if _sess is None:
            raise RuntimeError("chat session no longer exists")
        if _sess.agent_id != self.agent_id or (not _sess.is_group and _sess.user_id != self.user_id):
            raise PermissionError("Session ownership changed")
        initial_assistant = None
        first_user_created_at = None
        if self.pending_initial_assistant is not None:
            initial_assistant = await api.persist_initial_assistant_message_if_pristine(
                db,
                session=_sess,
                agent_id=self.agent_id,
                user_id=self.user_id,
                content=str(self.pending_initial_assistant["content"]),
                message_meta=dict(self.pending_initial_assistant.get("message_meta") or {}),
            )
            if initial_assistant is not None:
                initial_created_at = initial_assistant.created_at or api.datetime.now(api.tz.utc)
                first_user_created_at = initial_created_at + api.timedelta(microseconds=1)
        ingested = await ingest_incoming_chat_message(
            db,
            session=_sess,
            agent_id=self.agent_id,
            user_id=self.user_id,
            content=saved_content,
            source_channel=_sess.source_channel,
            allow_turn_inbox=self.agent_type != "openclaw",
            provider_event_id=str(client_message_id or "") or None,
            channel_config_id=_sess.id,
            actor_ref=str(self.user_id),
            message_meta={
                **self._scene_message_meta(),
                **({"model_id": model_id} if model_id else {}),
                **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
                **({"attachments": attachments, "display_content": display_content} if attachments is not None else {}),
            },
            created_at=first_user_created_at,
        )
        if ingested.blocked_by_confirmation:
            await db.commit()
            api.logger.info("[WS] Message blocked by pending confirmation %s", ingested.message.id)
            return None, True, None, ingested.pending_confirmation, False, None
        self.last_ingest_result = ingested
        turn_snapshot = conversation_turn_snapshot_for_session(_sess)
        if not ingested.consumed_by_onmessage:
            turn_snapshot = await api.transition_conversation_turn(
                db,
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
                turn_anchor_id=ingested.message.id,
                status="running",
            )
        _now = api.datetime.now(api.tz.utc)
        if _sess:
            _sess.last_message_at = _now
            if not self.history_messages and (_sess.title.startswith("Session ") or _sess.title == "New Session"):
                title_src = display_content if display_content else content
                clean_title = title_src.replace("[图片] ", "📷 ").replace("[image_data:", "").strip()
                if file_name and not clean_title:
                    clean_title = f"📎 {file_name}"
                _sess.title = clean_title[:40] if clean_title else content[:40]
        await db.commit()
    if ingested.ignored_confirmation is not None:
        from app.services.chat_history import finish_ignored_confirmation_ingest

        await finish_ignored_confirmation_ingest(ingested)
    api.logger.info("[WS] User message saved")
    return (
        ingested.message.id,
        ingested.consumed_by_onmessage,
        initial_assistant,
        None,
        ingested.ignored_confirmation is not None,
        turn_snapshot,
    )


async def route_openclaw_impl(
    api,
    self,
    content: str,
    *,
    turn_anchor_id,
    turn_snapshot,
):
    from app.models.gateway_message import GatewayMessage as GwMsg

    async with api.async_session() as db:
        gw_msg = GwMsg(
            agent_id=self.agent_id,
            sender_user_id=self.user_id,
            conversation_id=self.conv_id,
            content=content,
            status="pending",
        )
        db.add(gw_msg)
        await db.flush()
        if turn_anchor_id is not None:
            anchor = await db.get(api.ChatMessage, turn_anchor_id)
            if anchor is None:
                raise RuntimeError("OpenClaw turn anchor disappeared before queue commit")
            anchor.message_meta = {
                **dict(anchor.message_meta or {}),
                "gateway_message_id": str(gw_msg.id),
            }
        await db.commit()
    api.logger.info("[WS] OpenClaw: message queued for gateway poll")
    await self._publish_turn_lifecycle(turn_snapshot)
    await self._safe_send(
        api.with_turn_envelope(
            {
                "type": "info",
                "content": "Message forwarded to OpenClaw agent. Waiting for response...",
            },
            turn_snapshot,
            event_kind="turn_stream",
        )
    )
