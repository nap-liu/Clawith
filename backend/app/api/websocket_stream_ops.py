"""LLM streaming and persistence helpers for websocket chat."""

from __future__ import annotations


async def run_llm_and_stream_impl(
    api,
    self,
    effective_llm_model,
    is_onboarding_trigger: bool,
    *,
    onboarding_claim=None,
    turn_anchor_id=None,
    turn_snapshot=None,
):
    start_gen = api.perf_counter()
    partial_chunks: list[str] = []
    thinking_content: list[str] = []
    onboarding_lease_task = None
    onboarding_lease_lost = False

    async def stop_onboarding_lease() -> None:
        nonlocal onboarding_lease_task
        if onboarding_lease_task is None:
            return
        lease_task = onboarding_lease_task
        onboarding_lease_task = None
        lease_task.cancel()
        try:
            await lease_task
        except api.asyncio.CancelledError:
            pass

    try:
        api.logger.info(f"[WS] Calling LLM {effective_llm_model.model} (streaming)...")
        onboarding_claimed_at = onboarding_claim.claimed_at if onboarding_claim else None
        needs_onboarding_mark = bool(is_onboarding_trigger and onboarding_claimed_at is not None)
        onboarding_target_phase = "completed"
        onboarding_expected_phase = api.PHASE_PENDING if needs_onboarding_mark else None
        onboarding_mark_done = False
        onboarding_visible_output_started = False
        onboarding_lease_refreshed_at = 0.0

        async def maybe_mark_onboarding_progress() -> bool:
            nonlocal onboarding_mark_done, onboarding_lease_refreshed_at, onboarding_lease_task
            if not needs_onboarding_mark or onboarding_mark_done:
                return True
            try:
                async with api.async_session() as _ob_db:
                    advanced = await api.mark_onboarding_phase(
                        _ob_db,
                        self.agent_id,
                        self.user_id,
                        onboarding_target_phase,
                        expected_phase=onboarding_expected_phase,
                        expected_onboarded_at=onboarding_claimed_at,
                    )
                if not advanced:
                    api.logger.info("[WS] Onboarding phase changed before this turn could publish its first output")
                    return False
                onboarding_mark_done = True
                onboarding_lease_refreshed_at = api.perf_counter()
                if onboarding_target_phase == api.PHASE_GREETED:
                    onboarding_lease_task = api.asyncio.create_task(maintain_onboarding_lease())
                await self._safe_send({"type": "onboarded", "agent_id": str(self.agent_id)})
                return True
            except Exception as _ob_err:
                api.logger.warning(f"[WS] mark_onboarded failed: {_ob_err}")
                return False

        async def maybe_refresh_onboarding_lease(*, force: bool = False) -> bool:
            nonlocal onboarding_lease_refreshed_at, onboarding_lease_lost
            if onboarding_lease_lost:
                return False
            if (
                not onboarding_mark_done
                or onboarding_target_phase != api.PHASE_GREETED
                or (not force and api.perf_counter() - onboarding_lease_refreshed_at < api.ONBOARDING_LEASE_REFRESH_SECONDS)
            ):
                return True
            try:
                async with api.async_session() as _lease_db:
                    refreshed = await api.mark_onboarding_phase(
                        _lease_db,
                        self.agent_id,
                        self.user_id,
                        api.PHASE_GREETED,
                        expected_phase=api.PHASE_GREETED,
                    )
                if not refreshed:
                    api.logger.info("[WS] Onboarding greeting lease changed before assistant persistence")
                    onboarding_lease_lost = True
                    return False
                onboarding_lease_refreshed_at = api.perf_counter()
                return True
            except Exception as _lease_err:
                api.logger.warning(f"[WS] Onboarding greeting lease refresh failed: {_lease_err}")
                onboarding_lease_lost = True
                return False

        async def maintain_onboarding_lease() -> None:
            while True:
                await api.asyncio.sleep(api.ONBOARDING_LEASE_REFRESH_SECONDS)
                if not await maybe_refresh_onboarding_lease(force=True):
                    return

        async def reserve_visible_output() -> bool:
            nonlocal onboarding_visible_output_started
            if not await maybe_mark_onboarding_progress():
                return False
            if not await maybe_refresh_onboarding_lease():
                return False
            if is_onboarding_trigger:
                onboarding_visible_output_started = True
            return True

        async def stream_to_ws(text: str):
            if not await reserve_visible_output():
                raise RuntimeError("Onboarding claim lost before first output")
            partial_chunks.append(text)
            await self._safe_send(
                api.with_turn_envelope(
                    {"type": "chunk", "content": text},
                    turn_snapshot,
                    event_kind="turn_stream",
                )
            )

        async def tool_call_to_ws(data: dict):
            public_data = {k: v for k, v in data.items() if not k.startswith("_")}
            if not await reserve_visible_output():
                raise RuntimeError("Onboarding claim lost before tool output")
            if public_data.get("status") == "done":
                await self._inject_live_preview_and_workspace_metadata(public_data)
            _ws_data = (
                {**public_data, "args": api.sanitize_tool_args(public_data.get("args"))}
                if "args" in public_data
                else public_data
            )
            await self._safe_send(
                api.with_turn_envelope(
                    {"type": "tool_call", **_ws_data},
                    turn_snapshot,
                    event_kind="turn_tool",
                )
            )
            if public_data.get("status") in {"running", "done"} and not data.get("_durable_persisted"):
                await self._save_tool_call_to_db(public_data, turn_anchor_id=turn_anchor_id)

        async def thinking_to_ws(text: str):
            if not await reserve_visible_output():
                raise RuntimeError("Onboarding claim lost before thinking output")
            thinking_content.append(text)
            await self._safe_send(
                api.with_turn_envelope(
                    {"type": "thinking", "content": text},
                    turn_snapshot,
                    event_kind="turn_stream",
                )
            )

        _workspace_draft_cache: dict[str, str] = {}

        async def tool_delta_to_ws(data: dict):
            tool_name = data.get("name", "")
            _ws_tools = {
                "write_file",
                "edit_file",
                "move_file",
                "delete_file",
                "convert_markdown_to_docx",
                "convert_csv_to_xlsx",
                "convert_markdown_to_pdf",
                "convert_html_to_pdf",
                "convert_html_to_pptx",
            }
            if tool_name not in _ws_tools:
                return

            raw_args = data.get("arguments", "")
            if isinstance(raw_args, (dict, list)):
                raw_args = api.json.dumps(raw_args, ensure_ascii=False)
            elif raw_args is None:
                raw_args = ""
            else:
                raw_args = str(raw_args)

            draft_id = str(data.get("id") or f"draft-{data.get('index', 0)}")
            if _workspace_draft_cache.get(draft_id) == raw_args:
                return
            if not await reserve_visible_output():
                raise RuntimeError("Onboarding claim lost before tool draft output")
            _workspace_draft_cache[draft_id] = raw_args

            await self._safe_send(
                api.with_turn_envelope(
                    {
                        "type": "workspace_draft",
                        "id": draft_id,
                        "index": data.get("index", 0),
                        "name": tool_name,
                        "arguments": raw_args,
                    },
                    turn_snapshot,
                    event_kind="turn_tool_delta",
                )
            )

        async def _call_with_failover():
            nonlocal needs_onboarding_mark, onboarding_expected_phase, onboarding_target_phase

            if is_onboarding_trigger and onboarding_claimed_at is not None:
                async with api.async_session() as _claim_db:
                    claim_is_current = await api.onboarding_claim_is_current(
                        _claim_db,
                        self.agent_id,
                        self.user_id,
                        onboarding_claimed_at,
                    )
                if not claim_is_current:
                    raise RuntimeError("Onboarding claim lost before model invocation")

            async def _on_failover(reason: str):
                if not await reserve_visible_output():
                    raise RuntimeError("Onboarding claim lost before failover output")
                await self._safe_send(
                    api.with_turn_envelope(
                        {"type": "info", "content": f"Primary model error, {reason}"},
                        turn_snapshot,
                        event_kind="turn_stream",
                    )
                )

            from app.services.chat_history import strip_leading_orphan_tool_messages

            persisted_view = strip_leading_orphan_tool_messages(self.conversation)
            _truncated = list(persisted_view)
            ephemeral_overlays: list[dict] = []
            _onb = None
            skip_tools_for_greeting = False
            try:
                async with api.async_session() as _ob_db:
                    _agent_result = await _ob_db.execute(api.select(api.Agent).where(api.Agent.id == self.agent_id))
                    _agent = _agent_result.scalar_one_or_none()
                    if _agent is None:
                        raise RuntimeError("Agent no longer exists")
                    _onb = await api.resolve_onboarding_prompt(
                        _ob_db,
                        _agent,
                        self.user_id,
                        user_name=self.user_display_name,
                        user_locale=self.lang,
                        is_onboarding_trigger=is_onboarding_trigger,
                        complete_after_greeting=self.source_channel != "web",
                    )
                if _onb:
                    ephemeral_overlays = [{"role": "system", "content": _onb.prompt}]
                    _truncated = ephemeral_overlays + _truncated
                    if _onb.lock_on_first_chunk:
                        needs_onboarding_mark = True
                        onboarding_target_phase = _onb.target_phase
                        onboarding_expected_phase = _onb.expected_phase
                    if _onb.is_greeting_turn:
                        skip_tools_for_greeting = True
            except Exception as _onb_err:
                api.logger.warning(f"[WS] Onboarding prompt resolve failed (non-fatal): {_onb_err}")
                if is_onboarding_trigger:
                    raise RuntimeError("Onboarding prompt could not be resolved") from _onb_err
            if is_onboarding_trigger and _onb is None:
                raise RuntimeError("Onboarding claim changed before prompt resolution")

            context_recovery = None
            if turn_anchor_id is not None and persisted_view:
                from app.services.llm.turn_partition import effective_keep_recent_turns

                protected_keep_recent_turns = effective_keep_recent_turns(
                    effective_llm_model,
                    self.fallback_llm_model,
                )

                async def _recover_context(_recovery_model, dispatch_budget):
                    from app.services.chat_history import load_recoverable_history_for_turn
                    from app.services.llm.compactor import (
                        COMPACTION_NOT_APPLICABLE_REASONS,
                        ContextRecoveryMessages,
                        maybe_compact,
                    )

                    compacted = await maybe_compact(
                        agent_id=self.agent_id,
                        conversation_id=self.conv_id,
                        model=_recovery_model,
                        last_prompt_tokens=getattr(dispatch_budget, "authoritative_prompt_tokens", None),
                        current_anchor_id=turn_anchor_id,
                        force_required=getattr(dispatch_budget, "provider_overflow", False),
                        keep_recent_turns_override=(
                            getattr(dispatch_budget, "keep_recent_turns_override", None)
                            if getattr(dispatch_budget, "provider_overflow", False)
                            else protected_keep_recent_turns
                        ),
                    )
                    preflight_not_applicable = (
                        not compacted.triggered
                        and compacted.skipped_reason in COMPACTION_NOT_APPLICABLE_REASONS
                        and dispatch_budget.fits
                    )
                    if not compacted.triggered and not preflight_not_applicable:
                        api.logger.warning(
                            "[WS] context recovery could not compact session="
                            f"{self.conv_id}: {compacted.skipped_reason}"
                        )
                        return None
                    async with api.async_session() as recovery_db:
                        recovered = await load_recoverable_history_for_turn(
                            recovery_db,
                            agent_id=self.agent_id,
                            conversation_id=self.conv_id,
                            turn_anchor_id=turn_anchor_id,
                            ctx_size=self.ctx_size,
                            include_thinking=True,
                        )
                    if not recovered:
                        api.logger.warning(f"[WS] context recovery lost latest-anchor race session={self.conv_id}")
                        return None
                    self.conversation = recovered
                    return ContextRecoveryMessages(
                        ephemeral_overlays + self.conversation,
                        preflight_not_applicable=preflight_not_applicable,
                    )

                context_recovery = _recover_context

            live_code_chars_sent = 0
            live_code_truncated_sent = False

            async def code_output_to_ws(text: str, label: str = "stdout"):
                nonlocal live_code_chars_sent, live_code_truncated_sent
                if not await reserve_visible_output():
                    raise RuntimeError("Onboarding claim lost before code output")
                try:
                    remaining = api.MAX_LIVE_CODE_STREAM_CHARS - live_code_chars_sent
                    if remaining <= 0:
                        if not live_code_truncated_sent:
                            live_code_truncated_sent = True
                            await self.websocket.send_json(
                                {
                                    "type": "agentbay_live",
                                    "env": "code",
                                    "output": api.LIVE_CODE_TRUNCATED_NOTICE,
                                    "stream": label,
                                }
                            )
                        return

                    output = text[:remaining]
                    live_code_chars_sent += len(output)
                    await self.websocket.send_json(
                        {
                            "type": "agentbay_live",
                            "env": "code",
                            "output": output,
                            "stream": label,
                        }
                    )
                except Exception:
                    pass

            parent_events_before_round = None
            if turn_anchor_id is not None:
                from app.services.subagent_runtime import build_parent_subagent_before_round
                from app.services.turn_inbox import is_turn_inbox_channel

                parent_events_before_round = build_parent_subagent_before_round(
                    parent_session_id=self.conv_id,
                    active_turn_anchor_id=turn_anchor_id,
                    execution_agent_id=self.agent_id,
                    execution_user_id=self.user_id,
                    include_turn_inbox=is_turn_inbox_channel(self.source_channel),
                )

            return await api.call_llm_with_failover(
                primary_model=effective_llm_model,
                fallback_model=self.fallback_llm_model,
                messages=_truncated,
                agent_name=self.agent_name,
                role_description=self.role_description,
                agent_id=self.agent_id,
                user_id=self.user_id,
                session_id=self.conv_id,
                on_chunk=stream_to_ws,
                on_tool_call=tool_call_to_ws,
                on_tool_delta=tool_delta_to_ws,
                on_thinking=thinking_to_ws,
                on_failover=_on_failover,
                skip_tools=skip_tools_for_greeting,
                on_code_output=code_output_to_ws,
                channel_context=self._channel_context(),
                turn_anchor_id=turn_anchor_id,
                turn_type="web",
                context_recovery=context_recovery,
                before_round=parent_events_before_round,
            )

        llm_task = api.asyncio.create_task(_call_with_failover())
        queued_messages: list[dict] = []
        api.set_active_turn_cancel_task(llm_task)

        async def _stop_web_turn_tree(abort_message: dict) -> bool:
            from app.services.turn_control import stop_session_turn_tree

            expected_anchor = turn_anchor_id
            expected_generation = turn_snapshot.generation if turn_snapshot is not None else None
            raw_anchor = abort_message.get("turn_anchor_id")
            raw_generation = abort_message.get("generation")
            if raw_anchor is None or raw_generation is None:
                return False
            try:
                expected_anchor = api.uuid.UUID(str(raw_anchor))
                expected_generation = int(raw_generation)
            except (TypeError, ValueError):
                return False
            stopped = await stop_session_turn_tree(
                agent_id=self.agent_id,
                session_id=self.conv_id,
                reason=f"Web abort by user {self.user_id}",
                expected_anchor_id=expected_anchor,
                expected_generation=expected_generation,
            )
            return stopped.stopped

        try:
            assistant_response, _turn_outcome = await api._await_turn_with_abort(
                llm_task,
                self.websocket.receive_json,
                partial_chunks,
                on_abort=_stop_web_turn_tree,
            )
        finally:
            api.set_active_turn_cancel_task(None)
        aborted = _turn_outcome == "aborted"
        self.client_disconnected = _turn_outcome == "disconnected"
        from app.services.llm.failure_outcome import LLMFailure, localize_llm_failure

        if isinstance(assistant_response, LLMFailure):
            assistant_response = localize_llm_failure(assistant_response, self.lang)
            return assistant_response, thinking_content, queued_messages, "failed", bool(partial_chunks)
        if self.client_disconnected:
            api.logger.info(
                f"[WS] Client disconnected mid-turn — turn finished detached, "
                f"persisting reply: {str(assistant_response)[:80]}"
            )
        elif aborted:
            api.logger.info(f"[WS] LLM aborted, partial: {str(assistant_response)[:80]}")
        else:
            api.logger.info(f"[WS] LLM response: {str(assistant_response)[:80]}")

        if not aborted and assistant_response and any(assistant_response.startswith(p) for p in api.LLM_FAILURE_PREFIXES):
            raise RuntimeError(assistant_response)

        if not aborted and is_onboarding_trigger and assistant_response:
            if not await reserve_visible_output():
                raise RuntimeError("Onboarding claim lost before completion")

        await self._update_activity_and_quota(assistant_response)
        await maybe_refresh_onboarding_lease(force=True)
        await stop_onboarding_lease()

        if is_onboarding_trigger and onboarding_lease_lost:
            return "", thinking_content, queued_messages, "aborted", False

        produced_output = (
            bool(partial_chunks) or (not aborted and bool(assistant_response)) or onboarding_visible_output_started
        )
        return assistant_response, thinking_content, queued_messages, _turn_outcome, produced_output

    except api.WebSocketDisconnect:
        raise
    except Exception as e:
        gen_duration = api.perf_counter() - start_gen
        api.logger.exception(f"[WS] LLM error after {gen_duration:.3f}s: {e}")
        await stop_onboarding_lease()
        if is_onboarding_trigger and onboarding_lease_lost:
            return "", thinking_content, [], "aborted", False
        if is_onboarding_trigger and (partial_chunks or onboarding_visible_output_started):
            partial_response = "".join(partial_chunks).strip()
            if not partial_response:
                partial_response = "*[Welcome generation interrupted]*"
            return (
                (
                    partial_response
                    if partial_response.endswith("*[Welcome generation interrupted]*")
                    else partial_response + "\n\n*[Welcome generation interrupted]*"
                ),
                thinking_content,
                [],
                "failed",
                True,
            )
        return f"[LLM call error] {str(e)[:200]}", [], [], "failed", False
    finally:
        await stop_onboarding_lease()


async def inject_live_preview_and_workspace_metadata_impl(api, self, data: dict):
    try:
        tool_name = data.get("name", "")
        env = api.detect_agentbay_env(tool_name)
        if env == "desktop":
            b64_url = await api.get_desktop_screenshot(self.agent_id, session_id=self.conv_id)
            if b64_url:
                data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                api.logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
        elif env == "browser":
            b64_url = await api.get_browser_snapshot(self.agent_id, session_id=self.conv_id)
            if b64_url:
                data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                api.logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
        elif env == "code":
            tool_result = data.get("result", "") or ""
            data["live_preview"] = {"env": "code", "output": tool_result[:5000]}
    except Exception as _lp_err:
        api.logger.warning(f"[WS][LivePreview] Embed failed: {_lp_err}")

    _workspace_tool_actions = {
        "write_file": "write",
        "edit_file": "edit",
        "move_file": "move",
        "delete_file": "delete",
        "convert_markdown_to_docx": "convert",
        "convert_csv_to_xlsx": "convert",
        "convert_markdown_to_pdf": "convert",
        "convert_html_to_pdf": "convert",
        "convert_html_to_pptx": "convert",
    }
    _done_tool_name = data.get("name", "")
    if _done_tool_name in _workspace_tool_actions:
        _ws_args = data.get("args") or {}
        if isinstance(_ws_args, str):
            try:
                _ws_args = api.json.loads(_ws_args)
            except Exception:
                _ws_args = {}
        _ws_path = _ws_args.get("output_path") or _ws_args.get("destination_path") or _ws_args.get("path", "")
        _ws_result = str(data.get("result") or "")
        _pending_approval = "requires approval" in _ws_result.lower()
        data["workspace_activity"] = {
            "action": _workspace_tool_actions[_done_tool_name],
            "path": _ws_path,
            "tool": _done_tool_name,
            "ok": not _pending_approval,
            "pendingApproval": _pending_approval,
        }
        api.logger.info(f"[WS][Workspace] activity: {_done_tool_name} → {_ws_path}")


async def save_tool_call_to_db_impl(api, self, data: dict, *, turn_anchor_id=None):
    from app.services.chat_history import persist_tool_call

    await persist_tool_call(
        api.async_session,
        agent_id=self.agent_id,
        user_id=self.user_id,
        conversation_id=self.conv_id,
        evt=data,
        turn_anchor_id=turn_anchor_id,
    )
    try:
        async with api.async_session() as _tc_db:
            await api.maybe_mark_session_read_for_active_viewer(
                _tc_db,
                agent_id=self.agent_id,
                session_id=self.conv_id,
                user_id=self.user_id,
            )
            await _tc_db.commit()
    except Exception as _tc_err:
        api.logger.warning(f"[WS] Failed to mark session read: {_tc_err}")


async def update_activity_and_quota_impl(api, self, assistant_response: str):
    try:
        async with api.async_session() as _db:
            _ar = await _db.execute(api.select(api.Agent).where(api.Agent.id == self.agent_id))
            _agent = _ar.scalar_one_or_none()
            if _agent:
                _agent.last_active_at = api.datetime.now(api.tz.utc)
                await _db.commit()
    except Exception as e:
        api.logger.warning(f"[WS] Failed to update last_active_at: {e}")

    try:
        await api.increment_conversation_usage(self.user_id)
        await api.increment_agent_llm_usage(self.agent_id)
    except Exception:
        pass

    try:
        user_text = getattr(self, "current_user_text", "")
        await api.log_activity(
            self.agent_id,
            "chat_reply",
            f"Replied to web chat: {assistant_response[:80]}",
            detail={"channel": "web", "user_text": user_text[:200], "reply": assistant_response[:500]},
        )
    except Exception as e:
        api.logger.warning(f"[WS] Failed to log activity: {e}")


async def create_task_record_impl(api, self, task_title: str, assistant_response: str) -> str:
    if not task_title:
        return assistant_response
    try:
        async with api.async_session() as db:
            task = api.Task(
                agent_id=self.agent_id,
                title=task_title,
                created_by=self.user_id,
                execution_user_id=self.user_id,
                status="pending",
                priority="medium",
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            api.logger.info(f"[WS] Task created: {task.id}")
            task_id = task.id
        api.asyncio.create_task(api.execute_task(task_id, self.agent_id, self.user_id))
        assistant_response += f"\n\n📋 Task synced to task board: [{task_title}]"
    except Exception as te:
        api.logger.error(f"[WS] Task creation failed: {te}")
    return assistant_response


async def save_assistant_reply_impl(
    api,
    self,
    assistant_response: str,
    thinking_content: list[str],
    *,
    message_id=None,
    turn_anchor_id=None,
    turn_status: str = "completed",
    complete_onboarding: bool = False,
) -> bool:
    from app.services.llm.failure_outcome import llm_failure_code

    failure_code = llm_failure_code(assistant_response)
    async with api.async_session() as db:
        if message_id is not None and await db.get(api.ChatMessage, message_id):
            return False
        if turn_anchor_id is not None:
            from app.services.chat_history import lock_turn_anchor_for_finalization, terminal_assistant_tail

            await lock_turn_anchor_for_finalization(
                db,
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
                turn_anchor_id=turn_anchor_id,
                allow_cancelled=turn_status == "cancelled",
            )
            existing_terminal = await db.scalar(
                api.select(api.ChatMessage.id)
                .where(
                    api.ChatMessage.agent_id == self.agent_id,
                    api.ChatMessage.conversation_id == self.conv_id,
                    api.ChatMessage.role == "assistant",
                    api.ChatMessage.message_meta["turn_anchor_id"].as_string() == str(turn_anchor_id),
                    api.ChatMessage.message_meta["turn_status"].as_string().in_(("completed", "failed", "cancelled")),
                )
                .limit(1)
            )
            if existing_terminal is not None:
                return False
            assistant_response, intermediate_ids = await terminal_assistant_tail(
                db,
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
                turn_anchor_id=turn_anchor_id,
                content=assistant_response,
            )
        else:
            intermediate_ids = []
        assistant_msg = api.ChatMessage(
            id=message_id or api.uuid.uuid4(),
            agent_id=self.agent_id,
            user_id=self.user_id,
            role="assistant",
            content=assistant_response,
            conversation_id=self.conv_id,
            thinking="".join(thinking_content) if thinking_content else None,
            message_meta=(
                {
                    "turn_anchor_id": str(turn_anchor_id),
                    "turn_status": turn_status,
                    **({"error_code": failure_code} if failure_code else {}),
                    **({"intermediate_assistant_ids": [str(value) for value in intermediate_ids]} if intermediate_ids else {}),
                    **self._scene_message_meta(),
                }
                if turn_anchor_id is not None
                else self._scene_message_meta()
            ),
        )
        db.add(assistant_msg)
        if turn_anchor_id is not None:
            await api.transition_conversation_turn(
                db,
                agent_id=self.agent_id,
                conversation_id=self.conv_id,
                turn_anchor_id=turn_anchor_id,
                status=turn_status,
            )
            if turn_status in {"completed", "failed"}:
                from app.services.turn_inbox import promote_next_turn_inbox

                session = await db.get(api.ChatSession, api.uuid.UUID(self.conv_id))
                if session is not None:
                    await promote_next_turn_inbox(db, session=session)
        if complete_onboarding:
            completed = await db.execute(
                api.update(api.AgentUserOnboarding)
                .where(
                    api.AgentUserOnboarding.agent_id == self.agent_id,
                    api.AgentUserOnboarding.user_id == self.user_id,
                    api.AgentUserOnboarding.phase == api.PHASE_GREETED,
                )
                .values(phase=api.PHASE_COMPLETED)
            )
            if not completed.rowcount:
                raise RuntimeError("Onboarding state changed before assistant persistence")
        await api.maybe_mark_session_read_for_active_viewer(
            db,
            agent_id=self.agent_id,
            session_id=self.conv_id,
            user_id=self.user_id,
        )
        await db.commit()
    if turn_anchor_id is not None:
        from app.services.turn_inbox import kick_promoted_turn_inbox

        await kick_promoted_turn_inbox(agent_id=self.agent_id, session_id=self.conv_id)
    api.logger.info("[WS] Assistant message saved")
    return True
