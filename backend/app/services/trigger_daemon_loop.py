"""Trigger loading, daemon ticks, and public wake entry points."""

from app.services.trigger_daemon_invocation import *  # noqa: F401,F403
from app.services.agent_execution.bridge import supervised_operation

async def _load_enabled_triggers_for_scope(*, project_agents: bool) -> list[AgentTrigger]:
    """Load one Agent scope without coupling standard triggers to projects."""

    async with async_session() as db:
        statement = select(AgentTrigger).join(Agent, Agent.id == AgentTrigger.agent_id).where(
            AgentTrigger.is_enabled.is_(True)
        )
        if project_agents:
            from app.models.project import Project

            statement = statement.join(Project, Project.id == Agent.project_id).where(
                Agent.scope == "project",
                Project.status == "running",
            )
        else:
            statement = statement.where(Agent.scope != "project")

        result = await db.execute(statement)
        triggers = result.scalars().all()
        # Expunge each object before session.close() is called.
        # session.close() expires all objects still in the identity map;
        # explicit expunge() detaches them WITHOUT expiry so their scalar
        # attributes remain readable outside the session context.
        for trigger in triggers:
            db.expunge(trigger)
    return list(triggers)


async def _load_enabled_triggers() -> list[AgentTrigger]:
    """Keep standard triggers available when project loading fails."""

    standard = await _load_enabled_triggers_for_scope(project_agents=False)
    try:
        project = await _load_enabled_triggers_for_scope(project_agents=True)
    except Exception as exc:  # noqa: BLE001 - isolate optional project runtime
        logger.error("Project trigger loading failed; standard triggers remain available: {}", exc)
        project = []
    return [*standard, *project]


async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    new_trace_id()
    now = datetime.now(timezone.utc)

    all_triggers = await _load_enabled_triggers()

    if not all_triggers:
        return

    # Evaluate and enqueue due triggers. Agent invocation happens only after
    # executions are claimed through the distributed execution queue.
    for trigger in all_triggers:
        # Auto-disable expired triggers
        if trigger.expires_at and now >= trigger.expires_at:
            async with async_session() as db:
                result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                t = result.scalar_one_or_none()
                if t:
                    t.is_enabled = False
                    await db.commit()
            continue

        try:
            if not _is_trigger_eligible(trigger, now):
                continue
            cron_occurrence = None
            if trigger.type == "cron":
                cron_occurrence = await compute_next_trigger_cron_occurrence(trigger)
            if await _evaluate_trigger(
                trigger,
                now,
                cron_occurrence=cron_occurrence,
            ):
                handled = await _handle_okr_report_trigger(
                    trigger,
                    now,
                    scheduled_for=(cron_occurrence.scheduled_for if cron_occurrence is not None else None),
                    local_scheduled_for=(cron_occurrence.local_scheduled_for if cron_occurrence is not None else None),
                )
                if not handled:
                    handled = await _handle_okr_collection_trigger(trigger, now)
                if not handled:
                    # Fix 3: Rate limit on_message triggers per agent
                    if trigger.type == "on_message":
                        agent_fires = _on_msg_fire_log.get(trigger.agent_id, [])
                        cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
                        recent = [t for t in agent_fires if t > cutoff]
                        if len(recent) >= _ON_MSG_RATE_LIMIT:
                            logger.warning(
                                f"[A2A Safety] Agent {trigger.agent_id} hit "
                                f"on_message rate limit ({_ON_MSG_RATE_LIMIT}/hr). "
                                f"Auto-disabling trigger '{trigger.name}'."
                            )
                            async with async_session() as db:
                                result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                                t_obj = result.scalar_one_or_none()
                                if t_obj:
                                    t_obj.is_enabled = False
                                    await db.commit()
                            continue
                        recent.append(now)
                        _on_msg_fire_log[trigger.agent_id] = recent
                    await enqueue_due_trigger(
                        trigger,
                        now,
                        scheduled_for=(cron_occurrence.scheduled_for if cron_occurrence is not None else None),
                        scheduled_timezone=(cron_occurrence.timezone_name if cron_occurrence is not None else None),
                    )
        except Exception as e:
            logger.warning(f"Error evaluating trigger {trigger.name}: {e}")

    # Claim queued executions with a DB lease so only one worker handles each event.
    try:
        fired_by_invocation, force_invoke = await claim_ready_trigger_invocations(now)
    except Exception as e:
        logger.warning(f"Failed to claim trigger executions: {e}")
        fired_by_invocation = {}
        force_invoke = set()

    # Invoke each independent execution.  on_message buckets are force-invoked
    # and are serialized by their exact origin session in the invocation path.
    for invocation_key, agent_triggers in fired_by_invocation.items():
        (
            agent_id,
            _execution_user_id,
            _bucket,
            _model_id,
            _temperature,
            _reasoning_effort,
            _soul,
            _memory,
        ) = invocation_key
        last = _last_invoke.get(invocation_key)
        if invocation_key not in force_invoke and last and (now - last).total_seconds() < DEDUP_WINDOW:
            continue  # Skip — invoked too recently
        _last_invoke[invocation_key] = now

        # Trigger state (last_fired_at / fire_count / single-shot auto-disable /
        # legacy-webhook `_webhook_pending` clear) is updated atomically at claim
        # time by claim_pending_trigger_executions → apply_base_trigger_fired_state,
        # BEFORE this loop runs — which is what stops a long-running trigger from
        # re-firing on the next tick. Every runtime trigger reaching this point
        # carries an `_execution_id`, so the old inline pre-update block here was
        # unreachable dead code (it `continue`d on `_execution_id`). Worse, that
        # dead copy was the ONLY place clearing legacy `_webhook_pending`, so once
        # the lease path took over, legacy webhooks re-fired every cooldown
        # forever. Removed to keep a single source of truth in executions.py.
        asyncio.create_task(_invoke_agent_for_triggers(agent_id, agent_triggers))


@supervised_operation
async def wake_agent_with_context(
    agent_id: uuid.UUID,
    message_context: str,
    *,
    from_agent_id: uuid.UUID | None = None,
    skip_dedup: bool = False,
    a2a_session_id: str | None = None,
) -> None:
    """Public API: wake an agent asynchronously with a message context.

    Creates a synthetic trigger invocation so the agent processes the
    message in a Reflection Session via the standard trigger path.
    If a2a_session_id is provided, the agent's reply will also be saved
    to the A2A chat session for visibility in the admin chat history.
    Safe to call from any async context.

    Args:
        agent_id: The agent to wake.
        message_context: The message to deliver.
        from_agent_id: The agent that initiated this wake (for chain depth tracking).
        skip_dedup: If True, bypass the dedup window check.
        a2a_session_id: Optional A2A chat session ID to mirror the reply into.
    """
    now = datetime.now(timezone.utc)

    if from_agent_id:
        chain_key = f"{from_agent_id}->{agent_id}"
        current_depth = _A2A_WAKE_CHAIN.get(chain_key, 0)
        if current_depth >= _A2A_MAX_WAKE_DEPTH:
            logger.warning(
                f"[A2A] Wake chain depth {current_depth} reached for {chain_key}, stopping to prevent wake storm"
            )
            return

        _A2A_WAKE_CHAIN[chain_key] = current_depth + 1

        def _decay_chain():
            _A2A_WAKE_CHAIN.pop(chain_key, None)

        asyncio.get_running_loop().call_later(_A2A_WAKE_CHAIN_TTL, _decay_chain)

    if not skip_dedup and agent_id in _last_invoke:
        elapsed = (now - _last_invoke[agent_id]).total_seconds()
        if elapsed < DEDUP_WINDOW:
            logger.info(
                f"[A2A] Skipping wake for agent {agent_id} — invoked {elapsed:.0f}s ago (dedup window {DEDUP_WINDOW}s)"
            )
            return

    _last_invoke[agent_id] = now

    from_agent_name = ""
    if from_agent_id:
        try:
            async with async_session() as db:
                from app.models.agent import Agent as AgentModel

                r = await db.execute(select(AgentModel.name).where(AgentModel.id == from_agent_id))
                from_agent_name = r.scalar() or ""
        except Exception as e:
            logger.warning(f"Failed to lookup sender agent name: {e}")

    dummy_trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="a2a_wake",
        type="on_message",
        config={
            "from_agent_name": from_agent_name,
            "_matched_message": message_context[:2000],
            "_matched_from": "agent",
            "_a2a_session_id": a2a_session_id,
        },
        reason=(
            "You received a notification from another agent. "
            "Read the message content above, update your focus and memory if needed, "
            "and take any action you deem necessary. "
            "Do NOT reply back to the sender unless you have a genuine question — "
            "this was a notification, not a request for response."
        ),
        is_enabled=True,
        last_fired_at=now,
        fire_count=0,
    )
    asyncio.create_task(_invoke_agent_for_triggers(agent_id, [dummy_trigger]))


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup."""
    logger.info("⚡ Trigger Daemon started (15s tick, heartbeat every ~60s)")
    _heartbeat_counter = 0
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error(f"Trigger Daemon error: {e}")
            import traceback

            traceback.print_exc()

        # Run heartbeat check every 4th tick (~60 seconds)
        _heartbeat_counter += 1
        if _heartbeat_counter >= 4:
            _heartbeat_counter = 0
            _cleanup_stale_invoke_cache()
            try:
                from app.services.heartbeat import _heartbeat_tick

                await _heartbeat_tick()
            except Exception as e:
                logger.error(f"Heartbeat tick error: {e}")

        await asyncio.sleep(TICK_INTERVAL)

__all__ = [name for name in globals() if not name.startswith("__")]
