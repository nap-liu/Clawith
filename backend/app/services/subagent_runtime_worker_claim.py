"""Durable subagent claim and inbox loading helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_project_common import _project_parallel_run_limit

async def _claim_subagent(
    run_id: uuid.UUID | None = None,
    *,
    with_token: bool = False,
) -> uuid.UUID | tuple[uuid.UUID, str] | None:
    now = datetime.now(UTC)
    async with async_session() as db:
        lease_conditions = [
            or_(
                SubagentRun.status == RUN_QUEUED,
                and_(
                    SubagentRun.status == RUN_RUNNING,
                    SubagentRun.lease_expires_at < now,
                ),
            ),
        ]
        standard_conditions = [*lease_conditions, SubagentRun.project_id.is_(None)]
        if run_id is not None:
            standard_conditions.append(SubagentRun.id == run_id)
        standard_run = (
            await db.execute(
                select(SubagentRun)
                .where(*standard_conditions)
                .order_by(SubagentRun.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if standard_run is not None:
            lease_owner = (
                f"{settings.INSTANCE_ID}:{uuid.uuid4()}"
                if with_token
                else settings.INSTANCE_ID
            )
            standard_run.status = RUN_RUNNING
            standard_run.lease_owner = lease_owner
            standard_run.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            await db.commit()
            return (standard_run.id, lease_owner) if with_token else standard_run.id

        from app.models.project import Project, ProjectRun

        advisory_project_run_exists = exists(
            select(ProjectRun.id).where(
                ProjectRun.project_id == SubagentRun.project_id,
                ProjectRun.trigger_type == "group_leader_message",
                ProjectRun.status.in_(["queued", "running"]),
                ProjectRun.finished_at.is_(None),
                ProjectRun.output["subagent_run_id"].as_string()
                == cast(SubagentRun.id, String),
            )
        )

        project_conditions = [
            *lease_conditions,
            SubagentRun.project_id.is_not(None),
            exists(
                select(Project.id).where(
                    Project.id == SubagentRun.project_id,
                    or_(
                        Project.status.in_(["running", "planning"]),
                        and_(
                            Project.status.in_(["paused", "waiting", "completed"]),
                            advisory_project_run_exists,
                        ),
                    ),
                )
            ),
        ]
        skipped_ids: set[uuid.UUID] = set()
        saturated_project_ids: set[uuid.UUID] = set()
        while True:
            candidate_conditions = list(project_conditions)
            if run_id is not None:
                candidate_conditions.append(SubagentRun.id == run_id)
            else:
                if skipped_ids:
                    candidate_conditions.append(SubagentRun.id.notin_(skipped_ids))
                if saturated_project_ids:
                    candidate_conditions.append(SubagentRun.project_id.notin_(saturated_project_ids))
            run = (
                await db.execute(
                    select(SubagentRun)
                    .where(*candidate_conditions)
                    .order_by(SubagentRun.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if run is None:
                return None
            if await _project_accepts_new_subagent_anchor(db, run):
                processing = (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(run.id),
                            ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                            ChatMessage.message_meta["subagent_input_state"].as_string()
                            == INPUT_PROCESSING,
                        )
                        .order_by(ChatMessage.created_at, ChatMessage.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                pending = None
                if processing is None:
                    pending = (
                        await db.execute(
                            select(ChatMessage)
                            .where(
                                ChatMessage.conversation_id == str(run.id),
                                ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                                ChatMessage.message_meta["subagent_input_state"].as_string()
                                == INPUT_PENDING,
                            )
                            .order_by(ChatMessage.created_at, ChatMessage.id)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                capacity_input = processing or pending
                capacity_available = processing is not None or capacity_input is None
                if not capacity_available:
                    if await _normalize_pending_project_run(db, run=run, input_row=capacity_input):
                        await db.commit()
                        continue
                    allowed, _blocked = await _partition_project_inputs_by_capacity(
                        db,
                        run=run,
                        input_rows=[capacity_input],
                    )
                    capacity_available = bool(allowed)
                if capacity_available:
                    lease_owner = (
                        f"{settings.INSTANCE_ID}:{uuid.uuid4()}"
                        if with_token
                        else settings.INSTANCE_ID
                    )
                    run.status = RUN_RUNNING
                    run.lease_owner = lease_owner
                    run.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
                    await db.commit()
                    return (run.id, lease_owner) if with_token else run.id
                if run.project_id is not None:
                    saturated_project_ids.add(run.project_id)
            if run_id is not None:
                return None
            skipped_ids.add(run.id)


def _input_project_run_id(row: ChatMessage) -> uuid.UUID | None:
    raw_project_run_id = _message_meta(row).get("project_run_id")
    try:
        return uuid.UUID(str(raw_project_run_id))
    except (TypeError, ValueError):
        return None


async def _normalize_pending_project_run(
    db,
    *,
    run: SubagentRun,
    input_row: ChatMessage,
) -> bool:
    """Repair a pre-gate Run only when its exact input never entered processing."""
    project_run_id = _input_project_run_id(input_row)
    if project_run_id is None or run.project_id is None:
        return False

    from app.models.project import ProjectRun

    project_run = await db.get(ProjectRun, project_run_id, with_for_update=True)
    if (
        project_run is None
        or project_run.project_id != run.project_id
        or project_run.status != "running"
        or project_run.finished_at is not None
    ):
        return False
    processing_exists = await db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.message_meta["project_run_id"].as_string() == str(project_run.id),
            ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
        )
        .limit(1)
    )
    if processing_exists is not None:
        return False
    project_run.status = "queued"
    project_run.started_at = None
    project_run.output = {
        key: value
        for key, value in dict(project_run.output or {}).items()
        if key != "dispatch_claimed_at"
    }
    return True


async def _partition_project_inputs_by_capacity(
    db,
    *,
    run: SubagentRun,
    input_rows: list[ChatMessage],
) -> tuple[list[ChatMessage], list[ChatMessage]]:
    """Select project inputs that may atomically enter processing now."""
    if run.project_id is None or not input_rows:
        return list(input_rows), []

    from app.models.project import Project, ProjectRun

    project = await db.get(Project, run.project_id, with_for_update=True)
    if project is None:
        return [], list(input_rows)
    project_run_ids = {
        project_run_id
        for row in input_rows
        for project_run_id in [_input_project_run_id(row)]
        if project_run_id is not None
    }
    if not project_run_ids:
        return list(input_rows), []
    project_runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.id.in_(project_run_ids),
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {project_run.id: project_run for project_run in project_runs}
    running_tasks = int(
        await db.scalar(
            select(func.count(ProjectRun.id)).where(
                ProjectRun.project_id == project.id,
                ProjectRun.tenant_id == project.tenant_id,
                ProjectRun.status == "running",
                ProjectRun.finished_at.is_(None),
                ProjectRun.id.notin_(project_run_ids),
            )
        )
        or 0
    )
    remaining = max(0, _project_parallel_run_limit(project) - running_tasks)
    reserved_ids: set[uuid.UUID] = set()
    allowed: list[ChatMessage] = []
    blocked: list[ChatMessage] = []
    for row in input_rows:
        project_run_id = _input_project_run_id(row)
        project_run = by_id.get(project_run_id) if project_run_id is not None else None
        uses_slot = (
            project_run is not None
            and project_run.finished_at is None
            and project_run.status not in {"succeeded", "failed", "cancelled"}
        )
        if not uses_slot or project_run.id in reserved_ids:
            allowed.append(row)
        elif remaining > 0:
            reserved_ids.add(project_run.id)
            remaining -= 1
            allowed.append(row)
        else:
            blocked.append(row)
    return allowed, blocked


async def _mark_input_project_runs_running(
    db,
    *,
    run: SubagentRun,
    input_rows: list[ChatMessage],
) -> None:
    """Advance only the ProjectRuns explicitly owned by active child inputs.

    A project child is reusable, so neither its session id nor its claim time
    identifies which ProjectRun is executing.  The durable input row is the
    outbox/inbox hand-off and carries the exact ``project_run_id``.  Updating
    from that association in the same transaction that marks the input as
    processing makes the transition idempotent and safe when an expired lease
    is reclaimed after restart.
    """
    if run.project_id is None:
        return

    project_run_ids: set[uuid.UUID] = set()
    for row in input_rows:
        raw_project_run_id = _message_meta(row).get("project_run_id")
        if not raw_project_run_id:
            continue
        try:
            project_run_ids.add(uuid.UUID(str(raw_project_run_id)))
        except (TypeError, ValueError):
            logger.warning(
                "[subagent] ignoring invalid project_run_id on active input=%s",
                row.id,
            )
    if not project_run_ids:
        return

    from app.models.project import ProjectRun
    from app.services.project_service import apply_run_status

    project_runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.id.in_(project_run_ids),
                    ProjectRun.project_id == run.project_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for project_run in project_runs:
        # apply_run_status reconciles finished_at first and never regresses a
        # succeeded/failed/cancelled execution back to a live state.
        apply_run_status(project_run, "running")


async def _load_or_start_input(
    run_id: uuid.UUID,
) -> tuple[ChatMessage, bool] | None:
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if not _owns_subagent_lease(run):
            return None
        processing = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run_id),
                    ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                    ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        anchor = processing
        recovering = anchor is not None
        if anchor is None:
            anchor = (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
        if anchor is None:
            run.status = RUN_COMPLETED
            run.lease_owner = None
            run.lease_expires_at = None
            await db.commit()
            return None
        project_run_id = _input_project_run_id(anchor)
        if not recovering and not await _project_accepts_new_subagent_anchor(
            db,
            run,
            authorized_project_run_id=project_run_id,
        ):
            # Keep the pending input durable and release this worker.  The
            # canonical claim query will pick it up after the project resumes.
            run.status = RUN_QUEUED
            run.lease_owner = None
            run.lease_expires_at = None
            await db.commit()
            return None
        if not recovering:
            allowed, _blocked = await _partition_project_inputs_by_capacity(
                db,
                run=run,
                input_rows=[anchor],
            )
            if not allowed:
                run.status = RUN_QUEUED
                run.lease_owner = None
                run.lease_expires_at = None
                await db.commit()
                return None
        from app.services.conversation_turn_lifecycle import transition_conversation_turn

        await transition_conversation_turn(
            db,
            agent_id=anchor.agent_id,
            conversation_id=str(run_id),
            turn_anchor_id=anchor.id,
            status="running",
        )
        meta = _message_meta(anchor)
        meta.update(
            {
                "subagent_input_state": INPUT_PROCESSING,
                "subagent_turn_anchor_id": str(anchor.id),
            }
        )
        anchor.message_meta = meta
        await db.flush()
        active_inputs = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string() == str(anchor.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        await _mark_input_project_runs_running(
            db,
            run=run,
            input_rows=list(active_inputs),
        )
        run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return anchor, recovering


async def _assert_subagent_running(run_id: uuid.UUID) -> None:
    """Fail before a new round/tool side effect after stop, lease loss, or revocation."""
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id)
        if run is None or run.status == RUN_CANCELLED:
            raise asyncio.CancelledError
        if not _owns_subagent_lease(run):
            raise RuntimeError("Subagent lease lost")
        child = await db.get(ChatSession, run_id)
        if child is None:
            raise RuntimeError("Subagent session no longer exists")
        await _validate_execution_identity(db, run, child)


async def _drain_subagent_inbox(
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
    *,
    before_injection=None,
) -> list[dict]:
    """Atomically inject appended parent messages at an LLM round boundary."""
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status == RUN_CANCELLED:
            raise asyncio.CancelledError
        if not _owns_subagent_lease(run):
            raise RuntimeError("Subagent lease lost")
        pending = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        allowed, _blocked = await _partition_project_inputs_by_capacity(
            db,
            run=run,
            input_rows=list(pending),
        )
        if allowed and before_injection is not None:
            earliest_injection = min(row.created_at for row in allowed)
            await before_injection(
                created_at=earliest_injection - timedelta(microseconds=1)
            )

        injected: list[dict] = []
        for row in allowed:
            meta = _message_meta(row)
            meta.update(
                {
                    "subagent_input_state": INPUT_PROCESSING,
                    "subagent_turn_anchor_id": str(anchor_id),
                    "turn_status": "running",
                }
            )
            row.message_meta = meta
            injected.append({"role": "user", "content": row.content})
        await _mark_input_project_runs_running(
            db,
            run=run,
            input_rows=list(allowed),
        )
        run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        return injected


async def _park_subagent_confirmation(
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
) -> bool:
    """Release a child lease when the unified caller suspended on confirmation.

    The confirmation tool row remains the single durable truth.  A pending row
    parks the Run; a row resolved before this worker observes it re-queues the
    same processing anchor for standard restart recovery.  No confirmation
    transcript or continuation logic is duplicated here.
    """
    from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME

    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status == RUN_CANCELLED:
            return False
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.role == "tool_call",
                        ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor_id),
                    )
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                )
            )
            .scalars()
            .all()
        )
        confirmation_payload = None
        for row in rows:
            try:
                payload = json.loads(row.content or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME:
                confirmation_payload = payload
                break
        if confirmation_payload is None:
            return False
        pending = confirmation_payload.get("status") == "pending"
        run.status = RUN_WAITING if pending else RUN_QUEUED
        run.lease_owner = None
        run.lease_expires_at = None
        anchor = await db.get(ChatMessage, anchor_id)
        if anchor is not None:
            anchor.message_meta = {
                **_message_meta(anchor),
                # The Run waits for confirmation, while the normalized
                # conversation Turn is suspended.  Keep both current-session
                # and exact-anchor snapshots on the shared lifecycle contract.
                "turn_status": "suspended" if pending else "running",
            }
        from app.models.project import Project, ProjectEvent, ProjectRun, ProjectWorkItem
        from app.services.project_service import add_event

        processing = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string() == str(anchor_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        project_run_ids = {
            uuid.UUID(str(value)) for row in processing for value in [_message_meta(row).get("project_run_id")] if value
        }
        if project_run_ids:
            project_runs = (
                (
                    await db.execute(
                        select(ProjectRun).where(
                            ProjectRun.id.in_(project_run_ids),
                            ProjectRun.project_id == run.project_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for project_run in project_runs:
                if project_run.status not in {"succeeded", "failed", "cancelled"}:
                    project_run.status = "waiting" if pending else "queued"
                if pending:
                    existing = (
                        await db.execute(
                            select(ProjectEvent.id).where(
                                ProjectEvent.run_id == project_run.id,
                                ProjectEvent.event_type == "run.waiting_confirmation",
                            )
                        )
                    ).scalar_one_or_none()
                    project = await db.get(Project, project_run.project_id)
                    if existing is None and project is not None:
                        add_event(
                            db,
                            project,
                            "run.waiting_confirmation",
                            "本次执行正在等待人工确认",
                            actor_agent_id=project_run.agent_id,
                            work_item_id=project_run.work_item_id,
                            run_id=project_run.id,
                            metadata={
                                "project_run_id": str(project_run.id),
                                "session_id": str(run.id),
                                "subagent_session_id": str(run.id),
                            },
                        )
        await db.commit()
        if run.project_id is not None:
            from app.services.project_group_turn_lifecycle import (
                reconcile_and_publish_project_group_turn,
            )

            await reconcile_and_publish_project_group_turn(
                project_id=run.project_id,
                session_id=run.parent_session_id,
                payload={"type": "turn_state"},
                event_kind="turn_lifecycle",
            )
        return True
