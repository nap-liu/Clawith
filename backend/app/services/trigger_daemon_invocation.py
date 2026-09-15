"""Agent invocation flow for claimed triggers."""

from app.services.trigger_daemon_delivery import *  # noqa: F401,F403

async def _invoke_agent_for_triggers(
    agent_id: uuid.UUID, triggers: list[AgentTrigger], *, admit_only: bool = False,
):
    """Invoke an agent with context from one or more fired triggers.

    Creates a Reflection Session and calls the LLM.
    """
    from app.core.okr_feature import partition_retired_okr_triggers
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.services.background_turns import initialize_background_turn, run_background_turn
    from app.services.trigger_turn_completion import trigger_completion_snapshot
    from app.services.turn_interruption import TurnInterrupted

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
    turn_anchor_id = None

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
            turn_anchor_id = uuid.uuid5(_ONMESSAGE_TURN_NAMESPACE, f"anchor:{execution_ids[0]}")
            reply = await _resume_origin_session_for_on_message(
                agent_id,
                triggers[0],
                tenant_id=tenant_key,
            )
            from app.services.llm.failure_outcome import llm_failure_code

            failure_code = llm_failure_code(reply)
            if failure_code:
                invocation_error = str(reply)
                invocation_retryable = False
            return

        # Reclaimed executions continue their committed turn, never a new reflection.
        if execution_ids:
            async with async_session() as db:
                linked = (await db.execute(
                    select(TriggerExecution).where(TriggerExecution.id.in_(execution_ids))
                )).scalars().all()
                linked_ids = {row.conversation_id for row in linked if row.conversation_id}
                if len(linked_ids) > 1:
                    raise RuntimeError("Trigger invocation has inconsistent conversations")
                if linked_ids:
                    conversation_id = next(iter(linked_ids))
                    turn_anchor = (await db.execute(
                        select(ChatMessage).where(
                            ChatMessage.conversation_id == str(conversation_id),
                            ChatMessage.role == "user",
                            ChatMessage.message_meta["background_execution"]["kind"].as_string() == "trigger",
                        ).order_by(ChatMessage.created_at.asc()).limit(1)
                    )).scalar_one_or_none()
                    if turn_anchor is None:
                        raise RuntimeError("Trigger execution has no recoverable turn anchor")
                    turn_anchor_id = turn_anchor.id
            if turn_anchor_id:
                reply = await run_background_turn(turn_anchor_id)
                return

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
                override_reasoning_effort=triggers[0].reasoning_effort,
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
                        f'\nMatched message from {cfg.get("_matched_from", "?")}:\n"{cfg["_matched_message"]}"'
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
                id=uuid.uuid5(_ONMESSAGE_TURN_NAMESPACE, f"session:{execution_ids[0]}") if execution_ids else uuid.uuid4(),
                agent_id=agent_id,
                user_id=execution_user_id,
                participant_id=agent_participant.id if agent_participant else None,
                source_channel="trigger",
                title=title[:200],
            )
            db.add(session)
            await db.flush()
            session_id = session.id

            # Store trigger context as a message in the session
            turn_anchor = ChatMessage(
                id=uuid.uuid5(_ONMESSAGE_TURN_NAMESPACE, f"anchor:{execution_ids[0]}") if execution_ids else uuid.uuid4(),
                agent_id=agent_id,
                conversation_id=str(session_id),
                role="user",
                content=trigger_context,
                user_id=execution_user_id,
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
            await db.flush()
            turn_anchor.sender_user_id = None
            await initialize_background_turn(
                db,
                session=session,
                anchor=turn_anchor,
                kind="trigger",
                reference_id=str(execution_ids[0] if execution_ids else turn_anchor.id),
                settings={
                    "execution_agent_id": str(agent_id),
                    "model_override_id": str(triggers[0].model_id) if triggers[0].model_id else None,
                    "temperature_override": triggers[0].temperature,
                    "reasoning_effort_override": triggers[0].reasoning_effort,
                    "include_soul": triggers[0].soul,
                    "include_memory": triggers[0].memory,
                },
                completion={
                    "execution_ids": [str(value) for value in execution_ids],
                    "triggers": trigger_completion_snapshot(triggers),
                },
            )
            turn_anchor_id = turn_anchor.id
            await db.commit()
            # Treat the link as durable only after the session, first message,
            # and execution FK have committed atomically.
            conversation_id = session_id

        if admit_only:
            return turn_anchor_id
        reply = await run_background_turn(turn_anchor_id)
    except (asyncio.CancelledError, TurnInterrupted):
        # Process shutdown is not STOP. Leave its durable anchor and webhook
        # batch untouched; a new worker claims the same execution and Session.
        invocation_error = "Execution interrupted; awaiting recovery"
        invocation_retryable = True
        raise
    except Exception as exc:
        invocation_error = str(exc)
        if turn_anchor_id:
            async with async_session() as db:
                admitted = await db.get(ChatMessage, turn_anchor_id)
                if admitted is None or not (admitted.message_meta or {}).get("background_execution"):
                    turn_anchor_id = None
        invocation_retryable = bool(turn_anchor_id) or _is_retryable_invocation_error(exc)
        if conversation_id is None and execution_ids and not invocation_retryable:
            conversation_id = await _create_failed_trigger_conversation(agent_id, triggers, invocation_error)
        logger.exception("Failed to invoke agent {} for triggers", agent_id)
        if admit_only:
            raise
    finally:
        if lease_heartbeat_task is not None:
            lease_heartbeat_task.cancel()
            await asyncio.gather(lease_heartbeat_task, return_exceptions=True)
        # Once initialized, the shared turn finalizer owns business completion.
        # Retry release is safe even if a terminal commit won concurrently.
        if execution_ids and (turn_anchor_id is None or invocation_retryable):
            try:
                await _finalize_invocation_executions(
                    execution_ids, triggers, reply, invocation_error,
                    invocation_retryable, conversation_id,
                )
            except Exception:
                logger.exception("Failed to release trigger executions {}", execution_ids)


__all__ = [name for name in globals() if not name.startswith("__")]
