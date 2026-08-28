"""One authoritative STOP operation shared by Web, Project and IM."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.services.conversation_turn_lifecycle import (
    ConversationTurnSnapshot,
    cancel_current_conversation_turn,
    conversation_turn_snapshot_for_session,
    publish_conversation_turn_event,
)


@dataclass(frozen=True, slots=True)
class TurnTreeStopResult:
    session_ids: tuple[uuid.UUID, ...]
    cancelled_turns: tuple[
        tuple[uuid.UUID, uuid.UUID, ConversationTurnSnapshot], ...
    ]
    cancelled_subagent_ids: tuple[uuid.UUID, ...]

    @property
    def stopped(self) -> bool:
        return bool(self.cancelled_turns or self.cancelled_subagent_ids)


def _causal_input_filter(
    root_session_id: uuid.UUID,
    snapshot: ConversationTurnSnapshot,
):
    from app.services.subagent_runtime import (
        CAUSAL_ROOT_ANCHOR_ID,
        CAUSAL_ROOT_GENERATION,
        CAUSAL_ROOT_SESSION_ID,
    )

    return and_(
        ChatMessage.message_meta[CAUSAL_ROOT_SESSION_ID].as_string()
        == str(root_session_id),
        ChatMessage.message_meta[CAUSAL_ROOT_ANCHOR_ID].as_string()
        == str(snapshot.anchor_id),
        ChatMessage.message_meta[CAUSAL_ROOT_GENERATION].as_integer()
        == snapshot.generation,
    )


async def stop_session_turn_tree(
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | str,
    reason: str,
    expected_anchor_id: uuid.UUID | None = None,
    expected_generation: int | None = None,
    allow_legacy_recovery: bool = False,
) -> TurnTreeStopResult:
    """Cancel the current generation and only its causally-owned children.

    Session-row locks are the race arbiter: STOP-first prevents later
    completion, while completion-first is already terminal. No polling,
    recursive scan or channel-specific state machine is involved.
    """

    from app.services.subagent_runtime import (
        INPUT_CANCELLED,
        INPUT_PENDING,
        INPUT_PROCESSING,
        RUN_CANCELLED,
        SUBAGENT_INPUT,
        TERMINAL_STATUSES,
    )

    root_id = uuid.UUID(str(session_id))
    cancelled_turns: list[
        tuple[uuid.UUID, uuid.UUID, ConversationTurnSnapshot]
    ] = []
    cancelled_run_ids: list[uuid.UUID] = []
    lease_owner_by_run: dict[uuid.UUID, str | None] = {}
    project_id_by_run: dict[uuid.UUID, uuid.UUID | None] = {}
    stopped_session_ids = {root_id}

    async with async_session() as db:
        # A child worker owns Run -> input -> child Session. Preserve that
        # single order for direct child STOP; ordinary parent STOP may safely
        # lock its distinct parent Session before its child Runs.
        is_child = await db.scalar(
            select(SubagentRun.id).where(SubagentRun.id == root_id).limit(1)
        )
        root_run = (
            await db.get(SubagentRun, root_id, with_for_update=True)
            if is_child is not None
            else None
        )
        root = (
            await db.execute(
                select(ChatSession)
                .where(ChatSession.id == root_id, ChatSession.agent_id == agent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if root is None:
            raise LookupError("turn control session not found or changed owner")
        root_snapshot = conversation_turn_snapshot_for_session(root)
        if (
            expected_anchor_id is not None
            and root_snapshot.anchor_id != expected_anchor_id
        ) or (
            expected_generation is not None
            and root_snapshot.generation != expected_generation
        ):
            return TurnTreeStopResult((), (), ())

        if allow_legacy_recovery and root_snapshot.status == "idle":
            from app.services.chat_history import (
                mark_latest_incomplete_turn_cancelled,
            )

            legacy_anchor_id = await mark_latest_incomplete_turn_cancelled(
                db,
                agent_id=agent_id,
                conversation_id=str(root_id),
                reason=reason,
            )
            if legacy_anchor_id is not None:
                root_snapshot = conversation_turn_snapshot_for_session(root)

        if root_run is not None:
            child_ids = [root_id]
            input_filter = ChatMessage.conversation_id == str(root_id)
        elif root_snapshot.anchor_id is not None:
            child_ids = list(
                (
                    await db.execute(
                        select(SubagentRun.id).where(
                            SubagentRun.parent_session_id == root_id
                        )
                    )
                ).scalars()
            )
            input_filter = _causal_input_filter(root_id, root_snapshot)
        else:
            child_ids = []
            input_filter = ChatMessage.id.is_(None)

        # Discover candidate ownership without locking, then take the canonical
        # Run -> input -> child Session locks. Parent admission uses the parent
        # Session lock above, so no new causally-owned child can slip past STOP.
        candidate_input_rows = (
            list(
                (
                    await db.execute(
                        select(ChatMessage.id, ChatMessage.conversation_id)
                        .where(
                            ChatMessage.conversation_id.in_(
                                [str(child_id) for child_id in child_ids]
                            ),
                            ChatMessage.message_meta["kind"].as_string()
                            == SUBAGENT_INPUT,
                            ChatMessage.message_meta["subagent_input_state"]
                            .as_string()
                            .in_([INPUT_PENDING, INPUT_PROCESSING]),
                            input_filter,
                        )
                    )
                ).all()
            )
            if child_ids
            else []
        )
        run_ids = {uuid.UUID(row.conversation_id) for row in candidate_input_rows}
        if root_run is not None:
            run_ids.add(root_id)
        if root_run is not None:
            runs = [root_run]
        else:
            runs = (
                list(
                    (
                        await db.execute(
                            select(SubagentRun)
                            .where(SubagentRun.id.in_(run_ids))
                            .order_by(SubagentRun.id)
                            .with_for_update()
                        )
                    ).scalars()
                )
                if run_ids
                else []
            )
        child_inputs = (
            list(
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id.in_(
                                [str(child_id) for child_id in run_ids]
                            ),
                            ChatMessage.message_meta["kind"].as_string()
                            == SUBAGENT_INPUT,
                            ChatMessage.message_meta["subagent_input_state"]
                            .as_string()
                            .in_([INPUT_PENDING, INPUT_PROCESSING]),
                            input_filter,
                        )
                        .order_by(ChatMessage.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            if run_ids
            else []
        )
        inputs_by_run: dict[uuid.UUID, list[ChatMessage]] = {}
        for row in child_inputs:
            inputs_by_run.setdefault(uuid.UUID(row.conversation_id), []).append(row)

        child_sessions = (
            list(
                (
                    await db.execute(
                        select(ChatSession)
                        .where(ChatSession.id.in_(run_ids - {root_id}))
                        .order_by(ChatSession.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            if run_ids - {root_id}
            else []
        )
        child_by_id = {child.id: child for child in child_sessions}
        if root_run is not None:
            child_by_id[root_id] = root

        target_sessions = [root]
        for run in runs:
            child = child_by_id.get(run.id)
            if child is None:
                continue
            child_snapshot = conversation_turn_snapshot_for_session(child)
            owned_input_ids = {row.id for row in inputs_by_run.get(run.id, [])}
            if run.id != root_id and child_snapshot.anchor_id not in owned_input_ids:
                continue
            if child.id != root.id:
                target_sessions.append(child)
            stopped_session_ids.add(child.id)
            if run.status not in TERMINAL_STATUSES:
                lease_owner_by_run[run.id] = run.lease_owner
                project_id_by_run[run.id] = run.project_id
                run.status = RUN_CANCELLED
                run.lease_owner = None
                run.lease_expires_at = None
                cancelled_run_ids.append(run.id)

        from app.services.confirmation_service import (
            cancel_pending_confirmation_for_stop,
        )

        for target in target_sessions:
            current = conversation_turn_snapshot_for_session(target)
            await cancel_pending_confirmation_for_stop(
                db,
                agent_id=target.agent_id,
                conversation_id=str(target.id),
                turn_anchor_id=current.anchor_id,
            )
            if current.anchor_id is not None:
                from app.services.chat_history import (
                    close_running_tool_calls_for_stop,
                )

                await close_running_tool_calls_for_stop(
                    db,
                    agent_id=target.agent_id,
                    conversation_id=str(target.id),
                    turn_anchor_id=current.anchor_id,
                )
            snapshot = await cancel_current_conversation_turn(
                db,
                agent_id=target.agent_id,
                conversation_id=str(target.id),
            )
            if snapshot.status == "cancelled" and snapshot.anchor_id is not None:
                cancelled_turns.append((target.id, target.agent_id, snapshot))

        for row in child_inputs:
            row.message_meta = {
                **dict(row.message_meta or {}),
                "subagent_input_state": INPUT_CANCELLED,
            }

        if root_snapshot.anchor_id is not None:
            inbox_rows = list(
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(root_id),
                            ChatMessage.message_meta["turn_inbox_state"]
                            .as_string()
                            .in_(["pending", "processing"]),
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            for row in inbox_rows:
                row.message_meta = {
                    **dict(row.message_meta or {}),
                    "turn_inbox_state": "cancelled",
                    "turn_inbox_cancel_reason": reason,
                }

        project_run_ids = {
            uuid.UUID(str(raw_id))
            for row in child_inputs
            for raw_id in [dict(row.message_meta or {}).get("project_run_id")]
            if raw_id
        }
        if project_run_ids:
            from app.models.project import ProjectRun
            from app.services.project_service import TERMINAL_PROJECT_RUN_STATUSES

            project_runs = list(
                (
                    await db.execute(
                        select(ProjectRun)
                        .where(
                            ProjectRun.id.in_(project_run_ids),
                            ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            now = datetime.now(UTC)
            for project_run in project_runs:
                project_run.status = "cancelled"
                project_run.finished_at = now
                project_run.error = "Project subagent was stopped"
        await db.commit()

    from app.services.turn_control_bus import publish_turn_tree_stopped

    await publish_turn_tree_stopped(
        {
            str(target_id): str(snapshot.anchor_id)
            for target_id, _target_agent_id, snapshot in cancelled_turns
        },
        subagent_lease_owners={
            str(run_id): lease_owner_by_run.get(run_id)
            for run_id in cancelled_run_ids
        },
    )

    if cancelled_run_ids:
        from app.services.subagent_runtime import (
            cancel_local_subagent_tasks,
            publish_cancelled_subagent_turns,
        )

        await cancel_local_subagent_tasks(
            cancelled_run_ids,
            expected_lease_owners=lease_owner_by_run,
        )
        for project_id in {
            project_id_by_run.get(run_id) for run_id in cancelled_run_ids
        }:
            scoped_ids = [
                run_id
                for run_id in cancelled_run_ids
                if project_id_by_run.get(run_id) == project_id
            ]
            await publish_cancelled_subagent_turns(scoped_ids, project_id=project_id)

    for target_id, target_agent_id, snapshot in cancelled_turns:
        await publish_conversation_turn_event(
            agent_id=target_agent_id,
            conversation_id=str(target_id),
            payload={"type": "done", "role": "assistant", "content": ""},
            snapshot=snapshot,
            event_kind="turn_terminal",
        )

    return TurnTreeStopResult(
        session_ids=tuple(sorted(stopped_session_ids, key=str)),
        cancelled_turns=tuple(cancelled_turns),
        cancelled_subagent_ids=tuple(cancelled_run_ids),
    )
