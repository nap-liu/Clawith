"""Project-group adapter for the normalized conversation turn lifecycle."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import ProjectRun
from app.services.confirmation_service import find_pending_confirmation
from app.services.conversation_turn_lifecycle import (
    ConversationTurnSnapshot,
    get_conversation_turn_snapshot,
    publish_conversation_turn_event,
    transition_conversation_turn,
)
from app.services.project_service import TERMINAL_PROJECT_RUN_STATUSES

GROUP_RUN_TRIGGERS = ("group_leader_message", "group_mention")


@dataclass(frozen=True, slots=True)
class ProjectGroupTurnProjection:
    snapshot: ConversationTurnSnapshot
    anchor_message_id: uuid.UUID | None
    active_agent_ids: tuple[uuid.UUID, ...]
    run_count: int

    def to_client_dict(self) -> dict:
        return {
            **self.snapshot.to_client_dict(),
            "anchor_message_id": (
                str(self.anchor_message_id) if self.anchor_message_id is not None else None
            ),
            "active_agent_ids": [str(value) for value in self.active_agent_ids],
            "run_count": self.run_count,
        }


async def project_group_timeline_anchor_for_child_turn(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    child_session_id: uuid.UUID,
    child_turn_anchor_id: uuid.UUID,
) -> uuid.UUID | None:
    """Resolve the Human group message that owns one child timeline turn.

    A later Human message may join an existing lifecycle cohort. Timeline
    placement still follows the exact ProjectRun cause so live and history
    ordering remain identical.
    """

    child_anchor = await db.get(ChatMessage, child_turn_anchor_id)
    if (
        child_anchor is None
        or child_anchor.conversation_id != str(child_session_id)
        or child_anchor.role != "user"
    ):
        return None
    metadata = (
        dict(child_anchor.message_meta)
        if isinstance(child_anchor.message_meta, dict)
        else {}
    )
    try:
        project_run_id = uuid.UUID(str(metadata.get("project_run_id")))
    except (TypeError, ValueError):
        return None
    project_run = await db.get(ProjectRun, project_run_id)
    if (
        project_run is None
        or project_run.project_id != project_id
        or project_run.trigger_type not in GROUP_RUN_TRIGGERS
    ):
        return None
    run_input = project_run.input if isinstance(project_run.input, dict) else {}
    try:
        return uuid.UUID(str(run_input.get("group_message_id")))
    except (TypeError, ValueError):
        return None


async def find_project_group_blocking_confirmation(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    session_id: uuid.UUID,
):
    """Return a force-confirmation that blocks this group's next admission."""

    runs = list(
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.trigger_type.in_(GROUP_RUN_TRIGGERS),
                    ProjectRun.input["group_session_id"].as_string() == str(session_id),
                    ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
                )
            )
        ).scalars()
    )
    for run in runs:
        output = run.output if isinstance(run.output, dict) else {}
        raw_child_id = (
            output.get("session_id")
            or output.get("subagent_session_id")
            or output.get("subagent_run_id")
        )
        try:
            child_id = uuid.UUID(str(raw_child_id))
        except (TypeError, ValueError):
            continue
        child = await db.get(ChatSession, child_id)
        if child is None or child.project_id != project_id:
            continue
        pending = await find_pending_confirmation(
            db,
            agent_id=child.agent_id,
            conversation_id=str(child.id),
        )
        if pending is not None and pending.force_confirmation:
            return pending
    return None


async def publish_project_group_turn_event(
    *,
    session: ChatSession,
    projection: ProjectGroupTurnProjection,
    payload: dict,
    event_kind: str,
) -> None:
    """Publish a Project cohort through the platform turn envelope."""

    turn = projection.to_client_dict()
    await publish_conversation_turn_event(
        agent_id=session.agent_id,
        conversation_id=str(session.id),
        payload={**payload, "turn": turn},
        snapshot=projection.snapshot,
        event_kind=event_kind,
    )


async def reconcile_and_publish_project_group_turn(
    *,
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: dict,
    event_kind: str,
) -> ProjectGroupTurnProjection | None:
    """Commit the cohort projection before emitting its observable event."""

    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        if (
            session is None
            or session.project_id != project_id
            or session.source_channel != "project"
        ):
            return None
        projection = await reconcile_project_group_turn(
            db,
            project_id=project_id,
            session=session,
        )
        await db.commit()
    await publish_project_group_turn_event(
        session=session,
        projection=projection,
        payload=payload,
        event_kind=event_kind,
    )
    return projection


async def reconcile_and_publish_project_run_group_turn(
    project_run_id: uuid.UUID,
) -> ProjectGroupTurnProjection | None:
    """Publish the cohort affected by one committed ProjectRun state change."""

    async with async_session() as db:
        run = await db.get(ProjectRun, project_run_id)
        if run is None or run.trigger_type not in GROUP_RUN_TRIGGERS:
            return None
        run_input = run.input if isinstance(run.input, dict) else {}
        dispatch = run_input.get("dispatch")
        dispatch = dispatch if isinstance(dispatch, dict) else {}
        raw_session_id = run_input.get("group_session_id") or dispatch.get(
            "group_session_id"
        )
        try:
            session_id = uuid.UUID(str(raw_session_id))
        except (TypeError, ValueError):
            return None
        session = await db.get(ChatSession, session_id)
        if (
            session is None
            or session.project_id != run.project_id
            or session.source_channel != "project"
        ):
            return None
        projection = await reconcile_project_group_turn(
            db,
            project_id=run.project_id,
            session=session,
        )
        await db.commit()
    await publish_project_group_turn_event(
        session=session,
        projection=projection,
        payload={"type": "turn_state"},
        event_kind="turn_lifecycle",
    )
    return projection


async def reconcile_and_publish_project_group_turns(
    project_id: uuid.UUID,
) -> tuple[ProjectGroupTurnProjection, ...]:
    """Publish every group-session cohort changed by one project mutation."""

    async with async_session() as db:
        sessions = list(
            (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.project_id == project_id,
                        ChatSession.source_channel == "project",
                        ChatSession.is_group.is_(True),
                    )
                )
            ).scalars()
        )
        projected = [
            (
                session,
                await reconcile_project_group_turn(
                    db,
                    project_id=project_id,
                    session=session,
                ),
            )
            for session in sessions
        ]
        await db.commit()
    for session, projection in projected:
        await publish_project_group_turn_event(
            session=session,
            projection=projection,
            payload={"type": "turn_state"},
            event_kind="turn_lifecycle",
        )
    return tuple(projection for _session, projection in projected)


async def reconcile_project_group_turn(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    session: ChatSession,
) -> ProjectGroupTurnProjection:
    """Fold the latest Human group message's Run cohort into one lifecycle.

    The exact group ChatSession is the cohort mutex.  Lock it before reading
    anchors, the current pointer, or Runs so a waiting reader cannot calculate a
    terminal target from a view that predates a concurrently committed Run.
    This also preserves the platform-wide session -> anchor lock order.
    """

    locked_session = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.id == session.id,
                ChatSession.agent_id == session.agent_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_session is None:
        raise LookupError("project group lifecycle session not found")
    session = locked_session

    latest_anchor = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.role == "user",
                ChatMessage.message_meta["kind"].as_string() == "project_group_message",
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    snapshot = await get_conversation_turn_snapshot(
        db,
        agent_id=session.agent_id,
        conversation_id=str(session.id),
    )
    if latest_anchor is None:
        return ProjectGroupTurnProjection(snapshot, None, (), 0)

    anchor = latest_anchor
    if snapshot.phase in {"active", "suspended"} and snapshot.anchor_id is not None:
        existing_anchor = await db.get(ChatMessage, snapshot.anchor_id)
        if existing_anchor is not None:
            # A Project group may accept another Human message while member Runs
            # are still active. It joins the same cohort/generation instead of
            # creating a second visible progress turn.
            anchor = existing_anchor

    runs = list(
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.trigger_type.in_(GROUP_RUN_TRIGGERS),
                    ProjectRun.input["group_session_id"].as_string() == str(session.id),
                    ProjectRun.created_at >= anchor.created_at,
                )
            )
        ).scalars()
    )
    nonterminal = [run for run in runs if run.status not in TERMINAL_PROJECT_RUN_STATUSES]
    active_agent_ids = tuple(
        dict.fromkeys(run.agent_id for run in nonterminal if run.agent_id is not None)
    )

    if nonterminal:
        suspended = True
        for run in nonterminal:
            if run.status != "waiting" or run.agent_id is None:
                suspended = False
                break
            output = run.output if isinstance(run.output, dict) else {}
            child_session_id = (
                output.get("session_id")
                or output.get("subagent_session_id")
                or output.get("subagent_run_id")
            )
            if not child_session_id:
                suspended = False
                break
            pending = await find_pending_confirmation(
                db,
                agent_id=run.agent_id,
                conversation_id=str(child_session_id),
            )
            if pending is None:
                suspended = False
                break
        target_status = "suspended" if suspended else "running"
    else:
        target_status = (
            "completed"
            if runs and all(run.status == "succeeded" for run in runs)
            else "failed"
        )

    snapshot = await transition_conversation_turn(
        db,
        agent_id=session.agent_id,
        conversation_id=str(session.id),
        turn_anchor_id=anchor.id,
        status=target_status,
        state_token="|".join(
            [
                str(anchor.id),
                target_status,
                *sorted(
                    f"{run.id}:{run.agent_id}"
                    for run in nonterminal
                ),
            ]
        ),
    )
    return ProjectGroupTurnProjection(
        snapshot=snapshot,
        anchor_message_id=anchor.id,
        active_agent_ids=active_agent_ids,
        run_count=len(nonterminal),
    )
