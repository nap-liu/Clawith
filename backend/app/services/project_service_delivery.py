"""Project run terminal-state reconciliation and A2A delivery."""

from app.services.project_service_shared import *  # noqa: F403
from app.services.project_service_serialization import *  # noqa: F403
from app.services.project_service_serialization import (
    _project_a2a_receipt,
    _resolve_project_a2a_session_info,
)

def apply_run_status(run: ProjectRun, status: str) -> None:
    # A ProjectRun is an append-only execution fact. Retrying creates another
    # run; it must never move an already terminal row back to a live state.
    reconcile_project_run_terminal_state(run)
    if run.status in TERMINAL_PROJECT_RUN_STATUSES:
        return
    run.status = status
    now = datetime.now(timezone.utc)
    if status == "running" and run.started_at is None:
        run.started_at = now
    if status in TERMINAL_PROJECT_RUN_STATUSES:
        run.finished_at = now


def reconcile_project_run_terminal_state(run: ProjectRun) -> bool:
    """Repair the durable invariant ``finished_at => terminal status``.

    A previous dispatch race could commit ``running`` after the child turn had
    already written ``finished_at``. The timestamp is the stronger completion
    fact, so recovery promotes the row to a terminal status and never clears
    evidence/output. The operation is idempotent and safe in request/daemon
    recovery paths.
    """

    if run.finished_at is None or run.status in TERMINAL_PROJECT_RUN_STATUSES:
        return False
    run.status = "failed" if (run.error or "").strip() else "succeeded"
    if run.started_at is None:
        run.started_at = run.created_at or run.finished_at
    return True


async def reconcile_project_runs(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Repair stale non-terminal ProjectRuns for one tenant-scoped project."""

    conditions = [
        ProjectRun.project_id == project_id,
        ProjectRun.finished_at.is_not(None),
        ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
    ]
    if tenant_id is not None:
        conditions.append(ProjectRun.tenant_id == tenant_id)
    runs = (await db.execute(select(ProjectRun).where(*conditions))).scalars().all()
    return sum(reconcile_project_run_terminal_state(run) for run in runs)


async def deliver_project_a2a(run_id: uuid.UUID) -> None:
    """Consume one persisted project A2A run through the existing sender.

    This runs after the request transaction. Delivery status and its audit
    event are persisted in a fresh transaction, including truthful failures.
    """

    from app.services.agent_tools import _send_message_to_agent

    async with async_session() as db:
        run = await db.get(ProjectRun, run_id)
        if run is None or run.status != "queued" or run.trigger_type != "a2a":
            return
        project = await db.get(Project, run.project_id)
        if project is None:
            return
        if project.status != PROJECT_RUNTIME_STATUS_RUNNING:
            apply_run_status(run, "cancelled")
            run.error = "Member collaboration was cancelled because the project is paused"
            add_event(
                db,
                project,
                "a2a.cancelled",
                "Member collaboration was cancelled because the project is paused",
                actor_user_id=run.initiated_by_user_id,
                from_agent_id=uuid.UUID(str((run.input or {})["from_agent_id"])),
                to_agent_id=uuid.UUID(str((run.input or {})["to_agent_id"])),
                run_id=run.id,
                metadata={"reason": "project_paused"},
            )
            await db.commit()
            return
        payload = dict(run.input or {})
        project_id = run.project_id
        initiated_by_user_id = run.initiated_by_user_id
        from_agent_id = uuid.UUID(payload["from_agent_id"])
        to_agent_id = uuid.UUID(payload["to_agent_id"])
        active_member_ids = set(
            (
                await db.execute(
                    select(ProjectMemberSnapshot.agent_id).where(
                        ProjectMemberSnapshot.project_id == project.id,
                        ProjectMemberSnapshot.tenant_id == project.tenant_id,
                        ProjectMemberSnapshot.agent_id.in_([from_agent_id, to_agent_id]),
                        ProjectMemberSnapshot.is_enabled.is_(True),
                    )
                )
            ).scalars()
        )
        if active_member_ids != {from_agent_id, to_agent_id}:
            apply_run_status(run, "cancelled")
            run.error = "Member collaboration was cancelled because a member is no longer active"
            add_event(
                db,
                project,
                "a2a.cancelled",
                "Member collaboration was cancelled because a member is no longer active",
                actor_user_id=initiated_by_user_id,
                from_agent_id=from_agent_id,
                to_agent_id=to_agent_id,
                run_id=run.id,
                metadata={"reason": "project_member_departed"},
            )
            await db.commit()
            return
        mode_map = {"delegate": "task_delegate", "review": "consult"}
        msg_type = mode_map.get(payload.get("mode"), payload.get("mode", "notify"))
        apply_run_status(run, "running")
        await db.commit()

    try:
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_id": payload["to_agent_id"],
                "message": payload["message"],
                "msg_type": msg_type,
                "force_async": True,
                "new_conversation": bool(payload.get("new_conversation")),
                "_project_id": str(project_id),
                "_project_run_id": str(run_id),
            },
            user_id=initiated_by_user_id,
        )
    except Exception:  # delivery failures must become durable run state
        logger.exception("Project member collaboration delivery failed for run {}", run_id)
        result = "❌ Member collaboration could not be delivered"
    receipt, _ = _project_a2a_receipt(result)
    failed = result.startswith("❌") or bool(receipt and receipt.get("status") == "error")
    session_info: dict[str, Any] = {}
    async with async_session() as session_db:
        source_id = uuid.UUID(payload["from_agent_id"])
        target_id = uuid.UUID(payload["to_agent_id"])
        session_info, identity_error = await _resolve_project_a2a_session_info(
            session_db,
            project_id=project_id,
            project_run_id=run_id,
            source_agent_id=source_id,
            target_agent_id=target_id,
            result=result,
        )
        if identity_error:
            failed = True
            logger.warning(
                "Project member collaboration receipt rejected for run {}: {}",
                run_id,
                identity_error,
            )
            result = "❌ Member collaboration could not be delivered"
    async with async_session() as db:
        run = await db.get(ProjectRun, run_id)
        project = await db.get(Project, run.project_id) if run else None
        if run is None or project is None:
            return
        if failed:
            apply_run_status(run, "failed")
        elif not dict(run.output or {}).get("subagent_run_id"):
            # Compatibility for non-native/legacy transports that confirm
            # delivery but do not create a project execution child. Native
            # project A2A remains live until the durable child turn finishes.
            apply_run_status(run, "succeeded")
        delivery_metadata = {
            "delivery_status": "failed" if failed else "delivered",
            "group_session_id": payload.get("group_session_id"),
            **session_info,
        }
        run.output = {**dict(run.output or {}), **delivery_metadata}
        if failed:
            run.error = "Member collaboration could not be delivered"
        existing_delivery_event = (
            await db.execute(
                select(ProjectEvent.id).where(
                    ProjectEvent.run_id == run.id,
                    ProjectEvent.event_type == ("a2a.delivery_failed" if failed else "a2a.delivered"),
                )
            )
        ).scalar_one_or_none()
        if existing_delivery_event is None:
            add_event(
                db,
                project,
                "a2a.delivery_failed" if failed else "a2a.delivered",
                "Member collaboration could not be delivered" if failed else "Member collaboration message delivered",
                actor_user_id=run.initiated_by_user_id,
                actor_agent_id=uuid.UUID(payload["from_agent_id"]),
                from_agent_id=uuid.UUID(payload["from_agent_id"]),
                to_agent_id=uuid.UUID(payload["to_agent_id"]),
                work_item_id=run.work_item_id,
                run_id=run.id,
                metadata=delivery_metadata,
            )
        await db.commit()
