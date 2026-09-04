"""Claimed subagent execution entrypoints."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_tools import prepare_subagent_tools
from app.services.subagent_runtime_worker_claim import _claim_subagent
from app.services.subagent_runtime_worker_resume import _finish_subagent_turn

async def execute_claimed_subagent(
    run_id: uuid.UUID,
    *,
    lease_owner: str | None = None,
) -> None:
    """Run one claimed child until its inbox is empty or ownership is lost."""
    from app.services.channel_llm import _call_agent_llm
    from app.services.chat_history import load_history_prefix_before_anchor
    from app.services.llm.caller import is_error_result

    current = asyncio.current_task()
    lease_context_token = _current_subagent_lease_owner.set(
        lease_owner or settings.INSTANCE_ID
    )

    async def _lease_heartbeat() -> None:
        failures = 0
        while True:
            await asyncio.sleep(max(5, LEASE_SECONDS // 3))
            try:
                async with async_session() as heartbeat_db:
                    owned = await heartbeat_db.get(
                        SubagentRun,
                        run_id,
                        with_for_update=True,
                    )
                    if not _owns_subagent_lease(owned):
                        if current is not None and not current.done():
                            current.cancel()
                        return
                    owned.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
                    await heartbeat_db.commit()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - lease safety boundary
                failures += 1
                logger.warning(f"[subagent] lease renewal failed run={run_id} attempt={failures}: {exc}")
                if failures >= 3:
                    if current is not None and not current.done():
                        current.cancel()
                    return

    heartbeat = asyncio.create_task(
        _lease_heartbeat(),
        name=f"subagent-lease:{run_id}",
    )
    if current is not None:
        async with _running_tasks_guard:
            _running_tasks[run_id] = current
            _running_task_lease_owners[run_id] = _expected_subagent_lease_owner()
    current_anchor_id: uuid.UUID | None = None
    active_turn_capacity: AsyncExitStack | None = None
    try:
        while True:
            claimed_input = await _load_or_start_input(run_id)
            if claimed_input is None:
                return
            anchor, recovering = claimed_input
            current_anchor_id = anchor.id
            anchor_id = anchor.id
            anchor_content = anchor.content
            tenant_id = await _subagent_workload_tenant_id(run_id)
            active_turn_capacity = AsyncExitStack()
            try:
                await active_turn_capacity.enter_async_context(
                    get_workload_capacity().slot(WorkloadKind.PROJECT, tenant_id)
                )
            except WorkloadOverloadedError:
                await active_turn_capacity.aclose()
                active_turn_capacity = None
                await _requeue_capacity_blocked_subagent(run_id, anchor_id)
                current_anchor_id = None
                return
            async with async_session() as db:
                run = await db.get(SubagentRun, run_id)
                child = await db.get(ChatSession, run_id)
                if run is None or child is None:
                    await active_turn_capacity.aclose()
                    active_turn_capacity = None
                    return
                agent = await _validate_execution_identity(db, run, child)
                raw_project_run_id = _message_meta(anchor).get("project_run_id")
                try:
                    active_project_run_id = uuid.UUID(str(raw_project_run_id)) if raw_project_run_id else None
                except (TypeError, ValueError):
                    active_project_run_id = None
                if child.project_id is not None:
                    from app.models.project import Project, ProjectRun

                    active_project = await db.get(Project, child.project_id)
                    if active_project is None:
                        raise RuntimeError("Project Subagent runtime is no longer available")
                    active_dispatch: dict = {}
                    if active_project_run_id is not None:
                        active_project_run = await db.get(ProjectRun, active_project_run_id)
                        if (
                            active_project_run is not None
                            and active_project_run.project_id == active_project.id
                        ):
                            active_dispatch = dict(
                                dict(active_project_run.input or {}).get("dispatch") or {}
                            )
                    (
                        active_member,
                        active_member_config,
                        active_capabilities,
                        active_is_leader,
                        active_run_frozen,
                    ) = await _project_member_runtime_snapshot(
                        db,
                        project=active_project,
                        agent_id=agent.id,
                        project_run_id=active_project_run_id,
                    )
                    _apply_project_runtime_to_session(
                        child,
                        member=active_member,
                        member_config=active_member_config,
                        capabilities=active_capabilities,
                        is_leader=active_is_leader,
                        frozen=active_run_frozen,
                    )
                    child.im_config = {
                        **dict(child.im_config or {}),
                        # Freeze the execution boundary at message creation.
                        # Resuming a project while an advisory turn is queued
                        # must not turn that already-created turn into work.
                        "project_execution_tools_enabled": active_dispatch.get(
                            "execution_tools_enabled",
                            active_project.status == "running",
                        ),
                        "project_read_only_conversation": bool(
                            active_dispatch.get("read_only_conversation")
                        ),
                    }
                    await db.commit()
                ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
                if recovering:
                    from app.services.turn_recovery import (
                        prepare_recoverable_turn_history,
                    )

                    history = await prepare_recoverable_turn_history(
                        db,
                        anchor,
                        execution_agent_id=child.agent_id,
                        ctx_size=ctx_size,
                    )
                    if not history:
                        raise RuntimeError("Subagent durable turn recovery failed")
                else:
                    history = await load_history_prefix_before_anchor(
                        db,
                        agent_id=child.agent_id,
                        conversation_id=str(run_id),
                        turn_anchor_id=anchor.id,
                        ctx_size=ctx_size,
                    )
                    if history is None:
                        raise RuntimeError("Subagent fresh turn prefix changed")
                child_agent_id = child.agent_id
                child_session_id = child.id
                execution_user_id = run.execution_user_id
                model_name = run.model
                model_id = run.model_id
                temperature = run.temperature
                reasoning_effort = run.reasoning_effort
                include_soul = run.soul
                include_memory = run.memory
                member_runtime_config = dict(dict(child.im_config or {}).get("member_config_snapshot") or {})
                max_tool_rounds_override = member_runtime_config.get("max_tool_rounds")
                from app.services.agent_runtime_workspace import (
                    resolve_agent_runtime_workspace,
                )

                runtime_workspace = resolve_agent_runtime_workspace(
                    agent_id=agent.id,
                    agent_scope=getattr(agent, "scope", None),
                    agent_project_id=getattr(agent, "project_id", None),
                    tenant_id=agent.tenant_id,
                    session_project_id=child.project_id,
                    session_config=dict(child.im_config or {}),
                )
                web_broadcast_targets: list[tuple[uuid.UUID | str, str, dict]] = []
                if child.project_id is not None:
                    parent_session = await db.get(ChatSession, run.parent_session_id)
                    if parent_session is not None and parent_session.source_channel == "project":
                        from app.services.project_group_turn_lifecycle import (
                            project_group_timeline_anchor_for_child_turn,
                            publish_project_group_turn_event,
                            reconcile_project_group_turn,
                        )

                        child_config = dict(child.im_config or {})
                        group_timeline_anchor_id = (
                            await project_group_timeline_anchor_for_child_turn(
                                db,
                                project_id=parent_session.project_id,
                                child_session_id=child.id,
                                child_turn_anchor_id=anchor_id,
                            )
                        )
                        parent_projection = await reconcile_project_group_turn(
                            db,
                            project_id=parent_session.project_id,
                            session=parent_session,
                        )
                        await db.commit()
                        await publish_project_group_turn_event(
                            session=parent_session,
                            projection=parent_projection,
                            payload={"type": "turn_state"},
                            event_kind="turn_lifecycle",
                        )
                        web_broadcast_targets.append(
                            (
                                parent_session.agent_id,
                                str(parent_session.id),
                                {
                                    "producer_scope": f"project:{run_id}:{anchor_id}",
                                    "sender_agent_id": str(child_agent_id),
                                    "sender_name": str(
                                        child_config.get("project_member_name_snapshot")
                                        or agent.name
                                    ),
                                    "timeline_anchor_id": (
                                        str(group_timeline_anchor_id)
                                        if group_timeline_anchor_id is not None
                                        else None
                                    ),
                                    "turn": parent_projection.to_client_dict(),
                                },
                            )
                        )

            tools = await prepare_subagent_tools(
                child_agent_id,
                child_session_id,
                execution_user_id=execution_user_id,
            )
            thinking_parts: list[str] = []

            async def _capture_thinking(
                text: str,
                parts: list[str] = thinking_parts,
            ) -> None:
                if text:
                    parts.append(str(text))

            async with async_session() as llm_db:

                async def _before_round(
                    _round: int,
                    *,
                    before_injection=None,
                    active_anchor_id: uuid.UUID = anchor_id,
                ) -> list[dict]:
                    # ``_call_agent_llm`` resolves the Agent, model, scene, and
                    # anchor through this session before entering the provider
                    # loop. End that read transaction immediately before every
                    # network dispatch so concurrent long-running Subagents do
                    # not pin one database-pool connection each. The session
                    # factory uses ``expire_on_commit=False``, so the resolved
                    # ORM values remain valid for the provider call.
                    if llm_db.in_transaction():
                        await llm_db.commit()
                    return await _drain_subagent_inbox(
                        run_id,
                        active_anchor_id,
                        before_injection=before_injection,
                    )

                reply = await _call_agent_llm(
                    llm_db,
                    child_agent_id,
                    "" if recovering else anchor_content,
                    session_id=str(run_id),
                    user_id=execution_user_id,
                    history=history,
                    recovery_hint=None,
                    continue_turn=recovering,
                    recovery_mode=recovering,
                    turn_anchor_id=anchor_id,
                    turn_type="subagent",
                    model_name=model_name,
                    model_override_id=model_id,
                    temperature_override=temperature,
                    reasoning_effort_override=reasoning_effort,
                    include_soul=include_soul,
                    include_memory=include_memory,
                    prepared_tools=tools,
                    on_thinking=_capture_thinking,
                    before_round=_before_round,
                    before_tool_execution=lambda: _assert_subagent_running(run_id),
                    # Exact-session drawers are subscribers, never a second
                    # execution runtime.  Reuse the unified channel bridge for
                    # standard thinking/chunk/tool/done packets while this
                    # durable worker remains the sole model caller.
                    broadcast_web=True,
                    web_broadcast_targets=web_broadcast_targets,
                    runtime_session=child,
                    runtime_workspace=runtime_workspace,
                    max_tool_rounds_override=max_tool_rounds_override,
                )
            from app.services.llm.failure_outcome import llm_failure_code

            failure_code = llm_failure_code(reply)
            if not str(reply or "").strip() and await _park_subagent_confirmation(
                run_id,
                anchor_id,
            ):
                # ``request_confirmation`` is a standard suspended tool call.
                # Its ChatMessage row is the durable continuation point; this
                # worker only releases the child lease.
                await active_turn_capacity.aclose()
                active_turn_capacity = None
                return
            reply_text = str(reply or "")
            failed = (
                bool(failure_code)
                or is_error_result(reply_text)
                or reply_text.startswith(
                    (
                        "⚠️ 数字员工未找到",
                        "⚠️ Subagent 指定模型 ",
                    )
                )
                or (reply_text.startswith("⚠️ ") and "未配置 LLM 模型" in reply_text)
            )
            reply_quality: dict | None = None
            if child.project_id is not None and not failed:
                from app.services.project_reply_quality import (
                    assess_project_reply,
                    build_project_reply_correction_prompt,
                )

                assessment = assess_project_reply(reply_text)
                if assessment.needs_correction:
                    correction_history = [dict(message) for message in history]
                    if not recovering:
                        correction_history.append({"role": "user", "content": anchor_content})
                    correction_history.append({"role": "assistant", "content": reply_text})
                    project_runtime = dict(child.im_config or {})
                    correction_prompt = build_project_reply_correction_prompt(
                        reply_text,
                        role=project_runtime.get("project_member_role_snapshot"),
                        is_owner=project_runtime.get("project_role_snapshot") == "leader",
                    )
                    async with async_session() as correction_db:
                        corrected_reply = await _call_agent_llm(
                            correction_db,
                            child_agent_id,
                            correction_prompt,
                            session_id=str(run_id),
                            user_id=execution_user_id,
                            history=correction_history,
                            recovery_hint=None,
                            continue_turn=False,
                            # This synthetic correction is already bounded by
                            # the original frozen context and must not trigger a
                            # second compaction/recovery branch.
                            recovery_mode=True,
                            turn_anchor_id=anchor_id,
                            turn_type="subagent",
                            model_name=model_name,
                            model_override_id=model_id,
                            temperature_override=temperature,
                            reasoning_effort_override=reasoning_effort,
                            include_soul=include_soul,
                            include_memory=include_memory,
                            # Correction may improve prose only. It cannot replay
                            # tools, drain new inbox input, broadcast another
                            # stream, or create any project/A2A wake-up.
                            prepared_tools=[],
                            on_thinking=_capture_thinking,
                            before_tool_execution=lambda: _assert_subagent_running(run_id),
                            broadcast_web=False,
                            runtime_session=child,
                            runtime_workspace=runtime_workspace,
                            max_tool_rounds_override=max_tool_rounds_override,
                        )
                    corrected_failure_code = llm_failure_code(corrected_reply)
                    corrected_text = str(corrected_reply or "").strip()
                    corrected_failed = (
                        bool(corrected_failure_code)
                        or not corrected_text
                        or is_error_result(corrected_text)
                        or corrected_text.startswith("⚠️ ")
                    )
                    if corrected_failure_code:
                        reply = corrected_reply
                        reply_text = corrected_text
                        failure_code = corrected_failure_code
                        failed = True
                    elif not corrected_failed:
                        reply = corrected_reply
                        reply_text = corrected_text
                    reply_quality = {
                        "correction_attempted": True,
                        "correction_applied": not corrected_failed,
                        "initial_reasons": list(assessment.reasons),
                        "final_needs_correction": assess_project_reply(reply_text).needs_correction,
                    }
            terminal = await _finish_subagent_turn(
                run_id=run_id,
                anchor_id=anchor_id,
                reply=reply,
                failed=failed,
                failure_code=failure_code,
                thinking="".join(thinking_parts) or None,
                reply_quality=reply_quality,
            )
            await active_turn_capacity.aclose()
            active_turn_capacity = None
            if terminal:
                return
    except asyncio.CancelledError:
        try:
            from app.services.active_turns import is_current_turn_cancel_requested

            control_plane_cancelled = is_current_turn_cancel_requested()
            async with async_session() as cancel_db:
                owned = await cancel_db.get(SubagentRun, run_id, with_for_update=True)
                if _owns_subagent_lease(owned):
                    if control_plane_cancelled:
                        from app.services.conversation_turn_lifecycle import (
                            cancel_current_conversation_turn,
                        )

                        child = await cancel_db.get(ChatSession, run_id)
                        if child is not None:
                            await cancel_current_conversation_turn(
                                cancel_db,
                                agent_id=child.agent_id,
                                conversation_id=str(child.id),
                            )
                    owned.status = RUN_CANCELLED if control_plane_cancelled else RUN_QUEUED
                    owned.lease_owner = None
                    owned.lease_expires_at = None
                    if control_plane_cancelled:
                        rows = (
                            (
                                await cancel_db.execute(
                                    select(ChatMessage).where(
                                        ChatMessage.conversation_id == str(run_id),
                                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                                        ChatMessage.message_meta["subagent_input_state"].as_string()
                                        == INPUT_PROCESSING,
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        for row in rows:
                            meta = _message_meta(row)
                            meta["subagent_input_state"] = INPUT_CANCELLED
                            row.message_meta = meta
                    await cancel_db.commit()
        except Exception as exc:  # noqa: BLE001 - lease expiry remains the fallback
            logger.warning(f"[subagent] cancelled run release deferred run={run_id}: {exc}")
        raise
    except Exception as exc:  # noqa: BLE001 - durable worker failure boundary
        logger.exception(f"[subagent] execution failed run={run_id}: {exc}")
        if current_anchor_id is not None:
            terminal = await _finish_subagent_turn(
                run_id=run_id,
                anchor_id=current_anchor_id,
                reply="本次执行未完成，请稍后重试或查看项目状态。",
                failed=True,
            )
            if not terminal:
                # `_finish_subagent_turn` deliberately lets later parent input
                # win over this failed round.  Release the lease immediately so
                # sync callers and another worker can process that pending input
                # without waiting for lease expiry.
                async with async_session() as retry_db:
                    owned = await retry_db.get(
                        SubagentRun,
                        run_id,
                        with_for_update=True,
                    )
                    pending_exists = bool(
                        owned is not None
                        and (
                            await retry_db.execute(
                                select(ChatMessage.id)
                                .where(
                                    ChatMessage.conversation_id == str(run_id),
                                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                                    ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                                )
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                    )
                    if (
                        _owns_subagent_lease(owned)
                        and pending_exists
                    ):
                        owned.status = RUN_QUEUED
                        owned.lease_owner = None
                        owned.lease_expires_at = None
                        await retry_db.commit()
    finally:
        if active_turn_capacity is not None:
            await active_turn_capacity.aclose()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        async with _running_tasks_guard:
            if _running_tasks.get(run_id) is current:
                _running_tasks.pop(run_id, None)
                _running_task_lease_owners.pop(run_id, None)
        _current_subagent_lease_owner.reset(lease_context_token)


async def _latest_subagent_result(run_id: uuid.UUID) -> tuple[str, str, list[str]]:
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id)
        if run is None:
            raise SubagentError("Subagent 不存在。")
        result = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string().in_([SUBAGENT_COMPLETION, SUBAGENT_FAILURE]),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        parent_messages = (
            (
                await db.execute(
                    select(ChatMessage.content)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        return (
            run.status,
            result.content if result is not None else "",
            list(parent_messages),
        )


async def run_subagent_sync(run_id: uuid.UUID) -> tuple[str, str, list[str]]:
    """Claim locally when possible; otherwise wait for the durable owner."""
    while True:
        status, result, parent_messages = await _latest_subagent_result(run_id)
        if status in TERMINAL_STATUSES:
            return status, result, parent_messages
        claimed = await _claim_subagent(run_id, with_token=True)
        if claimed is not None:
            claimed_run_id, lease_owner = claimed
            await execute_claimed_subagent(claimed_run_id, lease_owner=lease_owner)
        else:
            await asyncio.sleep(0.25)
