"""Subagent creation, messaging, and cancellation entrypoints."""

from __future__ import annotations

from app.services.scene_activation import snapshot_project_scene

from app.services.subagent_runtime_shared import *  # noqa: F401,F403

async def create_subagent(
    *,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    parent_session_id: str,
    origin_tool_call_id: str,
    name: str | None = None,
    task: str,
    mode: str = "sync",
    model: str | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    fork: bool = False,
    soul: bool = True,
    memory: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
    project_run_id: uuid.UUID | None = None,
    input_metadata: dict | None = None,
    allow_parent_continuation: bool = False,
    executor: str = "agent",
) -> tuple[SubagentRun, bool]:
    """Create one child Session and lifecycle row, idempotent per parent tool call."""
    task_text = str(task or "").strip()
    if not task_text:
        raise SubagentError("task 不能为空。")
    child_title = _subagent_title(name, task_text)
    normalized_mode = str(mode or "sync").strip().lower()
    if normalized_mode not in {"sync", "async"}:
        raise SubagentError("mode 只支持 sync 或 async。")
    call_id = str(origin_tool_call_id or "").strip()
    if not call_id:
        raise SubagentError("缺少当前工具调用标识，无法创建可恢复的 Subagent。")
    try:
        parent_id = uuid.UUID(str(parent_session_id))
    except (TypeError, ValueError):
        raise SubagentError("当前 Session 无效。") from None

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        parent = await db.get(ChatSession, parent_id, with_for_update=True)
        if agent is None or parent is None or not await _agent_participates(db, parent, agent_id):
            raise SubagentError("当前 Agent 无权从这个 Session 创建 Subagent。")
        if executor not in {"agent", "media"}:
            raise SubagentError("Invalid executor")
        if parent.source_channel == SUBAGENT_CHANNEL and executor == "agent":
            raise SubagentError("Subagent 不能继续创建 Subagent。")

        project = None
        execution_origin = None
        if parent.project_id is not None:
            from app.models.project import Project
            from app.services.project_service import resolve_project_execution_user

            project = await db.get(Project, parent.project_id)
            if project is None:
                raise SubagentError("项目不存在。")
            try:
                resolved_user = await resolve_project_execution_user(db, project, execution_user_id)
            except HTTPException as exc:
                raise SubagentError("项目执行用户不可用。") from exc
            resolved_user_id = resolved_user.id
        else:
            from app.services.subagent_execution_identity import resolve_subagent_execution_user

            resolved_user_id, execution_origin = await resolve_subagent_execution_user(
                db,
                agent,
                execution_user_id,
                parent=parent,
                anchor_id=turn_anchor_id,
            )

        async def _load_authorized_existing() -> SubagentRun | None:
            existing_run = (
                await db.execute(
                    select(SubagentRun).where(
                        SubagentRun.parent_session_id == parent_id,
                        SubagentRun.origin_tool_call_id == call_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_run is None:
                return None
            existing_child = await db.get(ChatSession, existing_run.id)
            if (
                existing_child is None
                or existing_child.source_channel != SUBAGENT_CHANNEL
                or existing_child.agent_id != agent_id
                or existing_run.execution_user_id != resolved_user_id
                or existing_run.parent_session_id != parent_id
            ):
                raise SubagentError("当前执行身份无权恢复这个 Subagent。")
            return existing_run

        existing = await _load_authorized_existing()
        if existing is not None:
            return existing, False

        if allow_parent_continuation:
            await _ensure_parent_continuation(
                db,
                parent=parent,
                execution_user_id=resolved_user_id,
            )

        try:
            normalized_temperature = validate_temperature(temperature)
        except ValueError as exc:
            raise SubagentError("temperature 必须在 0 到 2 之间。") from exc
        try:
            normalized_reasoning_effort = validate_reasoning_effort(reasoning_effort)
        except ValueError as exc:
            raise SubagentError(str(exc)) from exc
        model_id, canonical_model = (None, None) if executor == "media" else await _resolve_model_override(db, agent, model)
        now = datetime.now(UTC)
        child_id = uuid.uuid4()
        child_user_id = (
            parent.user_id
            if parent.user_id == resolved_user_id
            and parent.source_channel not in {"agent", "trigger", SUBAGENT_CHANNEL}
            else None
        )
        project_member = None
        project_capabilities: list[dict] = []
        project_member_config: dict = {}
        project_member_is_leader = False
        project_run_frozen = False
        project_tool_policy_snapshot: dict = {}
        agent_runtime_workspace_snapshot: dict[str, str] = {}
        if parent.project_id is not None:
            (
                project_member,
                project_member_config,
                project_capabilities,
                project_member_is_leader,
                project_run_frozen,
            ) = await _project_member_runtime_snapshot(
                db,
                project=project,
                agent_id=agent_id,
                project_run_id=project_run_id,
            )
            if temperature is None and "temperature" in project_member_config:
                try:
                    normalized_temperature = validate_temperature(
                        project_member_config.get("temperature")
                    )
                except ValueError as exc:
                    raise SubagentError("项目成员想象力必须在 0 到 2 之间。") from exc
            if reasoning_effort is None and "reasoning_effort" in project_member_config:
                try:
                    normalized_reasoning_effort = validate_reasoning_effort(
                        project_member_config.get("reasoning_effort")
                    )
                except ValueError as exc:
                    raise SubagentError(str(exc)) from exc
            project_tool_policy_snapshot = (
                dict(dict((project.settings or {}).get("policies") or {}).get("project_tools") or {})
                if project is not None
                else {}
            )
            if str(getattr(agent, "scope", "") or "").strip().lower() == "project":
                from app.services.agent_runtime_workspace import (
                    project_agent_runtime_workspace,
                )

                if project is None or getattr(agent, "project_id", None) != project.id:
                    raise SubagentError("项目专用 Agent 不能在其他项目中运行。")
                agent_runtime_workspace_snapshot = project_agent_runtime_workspace(
                    agent_id=agent.id,
                    tenant_id=project.tenant_id,
                    project_id=project.id,
                ).as_session_config()

        task_metadata = dict(input_metadata or {})
        if executor != "media":
            await snapshot_project_scene(db, agent_id, parent, task_metadata)
        child = ChatSession(
            id=child_id,
            agent_id=agent_id,
            project_id=parent.project_id,
            user_id=child_user_id,
            title=child_title,
            source_channel=SUBAGENT_CHANNEL,
            is_primary=False,
            is_group=False,
            im_config={
                "execution_origin": execution_origin,
                "executor": executor,
                "project_id": str(parent.project_id) if parent.project_id else None,
                "project_group_session_id": str(parent.id) if parent.project_id else None,
                "project_member_id": str(project_member.id) if project_member else None,
                "project_membership_generation": max(
                    1,
                    int(dict(project_member_config.get("membership") or {}).get("generation") or 1),
                )
                if project_member
                else None,
                "membership_revoked": False,
                "project_role_snapshot": "leader" if project_member_is_leader else "participant",
                "project_name_snapshot": project.name if project is not None else "",
                "project_goal_snapshot": project.goal if project is not None else "",
                "project_success_criteria_snapshot": list(project.success_criteria or [])
                if project is not None
                else [],
                "project_member_name_snapshot": project_member.name_snapshot if project_member else agent.name,
                "project_member_role_snapshot": project_member.role_snapshot if project_member else "",
                "project_tool_policy_snapshot": project_tool_policy_snapshot,
                "member_config_snapshot": project_member_config,
                "capability_snapshot": project_capabilities,
                "project_run_frozen": project_run_frozen,
                "project_execution_tools_enabled": task_metadata.get(
                    "project_execution_tools_enabled"
                ),
                "project_read_only_conversation": bool(
                    task_metadata.get("project_read_only_conversation")
                ),
                "agent_runtime_workspace": agent_runtime_workspace_snapshot,
            },
            created_at=now,
            last_message_at=now,
        )
        run = SubagentRun(
            id=child_id,
            parent_session_id=parent.id,
            project_id=parent.project_id,
            project_member_id=project_member.id if project_member else None,
            execution_user_id=resolved_user_id,
            origin_tool_call_id=call_id,
            mode=normalized_mode,
            model=canonical_model,
            model_id=model_id,
            temperature=normalized_temperature,
            reasoning_effort=normalized_reasoning_effort,
            soul=bool(soul),
            memory=bool(memory),
            status=RUN_QUEUED,
        )
        db.add_all([child, run])
        await db.flush()

        message_time = now
        if fork:
            if turn_anchor_id is None:
                raise SubagentError("Fork 当前会话需要一个有效的当前轮次锚点。")
            message_time = await _copy_fork_context(
                db,
                parent=parent,
                child=child,
                execution_agent_id=agent_id,
                turn_anchor_id=turn_anchor_id,
                created_at=message_time,
                ctx_size=agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE,
            )

        message_time += timedelta(microseconds=1)
        causality = await _subagent_input_causality(
            db,
            parent=parent,
            explicit_parent_anchor_id=turn_anchor_id,
        )
        task_row = ChatMessage(
            agent_id=agent_id,
            user_id=resolved_user_id,
            sender_user_id=resolved_user_id,
            role="user",
            content=task_text,
            conversation_id=str(child_id),
            message_meta={
                **task_metadata,
                **causality,
                "kind": SUBAGENT_INPUT,
                "subagent_input_state": INPUT_PENDING,
                "attachments": [],
                **({"project_run_id": str(project_run_id)} if project_run_id else {}),
            },
            created_at=message_time,
        )
        db.add(task_row)
        child.last_message_at = message_time
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await _load_authorized_existing()
            if existing is None:
                raise
            return existing, False
        await db.refresh(run)
        return run, True


async def append_subagent_message(
    *,
    agent_id: uuid.UUID,
    parent_session_id: str,
    subagent_id: str,
    message: str,
    execution_user_id: uuid.UUID,
    origin_tool_call_id: str,
    project_run_id: uuid.UUID | None = None,
    input_metadata: dict | None = None,
    allow_parent_continuation: bool = False,
    executor: str = "agent",
) -> str:
    content = str(message or "").strip()
    if not content:
        raise SubagentError("message 不能为空。")
    try:
        parent_id = uuid.UUID(str(parent_session_id))
        child_id = uuid.UUID(str(subagent_id))
    except (TypeError, ValueError):
        raise SubagentError("subagent_id 无效。") from None
    call_id = str(origin_tool_call_id or "").strip()
    if not call_id:
        raise SubagentError("缺少当前工具调用标识，无法可靠追加消息。")
    event_key = f"subagent-parent-input:{child_id}:{call_id}"

    async with async_session() as db:
        # Parent Session is the admission fence shared with STOP. If STOP wins,
        # causality validation below rejects this late child input; if this
        # transaction wins, STOP sees and cancels it.
        parent = await db.get(ChatSession, parent_id, with_for_update=True)
        if parent is None:
            raise SubagentError("当前 Session 不存在。")
        if allow_parent_continuation:
            await _ensure_parent_continuation(
                db,
                parent=parent,
                execution_user_id=execution_user_id,
            )
        run = await db.get(SubagentRun, child_id, with_for_update=True)
        if run is None or not _run_owned_by_parent(run, parent_id):
            raise SubagentError("Subagent 不存在，或不属于当前 Session。")
        if run.execution_user_id != execution_user_id:
            raise SubagentError("当前执行身份无权操作这个 Subagent。")
        child = await db.get(ChatSession, child_id)
        if child is None or child.agent_id != agent_id:
            raise SubagentError("当前 Agent 无权操作这个 Subagent。")
        if dict(child.im_config or {}).get("executor", "agent") != executor:
            raise SubagentError("Session executor does not match this operation")
        try:
            agent = await _validate_execution_identity(db, run, child)
        except RuntimeError as exc:
            raise SubagentError("项目成员已退出，不能继续这个工作会话。") from exc
        existing = (
            await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == event_key))
        ).scalar_one_or_none()
        if existing is not None:
            return run.status
        supplied_metadata = dict(input_metadata or {})
        if not await _project_accepts_new_subagent_anchor(
            db,
            run,
            authorized_project_run_id=(project_run_id if supplied_metadata.get("project_dispatch") else None),
        ):
            raise SubagentError("项目已暂停；恢复项目后才能继续这个工作会话。")
        from app.services.confirmation_service import (
            find_pending_confirmation,
            ignore_pending_confirmation_for_new_input,
        )

        pending_confirmation = await find_pending_confirmation(
            db,
            agent_id=child.agent_id,
            conversation_id=str(child.id),
        )
        ignored_confirmation = None
        if pending_confirmation is not None:
            if pending_confirmation.force_confirmation:
                raise SubagentError("Subagent 正在等待人工确认，不能追加新的项目消息。")
            ignored_confirmation = await ignore_pending_confirmation_for_new_input(
                db,
                pending_confirmation,
            )
        now = datetime.now(UTC)
        if project_run_id is not None:
            from app.models.project import Project

            project = await db.get(Project, run.project_id)
            if project is None:
                raise SubagentError("项目不存在，不能继续这个工作会话。")
            (
                active_member,
                active_member_config,
                active_capabilities,
                active_is_leader,
                active_run_frozen,
            ) = await _project_member_runtime_snapshot(
                db,
                project=project,
                agent_id=agent.id,
                project_run_id=project_run_id,
            )
            _apply_project_runtime_to_session(
                child,
                member=active_member,
                member_config=active_member_config,
                capabilities=active_capabilities,
                is_leader=active_is_leader,
                frozen=active_run_frozen,
            )
            if supplied_metadata.get("project_dispatch"):
                child.im_config = {
                    **dict(child.im_config or {}),
                    "project_execution_tools_enabled": supplied_metadata.get(
                        "project_execution_tools_enabled"
                    ),
                    "project_read_only_conversation": bool(
                        supplied_metadata.get("project_read_only_conversation")
                    ),
                }
        if executor != "media":
            await snapshot_project_scene(db, agent.id, parent, supplied_metadata)
        supplied_attachments = list(supplied_metadata.pop("attachments", []) or [])
        causality = await _subagent_input_causality(db, parent=parent)
        input_row = ChatMessage(
            agent_id=child.agent_id,
            user_id=run.execution_user_id,
            sender_user_id=run.execution_user_id,
            role="user",
            content=content,
            conversation_id=str(child_id),
            external_event_key=event_key,
            message_meta={
                **supplied_metadata,
                **causality,
                "kind": SUBAGENT_INPUT,
                "subagent_input_state": INPUT_PENDING,
                "attachments": supplied_attachments,
                **({"project_run_id": str(project_run_id)} if project_run_id else {}),
            },
            created_at=now,
        )
        db.add(input_row)
        await db.flush()
        # Admission owns the durable lifecycle. Always admit the oldest live
        # input, never merely the row appended by this call: a queued or
        # processing predecessor remains the single session owner until it
        # reaches a terminal state.
        from app.services.conversation_turn_lifecycle import (
            ConversationTurnConflict,
            transition_conversation_turn,
        )

        lifecycle_anchor = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(child.id),
                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                    ChatMessage.message_meta["subagent_input_state"]
                    .as_string()
                    .in_([INPUT_PENDING, INPUT_PROCESSING]),
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .limit(1)
            )
        ).scalar_one()
        try:
            await transition_conversation_turn(
                db,
                agent_id=child.agent_id,
                conversation_id=str(child.id),
                turn_anchor_id=lifecycle_anchor.id,
                status="running",
            )
        except ConversationTurnConflict:
            pass
        child.last_message_at = now
        if run.status in {RUN_COMPLETED, RUN_FAILED, RUN_WAITING, RUN_CANCELLED}:
            run.status = RUN_QUEUED
            if executor != "media":
                run.mode = "async"
            run.lease_owner = None
            run.lease_expires_at = None
        await db.commit()
        if ignored_confirmation:
            from app.services.confirmation_service import publish_ignored_confirmation

            await publish_ignored_confirmation(
                pending_confirmation,
                ignored_confirmation,
            )
        return run.status


async def send_subagent_message_to_parent(
    *,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    origin_tool_call_id: str,
    subagent_session_id: str,
    message: str,
) -> None:
    content = str(message or "").strip()
    if not content:
        raise SubagentError("message 不能为空。")
    try:
        child_id = uuid.UUID(str(subagent_session_id))
    except (TypeError, ValueError):
        raise SubagentError("当前 Subagent Session 无效。") from None
    call_id = str(origin_tool_call_id or "").strip()
    if not call_id:
        raise SubagentError("缺少当前工具调用标识，无法可靠发送消息。")
    event_key = f"subagent-child-message:{child_id}:{call_id}"
    should_wake_dispatcher = False
    should_wake_project_dispatcher = False

    async with async_session() as db:
        run = await db.get(SubagentRun, child_id, with_for_update=True)
        child = await db.get(ChatSession, child_id)
        if run is None or child is None or child.source_channel != SUBAGENT_CHANNEL:
            raise SubagentError("send_message_to_parent 只能由 Subagent 使用。")
        if child.agent_id != agent_id or run.execution_user_id != execution_user_id:
            raise SubagentError("当前执行身份无权从这个 Subagent 发送消息。")
        if run.status == RUN_CANCELLED:
            raise SubagentError("Subagent 已停止。")
        try:
            await _validate_execution_identity(db, run, child)
        except RuntimeError as exc:
            raise SubagentError("项目成员已退出，不能继续发送项目消息。") from exc
        existing = (
            await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == event_key))
        ).scalar_one_or_none()
        if existing is not None:
            return
        should_wake_dispatcher = run.mode == "async"
        should_wake_project_dispatcher = should_wake_dispatcher and run.project_id is not None
        now = datetime.now(UTC)
        db.add(
            ChatMessage(
                agent_id=child.agent_id,
                user_id=run.execution_user_id,
                sender_agent_id=child.agent_id,
                role="assistant",
                content=content,
                conversation_id=str(child_id),
                external_event_key=event_key,
                message_meta={
                    "kind": SUBAGENT_PARENT_MESSAGE,
                    "subagent_wake": should_wake_dispatcher,
                    **(
                        {"subagent_dispatch_state": SUBAGENT_DISPATCH_PENDING}
                        if should_wake_dispatcher
                        else {}
                    ),
                    "attachments": [],
                },
                created_at=now,
            )
        )
        child.last_message_at = now
        await db.commit()
    if should_wake_project_dispatcher:
        _signal_project_dispatch_work()
    elif should_wake_dispatcher:
        _signal_dispatch_work()


async def stop_subagent(
    *,
    agent_id: uuid.UUID,
    parent_session_id: str,
    subagent_id: str,
    execution_user_id: uuid.UUID,
    task_id: str | None = None,
) -> str:
    try:
        parent_id = uuid.UUID(str(parent_session_id))
        child_id = uuid.UUID(str(subagent_id))
    except (TypeError, ValueError):
        raise SubagentError("subagent_id 无效。") from None

    async with async_session() as db:
        run = await db.get(SubagentRun, child_id, with_for_update=True)
        if run is None or not _run_owned_by_parent(run, parent_id):
            raise SubagentError("Subagent 不存在，或不属于当前 Session。")
        if run.execution_user_id != execution_user_id:
            raise SubagentError("当前执行身份无权操作这个 Subagent。")
        child = await db.get(ChatSession, child_id)
        if child is None or child.agent_id != agent_id:
            raise SubagentError("当前 Agent 无权操作这个 Subagent。")
        if task_id and dict(child.im_config or {}).get("executor") == "media":
            from app.services.media_ai_sessions import cancel_pending_media_task

            task_status = await cancel_pending_media_task(db, child, task_id)
            if task_status is not None:
                return task_status
        if run.status in TERMINAL_STATUSES:
            return run.status
        await db.rollback()

    from app.services.turn_control import stop_session_turn_tree

    await stop_session_turn_tree(
        agent_id=agent_id,
        session_id=child_id,
        reason=f"Subagent stop requested by user {execution_user_id}",
        expected_anchor_id=uuid.UUID(task_id) if task_id else None,
    )
    return RUN_CANCELLED
