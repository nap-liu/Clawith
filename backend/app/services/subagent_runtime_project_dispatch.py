"""Project run dispatch loops and daemon entrypoints."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_lifecycle import append_subagent_message, create_subagent
from app.services.subagent_runtime_parent_dispatch import (
    _dispatch_parent_event,
    _dispatch_parent_event_batch,
    _pending_parent_event_groups,
)
from app.services.subagent_runtime_parent_events import _subagent_worker_loop
from app.services.subagent_runtime_parent_round import _pending_parent_events
from app.services.subagent_runtime_project_common import (
    PROJECT_DISPATCH_BATCH_SIZE,
    PROJECT_DISPATCH_TRIGGERS,
    ProjectDispatchCursor,
    _pending_project_leader_groups,
    _project_member_origin_tool_call_id,
    _project_run_child_input_state,
    _project_run_has_child_input,
    _publish_project_run_group_turn,
)
from app.services.subagent_runtime_project_leader import (
    _dispatch_project_leader_batch,
)

async def dispatch_project_run(project_run_id: uuid.UUID) -> dict:
    """Idempotently deliver one durable project dispatch outbox row.

    Project endpoints commit the ProjectRun before calling this helper. The
    daemon scans the same queued rows, so a process exit between the database
    commit and Subagent creation cannot strand kickoff or a group mention.
    """
    from app.models.project import Project, ProjectEvent, ProjectMemberSnapshot, ProjectRun
    from app.services.project_service import (
        TERMINAL_PROJECT_RUN_STATUSES,
        add_event,
        apply_run_status,
        reconcile_project_run_terminal_state,
    )

    async with async_session() as db:
        project_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
        if project_run is None or project_run.trigger_type not in PROJECT_DISPATCH_TRIGGERS:
            return {"status": "gone"}
        repaired = reconcile_project_run_terminal_state(project_run)
        output = dict(project_run.output or {})
        if output.get("subagent_run_id"):
            try:
                child_id = uuid.UUID(str(output["subagent_run_id"]))
            except (TypeError, ValueError):
                child_id = None
            input_state = (
                await _project_run_child_input_state(
                    db,
                    child_id=child_id,
                    project_run_id=project_run.id,
                )
                if child_id is not None
                else None
            )
            if (
                project_run.finished_at is None
                and project_run.status == "running"
                and input_state == INPUT_PENDING
            ):
                project_run.status = "queued"
                project_run.started_at = None
                repaired = True
            if repaired:
                await db.commit()
                await _publish_project_run_group_turn(project_run.id)
            return {**output, "status": project_run.status}
        dispatch = dict((project_run.input or {}).get("dispatch") or {})
        try:
            group_session_id = uuid.UUID(str(dispatch["group_session_id"]))
            member_id = uuid.UUID(str(dispatch["project_member_id"]))
            anchor_id = uuid.UUID(str(dispatch["turn_anchor_id"]))
        except (KeyError, TypeError, ValueError):
            return {"status": "not_dispatchable"}
        task = str(dispatch.get("task") or "").strip()
        if not task:
            return {"status": "not_dispatchable"}
        project = await db.get(Project, project_run.project_id, with_for_update=True)
        parent = await db.get(ChatSession, group_session_id)
        member = await db.get(ProjectMemberSnapshot, member_id)
        runtime_ready = project is not None and (
            project.status == "running"
            or (project_run.trigger_type == "leader_kickoff" and project.status == "initializing")
            or (
                project_run.trigger_type == "group_leader_message"
                and project.status in {"planning", "paused", "waiting", "completed"}
            )
        )
        if project is not None and not runtime_ready:
            return {"status": "paused"}
        if (
            project is None
            or parent is None
            or parent.project_id != project.id
            or parent.source_channel not in {"project", "agent"}
            or (project_run.trigger_type != "a2a" and parent.source_channel != "project")
            or (project_run.trigger_type == "a2a" and parent.source_channel != "agent")
            or member is None
            or member.project_id != project.id
            or member.agent_id != project_run.agent_id
            or not member.is_enabled
        ):
            if project_run.trigger_type == "leader_kickoff" and project is not None:
                project.status = "planning"
                settings = dict(project.settings or {})
                planning = dict(settings.get("planning") or {})
                planning["state"] = "discussion"
                planning["launch_confirmed"] = False
                settings["planning"] = planning
                project.settings = settings
            project_run.status = "failed"
            project_run.error = "The selected project member is no longer available"
            project_run.finished_at = datetime.now(UTC)
            await db.commit()
            await _publish_project_run_group_turn(project_run.id)
            return {"status": "failed", "error": project_run.error}
        agent_id = member.agent_id
        execution_user_id = project_run.execution_user_id or project.owner_user_id
        if project_run.trigger_type == "group_leader_message":
            group_anchor = await db.get(ChatMessage, anchor_id)
            explicit_mentions = (
                {str(value) for value in _message_meta(group_anchor).get("mentions", [])}
                if group_anchor is not None
                else set()
            )
            if explicit_mentions and str(agent_id) not in explicit_mentions:
                project_run.status = "cancelled"
                project_run.finished_at = datetime.now(UTC)
                project_run.output = {
                    **dict(project_run.output or {}),
                    "status": "skipped",
                    "skip_reason": "explicit_mentions_route_to_specialists",
                }
                if group_anchor is not None:
                    group_anchor.message_meta = {
                        **_message_meta(group_anchor),
                        "wake_policy": "structured_mentions_only",
                        "owner_deferred_until_specialist_result": True,
                    }
                await db.commit()
                await _publish_project_run_group_turn(project_run.id)
                return dict(project_run.output)
        existing_child = (
            await db.execute(
                select(SubagentRun)
                .where(
                    SubagentRun.parent_session_id == parent.id,
                    SubagentRun.project_member_id == member.id,
                    SubagentRun.origin_tool_call_id
                    == _project_member_origin_tool_call_id(
                        member,
                        execution_user_id,
                        project.owner_user_id,
                    ),
                )
                .order_by(SubagentRun.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        await db.commit()

    async with async_session() as db:
        current_project = await db.get(Project, project_run.project_id)
        runtime_ready = current_project is not None and (
            current_project.status == "running"
            or (project_run.trigger_type == "leader_kickoff" and current_project.status == "initializing")
            or (
                project_run.trigger_type == "group_leader_message"
                and current_project.status in {"planning", "paused", "waiting", "completed"}
            )
        )
        if not runtime_ready:
            return {"status": "paused" if current_project is not None else "gone"}

    try:
        created = False
        if existing_child is None:
            child, created = await create_subagent(
                agent_id=agent_id,
                execution_user_id=execution_user_id,
                parent_session_id=str(group_session_id),
                origin_tool_call_id=_project_member_origin_tool_call_id(
                    member,
                    execution_user_id,
                    project.owner_user_id,
                ),
                task=task,
                mode="async",
                fork=True,
                turn_anchor_id=anchor_id,
                project_run_id=project_run_id,
                input_metadata={
                    "project_dispatch": True,
                    "project_a2a": project_run.trigger_type == "a2a",
                    "a2a_session_id": str(parent.id) if project_run.trigger_type == "a2a" else None,
                    "project_execution_tools_enabled": dispatch.get(
                        "execution_tools_enabled",
                        project.status == "running",
                    ),
                    "project_read_only_conversation": bool(
                        dispatch.get("read_only_conversation")
                    ),
                },
            )
            child_id = child.id
            child_status = child.status
        else:
            child_id = existing_child.id
            child_status = existing_child.status
        if not created and not await _project_run_has_child_input(child_id, project_run_id):
            child_status = await append_subagent_message(
                agent_id=agent_id,
                parent_session_id=str(group_session_id),
                subagent_id=str(child_id),
                message=task,
                execution_user_id=execution_user_id,
                origin_tool_call_id=f"project-dispatch:{project_run_id}",
                project_run_id=project_run_id,
                input_metadata={
                    "project_dispatch": True,
                    "project_a2a": project_run.trigger_type == "a2a",
                    "a2a_session_id": str(parent.id) if project_run.trigger_type == "a2a" else None,
                    "project_execution_tools_enabled": dispatch.get(
                        "execution_tools_enabled",
                        project.status == "running",
                    ),
                    "project_read_only_conversation": bool(
                        dispatch.get("read_only_conversation")
                    ),
                },
            )
    except SubagentError:
        async with async_session() as db:
            failed_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
            if (
                failed_run is not None
                and failed_run.finished_at is None
                and failed_run.status not in TERMINAL_PROJECT_RUN_STATUSES
                and not dict(failed_run.output or {}).get("subagent_run_id")
            ):
                failed_run.status = "queued"
                failed_run.started_at = None
                failed_run.error = None
                failed_run.output = {
                    **dict(failed_run.output or {}),
                    "last_dispatch_error": "Project execution is temporarily unavailable",
                    "last_dispatch_error_at": datetime.now(UTC).isoformat(),
                }
                await db.commit()
        return {"status": "queued", "error": "Project execution is temporarily unavailable"}

    async with async_session() as db:
        project_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
        if project_run is None:
            return {"status": "gone"}
        dispatch = dict((project_run.input or {}).get("dispatch") or {})
        # The child may finish between append_subagent_message() and this
        # transaction. Its completion transaction writes finished_at first;
        # dispatch must enrich output without regressing that terminal fact.
        reconcile_project_run_terminal_state(project_run)
        if project_run.status in {"queued", "running"}:
            input_state = await _project_run_child_input_state(
                db,
                child_id=child_id,
                project_run_id=project_run.id,
            )
            if input_state == INPUT_PROCESSING:
                apply_run_status(project_run, "running")
            else:
                project_run.status = "queued"
                project_run.started_at = None
        project_run.output = {
            **{
                key: value
                for key, value in dict(project_run.output or {}).items()
                if key != "dispatch_claimed_at"
            },
            "subagent_run_id": str(child_id),
            "subagent_session_id": str(child_id),
            **(
                {
                    "session_id": str(parent.id),
                    "a2a_session_id": str(parent.id),
                }
                if project_run.trigger_type == "a2a"
                else {}
            ),
            "status": child_status,
            "subagent_status": child_status,
        }

        project = await db.get(Project, project_run.project_id, with_for_update=True)
        anchor = await db.get(ChatMessage, anchor_id, with_for_update=True)
        if project_run.trigger_type == "leader_kickoff" and project is not None:
            kickoff = dict(dispatch.get("kickoff") or {})
            project.status = "running"
            project.settings = {
                **dict(project.settings or {}),
                "kickoff": {
                    **kickoff,
                    "project_run_id": str(project_run.id),
                    "subagent_session_id": str(child_id),
                },
            }
            if anchor is not None:
                anchor.message_meta = {
                    **_message_meta(anchor),
                    "awakened_agent_ids": [str(agent_id)],
                    "project_run_id": str(project_run.id),
                    "subagent_session_id": str(child_id),
                }
            existing_event = (
                await db.execute(
                    select(ProjectEvent.id).where(
                        ProjectEvent.run_id == project_run.id,
                        ProjectEvent.event_type == "project.kickoff.confirmed",
                    )
                )
            ).scalar_one_or_none()
            if existing_event is None:
                add_event(
                    db,
                    project,
                    "project.kickoff.confirmed",
                    "User confirmed the Leader plan and autonomous execution started",
                    actor_user_id=project_run.initiated_by_user_id,
                    actor_agent_id=agent_id,
                    run_id=project_run.id,
                    metadata={
                        **kickoff,
                        "leader_agent_id": str(agent_id),
                        "awakened_agent_ids": [str(agent_id)],
                        "project_run_id": str(project_run.id),
                        "subagent_run_id": str(child_id),
                        "subagent_session_id": str(child_id),
                    },
                )
        elif project_run.trigger_type == "a2a" and anchor is not None:
            a2a = dict(dispatch.get("a2a") or {})
            anchor.message_meta = {
                **_message_meta(anchor),
                "project_run_id": str(project_run.id),
                "a2a_session_id": str(parent.id),
                "subagent_run_id": str(child_id),
                "subagent_session_id": str(child_id),
                "awakened_agent_ids": [str(agent_id)],
                "wake_policy": "single_explicit_project_target",
            }
            existing_event = (
                await db.execute(
                    select(ProjectEvent)
                    .where(
                        ProjectEvent.run_id == project_run.id,
                        ProjectEvent.event_type == "a2a.delivered",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            event_metadata = {
                "session_id": str(parent.id),
                "a2a_session_id": str(parent.id),
                "message_id": str(anchor.id),
                "subagent_run_id": str(child_id),
                "subagent_session_id": str(child_id),
                "from_agent_id": a2a.get("from_agent_id"),
                "to_agent_id": a2a.get("to_agent_id") or str(agent_id),
                "awakened_agent_ids": [str(agent_id)],
                "broadcast": False,
            }
            if existing_event is None and project is not None:
                add_event(
                    db,
                    project,
                    "a2a.delivered",
                    "Project A2A message delivered to one durable project Agent runtime",
                    actor_user_id=project_run.initiated_by_user_id,
                    actor_agent_id=(uuid.UUID(str(a2a["from_agent_id"])) if a2a.get("from_agent_id") else None),
                    from_agent_id=(uuid.UUID(str(a2a["from_agent_id"])) if a2a.get("from_agent_id") else None),
                    to_agent_id=agent_id,
                    run_id=project_run.id,
                    metadata=event_metadata,
                )
            elif existing_event is not None:
                existing_event.event_metadata = {
                    **dict(existing_event.event_metadata or {}),
                    **event_metadata,
                }
        elif anchor is not None:
            meta = _message_meta(anchor)
            awakened = list(dict.fromkeys([*meta.get("awakened_agent_ids", []), str(agent_id)]))
            subagent_rows = list(meta.get("subagent_runs", []))
            if not any(str(row.get("project_run_id")) == str(project_run.id) for row in subagent_rows):
                subagent_rows.append(
                    {
                        "project_run_id": str(project_run.id),
                        "run_id": str(child_id),
                        "session_id": str(child_id),
                        "agent_id": str(agent_id),
                        "status": child_status,
                    }
                )
            anchor.message_meta = {
                **meta,
                "awakened_agent_ids": awakened,
                "subagent_runs": subagent_rows,
            }
            existing_event = (
                await db.execute(
                    select(ProjectEvent)
                    .where(
                        ProjectEvent.project_id == project_run.project_id,
                        ProjectEvent.event_type == "group.message.created",
                        ProjectEvent.event_metadata["group_message_id"].as_string() == str(anchor.id),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_event is None and project is not None:
                add_event(
                    db,
                    project,
                    "group.message.created",
                    "Appended project group message and dispatched Leader plus structured mentions",
                    actor_user_id=project_run.initiated_by_user_id,
                    metadata={
                        "group_session_id": str(group_session_id),
                        "group_message_id": str(anchor.id),
                        "initiator_user_id": str(project_run.initiated_by_user_id),
                        "visible_to_group": True,
                        "mentioned_agent_ids": meta.get("mentions", []),
                        "default_leader_agent_id": meta.get("default_leader_agent_id"),
                        "awakened_agent_ids": awakened,
                        "subagent_runs": subagent_rows,
                        "zero_wake_default": False,
                    },
                )
            elif existing_event is not None:
                existing_event.event_metadata = {
                    **dict(existing_event.event_metadata or {}),
                    "awakened_agent_ids": awakened,
                    "subagent_runs": subagent_rows,
                }
        await db.commit()
        return {**dict(project_run.output or {}), "status": project_run.status}


async def _pending_project_dispatch_batch(
    *,
    after: ProjectDispatchCursor | None = None,
) -> tuple[list[uuid.UUID], ProjectDispatchCursor | None]:
    from app.models.project import Project, ProjectRun
    from app.services.project_service import reconcile_project_run_terminal_state

    async with async_session() as db:
        # Repair terminal timestamps independently so stale completed rows can
        # never consume capacity from the durable dispatch outbox query.
        repair_rows = (
            (
                await db.execute(
                    select(ProjectRun)
                    .where(
                        ProjectRun.status.in_(["queued", "running"]),
                        ProjectRun.trigger_type.in_(PROJECT_DISPATCH_TRIGGERS),
                        ProjectRun.finished_at.is_not(None),
                    )
                    .order_by(ProjectRun.created_at, ProjectRun.id)
                    .limit(PROJECT_DISPATCH_BATCH_SIZE)
                )
            )
            .scalars()
            .all()
        )
        repaired_rows = [
            row for row in repair_rows if reconcile_project_run_terminal_state(row)
        ]
        repaired = len(repaired_rows)
        if repaired:
            await db.commit()
            from app.services.project_group_turn_lifecycle import (
                reconcile_and_publish_project_run_group_turn,
            )

            for repaired_row in repaired_rows:
                await reconcile_and_publish_project_run_group_turn(repaired_row.id)

        # JSON ``as_string`` compiles to JSON_EXTRACT on SQLite and ->> on
        # PostgreSQL. Applying every durable marker in SQL ensures an arbitrary
        # number of active, already-dispatched Runs cannot starve newer work.
        query = (
            select(ProjectRun.id, ProjectRun.created_at)
            .join(Project, Project.id == ProjectRun.project_id)
            .where(
                or_(
                    Project.status == "running",
                    and_(
                        Project.status == "initializing",
                        ProjectRun.trigger_type == "leader_kickoff",
                    ),
                    and_(
                        Project.status.in_(["planning", "paused", "waiting", "completed"]),
                        ProjectRun.trigger_type == "group_leader_message",
                    ),
                ),
                ProjectRun.status.in_(["queued", "running"]),
                ProjectRun.trigger_type.in_(PROJECT_DISPATCH_TRIGGERS),
                ProjectRun.finished_at.is_(None),
                ProjectRun.input["dispatch"]["group_session_id"].as_string().is_not(None),
                ProjectRun.input["dispatch"]["project_member_id"].as_string().is_not(None),
                ProjectRun.input["dispatch"]["turn_anchor_id"].as_string().is_not(None),
                ProjectRun.input["dispatch"]["task"].as_string().is_not(None),
                ProjectRun.output["subagent_run_id"].as_string().is_(None),
                or_(
                    ProjectRun.output["last_dispatch_error"].as_string().is_(None),
                    ProjectRun.updated_at <= datetime.now(UTC) - timedelta(seconds=5),
                ),
            )
        )
        if after is not None:
            created_at, run_id = after
            query = query.where(
                or_(
                    ProjectRun.created_at > created_at,
                    and_(ProjectRun.created_at == created_at, ProjectRun.id > run_id),
                )
            )
        rows = (
            await db.execute(query.order_by(ProjectRun.created_at, ProjectRun.id).limit(PROJECT_DISPATCH_BATCH_SIZE))
        ).all()
        if not rows:
            return [], None
        last = rows[-1]
        return [row.id for row in rows], (last.created_at, last.id)


async def _pending_project_dispatch_runs() -> list[uuid.UUID]:
    pending, _cursor = await _pending_project_dispatch_batch()
    return pending


async def _recover_project_dispatch_outbox_once() -> bool:
    """Visit every queued dispatch once without retaining a database session."""

    cursor: ProjectDispatchCursor | None = None
    made_progress = False
    while True:
        pending, next_cursor = await _pending_project_dispatch_batch(after=cursor)
        if not pending:
            return made_progress
        for project_run_id in pending:
            try:
                result = await dispatch_project_run(project_run_id)
                made_progress = result.get("status") not in {"gone", "not_dispatchable"} or made_progress
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate each durable outbox row
                logger.exception(f"[subagent] project dispatch recovery failed run={project_run_id}: {exc}")
        if len(pending) < PROJECT_DISPATCH_BATCH_SIZE or next_cursor is None:
            return made_progress
        cursor = next_cursor
        await asyncio.sleep(0)


async def _dispatch_event_loop(*, project_scope: bool) -> None:
    startup_pass = True
    legacy_backlog = True
    loop = asyncio.get_running_loop()
    wake_event = _project_dispatch_wakeup if project_scope else _dispatch_wakeup
    signal_work = (
        _signal_project_dispatch_work if project_scope else _signal_dispatch_work
    )
    next_recovery_at = loop.time()
    next_legacy_recovery_at = loop.time()
    while True:
        legacy_pass = legacy_backlog or loop.time() >= next_legacy_recovery_at
        recovery_pass = startup_pass
        if startup_pass:
            startup_pass = False
            wake_event.clear()
            await asyncio.sleep(PARENT_EVENT_BATCH_DEBOUNCE_SECONDS)
        else:
            signaled = False
            try:
                await asyncio.wait_for(
                    wake_event.wait(),
                    timeout=max(
                        0.0,
                        min(next_recovery_at, next_legacy_recovery_at) - loop.time(),
                    ),
                )
                signaled = True
            except TimeoutError:
                pass
            wake_event.clear()
            recovery_pass = loop.time() >= next_recovery_at
            legacy_pass = legacy_backlog or loop.time() >= next_legacy_recovery_at
            if signaled:
                # One shared window coalesces ordinary parent events and
                # Project Leader replies before either queue is read.
                await asyncio.sleep(PARENT_EVENT_BATCH_DEBOUNCE_SECONDS)
        try:
            legacy_counts: list[int] = []
            legacy_ids: set[uuid.UUID] = set()
            pending = await _pending_parent_events(
                debounce_seconds=0,
                include_legacy=legacy_pass,
                project_scope=project_scope,
                legacy_count_out=legacy_counts,
                legacy_ids_out=legacy_ids,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - daemon must survive transient DB faults
            logger.exception(f"[subagent] parent event scan failed: {exc}")
            wake_event.set()
            await asyncio.sleep(1)
            continue
        made_progress = False
        legacy_retry_needed = False
        try:
            parent_batches, special_events = await _pending_parent_event_groups(pending)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - daemon must survive transient DB faults
            logger.exception(f"[subagent] parent event grouping failed: {exc}")
            wake_event.set()
            await asyncio.sleep(1)
            continue
        for message_ids in parent_batches:
            try:
                processed = await _dispatch_parent_event_batch(message_ids)
                made_progress = processed or made_progress
                if not processed:
                    legacy_retry_needed = legacy_retry_needed or bool(
                        legacy_ids.intersection(message_ids)
                    )
                    loop.call_later(DISPATCH_RETRY_INTERVAL_SECONDS, signal_work)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate each durable event
                logger.exception(
                    f"[subagent] parent event batch dispatch failed messages={message_ids}: {exc}"
                )
                legacy_retry_needed = legacy_retry_needed or bool(
                    legacy_ids.intersection(message_ids)
                )
                loop.call_later(DISPATCH_RETRY_INTERVAL_SECONDS, signal_work)
        for message_id in special_events:
            try:
                processed = await _dispatch_parent_event(message_id)
                made_progress = processed or made_progress
                if not processed:
                    legacy_retry_needed = (
                        legacy_retry_needed or message_id in legacy_ids
                    )
                    loop.call_later(DISPATCH_RETRY_INTERVAL_SECONDS, signal_work)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate each durable event
                logger.exception(f"[subagent] parent event dispatch failed message={message_id}: {exc}")
                legacy_retry_needed = legacy_retry_needed or message_id in legacy_ids
                loop.call_later(DISPATCH_RETRY_INTERVAL_SECONDS, signal_work)
        leader_groups: list[uuid.UUID] = []
        if project_scope:
            try:
                leader_groups = await _pending_project_leader_groups(
                    debounce_seconds=0
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - daemon survives scan faults
                logger.exception(
                    f"[subagent] project Leader queue scan failed: {exc}"
                )
                loop.call_later(DISPATCH_RETRY_INTERVAL_SECONDS, signal_work)
            for group_id in leader_groups:
                try:
                    made_progress = (
                        await _dispatch_project_leader_batch(
                            group_id,
                            debounce_seconds=0,
                        )
                        or made_progress
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - isolate each project
                    logger.exception(
                        "[subagent] project Leader batch dispatch failed "
                        f"group={group_id}: {exc}"
                    )
                    loop.call_later(DISPATCH_RETRY_INTERVAL_SECONDS, signal_work)
        if made_progress and (len(pending) >= 50 or len(leader_groups) >= 20):
            wake_event.set()
        if legacy_pass:
            legacy_backlog = bool(legacy_counts and legacy_counts[0] >= 50)
            if legacy_retry_needed:
                next_legacy_recovery_at = loop.time() + DISPATCH_RETRY_INTERVAL_SECONDS
            elif legacy_backlog and made_progress:
                wake_event.set()
                next_legacy_recovery_at = loop.time()
            elif legacy_backlog:
                next_legacy_recovery_at = (
                    loop.time() + DISPATCH_RECOVERY_INTERVAL_SECONDS
                )
            else:
                next_legacy_recovery_at = (
                    loop.time() + LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS
                )
        if recovery_pass and project_scope:
            try:
                await _recover_project_dispatch_outbox_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - daemon must survive transient scan faults
                logger.exception(f"[subagent] project dispatch recovery failed: {exc}")
            finally:
                next_recovery_at = loop.time() + DISPATCH_RECOVERY_INTERVAL_SECONDS
        elif recovery_pass:
            next_recovery_at = loop.time() + DISPATCH_RECOVERY_INTERVAL_SECONDS


async def _subagent_parent_dispatch_loop() -> None:
    """Dispatch ordinary Subagent parent events independently of projects."""

    await _dispatch_event_loop(project_scope=False)


async def _project_dispatch_loop() -> None:
    """Dispatch project replies and outbox work behind a fault boundary."""

    await _dispatch_event_loop(project_scope=True)


async def start_subagent_daemon() -> None:
    """Run durable child workers and the parent-event dispatcher together."""
    await asyncio.gather(
        _subagent_worker_loop(),
        _subagent_parent_dispatch_loop(),
        _project_dispatch_loop(),
    )
