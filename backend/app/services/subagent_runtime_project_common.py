"""Shared project dispatch and leader-batch helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403

async def _pending_project_leader_groups(
    *,
    debounce_seconds: float = 0.5,
    limit: int = 20,
) -> list[uuid.UUID]:
    """Find durable project reply queues ready for one Leader batch turn."""
    cutoff = datetime.now(UTC) - timedelta(seconds=max(0.0, debounce_seconds))
    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(ChatMessage.conversation_id)
                    .where(
                        ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                        ChatMessage.message_meta["leader_batch_state"].as_string().in_(["pending", "claimed"]),
                        ChatMessage.created_at <= cutoff,
                    )
                    .distinct()
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    groups: list[uuid.UUID] = []
    for value in rows:
        try:
            groups.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return groups


async def _resolve_batch_work_item_lineage(
    db,
    project_id: uuid.UUID,
    source_rows: list[ChatMessage],
) -> tuple[uuid.UUID | None, list[uuid.UUID]]:
    """Resolve only exact structured Run lineage for a coalesced reply batch.

    A batch receives one ``work_item_id`` only when every source reply maps to
    the same single project work item. Mixed, missing, malformed, or
    cross-project references remain deliberately unbound; their valid related
    work items are still retained as a set for traceability.
    """
    from app.models.project import ProjectRun

    row_run_ids: list[list[uuid.UUID] | None] = []
    all_run_ids: set[uuid.UUID] = set()
    for row in source_rows:
        raw_ids = _message_meta(row).get("source_project_run_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            row_run_ids.append(None)
            continue
        parsed: list[uuid.UUID] = []
        malformed = False
        for raw_id in raw_ids:
            try:
                parsed.append(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError):
                malformed = True
                break
        if malformed or not parsed:
            row_run_ids.append(None)
            continue
        row_run_ids.append(parsed)
        all_run_ids.update(parsed)

    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.id.in_(all_run_ids),
                )
            )
        )
        .scalars()
        .all()
        if all_run_ids
        else []
    )
    run_by_id = {run.id: run for run in runs}
    related_work_item_ids = sorted(
        {run.work_item_id for run in runs if run.work_item_id is not None},
        key=str,
    )
    row_work_item_ids: list[uuid.UUID] = []
    for run_ids in row_run_ids:
        if run_ids is None or any(run_id not in run_by_id for run_id in run_ids):
            return None, related_work_item_ids
        work_item_ids = {run_by_id[run_id].work_item_id for run_id in run_ids}
        if None in work_item_ids or len(work_item_ids) != 1:
            return None, related_work_item_ids
        row_work_item_ids.append(next(iter(work_item_ids)))
    exact_work_item_ids = set(row_work_item_ids)
    return (
        next(iter(exact_work_item_ids)) if len(exact_work_item_ids) == 1 else None,
        related_work_item_ids,
    )


def _batch_row_run_ids(row: ChatMessage) -> list[uuid.UUID]:
    parsed: list[uuid.UUID] = []
    raw_ids = _message_meta(row).get("source_project_run_ids")
    if not isinstance(raw_ids, list):
        return parsed
    for raw_id in raw_ids:
        try:
            parsed.append(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError):
            return []
    return parsed


async def _leader_batch_causal_keys(
    db,
    project_id: uuid.UUID,
    rows: list[ChatMessage],
) -> dict[uuid.UUID, str]:
    """Keep unrelated work items and request chains out of one owner turn."""
    from app.models.project import ProjectRun

    run_ids = {run_id for row in rows for run_id in _batch_row_run_ids(row)}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project_id,
                    ProjectRun.id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    run_by_id = {run.id: run for run in runs}
    keys: dict[uuid.UUID, str] = {}
    for row in rows:
        related = [run_by_id[run_id] for run_id in _batch_row_run_ids(row) if run_id in run_by_id]
        work_item_ids = {run.work_item_id for run in related if run.work_item_id is not None}
        if len(work_item_ids) == 1:
            keys[row.id] = f"work-item:{next(iter(work_item_ids))}"
            continue

        causes: set[str] = set()
        for run in related:
            payload = dict(run.input or {})
            dispatch = dict(payload.get("dispatch") or {})
            cause = (
                payload.get("parent_project_run_id")
                or payload.get("group_message_id")
                or dispatch.get("turn_anchor_id")
            )
            if cause:
                causes.add(str(cause))
        if len(causes) == 1:
            keys[row.id] = f"cause:{next(iter(causes))}"
            continue

        a2a_session_id = _message_meta(row).get("source_a2a_session_id")
        keys[row.id] = f"a2a:{a2a_session_id}" if a2a_session_id else f"reply:{row.id}"
    return keys


def _select_leader_batch_rows(
    rows: list[ChatMessage],
    causal_keys: dict[uuid.UUID, str],
) -> list[ChatMessage]:
    """Select one bounded causal slice while leaving the remainder pending."""
    if not rows:
        return []
    selected: list[ChatMessage] = []
    selected_bytes = 0
    first_key = causal_keys[rows[0].id]
    for row in rows:
        if causal_keys[row.id] != first_key:
            continue
        row_bytes = len(str(row.content or "").encode("utf-8")) + 512
        if selected and (
            len(selected) >= PROJECT_LEADER_BATCH_MAX_REPLIES
            or selected_bytes + row_bytes > PROJECT_LEADER_BATCH_MAX_BYTES
        ):
            break
        selected.append(row)
        selected_bytes += min(row_bytes, PROJECT_LEADER_BATCH_MAX_BYTES)
    return selected


def _truncate_batch_content(content: str, *, limit: int = PROJECT_LEADER_BATCH_MAX_BYTES) -> str:
    raw = str(content or "").encode("utf-8")
    if len(raw) <= limit:
        return str(content or "")
    marker = "\n\n[内容已截断；完整记录保留在原会话]"
    marker_bytes = marker.encode("utf-8")
    return raw[: max(0, limit - len(marker_bytes))].decode("utf-8", errors="ignore") + marker


async def _ensure_project_leader_or_none(db, project):
    """Repair a runnable project owner at every autonomous coordination entry."""

    from app.services.project_service import ensure_enabled_project_leader

    try:
        return await ensure_enabled_project_leader(db, project)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        return None


def _batch_attachment_refs(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    refs: list[dict] = []
    for attachment in value[:8]:
        if not isinstance(attachment, dict):
            continue
        ref = {
            key: str(attachment[key])[:256]
            for key in ("id", "name", "path", "type", "mime_type")
            if attachment.get(key) is not None
        }
        if ref:
            refs.append(ref)
    return refs


async def _project_work_item_snapshots(
    db,
    project_id: uuid.UUID,
    work_item_ids: list[uuid.UUID],
) -> list[dict]:
    """Freeze bounded work-item context for one collaboration dispatch."""

    from app.models.project import ProjectEvent, ProjectWorkItem
    from app.services.project_collaboration_prompt import normalize_project_work_item_snapshot

    ordered_ids = list(dict.fromkeys(work_item_ids))
    if not ordered_ids:
        return []
    work_items = (
        (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.project_id == project_id,
                    ProjectWorkItem.id.in_(ordered_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    item_by_id = {item.id: item for item in work_items}
    dependency_ids: set[uuid.UUID] = set()
    for item in work_items:
        for raw_id in list(item.dependency_ids or [])[:12]:
            try:
                dependency_ids.add(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError):
                continue
    dependencies = (
        (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.project_id == project_id,
                    ProjectWorkItem.id.in_(dependency_ids),
                )
            )
        )
        .scalars()
        .all()
        if dependency_ids
        else []
    )
    dependency_by_id = {item.id: item for item in dependencies}
    events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project_id,
                    ProjectEvent.work_item_id.in_(ordered_ids),
                    ProjectEvent.event_type == "work_item.updated",
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
                .limit(max(32, len(ordered_ids) * 24))
            )
        )
        .scalars()
        .all()
    )
    evidence_by_item: dict[uuid.UUID, list[object]] = {item_id: [] for item_id in ordered_ids}
    for event in events:
        if event.work_item_id not in evidence_by_item:
            continue
        evidence = dict(event.event_metadata or {}).get("evidence")
        if not isinstance(evidence, list):
            continue
        for value in evidence:
            if value not in evidence_by_item[event.work_item_id]:
                evidence_by_item[event.work_item_id].append(value)

    snapshots: list[dict] = []
    for item_id in ordered_ids:
        item = item_by_id.get(item_id)
        if item is None:
            continue
        item_dependencies: list[dict[str, str]] = []
        for raw_id in list(item.dependency_ids or [])[:12]:
            try:
                dependency_id = uuid.UUID(str(raw_id))
            except (TypeError, ValueError):
                continue
            dependency = dependency_by_id.get(dependency_id)
            item_dependencies.append(
                {
                    "id": str(dependency_id),
                    "title": dependency.title if dependency else "",
                    "status": dependency.status if dependency else "unknown",
                }
            )
        normalized = normalize_project_work_item_snapshot(
            {
                "id": str(item.id),
                "title": item.title,
                "description": item.description,
                "status": item.status,
                "acceptance_criteria": list(item.acceptance_criteria or []),
                "dependencies": item_dependencies,
                "evidence": evidence_by_item.get(item.id, []),
            }
        )
        if normalized is not None:
            snapshots.append(normalized)
    return snapshots


async def _batch_original_human_request(
    db,
    project_id: uuid.UUID,
    group_session_id: uuid.UUID,
    source_rows: list[ChatMessage],
) -> dict[str, str] | None:
    """Resolve the bounded Human request at the root of a reply batch."""

    from app.models.project import ProjectRun
    from app.services.project_collaboration_prompt import (
        PROJECT_HUMAN_REQUEST_MAX_CHARS,
        bounded_project_text,
    )

    frontier = {run_id for row in source_rows for run_id in _batch_row_run_ids(row)}
    visited: set[uuid.UUID] = set()
    message_ids: set[uuid.UUID] = set()
    for _depth in range(8):
        current = frontier - visited
        if not current:
            break
        visited.update(current)
        runs = (
            (
                await db.execute(
                    select(ProjectRun).where(
                        ProjectRun.project_id == project_id,
                        ProjectRun.id.in_(current),
                    )
                )
            )
            .scalars()
            .all()
        )
        frontier = set()
        for run in runs:
            payload = dict(run.input or {})
            dispatch = dict(payload.get("dispatch") or {})
            for raw_message_id in (payload.get("group_message_id"), dispatch.get("turn_anchor_id")):
                try:
                    message_ids.add(uuid.UUID(str(raw_message_id)))
                except (TypeError, ValueError):
                    continue
            try:
                frontier.add(uuid.UUID(str(payload.get("parent_project_run_id"))))
            except (TypeError, ValueError):
                continue

    if not message_ids:
        return None
    request = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.id.in_(message_ids),
                ChatMessage.conversation_id == str(group_session_id),
                ChatMessage.role == "user",
                ChatMessage.sender_user_id.is_not(None),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if request is None:
        return None
    return {
        "message_id": str(request.id),
        "content": bounded_project_text(request.content, PROJECT_HUMAN_REQUEST_MAX_CHARS),
    }

PROJECT_DISPATCH_TRIGGERS = frozenset(
    {
        "a2a",
        "leader_kickoff",
        "group_leader_message",
        "group_mention",
        "manual",
        "leader",
        "schedule",
        "retry",
    }
)
def _project_parallel_run_limit(project) -> int:
    project_settings = project.settings if isinstance(project.settings, dict) else {}
    runtime_settings = project_settings.get("runtime")
    runtime_settings = runtime_settings if isinstance(runtime_settings, dict) else {}
    policy_settings = project_settings.get("policies")
    policy_settings = policy_settings if isinstance(policy_settings, dict) else {}
    raw_limit = runtime_settings.get(
        "max_parallel_runs",
        policy_settings.get("max_parallel_runs", 4),
    )
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 4
    return max(1, min(limit, 32))


PROJECT_DISPATCH_BATCH_SIZE = 50
ProjectDispatchCursor = tuple[datetime, uuid.UUID]

async def _project_run_has_child_input(
    child_id: uuid.UUID,
    project_run_id: uuid.UUID,
) -> bool:
    async with async_session() as db:
        existing = (
            await db.execute(
                select(ChatMessage.id)
                .where(
                    ChatMessage.conversation_id == str(child_id),
                    ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return existing is not None


async def _project_run_child_input_state(
    db,
    *,
    child_id: uuid.UUID,
    project_run_id: uuid.UUID,
) -> str | None:
    return await db.scalar(
        select(ChatMessage.message_meta["subagent_input_state"].as_string())
        .where(
            ChatMessage.conversation_id == str(child_id),
            ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
        )
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .limit(1)
    )


async def _publish_project_run_group_turn(project_run_id: uuid.UUID) -> None:
    from app.services.project_group_turn_lifecycle import (
        reconcile_and_publish_project_run_group_turn,
    )

    await reconcile_and_publish_project_run_group_turn(project_run_id)
