"""Agent invocation flow for claimed triggers."""

from app.services.trigger_daemon_delivery import *  # noqa: F401,F403

async def _invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTrigger]):
    """Invoke an agent with context from one or more fired triggers.

    Creates a Reflection Session and calls the LLM.
    """
    from app.core.okr_feature import partition_retired_okr_triggers
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.services.audit_logger import write_audit_log
    from app.services.llm import call_llm

    retired_triggers, active_triggers = partition_retired_okr_triggers(triggers)
    if retired_triggers:
        retired_execution_ids: list[uuid.UUID] = []
        for trigger in retired_triggers:
            execution_id = (trigger.config or {}).get("_execution_id")
            if execution_id:
                try:
                    retired_execution_ids.append(uuid.UUID(str(execution_id)))
                except (ValueError, TypeError):
                    pass
        if retired_execution_ids:
            await mark_trigger_executions_completed(retired_execution_ids)
        triggers = active_triggers
        logger.info(
            "Skipped %s retired system trigger(s) for agent %s",
            len(retired_triggers),
            agent_id,
        )
        if not triggers:
            return

    # Each runtime trigger carries the id of the leased TriggerExecution it was
    # claimed from (build_execution_runtime_trigger injects `_execution_id`). The
    # lease is held in status="processing" with a 5-minute expiry; if we never
    # finalize it, claim_pending_trigger_executions re-grabs the expired lease and
    # the trigger re-fires forever. Finalize every claimed execution below.
    execution_ids: list[uuid.UUID] = []
    for _t in triggers:
        _cfg = _t.config if isinstance(_t.config, dict) else {}
        _eid = _cfg.get("_execution_id")
        if _eid:
            try:
                execution_ids.append(uuid.UUID(str(_eid)))
            except (ValueError, TypeError):
                pass
    invocation_error: str | None = None
    invocation_retryable = False
    reply: str | None = None
    conversation_id: uuid.UUID | None = None
    lease_heartbeat_task: asyncio.Task | None = None
    capacity_stack = AsyncExitStack()

    if execution_ids:

        async def _lease_heartbeat() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    await renew_trigger_execution_leases(execution_ids)
                except Exception as exc:
                    logger.warning("Failed to renew trigger execution leases %s: %s", execution_ids, exc)

        lease_heartbeat_task = asyncio.create_task(_lease_heartbeat())

    try:
        configured_execution_users = {trigger.execution_user_id for trigger in triggers if trigger.execution_user_id}
        if len(configured_execution_users) > 1:
            raise RuntimeError("A trigger invocation cannot mix execution users")
        configured_execution_user_id = next(iter(configured_execution_users), None)
        async with async_session() as identity_db:
            identity_agent = await identity_db.get(Agent, agent_id)
            if identity_agent is None:
                raise RuntimeError("Agent is unavailable")
            if is_project_agent(identity_agent):
                try:
                    project_running = await project_agent_runtime_allows(
                        identity_db,
                        identity_agent,
                    )
                except Exception as exc:  # noqa: BLE001 - retry project-only failure
                    invocation_error = f"Project runtime check failed: {exc}"
                    invocation_retryable = True
                    return
                if not project_running:
                    invocation_error = "Project runtime is paused"
                    invocation_retryable = True
                    return
            from app.services.execution_identity import resolve_execution_user_id

            execution_user_id = await resolve_execution_user_id(
                identity_db,
                identity_agent,
                configured_execution_user_id,
                legacy_user_id=identity_agent.creator_id,
            )
            tenant_key = (
                getattr(identity_agent, "company_id", None)
                or getattr(identity_agent, "tenant_id", None)
                or execution_user_id
                or agent_id
            )

        if (
            len(triggers) == 1
            and triggers[0].type == "on_message"
            and (triggers[0].config or {}).get("_origin_session_id")
            and (triggers[0].config or {}).get("_matched_message_id")
            and (triggers[0].config or {}).get("_execution_id")
        ):
            origin_conversation_id = uuid.UUID(str((triggers[0].config or {})["_origin_session_id"]))
            await _link_invocation_executions(execution_ids, origin_conversation_id)
            conversation_id = origin_conversation_id
            trigger_config = dict(triggers[0].config or {})
            trigger_config["_execution_user_id"] = str(execution_user_id)
            triggers[0].config = trigger_config
            await _resume_origin_session_for_on_message(
                agent_id,
                triggers[0],
                tenant_id=tenant_key,
            )
            return

        # Admission is deliberately outside the identity transaction. A busy
        # scheduled lane can queue without consuming a database connection.
        await capacity_stack.enter_async_context(get_workload_capacity().slot(WorkloadKind.SCHEDULED, tenant_key))

        async with async_session() as db:
            # Load agent
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent or agent.is_expired:
                raise RuntimeError("Agent is unavailable or expired")
            if is_project_agent(agent):
                try:
                    project_running = await project_agent_runtime_allows(db, agent)
                except Exception as exc:  # noqa: BLE001 - retry project-only failure
                    invocation_error = f"Project runtime recheck failed: {exc}"
                    invocation_retryable = True
                    return
                if not project_running:
                    invocation_error = "Project runtime is paused"
                    invocation_retryable = True
                    return
            from app.core.okr_feature import is_retired_okr_agent

            if await is_retired_okr_agent(db, agent):
                return

            from app.services.chat_model_selection import (
                MODEL_OVERRIDE_OK,
                resolve_runtime_models,
            )

            runtime_models = await resolve_runtime_models(
                db,
                agent=agent,
                override_model_id=triggers[0].model_id,
                override_temperature=triggers[0].temperature,
            )
            if triggers[0].model_id and runtime_models.override_status != MODEL_OVERRIDE_OK:
                raise RuntimeError("Trigger model override is unavailable")
            model = runtime_models.primary_model
            if model is None:
                raise RuntimeError(f"Agent {agent.name} has no LLM model")

            # Build trigger context. Keep this model-facing prompt in English so
            # autonomous wakeups behave consistently across UI locales.
            context_parts = []
            trigger_names = []
            context_executed_at = datetime.now(timezone.utc)
            for t in triggers:
                part = f"Trigger: {t.name} ({t.type})\nReason: {t.reason}"
                if t.name == "daily_okr_collection":
                    part += (
                        "\nExecution requirements: First call get_okr_settings to confirm whether daily report collection is enabled. "
                        "If it is enabled, only contact members and digital employees in your relationship network to collect today's final daily reports, "
                        "then organize them into a formal daily report no longer than 2000 characters. "
                        "If it is disabled, state that no action is needed and stop."
                    )
                elif t.name in ("daily_okr_report", "weekly_okr_report", "monthly_okr_report"):
                    part += (
                        "\nExecution requirements: This company-level report is generated automatically by the system. "
                        "If you are awakened, only add necessary clarification. Do not start another member collection round."
                    )
                elif t.name == "biweekly_okr_checkin":
                    part += (
                        "\nExecution requirements: First call get_okr_settings to confirm whether OKR is enabled. "
                        "If enabled, check the current-cycle company and member OKRs, then proactively remind members who have not set OKRs or whose progress is lagging. "
                        "If disabled, state that no action is needed and stop."
                    )
                elif t.name == "monthly_okr_report":
                    part += (
                        "\nExecution requirements: First call get_okr_settings to confirm whether OKR is enabled. "
                        "If enabled, call generate_monthly_okr_report to generate the OKR monthly report for the month that just ended, "
                        "then send it to admins. If disabled, state that no action is needed and stop."
                    )
                if t.focus_ref:
                    part += f"\nRelated Focus: {t.focus_ref}"
                # Include matched message for on_message triggers
                cfg = t.config or {}
                if t.type == "cron":
                    part += format_cron_timing_context(cfg, context_executed_at)
                if t.type == "on_message" and cfg.get("_matched_message"):
                    part += (
                        f'\nMatched message from {cfg.get("_matched_from", "?")}:\n"{cfg["_matched_message"][:500]}"'
                    )
                if t.type == "on_message" and cfg.get("okr_member_id") and cfg.get("okr_report_date"):
                    part += (
                        "\nExecution requirements: This is a daily-report reply ingestion event."
                        f"\n1. Organize the other party's reply into a final daily report no longer than 2000 characters."
                        f'\n2. Immediately call upsert_member_daily_report(report_date="{cfg["okr_report_date"]}", '
                        f'member_type="{cfg.get("okr_member_type", "user")}", '
                        f'member_id="{cfg["okr_member_id"]}", content="<organized daily report>").'
                        "\n3. After the tool call succeeds, send a brief confirmation that you received and recorded it."
                        "\n4. Do not only confirm without calling the tool, and do not store the raw long conversation verbatim as the daily report."
                    )
                # Include webhook payload (by mode)
                if t.type == "webhook":
                    inbox_context = format_webhook_inbox_context(cfg)
                    if inbox_context:
                        part += inbox_context
                    else:
                        wmode = cfg.get("webhook_mode", "legacy")
                        if wmode == "legacy":
                            payload_str = cfg.get("_webhook_payload")
                            if payload_str:
                                part += f"\nWebhook Payload:\n{payload_str}"
                        elif wmode == "queue":
                            q = cfg.get("_webhook_queue") or []
                            if q and isinstance(q[0], str):
                                part += f"\nWebhook Payload:\n{q[0]}"
                        elif wmode == "merge":
                            # Render the same legacy inline batch that completion
                            # will remove. New inbox events are execution-owned
                            # references handled above and need no fresh read.
                            async with async_session() as _wdb:
                                _wres = await _wdb.execute(
                                    select(AgentTrigger).where(AgentTrigger.id == t.id)
                                )
                                _wtrig = _wres.scalar_one_or_none()
                            _fresh_cfg = (_wtrig.config if _wtrig else cfg) or {}
                            _q = _fresh_cfg.get("_webhook_queue") or []
                            _bs = _fresh_cfg.get("_webhook_batch_size", len(_q))
                            _batch = [item for item in _q[:_bs] if isinstance(item, str)]
                            if _batch:
                                merged = _merge_webhook_payloads(_batch)
                                part += f"\nWebhook Payload (merged, {len(_batch)} entries):\n{merged}"
                context_parts.append(part)
                trigger_names.append(t.name)

            trigger_context = (
                "===== Wake Context =====\n"
                f"Wake source: trigger ({'multiple triggers fired together' if len(triggers) > 1 else 'trigger fired'})\n\n"
                + "\n---\n".join(context_parts)
                + "\n========================"
            )

            # Create Reflection Session
            title = f"🤖 Reflection: {', '.join(trigger_names)}"
            # Find agent's participant
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            session = ChatSession(
                agent_id=agent_id,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                source_channel="trigger",
                title=title[:200],
            )
            db.add(session)
            await db.flush()
            session_id = session.id

            # Messages: trigger context only (call_llm builds system prompt internally)
            messages = [
                {"role": "user", "content": trigger_context},
            ]

            # Store trigger context as a message in the session
            turn_anchor = ChatMessage(
                agent_id=agent_id,
                conversation_id=str(session_id),
                role="user",
                content=trigger_context,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
            )
            db.add(turn_anchor)
            if execution_ids:
                execution_rows = (
                    (await db.execute(select(TriggerExecution).where(TriggerExecution.id.in_(execution_ids))))
                    .scalars()
                    .all()
                )
                for execution in execution_rows:
                    execution.conversation_id = session_id
            await db.commit()
            # Treat the link as durable only after the session, first message,
            # and execution FK have committed atomically.
            conversation_id = session_id
            # Cache participant ID for callbacks
            agent_participant_id = agent_participant.id if agent_participant else None

        # Call LLM (outside the DB session to avoid long transactions)
        collected_content = []
        collected_thinking = []
        delivered_platform_message_via_tool = False

        async def on_chunk(text):
            collected_content.append(text)

        # Collect reasoning/thinking for UI persistence — shown when a human
        # views the session, NEVER fed back into the LLM. Mirrors web chat.
        async def on_thinking(text):
            collected_thinking.append(text)

        # Persist tool calls into Reflection Session for Reflections visibility
        async def on_tool_call(data):
            nonlocal delivered_platform_message_via_tool
            try:
                tool_name = data.get("name")
                tool_status = data.get("status")
                if tool_status == "done" and tool_name == "send_platform_message":
                    result_text = str(data.get("result", ""))
                    if result_text.startswith("✅"):
                        delivered_platform_message_via_tool = True

                async with async_session() as _tc_db:
                    if data["status"] == "running":
                        # Store args RAW — this row is replayed into the LLM context;
                        # masking here poisons the model (it copies "******" back into
                        # tool calls). Secrets are masked at output boundaries only.
                        _tc_db.add(
                            ChatMessage(
                                agent_id=agent_id,
                                conversation_id=str(session_id),
                                role="tool_call",
                                content=_json.dumps(
                                    {"name": data["name"], "args": data.get("args")}, ensure_ascii=False, default=str
                                ),
                                user_id=agent.creator_id,
                                participant_id=agent_participant_id,
                            )
                        )
                    elif data["status"] == "done":
                        # data["result"] is already the bounded llm_view
                        # from tool_output_store.finalize_tool_output.
                        result_str = str(data.get("result", ""))
                        _tc_db.add(
                            ChatMessage(
                                agent_id=agent_id,
                                conversation_id=str(session_id),
                                role="tool_call",
                                content=_json.dumps(
                                    {"name": data["name"], "result": result_str}, ensure_ascii=False, default=str
                                ),
                                user_id=agent.creator_id,
                                participant_id=agent_participant_id,
                            )
                        )
                    await _tc_db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist tool call for trigger session: {e}")

        reply = await call_llm(
            model=model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=execution_user_id,
            session_id=str(session_id),
            on_chunk=on_chunk,
            on_tool_call=on_tool_call,
            on_thinking=on_thinking,
            turn_anchor_id=turn_anchor.id,
            turn_type="trigger",
            include_soul=triggers[0].soul,
            include_memory=triggers[0].memory,
            # A2A wake uses the agent's own max_tool_rounds setting (no override)
        )

        # Cap the turn's accumulated thinking once; reused by all assistant rows
        # persisted below (Reflection / A2A mirror / delivery). UI-only field.
        from app.services.chat_history import (
            cap_thinking,
            lock_turn_anchor_for_finalization,
        )

        _capped_thinking = cap_thinking("".join(collected_thinking))

        # Compute final reply text once
        final_reply = reply or "".join(collected_content)

        # Save assistant reply to Reflection session
        async with async_session() as db:
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            await lock_turn_anchor_for_finalization(
                db,
                agent_id=agent_id,
                conversation_id=str(session_id),
                turn_anchor_id=turn_anchor.id,
            )
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    conversation_id=str(session_id),
                    role="assistant",
                    content=final_reply,
                    user_id=agent.creator_id,
                    participant_id=agent_participant.id if agent_participant else None,
                    thinking=_capped_thinking,
                    message_meta={
                        "turn_anchor_id": str(turn_anchor.id),
                        "turn_status": "completed",
                    },
                )
            )

            # NOTE: trigger state (last_fired_at, fire_count, auto-disable)
            # is already updated in _tick() BEFORE this task was launched,
            # to prevent race-condition duplicate fires.

            await db.commit()

        # ── Save reply to A2A session if this was an agent-to-agent wake ──
        # This makes the target agent's reply visible in the A2A chat history
        for t in triggers:
            a2a_sid = (t.config or {}).get("_a2a_session_id")
            if a2a_sid and final_reply:
                try:
                    async with async_session() as db:
                        from app.models.participant import Participant as _P

                        _p_r = await db.execute(select(_P).where(_P.type == "agent", _P.ref_id == agent_id))
                        _p = _p_r.scalar_one_or_none()
                        from app.models.chat_session import ChatSession as _CS

                        _cs_r = await db.execute(select(_CS).where(_CS.id == uuid.UUID(a2a_sid)))
                        _cs = _cs_r.scalar_one_or_none()
                        _source_execution_id = str((t.config or {}).get("_execution_id") or "").strip()
                        _reply_id = (
                            uuid.uuid5(
                                _ONMESSAGE_TURN_NAMESPACE,
                                f"a2a-reply:{_source_execution_id}",
                            )
                            if _source_execution_id
                            else uuid.uuid4()
                        )
                        _existing_reply = await db.get(ChatMessage, _reply_id)
                        if _existing_reply is not None:
                            logger.info(
                                "[A2A] Reply already persisted for execution %s",
                                _source_execution_id,
                            )
                            break
                        reply_row = ChatMessage(
                            id=_reply_id,
                            agent_id=_cs.agent_id if _cs else agent_id,
                            conversation_id=a2a_sid,
                            role="assistant",
                            content=final_reply,
                            user_id=agent.creator_id,
                            participant_id=_p.id if _p else None,
                            thinking=_capped_thinking,
                            external_event_key=(
                                f"a2a-inbound:{_source_execution_id}"[:500] if _source_execution_id else None
                            ),
                            message_meta={
                                "direction": "inbound",
                                "source_channel": "agent",
                                "actor_ref": str(_p.id if _p else agent_id),
                            },
                        )
                        db.add(reply_row)
                        # Update session timestamp
                        if _cs:
                            _cs.last_message_at = datetime.now(timezone.utc)
                            await db.flush()
                            from app.services.trigger_runtime.evaluator import (
                                match_incoming_chat_message,
                            )

                            await match_incoming_chat_message(db, reply_row, _cs)
                        await db.commit()
                        logger.info(f"[A2A] Saved reply to A2A session {a2a_sid}")
                except Exception as e:
                    logger.warning(f"[A2A] Failed to save reply to A2A session {a2a_sid}: {e}")
                break  # Only save once

        # Route trigger results to a single deterministic destination. Pure reflection/system
        # wakes stay inside the reflection session and should not spill into arbitrary user chats.
        is_a2a_internal = all(t.name == "a2a_wake" for t in triggers)
        delivery_target = None if is_a2a_internal else await _resolve_trigger_delivery_target(agent, triggers)

        if final_reply and delivery_target and not delivered_platform_message_via_tool:
            try:
                from app.api.websocket import manager as ws_manager

                agent_id_str = str(agent_id)

                # Build notification message with trigger badge
                trigger_reasons = []
                for t in triggers:
                    ns = (t.config or {}).get("_notification_summary", "").strip()
                    if ns:
                        trigger_reasons.append(ns)
                    else:
                        r = (t.reason or "").strip()
                        if r and len(r) <= 80:
                            trigger_reasons.append(r)
                        elif r:
                            trigger_reasons.append(r[:77] + "...")
                summary = trigger_reasons[0] if trigger_reasons else "有新的事件需要处理"

                _is_a2a_wait = any(t.name.startswith("a2a_wait_") for t in triggers)
                if _is_a2a_wait:
                    import re as _re

                    cleaned = final_reply
                    _internal_patterns = [
                        r"\b(a2a_wait_\w+|a2a_wake)\b",
                        r"\bwait_?\w+_?(task|reply|followup|meeting|sync|api_key)\w*\b",
                        r"\bresolve_\w+\b",
                        r"\bfocus[_ ]?item\b",
                        r"\btask_delegate\b",
                        r"\bfocus_ref\b",
                        r"✅\s*(a2a\w+|wait\w+|触发器\w*|focus\w*).*(?:已取消|已为|保持|活跃|完成状态)[^\n]*",
                        r"[\-•]\s*(?:触发器|trigger|focus|wait_\w+|a2a\w+).*[^\n]*",
                        r"(?:触发器|trigger)\s+\S+\s*(?:已取消|保持活跃|已为完成状态|fired)",
                        r"已静默清理触发器",
                        r"已静默处理完毕",
                        r"继续待命[。，]?\s*",
                        r"，?\s*(?:继续)?待命。",
                    ]
                    for _pat in _internal_patterns:
                        cleaned = _re.sub(_pat, "", cleaned, flags=_re.IGNORECASE)
                    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned).strip()
                    cleaned = _re.sub(r"[。，]\s*$", "", cleaned).strip()
                    if not cleaned:
                        cleaned = final_reply
                else:
                    cleaned = final_reply

                notification = f"⚡ {summary}\n\n{cleaned}"

                target_session_id = delivery_target["session_id"]
                owner_user_id = delivery_target.get("owner_user_id")

                # Save to the resolved destination session for persistence.
                async with async_session() as db:
                    from app.api.websocket import maybe_mark_session_read_for_active_viewer
                    from app.models.chat_session import ChatSession

                    db.add(
                        ChatMessage(
                            agent_id=agent_id,
                            conversation_id=target_session_id,
                            role="assistant",
                            content=notification,
                            user_id=agent.creator_id,
                            thinking=_capped_thinking,
                        )
                    )
                    session_row = await db.get(ChatSession, uuid.UUID(target_session_id))
                    if session_row:
                        session_row.last_message_at = datetime.now(timezone.utc)
                    if owner_user_id:
                        await maybe_mark_session_read_for_active_viewer(
                            db,
                            agent_id=agent_id,
                            session_id=target_session_id,
                            user_id=uuid.UUID(owner_user_id),
                        )
                    await db.commit()

                payload = {
                    "type": "trigger_notification",
                    "content": notification,
                    "triggers": [t.name for t in triggers],
                    "session_id": target_session_id,
                }

                # Notify only the user who owns the destination session. The frontend will append
                # the message only when that exact session is open; otherwise it just refreshes
                # unread/session state.
                if owner_user_id:
                    await ws_manager.send_to_user(agent_id_str, owner_user_id, payload)
            except Exception as e:
                logger.error(f"Failed to push trigger result to WebSocket: {e}")
                import traceback

                traceback.print_exc()

        # Audit log
        await write_audit_log(
            "trigger_fired",
            {
                "agent_name": agent.name,
                "triggers": [{"name": t.name, "type": t.type} for t in triggers],
            },
            agent_id=agent_id,
        )

        logger.info(f"⚡ Triggers fired for {agent.name}: {[t.name for t in triggers]}")

    except asyncio.CancelledError:
        invocation_error = "cancelled by control plane"
        invocation_retryable = False
        raise
    except Exception as e:
        invocation_error = str(e)
        invocation_retryable = _is_retryable_invocation_error(e)
        if conversation_id is None and execution_ids and not invocation_retryable:
            conversation_id = await _create_failed_trigger_conversation(
                agent_id,
                triggers,
                invocation_error,
            )
        logger.error(f"Failed to invoke agent {agent_id} for triggers: {e}")
        import traceback

        traceback.print_exc()
    finally:
        if lease_heartbeat_task is not None:
            lease_heartbeat_task.cancel()
            await asyncio.gather(lease_heartbeat_task, return_exceptions=True)
        # Release the lease on every claimed execution so it is not re-fired.
        # Runs on success, on early return (agent expired / model disabled), and
        # on exception. Early returns leave invocation_error=None → completed,
        # which is correct: the trigger was handled (decided to skip), so re-firing
        # would not help.
        if execution_ids:
            try:
                await _finalize_invocation_executions(
                    execution_ids,
                    triggers,
                    reply,
                    invocation_error,
                    invocation_retryable,
                    conversation_id,
                )
            except Exception as _mark_err:
                logger.warning(
                    f"Failed to finalize trigger executions {execution_ids} for agent {agent_id}: {_mark_err}"
                )
        await capacity_stack.aclose()


# ── Main Tick Loop ──────────────────────────────────────────────────

__all__ = [name for name in globals() if not name.startswith("__")]
