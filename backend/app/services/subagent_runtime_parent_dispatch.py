"""Parent wake-event dispatch helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_parent_round import _materialize_project_a2a_turn
from app.services.subagent_runtime_project_common import _ensure_project_leader_or_none

async def _dispatch_parent_event(child_message_id: uuid.UUID) -> bool:
    async with async_session() as db:
        event = await db.get(ChatMessage, child_message_id)
        if event is None:
            return True
        try:
            child_id = uuid.UUID(str(event.conversation_id))
        except (TypeError, ValueError):
            await db.rollback()
            await _finish_parent_event_dispatch(child_message_id, SUBAGENT_DISPATCH_DISCARDED)
            return True
        run = await db.get(SubagentRun, child_id)
        child = await db.get(ChatSession, child_id)
        parent = await db.get(ChatSession, run.parent_session_id) if run else None
        if run is None or child is None or parent is None:
            await db.rollback()
            await _finish_parent_event_dispatch(child_message_id, SUBAGENT_DISPATCH_DISCARDED)
            return True

    if parent.source_channel == SUBAGENT_CHANNEL:
        from app.services.subagent_runtime_child_events import dispatch_child_parent_event

        return await dispatch_child_parent_event(child_message_id)

    if parent.source_channel == "agent" and parent.project_id is not None and child.project_id == parent.project_id:
        return await _materialize_project_a2a_turn(
            event=event,
            child=child,
            run=run,
            parent=parent,
        )

    if parent.source_channel == "project" and parent.project_id is not None:
        # Project Agent Group is an append-only coordination surface. Child
        # output becomes a visible group reply but never resumes the root/Leader
        # LLM, so completion cannot fan out into an implicit broadcast storm.
        external_key = f"project-subagent:{child_message_id}"
        async with async_session() as db:
            from app.models.project import Project

            stored_event = await db.get(ChatMessage, child_message_id, with_for_update=True)
            if stored_event is None:
                return True
            existing = (
                await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == external_key))
            ).scalar_one_or_none()
            if existing is not None:
                stored_event.message_meta = {
                    **_message_meta(stored_event),
                    "subagent_dispatch_state": SUBAGENT_DISPATCH_DELIVERED,
                }
                await db.commit()
                return True
            project = await db.get(Project, parent.project_id, with_for_update=True)
            repaired_leader = (
                await _ensure_project_leader_or_none(db, project)
                if project is not None
                else None
            )
            leader_agent_id = repaired_leader.agent_id if repaired_leader is not None else None
            is_leader_reply = leader_agent_id == child.agent_id
            event_meta = _message_meta(event)
            from app.services.project_group_turn_lifecycle import (
                project_group_timeline_anchor_for_child_turn,
            )

            child_anchor_id = str(event_meta.get("turn_anchor_id") or "")
            producer_scope = f"project:{child.id}:{child_anchor_id}"
            try:
                group_timeline_anchor_id = (
                    await project_group_timeline_anchor_for_child_turn(
                        db,
                        project_id=parent.project_id,
                        child_session_id=child.id,
                        child_turn_anchor_id=uuid.UUID(child_anchor_id),
                    )
                )
            except (TypeError, ValueError):
                group_timeline_anchor_id = None
            materialized = ChatMessage(
                agent_id=parent.agent_id,
                sender_agent_id=child.agent_id,
                role="assistant",
                content=event.content,
                conversation_id=str(parent.id),
                external_event_key=external_key,
                message_meta={
                    "kind": "project_subagent_reply",
                    "project_id": str(parent.project_id),
                    "visible_to_group": True,
                    "mentions": [],
                    "awakened_agent_ids": [],
                    "subagent_id": str(child.id),
                    "child_message_id": str(child_message_id),
                    "source_project_run_ids": event_meta.get("project_run_ids", []),
                    "timeline_anchor_id": (
                        str(group_timeline_anchor_id)
                        if group_timeline_anchor_id is not None
                        else None
                    ),
                    "producer_scope": producer_scope,
                    "attachments": event_meta.get("attachments", []),
                    "wake_policy": (
                        "blocked_no_leader"
                        if leader_agent_id is None
                        else "leader_batch_pending"
                        if not is_leader_reply
                        else "leader_self_no_wake"
                    ),
                    "leader_batch_state": (
                        "blocked_no_leader"
                        if leader_agent_id is None
                        else "ignored_leader_self"
                        if is_leader_reply
                        else "pending"
                    ),
                    "default_leader_agent_id": str(leader_agent_id) if leader_agent_id else None,
                },
            )
            db.add(materialized)
            stored_event.message_meta = {
                **_message_meta(stored_event),
                "subagent_dispatch_state": SUBAGENT_DISPATCH_DELIVERED,
            }
            stored_parent = await db.get(ChatSession, parent.id)
            if stored_parent is not None:
                stored_parent.last_message_at = datetime.now(UTC)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return False
            if not is_leader_reply:
                _signal_project_dispatch_work()
            from app.services.project_group_turn_lifecycle import (
                reconcile_and_publish_project_group_turn,
            )
            await reconcile_and_publish_project_group_turn(
                project_id=parent.project_id,
                session_id=parent.id,
                payload={
                    "type": "assistant_message_committed",
                    "id": str(materialized.id),
                    "message_id": str(materialized.id),
                    "transient_message_id": f"project-stream:{child.id}:{child_anchor_id}",
                    "producer_scope": producer_scope,
                    "timeline_anchor_id": (
                        str(group_timeline_anchor_id)
                        if group_timeline_anchor_id is not None
                        else None
                    ),
                    "role": "assistant",
                    "content": event.content,
                    "thinking": event.thinking,
                    "attachments": event_meta.get("attachments", []),
                    "sender_agent_id": str(child.agent_id),
                    "created_at": (
                        materialized.created_at.isoformat()
                        if materialized.created_at is not None
                        else None
                    ),
                },
                event_kind="turn_assistant_committed",
            )
            return True
    return await _dispatch_parent_event_batch([child_message_id])


async def _pending_parent_event_groups(
    message_ids: list[uuid.UUID],
) -> tuple[
    list[tuple[uuid.UUID, list[uuid.UUID]]],
    list[tuple[uuid.UUID, uuid.UUID]],
]:
    """Split ordinary parent batches from project-specific single events."""
    if not message_ids:
        return [], []
    child_session = aliased(ChatSession)
    parent_session = aliased(ChatSession)
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    ChatMessage.id,
                    SubagentRun.parent_session_id,
                    SubagentRun.execution_user_id,
                    child_session.agent_id,
                    parent_session.project_id,
                    parent_session.source_channel,
                )
                .join(
                    SubagentRun,
                    cast(ChatMessage.conversation_id, String)
                    == cast(SubagentRun.id, String),
                )
                .join(child_session, child_session.id == SubagentRun.id)
                .join(parent_session, parent_session.id == SubagentRun.parent_session_id)
                .where(ChatMessage.id.in_(message_ids))
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).all()
    batches: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], list[uuid.UUID]] = {}
    special: list[tuple[uuid.UUID, uuid.UUID]] = []
    grouped_ids: set[uuid.UUID] = set()
    for message_id, parent_id, execution_user_id, execution_agent_id, project_id, source_channel in rows:
        grouped_ids.add(message_id)
        if project_id is not None or source_channel == SUBAGENT_CHANNEL:
            special.append((parent_id, message_id))
            continue
        key = (parent_id, execution_user_id, execution_agent_id)
        batches.setdefault(key, []).append(message_id)
    # Rows without a complete parent/run join are handled by the compatibility
    # dispatcher under a per-event surrogate key; it will discard them safely.
    special.extend(
        (message_id, message_id)
        for message_id in message_ids
        if message_id not in grouped_ids
    )
    return [
        (parent_id, batch)
        for (parent_id, _user_id, _agent_id), batch in batches.items()
    ], special


async def _persist_parent_batch_identity_failure(
    *,
    anchor: ChatMessage,
    parent: ChatSession,
    run: SubagentRun,
    child: ChatSession,
    exc: BaseException,
) -> None:
    from app.services.chat_history import persist_assistant_reply_row

    logger.warning("Subagent event execution identity is unavailable: {}", exc)
    visible_error = "协作任务未能继续，请检查资源访问权限后重试。"
    async with async_session() as db:
        stored_anchor = await db.get(ChatMessage, anchor.id, with_for_update=True)
        if stored_anchor is None:
            return
        completed = await db.scalar(
            select(ChatMessage.id)
            .where(
                ChatMessage.conversation_id == str(parent.id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string()
                == str(stored_anchor.id),
            )
            .limit(1)
        )
        if completed is not None:
            return
        assistant_message_id = await persist_assistant_reply_row(
            db,
            agent_id=parent.agent_id,
            user_id=run.execution_user_id,
            conversation_id=str(parent.id),
            content=visible_error,
            message_meta={
                "kind": "subagent_event_failure",
                "attachments": [],
            },
            turn_anchor_id=stored_anchor.id,
            sender_agent_id=child.agent_id,
            turn_terminal_status="failed",
        )
        stored_parent = await db.get(ChatSession, parent.id)
        if stored_parent is not None:
            stored_parent.last_message_at = datetime.now(UTC)
        await db.commit()
    from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

    await publish_committed_turn_terminal(
        agent_id=parent.agent_id,
        conversation_id=str(parent.id),
        turn_anchor_id=stored_anchor.id,
        message_id=assistant_message_id,
        content=visible_error,
    )


async def _dispatch_parent_event_batch(message_ids: list[uuid.UUID]) -> bool:
    """Materialize one bounded ordinary-parent batch and resume it once."""
    if not message_ids:
        return False
    from app.services.channel_dispatch import (
        ChannelReactions,
        chat_session_lock_key,
        run_channel_message,
    )
    from app.services.execution_identity import ExecutionIdentityError
    from app.services.turn_recovery import resume_turn

    async with async_session() as db:
        first_event = await db.get(ChatMessage, message_ids[0])
        if first_event is None:
            return True
        try:
            child_id = uuid.UUID(str(first_event.conversation_id))
        except (TypeError, ValueError):
            return True
        run = await db.get(SubagentRun, child_id)
        child = await db.get(ChatSession, child_id)
        parent = await db.get(ChatSession, run.parent_session_id) if run else None
        if run is None or child is None or parent is None:
            return True
        if parent.project_id is not None:
            return False
        lock_key = chat_session_lock_key(parent)

    async def _work() -> str:
        projected_event_ids: list[uuid.UUID] = []
        anchor, _injected, status = await _materialize_parent_event_batch(
            parent_session_id=parent.id,
            execution_agent_id=child.agent_id,
            execution_user_id=run.execution_user_id,
            candidate_ids=message_ids,
            projected_event_ids=projected_event_ids,
        )
        if status in {"gone", "empty", "completed"}:
            if anchor is not None and status == "completed":
                await _finish_parent_events_for_root(anchor.id, projected_event_ids)
            return "processed"
        if status == "identity_invalid" and anchor is None:
            return "identity_invalid"
        if anchor is None:
            return "busy"

        root_meta = _message_meta(anchor)
        try:
            root_child_id = uuid.UUID(str(root_meta.get("subagent_id")))
        except (TypeError, ValueError):
            return "busy"
        async with async_session() as check_db:
            live_run = await check_db.get(SubagentRun, root_child_id)
            live_child = await check_db.get(ChatSession, root_child_id)
            try:
                if live_run is None or live_child is None:
                    raise RuntimeError("Subagent lifecycle no longer exists")
                await _validate_execution_identity(check_db, live_run, live_child)
            except (ExecutionIdentityError, RuntimeError) as exc:
                if live_run is None or live_child is None:
                    live_run = run
                    live_child = child
                await _persist_parent_batch_identity_failure(
                    anchor=anchor,
                    parent=parent,
                    run=live_run,
                    child=live_child,
                    exc=exc,
                )
                await _finish_parent_events_for_root(anchor.id, projected_event_ids)
                return "processed"
        resumed = await resume_turn(anchor)
        if resumed:
            await _finish_parent_events_for_root(anchor.id, projected_event_ids)
        return "processed" if resumed else "busy"

    result = await run_channel_message(
        lock_key,
        is_command=False,
        reactions=ChannelReactions(),
        work=_work,
        distributed=True,
        workload_kind=WorkloadKind.PROJECT,
        tenant_id=await _subagent_workload_tenant_id(child.id),
    )
    return result != "busy"
