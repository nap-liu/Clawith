"""Parent wake-event materialization and worker loop helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403

async def _subagent_worker_loop() -> None:
    semaphore = asyncio.Semaphore(WORKER_CONCURRENCY)
    active: set[asyncio.Task] = set()

    async def _execute(run_id: uuid.UUID, lease_owner: str) -> None:
        async with semaphore:
            await execute_claimed_subagent(run_id, lease_owner=lease_owner)

    while True:
        active = {task for task in active if not task.done()}
        if len(active) >= WORKER_CONCURRENCY:
            await asyncio.sleep(0.1)
            continue
        try:
            claimed = await _claim_subagent(with_token=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - daemon must survive transient DB faults
            logger.exception(f"[subagent] worker claim failed: {exc}")
            await asyncio.sleep(1)
            continue
        if claimed is None:
            await asyncio.sleep(0.5)
            continue
        claimed_run_id, lease_owner = claimed
        task = asyncio.create_task(
            _execute(claimed_run_id, lease_owner),
            name=f"subagent:{claimed_run_id}",
        )
        active.add(task)


def _parent_event_external_key(child_message_id: uuid.UUID) -> str:
    return f"subagent-parent:{child_message_id}"


def _parent_event_content(event: ChatMessage, child: ChatSession) -> str:
    label = {
        SUBAGENT_PARENT_MESSAGE: "message",
        SUBAGENT_COMPLETION: "completed",
        SUBAGENT_FAILURE: "failed",
    }.get(_message_meta(event).get("kind"), "event")
    return (
        f'<subagent-event subagent_id="{child.id}" type="{label}">\n'
        f"{event.content}\n"
        "</subagent-event>"
    )


async def _parent_event_candidates(
    db,
    *,
    parent_session_id: uuid.UUID,
    execution_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    candidate_ids: list[uuid.UUID] | None = None,
) -> list[tuple[ChatMessage, SubagentRun, ChatSession]]:
    child_session = aliased(ChatSession)
    parent_projection = aliased(ChatMessage)
    parent_final = aliased(ChatMessage)
    completed_projection_exists = exists(
        select(parent_final.id)
        .select_from(parent_projection)
        .join(
            parent_final,
            parent_final.conversation_id == parent_projection.conversation_id,
        )
        .where(
            parent_projection.external_event_key
            == ("subagent-parent:" + cast(ChatMessage.id, String)),
            parent_final.role == "assistant",
            parent_final.message_meta["turn_anchor_id"].as_string()
            == func.coalesce(
                parent_projection.message_meta["subagent_turn_anchor_id"].as_string(),
                cast(parent_projection.id, String),
            ),
        )
    )
    dispatch_state = ChatMessage.message_meta["subagent_dispatch_state"].as_string()
    wake_predicate = or_(
        dispatch_state == SUBAGENT_DISPATCH_PENDING,
        and_(
            dispatch_state.is_(None),
            ChatMessage.message_meta["subagent_wake"].as_boolean().is_(True),
        ),
    )
    candidate_conditions = [
        SubagentRun.parent_session_id == parent_session_id,
        SubagentRun.execution_user_id == execution_user_id,
        child_session.agent_id == execution_agent_id,
        wake_predicate,
        ChatMessage.message_meta["kind"]
        .as_string()
        .in_([SUBAGENT_PARENT_MESSAGE, SUBAGENT_COMPLETION, SUBAGENT_FAILURE]),
    ]
    if candidate_ids is None:
        candidate_conditions.append(~completed_projection_exists)
    stmt = (
        select(ChatMessage, SubagentRun, child_session)
        .join(
            SubagentRun,
            cast(ChatMessage.conversation_id, String) == cast(SubagentRun.id, String),
        )
        .join(child_session, child_session.id == SubagentRun.id)
        .where(*candidate_conditions)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .limit(PARENT_EVENT_BATCH_MAX_MESSAGES * 4)
    )
    if candidate_ids is not None:
        if not candidate_ids:
            return []
        stmt = stmt.where(ChatMessage.id.in_(candidate_ids))
    return [tuple(row) for row in (await db.execute(stmt)).all()]


async def _parent_event_projections(
    db,
    event_ids: list[uuid.UUID],
) -> dict[uuid.UUID, ChatMessage]:
    if not event_ids:
        return {}
    key_to_event_id = {
        _parent_event_external_key(event_id): event_id for event_id in event_ids
    }
    rows = (
        (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.external_event_key.in_(list(key_to_event_id))
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        key_to_event_id[row.external_event_key]: row
        for row in rows
        if row.external_event_key in key_to_event_id
    }


async def _parent_turn_has_terminal_reply(
    db,
    *,
    parent_session_id: uuid.UUID,
    anchor_id: uuid.UUID,
) -> bool:
    return bool(
        await db.scalar(
            select(ChatMessage.id)
            .where(
                ChatMessage.conversation_id == str(parent_session_id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string()
                == str(anchor_id),
                or_(
                    ChatMessage.message_meta["artifact_role"].as_string().is_(None),
                    ChatMessage.message_meta["artifact_role"].as_string()
                    != "intermediate_assistant",
                ),
            )
            .limit(1)
        )
    )


async def _materialize_parent_event_batch(
    *,
    parent_session_id: uuid.UUID,
    execution_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    active_turn_anchor_id: uuid.UUID | None = None,
    candidate_ids: list[uuid.UUID] | None = None,
    projected_event_ids: list[uuid.UUID] | None = None,
    before_injection=None,
) -> tuple[ChatMessage | None, list[dict], str]:
    """Project a bounded event batch onto one ordinary parent logical turn."""
    from app.services.execution_identity import ExecutionIdentityError

    async with async_session() as db:
        parent = await db.get(ChatSession, parent_session_id, with_for_update=True)
        if parent is None:
            return None, [], "gone"
        if parent.project_id is not None:
            return None, [], "special"

        candidates = await _parent_event_candidates(
            db,
            parent_session_id=parent.id,
            execution_agent_id=execution_agent_id,
            execution_user_id=execution_user_id,
            candidate_ids=candidate_ids,
        )
        if not candidates:
            return None, [], "empty"

        event_ids = [event.id for event, _run, _child in candidates]
        projections = await _parent_event_projections(db, event_ids)

        async def _discard_unowned_legacy_projection_group(
            legacy_root_id: uuid.UUID,
        ) -> bool:
            """Delete one broken derived group while preserving source events."""
            from app.services.conversation_turn_lifecycle import (
                conversation_turn_snapshot_for_session,
            )

            current = conversation_turn_snapshot_for_session(parent)
            if current.status in {"running", "suspended"}:
                return False
            stale_rows = list(
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(parent.id),
                            ChatMessage.message_meta["kind"].as_string()
                            == SUBAGENT_PARENT_EVENT,
                            or_(
                                ChatMessage.id == legacy_root_id,
                                ChatMessage.message_meta[
                                    "subagent_turn_anchor_id"
                                ].as_string()
                                == str(legacy_root_id),
                            ),
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            stale_ids = {row.id for row in stale_rows}
            for row in stale_rows:
                await db.delete(row)
            # Release every external-event key in the old batch before this
            # transaction inserts a repaired subset under a new root.
            await db.flush()
            for event_id, projection in list(projections.items()):
                if projection.id in stale_ids:
                    projections.pop(event_id, None)
            return True

        projected_root_ids: list[uuid.UUID] = []
        for projection in projections.values():
            raw_root_id = _message_meta(projection).get("subagent_turn_anchor_id")
            try:
                projected_root_ids.append(
                    uuid.UUID(str(raw_root_id)) if raw_root_id else projection.id
                )
            except (TypeError, ValueError):
                continue

        root: ChatMessage | None = None
        if active_turn_anchor_id is not None:
            root = await db.get(ChatMessage, active_turn_anchor_id, with_for_update=True)
            if (
                root is None
                or root.conversation_id != str(parent.id)
                or root.user_id != execution_user_id
            ):
                return None, [], "busy"
            root_meta = _message_meta(root)
            if root_meta.get("turn_status") in {"cancelled", "failed"}:
                return None, [], "busy"
            raw_root_execution_agent_id = root_meta.get("execution_agent_id")
            if raw_root_execution_agent_id:
                try:
                    if uuid.UUID(str(raw_root_execution_agent_id)) != execution_agent_id:
                        return None, [], "busy"
                except (TypeError, ValueError):
                    return None, [], "busy"
            elif root.agent_id != execution_agent_id:
                return None, [], "busy"
            if any(root_id != root.id for root_id in projected_root_ids):
                return None, [], "busy"
        elif projected_root_ids:
            root_id = projected_root_ids[0]
            root = await db.get(ChatMessage, root_id, with_for_update=True)
            if root is None or root.conversation_id != str(parent.id):
                if not await _discard_unowned_legacy_projection_group(root_id):
                    return None, [], "busy"
                root = None
            # Only synthetic Subagent roots are resumed by the idle daemon.
            # A projection already attached to a human/channel turn is owned by
            # that turn and its ordinary crash-recovery path.
            if (
                root is not None
                and _message_meta(root).get("kind") != SUBAGENT_PARENT_EVENT
            ):
                return None, [], "busy"
            root_meta = _message_meta(root) if root is not None else {}
            if root is not None and root_meta.get("conversation_turn_lifecycle") is not True:
                # Compatibility repair for roots materialized by an older
                # process before durable admission. Their child rows remain
                # the authoritative pending audit events, so discard only the
                # broken parent projections and let this transaction create a
                # newly admitted idempotent batch below.
                if not await _discard_unowned_legacy_projection_group(root.id):
                    return None, [], "busy"
                root = None
        else:
            latest = (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(parent.id))
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is not None and latest.role != "assistant":
                return None, [], "busy"

        if root is not None and projected_event_ids is not None:
            for event_id, projection in projections.items():
                raw_root_id = _message_meta(projection).get("subagent_turn_anchor_id")
                if str(raw_root_id or projection.id) == str(root.id):
                    projected_event_ids.append(event_id)

        if root is not None and await _parent_turn_has_terminal_reply(
            db,
            parent_session_id=parent.id,
            anchor_id=root.id,
        ):
            return root, [], "completed"

        valid: list[tuple[ChatMessage, SubagentRun, ChatSession, str]] = []
        invalid: list[tuple[ChatMessage, SubagentRun, ChatSession, str]] = []
        total_bytes = 0
        for event, run, child in candidates:
            if event.id in projections:
                continue
            try:
                await _validate_execution_identity(db, run, child)
            except (ExecutionIdentityError, RuntimeError) as exc:
                logger.warning(
                    "[subagent] parent event identity invalid event=%s: %s",
                    event.id,
                    type(exc).__name__,
                )
                invalid.append((event, run, child, _parent_event_content(event, child)))
                continue
            content = _parent_event_content(event, child)
            event_bytes = len(content.encode("utf-8")) + 256
            if valid and (
                len(valid) >= PARENT_EVENT_BATCH_MAX_MESSAGES
                or total_bytes + event_bytes > PARENT_EVENT_BATCH_MAX_BYTES
            ):
                break
            valid.append((event, run, child, content))
            total_bytes += min(event_bytes, PARENT_EVENT_BATCH_MAX_BYTES)

        materialized_status = "materialized"
        if not valid:
            if active_turn_anchor_id is not None or projections or not invalid:
                return root, [], "identity_invalid" if not projections else "claimed"
            # Keep the pre-existing observable failure contract: an idle event
            # with a revoked execution identity gets one auditable parent turn
            # and a terminal failure reply, but it is never sent to the model.
            valid = [invalid[0]]
            materialized_status = "identity_invalid"

        if valid and before_injection is not None:
            await before_injection()

        now = datetime.now(UTC)
        created_root = False
        injected: list[dict] = []
        for index, (event, run, child, content) in enumerate(valid):
            projection_id = uuid.uuid4()
            is_new_root = root is None and index == 0
            if is_new_root:
                root_id = projection_id
            else:
                root_id = root.id if root is not None else projection_id
            event_meta = _message_meta(event)
            projection_meta = {
                "kind": SUBAGENT_PARENT_EVENT,
                "execution_agent_id": str(child.agent_id),
                "subagent_id": str(child.id),
                "child_message_id": str(event.id),
                "attachments": (
                    [] if (event_meta.get("media_result", {}).get("delivery") or {}).get("status") in {"sent", "already_sent"}
                    else list(event_meta.get("attachments") or [])
                ),
                **({"media_result": event_meta["media_result"]} if event_meta.get("media_result") else {}),
                **(
                    {"turn_status": "running", "subagent_event_batch": True}
                    if is_new_root
                    else {"subagent_turn_anchor_id": str(root_id)}
                ),
            }
            projection = ChatMessage(
                id=projection_id,
                agent_id=parent.agent_id,
                user_id=run.execution_user_id,
                sender_agent_id=child.agent_id,
                role="user",
                content=content,
                conversation_id=str(parent.id),
                external_event_key=_parent_event_external_key(event.id),
                message_meta=projection_meta,
                created_at=now + timedelta(microseconds=index),
            )
            db.add(projection)
            if projected_event_ids is not None:
                projected_event_ids.append(event.id)
            if is_new_root:
                root = projection
                created_root = True
            injected.append({"role": "user", "content": content})

        parent.last_message_at = now + timedelta(microseconds=len(valid) - 1)
        if created_root and root is not None:
            # ChatSession lifecycle admission is the durable mutex shared by
            # Web, IM, Trigger, Subagent, Recovery, A2A and MCP. Commit the
            # synthetic wake and its ownership atomically: a competing turn
            # rolls the whole projection back instead of leaving an orphan
            # ``running`` root that absorbs every later child event.
            from app.services.conversation_turn_lifecycle import (
                ConversationTurnConflict,
                transition_conversation_turn,
            )

            try:
                await transition_conversation_turn(
                    db,
                    agent_id=parent.agent_id,
                    conversation_id=str(parent.id),
                    turn_anchor_id=root.id,
                    status="running",
                )
            except ConversationTurnConflict:
                await db.rollback()
                return None, [], "busy"
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return None, [], "busy"
        return root, injected, materialized_status


async def _finish_parent_events_for_root(
    root_id: uuid.UUID,
    candidate_ids: list[uuid.UUID],
) -> None:
    """Close every source event durably projected onto one accepted turn."""

    keys = {
        _parent_event_external_key(message_id): message_id
        for message_id in candidate_ids
    }
    async with async_session() as db:
        root = await db.get(ChatMessage, root_id)
        if root is None:
            return
        projections = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == root.conversation_id,
                        or_(
                            ChatMessage.id == root_id,
                            ChatMessage.message_meta["subagent_turn_anchor_id"]
                            .as_string()
                            == str(root_id),
                        ),
                        ChatMessage.message_meta["kind"].as_string()
                        == SUBAGENT_PARENT_EVENT,
                    )
                )
            )
            .scalars()
            .all()
        )
        source_ids: list[uuid.UUID] = []
        for projection in projections:
            raw_root_id = _message_meta(projection).get("subagent_turn_anchor_id")
            if str(raw_root_id or projection.id) != str(root_id):
                continue
            raw_source_id = _message_meta(projection).get("child_message_id")
            try:
                source_ids.append(uuid.UUID(str(raw_source_id)))
            except (TypeError, ValueError):
                source_id = keys.get(str(projection.external_event_key))
                if source_id is not None:
                    source_ids.append(source_id)
        if not source_ids:
            return
        events = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.id.in_(source_ids))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            event.message_meta = {
                **_message_meta(event),
                "subagent_dispatch_state": SUBAGENT_DISPATCH_DELIVERED,
            }
        await db.commit()


async def drain_parent_subagent_events(
    *,
    parent_session_id: str,
    active_turn_anchor_id: uuid.UUID,
    execution_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    before_injection=None,
) -> list[dict]:
    """Inject newly completed child events at one parent LLM round boundary."""
    try:
        parent_id = uuid.UUID(str(parent_session_id))
    except (TypeError, ValueError):
        return []
    projected_event_ids: list[uuid.UUID] = []
    root, injected, _status = await _materialize_parent_event_batch(
        parent_session_id=parent_id,
        execution_agent_id=execution_agent_id,
        execution_user_id=execution_user_id,
        active_turn_anchor_id=active_turn_anchor_id,
        projected_event_ids=projected_event_ids,
        before_injection=before_injection,
    )
    if root is not None and injected:
        await _finish_parent_events_for_root(root.id, projected_event_ids)
    return injected
