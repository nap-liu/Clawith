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
from app.models.subagent_run import SubagentRun
from app.services.confirmation_service import find_pending_confirmation
from app.services.conversation_turn_lifecycle import (
    ConversationTurnSnapshot,
    conversation_turn_snapshot_for_session,
    get_conversation_turn_snapshot,
    publish_conversation_turn_event,
    transition_conversation_turn,
)
from app.services.project_service import TERMINAL_PROJECT_RUN_STATUSES

GROUP_RUN_TRIGGERS = (
    "group_leader_message",
    "group_mention",
    "leader_kickoff",
    "manual",
    "leader",
    "schedule",
    "retry",
)
EXTERNAL_CONTINUATION_KIND = "project_subagent_external_continuation"
GROUP_ANCHOR_KINDS = (
    "project_group_message",
    "project_kickoff_confirmation",
    "project_run_request",
    EXTERNAL_CONTINUATION_KIND,
)


def project_run_group_anchor_id(run: ProjectRun) -> uuid.UUID | None:
    """Return the exact visible group anchor that caused one ProjectRun."""

    run_input = run.input if isinstance(run.input, dict) else {}
    dispatch = run_input.get("dispatch")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    raw_anchor_id = run_input.get("group_message_id") or dispatch.get(
        "turn_anchor_id"
    )
    try:
        return uuid.UUID(str(raw_anchor_id))
    except (TypeError, ValueError):
        return None


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


@dataclass(frozen=True, slots=True)
class _CausalChildCohort:
    child_ids: frozenset[uuid.UUID]
    active_child_ids: frozenset[uuid.UUID]
    active_agent_ids: tuple[uuid.UUID, ...]
    children: tuple[ChatSession, ...]
    all_active_suspended: bool
    all_succeeded: bool
    state_tokens: tuple[str, ...]


async def _load_causal_child_cohort(
    db: AsyncSession,
    *,
    session: ChatSession,
    anchor_id: uuid.UUID | None,
    generation: int,
) -> _CausalChildCohort:
    """Load exact direct-child ownership for one parent generation."""

    if anchor_id is None or generation < 1:
        return _CausalChildCohort(frozenset(), frozenset(), (), (), False, False, ())

    from app.services.subagent_runtime import (
        CAUSAL_ROOT_ANCHOR_ID,
        CAUSAL_ROOT_GENERATION,
        CAUSAL_ROOT_SESSION_ID,
        INPUT_DONE,
        INPUT_PENDING,
        INPUT_PROCESSING,
        RUN_COMPLETED,
        RUN_QUEUED,
        RUN_RUNNING,
        RUN_WAITING,
        SUBAGENT_INPUT,
    )

    parent_runs = list(
        (
            await db.execute(
                select(SubagentRun).where(SubagentRun.parent_session_id == session.id)
            )
        ).scalars()
    )
    candidate_child_ids = {run.id for run in parent_runs}
    inputs = (
        list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id.in_(
                            [str(child_id) for child_id in candidate_child_ids]
                        ),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta[CAUSAL_ROOT_SESSION_ID].as_string()
                        == str(session.id),
                        ChatMessage.message_meta[CAUSAL_ROOT_ANCHOR_ID].as_string()
                        == str(anchor_id),
                        ChatMessage.message_meta[CAUSAL_ROOT_GENERATION].as_integer()
                        == generation,
                    )
                )
            ).scalars()
        )
        if candidate_child_ids
        else []
    )
    child_ids = frozenset(uuid.UUID(row.conversation_id) for row in inputs)
    run_by_id = {run.id: run for run in parent_runs if run.id in child_ids}
    children = (
        tuple(
            (
                await db.execute(select(ChatSession).where(ChatSession.id.in_(child_ids)))
            ).scalars()
        )
        if child_ids
        else ()
    )
    child_by_id = {child.id: child for child in children}
    input_states_by_child: dict[uuid.UUID, list[str]] = {}
    state_tokens: list[str] = []
    for row in inputs:
        child_id = uuid.UUID(row.conversation_id)
        state = str(dict(row.message_meta or {}).get("subagent_input_state") or "")
        input_states_by_child.setdefault(child_id, []).append(state)
        state_tokens.append(f"input:{row.id}:{state}")

    active_child_ids = frozenset(
        child_id
        for child_id in child_ids
        if any(
            state in {INPUT_PENDING, INPUT_PROCESSING}
            for state in input_states_by_child.get(child_id, ())
        )
        or getattr(run_by_id.get(child_id), "status", None)
        in {RUN_QUEUED, RUN_RUNNING, RUN_WAITING}
        or (
            child_id in child_by_id
            and conversation_turn_snapshot_for_session(child_by_id[child_id]).phase
            in {"active", "suspended"}
        )
    )
    all_active_suspended = bool(active_child_ids) and all(
        getattr(run_by_id.get(child_id), "status", None) == RUN_WAITING
        and child_id in child_by_id
        and conversation_turn_snapshot_for_session(child_by_id[child_id]).status
        == "suspended"
        for child_id in active_child_ids
    )
    all_succeeded = bool(child_ids) and all(
        all(state == INPUT_DONE for state in input_states_by_child.get(child_id, ()))
        and getattr(run_by_id.get(child_id), "status", None) == RUN_COMPLETED
        and child_id in child_by_id
        and conversation_turn_snapshot_for_session(child_by_id[child_id]).status
        == "completed"
        for child_id in child_ids
    )
    for child_id in child_ids:
        child = child_by_id.get(child_id)
        state_tokens.append(
            f"child:{child_id}:{getattr(run_by_id.get(child_id), 'status', None)}:"
            f"{conversation_turn_snapshot_for_session(child).status if child else None}"
        )

    return _CausalChildCohort(
        child_ids=child_ids,
        active_child_ids=active_child_ids,
        active_agent_ids=tuple(
            dict.fromkeys(
                child_by_id[child_id].agent_id
                for child_id in sorted(active_child_ids, key=str)
                if child_id in child_by_id
            )
        ),
        children=children,
        all_active_suspended=all_active_suspended,
        all_succeeded=all_succeeded,
        state_tokens=tuple(sorted(state_tokens)),
    )


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
    return project_run_group_anchor_id(project_run)


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
    candidate_children: dict[uuid.UUID, ChatSession] = {}
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
        candidate_children[child.id] = child

    session = await db.get(ChatSession, session_id)
    if session is not None and session.project_id == project_id:
        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.phase in {"active", "suspended"}:
            causal = await _load_causal_child_cohort(
                db,
                session=session,
                anchor_id=snapshot.anchor_id,
                generation=snapshot.generation,
            )
            candidate_children.update({child.id: child for child in causal.children})

    for child in candidate_children.values():
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
                ChatMessage.message_meta["kind"].as_string().in_(GROUP_ANCHOR_KINDS),
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

    causal = await _load_causal_child_cohort(
        db,
        session=session,
        anchor_id=anchor.id,
        generation=snapshot.generation,
    )

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

    project_runs_suspended = bool(nonterminal)
    if nonterminal:
        for run in nonterminal:
            if run.status != "waiting" or run.agent_id is None:
                project_runs_suspended = False
                break
            output = run.output if isinstance(run.output, dict) else {}
            child_session_id = (
                output.get("session_id")
                or output.get("subagent_session_id")
                or output.get("subagent_run_id")
            )
            if not child_session_id:
                project_runs_suspended = False
                break
            pending = await find_pending_confirmation(
                db,
                agent_id=run.agent_id,
                conversation_id=str(child_session_id),
            )
            if pending is None:
                project_runs_suspended = False
                break

    if nonterminal or causal.active_child_ids:
        suspended = (
            (not nonterminal or project_runs_suspended)
            and (
                not causal.active_child_ids
                or causal.all_active_suspended
            )
        )
        target_status = "suspended" if suspended else "running"
    else:
        cohort_exists = bool(runs or causal.child_ids)
        project_runs_succeeded = not runs or all(
            run.status == "succeeded" for run in runs
        )
        causal_children_succeeded = (
            not causal.child_ids or causal.all_succeeded
        )
        target_status = (
            "completed"
            if cohort_exists and project_runs_succeeded and causal_children_succeeded
            else "failed"
        )

    active_agent_ids = tuple(
        dict.fromkeys((*active_agent_ids, *causal.active_agent_ids))
    )
    project_child_ids: set[uuid.UUID] = set()
    for run in nonterminal:
        output = run.output if isinstance(run.output, dict) else {}
        raw_child_id = (
            output.get("session_id")
            or output.get("subagent_session_id")
            or output.get("subagent_run_id")
        )
        try:
            project_child_ids.add(uuid.UUID(str(raw_child_id)))
        except (TypeError, ValueError):
            pass
    active_direct_child_ids = causal.active_child_ids - project_child_ids

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
                    f"run:{run.id}:{run.agent_id}:{run.status}"
                    for run in runs
                ),
                *causal.state_tokens,
            ]
        ),
    )
    return ProjectGroupTurnProjection(
        snapshot=snapshot,
        anchor_message_id=anchor.id,
        active_agent_ids=active_agent_ids,
        run_count=len(nonterminal) + len(active_direct_child_ids),
    )
