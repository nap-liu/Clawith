"""Parent round preparation and A2A turn materialization helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_parent_events import drain_parent_subagent_events
from app.services.subagent_runtime_project_common import _ensure_project_leader_or_none

def build_parent_subagent_before_round(
    *,
    parent_session_id: str,
    active_turn_anchor_id: uuid.UUID,
    execution_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    turn_anchor_agent_id: uuid.UUID | None = None,
    include_turn_inbox: bool = False,
    upstream: Callable[[int], Awaitable[list[dict]]] | None = None,
) -> Callable[[int], Awaitable[list[dict]]]:
    """Compose one shared parent-event round hook for Web and channel turns."""

    async def _before_round(round_i: int, *, before_injection=None) -> list[dict]:
        injected = list(await upstream(round_i)) if upstream is not None else []
        before_injection_done = False

        async def _before_injection_once(**kwargs):
            nonlocal before_injection_done
            if before_injection is None or before_injection_done:
                return
            before_injection_done = True
            await before_injection(**kwargs)

        if injected:
            await _before_injection_once()
        if include_turn_inbox:
            from app.services.turn_inbox import drain_turn_inbox

            turn_inbox_kwargs = (
                {"before_injection": _before_injection_once}
                if before_injection is not None
                else {}
            )
            injected.extend(
                await drain_turn_inbox(
                    session_id=parent_session_id,
                    active_turn_anchor_id=active_turn_anchor_id,
                    execution_agent_id=(
                        turn_anchor_agent_id or execution_agent_id
                    ),
                    execution_user_id=execution_user_id,
                    **turn_inbox_kwargs,
                )
            )
        parent_event_kwargs = (
            {"before_injection": _before_injection_once}
            if before_injection is not None
            else {}
        )
        injected.extend(
            await drain_parent_subagent_events(
                parent_session_id=parent_session_id,
                active_turn_anchor_id=active_turn_anchor_id,
                execution_agent_id=execution_agent_id,
                execution_user_id=execution_user_id,
                **parent_event_kwargs,
            )
        )
        return injected

    return _before_round


async def _pending_parent_events(
    limit: int = 50,
    *,
    debounce_seconds: float = PARENT_EVENT_BATCH_DEBOUNCE_SECONDS,
    include_legacy: bool = True,
    project_scope: bool | None = None,
    legacy_count_out: list[int] | None = None,
    legacy_ids_out: set[uuid.UUID] | None = None,
    exclude_ids: set[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    cutoff = datetime.now(UTC) - timedelta(seconds=max(0.0, debounce_seconds))
    async with async_session() as db:
        parent_anchor = aliased(ChatMessage)
        parent_final = aliased(ChatMessage)
        project_materialized = aliased(ChatMessage)
        project_group_reply = aliased(ChatMessage)
        parent_session = aliased(ChatSession)
        completed_exists = exists(
            select(parent_final.id)
            .select_from(parent_anchor)
            .join(
                parent_final,
                parent_final.conversation_id == parent_anchor.conversation_id,
            )
            .where(
                parent_anchor.external_event_key == ("subagent-parent:" + cast(ChatMessage.id, String)),
                parent_final.role == "assistant",
                parent_final.message_meta["turn_anchor_id"].as_string()
                == func.coalesce(
                    parent_anchor.message_meta["subagent_turn_anchor_id"].as_string(),
                    cast(parent_anchor.id, String),
                ),
            )
        )
        project_materialized_exists = exists(
            select(project_materialized.id).where(
                project_materialized.external_event_key == ("project-subagent:" + cast(ChatMessage.id, String))
            )
        )
        project_group_reply_exists = exists(
            select(project_group_reply.id).where(
                project_group_reply.external_event_key
                == ("project-a2a-group-reply:" + cast(ChatMessage.id, String))
            )
        )
        dispatch_state = ChatMessage.message_meta["subagent_dispatch_state"].as_string()
        active_query = select(ChatMessage.id)
        if project_scope is not None:
            active_query = active_query.join(
                SubagentRun,
                cast(ChatMessage.conversation_id, String) == cast(SubagentRun.id, String),
            ).where(
                SubagentRun.project_id.is_not(None)
                if project_scope
                else SubagentRun.project_id.is_(None)
            )
        if exclude_ids:
            active_query = active_query.where(ChatMessage.id.not_in(exclude_ids))
        active_rows = (
            (
                await db.execute(
                    active_query
                    .where(
                        dispatch_state == SUBAGENT_DISPATCH_PENDING,
                        ChatMessage.message_meta["kind"]
                        .as_string()
                        .in_(
                            [
                                SUBAGENT_PARENT_MESSAGE,
                                SUBAGENT_COMPLETION,
                                SUBAGENT_FAILURE,
                            ]
                        ),
                        ChatMessage.created_at <= cutoff,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        legacy_rows: list[uuid.UUID] = []
        if include_legacy:
            legacy_scope = (
                SubagentRun.project_id.is_not(None)
                if project_scope is True
                else SubagentRun.project_id.is_(None)
                if project_scope is False
                else True
            )
            legacy_query = (
                select(ChatMessage.id)
                .join(
                    SubagentRun,
                    cast(ChatMessage.conversation_id, String) == cast(SubagentRun.id, String),
                )
                .where(
                    dispatch_state.is_(None),
                    ChatMessage.message_meta["subagent_wake"].as_boolean().is_(True),
                    ChatMessage.message_meta["kind"]
                    .as_string()
                    .in_(
                        [
                            SUBAGENT_PARENT_MESSAGE,
                            SUBAGENT_COMPLETION,
                            SUBAGENT_FAILURE,
                        ]
                    ),
                    ChatMessage.created_at <= cutoff,
                    ~completed_exists,
                    ~project_materialized_exists,
                    legacy_scope,
                )
            )
            if exclude_ids:
                legacy_query = legacy_query.where(ChatMessage.id.not_in(exclude_ids))
            legacy_rows = (
                (
                    await db.execute(
                        legacy_query.order_by(ChatMessage.created_at, ChatMessage.id).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        # A crash can happen after the exact A2A timeline is committed (the
        # `project-subagent:*` key exists) but before its project-group handoff
        # is written.  That half-delivered row is no longer in the ordinary
        # parent queue, so reconcile it explicitly and idempotently.
        a2a_handoffs: list[uuid.UUID] = []
        remaining = max(0, limit - len(legacy_rows))
        if include_legacy and project_scope is not False and remaining:
            a2a_query = (
                select(ChatMessage.id)
                .join(
                    SubagentRun,
                    cast(ChatMessage.conversation_id, String)
                    == cast(SubagentRun.id, String),
                )
                .join(
                    parent_session,
                    parent_session.id == SubagentRun.parent_session_id,
                )
                .where(
                    dispatch_state.is_(None),
                    ChatMessage.message_meta["subagent_wake"].as_boolean().is_(True),
                    ChatMessage.message_meta["kind"]
                    .as_string()
                    .in_([SUBAGENT_COMPLETION, SUBAGENT_FAILURE]),
                    parent_session.source_channel == "agent",
                    parent_session.project_id.is_not(None),
                    project_materialized_exists,
                    ~project_group_reply_exists,
                )
            )
            if exclude_ids:
                a2a_query = a2a_query.where(ChatMessage.id.not_in(exclude_ids))
            a2a_handoffs = (
                (
                    await db.execute(
                        a2a_query.order_by(ChatMessage.created_at, ChatMessage.id).limit(
                            remaining
                        )
                    )
                )
                .scalars()
                .all()
            )
        legacy_ids = list(dict.fromkeys([*legacy_rows, *a2a_handoffs]))
        if legacy_count_out is not None:
            legacy_count_out.append(len(legacy_ids))
        if legacy_ids_out is not None:
            legacy_ids_out.update(legacy_ids)
        # Active and compatibility queues each keep their own bounded budget;
        # a permanently busy row in either queue cannot starve the other.
        return list(dict.fromkeys([*legacy_ids, *active_rows]))


async def _a2a_completion_leader_policy(
    db,
    *,
    project_run,
    leader_agent_id: uuid.UUID | None,
    failed: bool,
) -> tuple[bool, str]:
    """Wake the owner only for owner-originated work or explicit escalation."""
    from app.models.project import ProjectWorkItem

    a2a = dict(dict((project_run.input or {}).get("dispatch") or {}).get("a2a") or {})
    try:
        from_agent_id = uuid.UUID(str(a2a.get("from_agent_id")))
    except (TypeError, ValueError):
        from_agent_id = None
    if leader_agent_id is not None and from_agent_id == leader_agent_id:
        return True, "owner_requested_completion"
    if failed:
        return True, "failed_completion"
    if str(a2a.get("mode") or "").strip() == "consult":
        return True, "decision_consult"
    if project_run.work_item_id is not None:
        work_item = await db.get(ProjectWorkItem, project_run.work_item_id)
        if work_item is not None and work_item.status in {"blocked", "review"}:
            return True, f"work_item_{work_item.status}"
    return False, "peer_completion_recorded"


async def _materialize_project_a2a_turn(
    *,
    event: ChatMessage,
    child: ChatSession,
    run: SubagentRun,
    parent: ChatSession,
) -> bool:
    """Project one child turn onto its exact visible A2A Session.

    The execution child remains the source of truth.  Timeline rows are copied
    with stable external keys so the ordinary Web Chat renderer can display the
    same thinking/tool/final-reply contract without inventing an A2A renderer.
    """
    from app.models.project import Project, ProjectEvent, ProjectRun
    from app.services.project_service import add_event

    event_meta = _message_meta(event)
    raw_project_run_ids = list(event_meta.get("project_run_ids") or [])
    project_run_ids: list[uuid.UUID] = []
    for value in raw_project_run_ids:
        try:
            project_run_ids.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not project_run_ids:
        await _finish_parent_event_dispatch(event.id, SUBAGENT_DISPATCH_DISCARDED)
        return True

    async with async_session() as db:
        stored_event = await db.get(ChatMessage, event.id, with_for_update=True)
        if stored_event is None:
            return True
        event = stored_event
        event_meta = _message_meta(event)
        project_runs = (
            (
                await db.execute(
                    select(ProjectRun).where(
                        ProjectRun.id.in_(project_run_ids),
                        ProjectRun.project_id == parent.project_id,
                        ProjectRun.trigger_type == "a2a",
                    )
                )
            )
            .scalars()
            .all()
        )
        if not project_runs:
            event.message_meta = {
                **event_meta,
                "subagent_dispatch_state": SUBAGENT_DISPATCH_DISCARDED,
            }
            await db.commit()
            return True
        project_run = project_runs[0]
        anchor = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(child.id),
                    ChatMessage.message_meta["project_run_id"].as_string() == str(project_run.id),
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if anchor is None:
            event.message_meta = {
                **event_meta,
                "subagent_dispatch_state": SUBAGENT_DISPATCH_DISCARDED,
            }
            await db.commit()
            return True

        trace_rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(child.id),
                        or_(
                            ChatMessage.id == event.id,
                            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
                        ),
                        ChatMessage.role.in_(["assistant", "tool_call"]),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        if not any(row.id == event.id for row in trace_rows):
            trace_rows.append(event)
            trace_rows.sort(key=lambda row: (row.created_at, row.id))

        for row in trace_rows:
            external_key = f"project-subagent:{row.id}" if row.id == event.id else f"project-a2a-trace:{row.id}"
            exists_row = (
                await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == external_key))
            ).scalar_one_or_none()
            if exists_row is not None:
                continue
            metadata = copy.deepcopy(_message_meta(row))
            metadata.pop("subagent_wake", None)
            metadata.update(
                {
                    "kind": "project_a2a_trace",
                    "project_id": str(parent.project_id),
                    "project_run_id": str(project_run.id),
                    "a2a_session_id": str(parent.id),
                    "subagent_run_id": str(child.id),
                    "subagent_session_id": str(child.id),
                    "source_child_message_id": str(row.id),
                    "execution_agent_id": str(child.agent_id),
                    "visible_to_group": False,
                }
            )
            db.add(
                ChatMessage(
                    agent_id=parent.agent_id,
                    user_id=run.execution_user_id,
                    sender_agent_id=child.agent_id,
                    role=row.role,
                    content=row.content,
                    conversation_id=str(parent.id),
                    external_event_key=external_key,
                    message_meta=metadata,
                    thinking=row.thinking,
                    created_at=row.created_at,
                )
            )
        stored_parent = await db.get(ChatSession, parent.id, with_for_update=True)
        if stored_parent is not None:
            stored_parent.last_message_at = event.created_at or datetime.now(UTC)
        project = await db.get(Project, parent.project_id)
        existing_event = (
            await db.execute(
                select(ProjectEvent.id).where(
                    ProjectEvent.run_id == project_run.id,
                    ProjectEvent.event_type == "a2a.completed",
                )
            )
        ).scalar_one_or_none()
        dispatch_a2a = dict(dict((project_run.input or {}).get("dispatch") or {}).get("a2a") or {})
        if project is not None and existing_event is None:
            add_event(
                db,
                project,
                "a2a.completed",
                "Project Agent completed one exact A2A turn",
                actor_agent_id=child.agent_id,
                from_agent_id=(
                    uuid.UUID(str(dispatch_a2a["from_agent_id"])) if dispatch_a2a.get("from_agent_id") else None
                ),
                to_agent_id=child.agent_id,
                run_id=project_run.id,
                metadata={
                    "session_id": str(parent.id),
                    "a2a_session_id": str(parent.id),
                    "subagent_run_id": str(child.id),
                    "subagent_session_id": str(child.id),
                    "result_message_id": str(event.id),
                    "trace_message_ids": [str(row.id) for row in trace_rows],
                },
            )

        # Exact A2A is the visible peer-to-peer conversation, but its durable
        # completion must also re-enter the project's coordination loop.  The
        # project group is deliberately append-only: we mirror one final reply
        # there. Owner-originated work and explicit decision/blocking gates enter
        # the coalesced owner inbox; ordinary peer completion stays observable
        # without creating an unnecessary owner turn.
        group = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.project_id == parent.project_id,
                    ChatSession.source_channel == "project",
                )
                .order_by(ChatSession.created_at, ChatSession.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        wake_leader = False
        if group is not None:
            group_external_key = f"project-a2a-group-reply:{event.id}"
            group_reply_exists = (
                await db.execute(select(ChatMessage.id).where(ChatMessage.external_event_key == group_external_key))
            ).scalar_one_or_none()
            if group_reply_exists is None:
                repaired_leader = (
                    await _ensure_project_leader_or_none(db, project)
                    if project is not None
                    else None
                )
                leader_agent_id = repaired_leader.agent_id if repaired_leader is not None else None
                is_leader_reply = leader_agent_id == child.agent_id
                should_wake_leader, completion_policy = await _a2a_completion_leader_policy(
                    db,
                    project_run=project_run,
                    leader_agent_id=leader_agent_id,
                    failed=event_meta.get("kind") == SUBAGENT_FAILURE,
                )
                leader_batch_state = (
                    "blocked_no_leader"
                    if leader_agent_id is None
                    else "ignored_leader_self"
                    if is_leader_reply
                    else "pending"
                    if should_wake_leader
                    else "observed_peer_completion"
                )
                wake_leader = leader_batch_state == "pending"
                db.add(
                    ChatMessage(
                        agent_id=group.agent_id,
                        sender_agent_id=child.agent_id,
                        role="assistant",
                        content=event.content,
                        conversation_id=str(group.id),
                        external_event_key=group_external_key,
                        message_meta={
                            "kind": "project_subagent_reply",
                            "project_id": str(parent.project_id),
                            "visible_to_group": True,
                            "mentions": [],
                            "awakened_agent_ids": [],
                            "subagent_id": str(child.id),
                            "child_message_id": str(event.id),
                            "source_project_run_ids": [str(value) for value in project_run_ids],
                            "source_a2a_session_id": str(parent.id),
                            "attachments": event_meta.get("attachments", []),
                            "wake_policy": (
                                "blocked_no_leader"
                                if leader_agent_id is None
                                else "leader_self_no_wake"
                                if is_leader_reply
                                else "leader_batch_pending"
                                if should_wake_leader
                                else "peer_completion_no_owner_wake"
                            ),
                            "leader_batch_state": leader_batch_state,
                            "leader_escalation_reason": completion_policy,
                            "default_leader_agent_id": (str(leader_agent_id) if leader_agent_id else None),
                        },
                        created_at=event.created_at,
                    )
                )
                group.last_message_at = event.created_at or datetime.now(UTC)
        event.message_meta = {
            **event_meta,
            "subagent_dispatch_state": SUBAGENT_DISPATCH_DELIVERED,
        }
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return False
        if wake_leader:
            _signal_project_dispatch_work()
        return True
