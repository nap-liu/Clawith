"""Project group timeline projection backed by standard Web Chat rows.

The project group session remains the durable coordination log. This module
adds the current child turns as a read projection so thinking, tools and
confirmations use exactly the same client contract as ordinary Web Chat.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.project import ProjectRun
from app.services.chat_history import parse_tool_call_for_display
from app.services.chat_message_serializer import serialize_chat_message_for_client

_GROUP_RUN_TRIGGERS = {"group_leader_message", "group_mention", "leader_reply_batch"}
_HIDDEN_CHILD_KINDS = {"subagent_fork_context", "subagent_input"}


def _metadata(message: ChatMessage) -> dict[str, Any]:
    value = message.message_meta
    return dict(value) if isinstance(value, dict) else {}


def serialize_project_group_message(message: ChatMessage) -> dict[str, Any]:
    """Serialize a stored group row through the standard client serializer."""

    metadata = _metadata(message)
    entry = serialize_chat_message_for_client(
        message,
        source_channel="project",
        sender_user_id=message.sender_user_id,
        sender_agent_id=message.sender_agent_id,
    )
    # Keep the established project-group response alias while the standard
    # Web Chat renderer consumes ``display_name``. Existing API clients still
    # read ``name`` from this endpoint.
    entry["attachments"] = [
        {**attachment, "name": attachment.get("display_name")}
        for attachment in entry.get("attachments") or []
    ]
    entry.update(
        {
            "session_id": message.conversation_id,
            "metadata": metadata,
            "message_meta": metadata,
        }
    )
    return entry


def _serialize_child_message(message: ChatMessage) -> dict[str, Any]:
    """Apply the same serializer/parser contract as the Web Chat history API."""

    sender_agent_id = message.sender_agent_id or message.agent_id
    entry = serialize_chat_message_for_client(
        message,
        source_channel="subagent",
        sender_agent_id=sender_agent_id,
    )
    if message.role == "tool_call":
        # Pending confirmations intentionally use the row id as their resolve
        # handle. Canonical running/done tool rows use their persisted call id.
        entry["toolCallId"] = str(message.id)
        parsed = parse_tool_call_for_display(message.content)
        explicit_tool_call_id = bool(parsed.get("toolCallId"))
        if parsed:
            entry["content"] = ""
            entry.update(parsed)
        if entry.get("toolName") == "request_confirmation":
            entry["toolCallId"] = str(message.id)
            explicit_tool_call_id = True
        entry["toolCallIdExplicit"] = explicit_tool_call_id
    metadata = _metadata(message)
    entry.update(
        {
            "session_id": message.conversation_id,
            "metadata": metadata,
            "message_meta": metadata,
            "project_child_timeline": True,
        }
    )
    return entry


def _run_child_id(run: ProjectRun) -> str | None:
    output = run.output if isinstance(run.output, dict) else {}
    value = output.get("subagent_session_id") or output.get("subagent_run_id")
    if not value:
        dispatch = (run.input if isinstance(run.input, dict) else {}).get("dispatch")
        if isinstance(dispatch, dict):
            value = dispatch.get("subagent_session_id") or dispatch.get("subagent_run_id")
    try:
        return str(uuid.UUID(str(value))) if value else None
    except (TypeError, ValueError):
        return None


def _group_anchor_id(run: ProjectRun) -> str | None:
    data = run.input if isinstance(run.input, dict) else {}
    value = data.get("group_message_id")
    try:
        return str(uuid.UUID(str(value))) if value else None
    except (TypeError, ValueError):
        return None


async def build_project_group_timeline(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    group_messages: list[ChatMessage],
) -> list[dict[str, Any]]:
    """Merge visible group rows with only their corresponding child turns.

    Forked context and internal child inputs stay private to the child session.
    A materialized group reply owns the final bubble; its matching child row is
    used only to restore standard thinking/attachment fields, preventing a
    duplicated final answer.
    """

    if not group_messages:
        return []
    group_message_ids = {str(message.id) for message in group_messages}
    referenced_run_ids: set[uuid.UUID] = set()
    for message in group_messages:
        metadata = _metadata(message)
        raw_run_ids = list(metadata.get("source_project_run_ids") or [])
        raw_run_ids.extend(
            raw.get("project_run_id")
            for raw in metadata.get("subagent_runs") or []
            if isinstance(raw, dict)
        )
        for raw_run_id in raw_run_ids:
            try:
                referenced_run_ids.add(uuid.UUID(str(raw_run_id)))
            except (TypeError, ValueError):
                continue
    run_scope = ProjectRun.input["group_message_id"].as_string().in_(
        group_message_ids
    )
    if referenced_run_ids:
        run_scope = or_(run_scope, ProjectRun.id.in_(referenced_run_ids))
    runs = (
        await db.execute(
            select(ProjectRun).where(
                ProjectRun.project_id == project_id,
                ProjectRun.trigger_type.in_(_GROUP_RUN_TRIGGERS),
                run_scope,
            )
        )
    ).scalars().all()
    run_by_id = {str(run.id): run for run in runs}

    child_ids_by_run: dict[str, str] = {}
    for run in runs:
        child_id = _run_child_id(run)
        if child_id:
            child_ids_by_run[str(run.id)] = child_id
    # The Human anchor is the durable outbox and may know the child before the
    # ProjectRun response projection is refreshed.
    for message in group_messages:
        for raw in _metadata(message).get("subagent_runs") or []:
            if not isinstance(raw, dict):
                continue
            run_id = str(raw.get("project_run_id") or "")
            child_id = raw.get("session_id") or raw.get("run_id")
            if run_id in run_by_id and child_id:
                try:
                    child_ids_by_run.setdefault(run_id, str(uuid.UUID(str(child_id))))
                except (TypeError, ValueError):
                    continue

    child_ids = set(child_ids_by_run.values())
    materialized_child_ids = {
        str(_metadata(message).get("child_message_id"))
        for message in group_messages
        if _metadata(message).get("child_message_id")
    }
    requested_child_message_ids: set[uuid.UUID] = set()
    for value in materialized_child_ids:
        try:
            requested_child_message_ids.add(uuid.UUID(value))
        except (TypeError, ValueError):
            continue

    child_inputs: list[ChatMessage] = []
    if child_ids:
        child_inputs = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id.in_(child_ids),
                        ChatMessage.role == "user",
                        ChatMessage.message_meta["project_run_id"]
                        .as_string()
                        .in_(set(run_by_id)),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars().all()
        )

    allowed_anchors_by_child: dict[str, set[str]] = defaultdict(set)
    run_ids_by_anchor: dict[tuple[str, str], set[str]] = defaultdict(set)
    group_id_by_anchor: dict[tuple[str, str], str] = {}
    for child_input in child_inputs:
        metadata = _metadata(child_input)
        run_id = str(metadata.get("project_run_id") or "")
        if run_id not in run_by_id or child_ids_by_run.get(run_id) != child_input.conversation_id:
            continue
        anchor_id = str(metadata.get("subagent_turn_anchor_id") or child_input.id)
        allowed_anchors_by_child[child_input.conversation_id].add(anchor_id)
        run_ids_by_anchor[(child_input.conversation_id, anchor_id)].add(run_id)
        group_anchor = _group_anchor_id(run_by_id[run_id])
        if group_anchor:
            group_id_by_anchor[(child_input.conversation_id, anchor_id)] = group_anchor

    allowed_anchor_ids = {
        anchor_id
        for anchor_ids in allowed_anchors_by_child.values()
        for anchor_id in anchor_ids
    }
    child_rows: list[ChatMessage] = []
    if child_ids and allowed_anchor_ids:
        child_rows.extend(
            list(
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id.in_(child_ids),
                            ChatMessage.role.in_(["assistant", "tool_call"]),
                            ChatMessage.message_meta["turn_anchor_id"]
                            .as_string()
                            .in_(allowed_anchor_ids),
                        )
                        .order_by(ChatMessage.created_at, ChatMessage.id)
                    )
                ).scalars().all()
            )
        )
    if requested_child_message_ids:
        existing_ids = {row.id for row in child_rows}
        materialized_rows = (
            await db.execute(select(ChatMessage).where(ChatMessage.id.in_(requested_child_message_ids)))
        ).scalars().all()
        child_rows.extend(row for row in materialized_rows if row.id not in existing_ids)

    final_key_to_id = {
        f"project-subagent:{row.id}": str(row.id)
        for row in child_rows
        if row.role == "assistant"
    }
    globally_materialized_child_ids: set[str] = set()
    if final_key_to_id:
        materialized_keys = (
            await db.execute(
                select(ChatMessage.external_event_key).where(
                    ChatMessage.external_event_key.in_(set(final_key_to_id))
                )
            )
        ).scalars().all()
        globally_materialized_child_ids = {
            final_key_to_id[key]
            for key in materialized_keys
            if key in final_key_to_id
        }

    visible_child_rows: list[ChatMessage] = []
    child_row_by_id: dict[str, ChatMessage] = {}
    for row in child_rows:
        child_row_by_id[str(row.id)] = row
        metadata = _metadata(row)
        if metadata.get("kind") in _HIDDEN_CHILD_KINDS:
            continue
        anchor_id = str(metadata.get("turn_anchor_id") or "")
        anchor_key = (row.conversation_id, anchor_id)
        if (
            anchor_id
            and anchor_id
            in allowed_anchors_by_child.get(row.conversation_id, set())
            and group_id_by_anchor.get(anchor_key) in group_message_ids
        ):
            visible_child_rows.append(row)

    projected: list[tuple[Any, str, dict[str, Any]]] = []
    consumed_child_ids: set[str] = set(globally_materialized_child_ids)
    for message in group_messages:
        entry = serialize_project_group_message(message)
        child_message_id = str(_metadata(message).get("child_message_id") or "")
        child_final = child_row_by_id.get(child_message_id)
        if child_final is not None:
            standard_final = _serialize_child_message(child_final)
            for key in ("content", "display_content", "attachments", "thinking"):
                if key in standard_final:
                    entry[key] = standard_final[key]
            child_anchor_id = str(_metadata(child_final).get("turn_anchor_id") or "")
            anchor_key = (child_final.conversation_id, child_anchor_id)
            message_metadata = _metadata(message)
            group_anchor_id = str(
                message_metadata.get("timeline_anchor_id")
                or group_id_by_anchor.get(anchor_key)
                or ""
            ) or None
            producer_scope = str(
                message_metadata.get("producer_scope")
                or f"project:{child_final.conversation_id}:{child_anchor_id}"
            )
            entry["metadata"] = {
                **entry["metadata"],
                "turn_anchor_id": group_anchor_id,
                "producer_scope": producer_scope,
            }
            entry["message_meta"] = entry["metadata"]
            entry["turnAnchorId"] = group_anchor_id
            entry["producerScope"] = producer_scope
            entry["producer_scope"] = producer_scope
            entry["canonicalDone"] = True
            consumed_child_ids.add(child_message_id)
        projected.append((message.created_at, str(message.id), entry))

    tool_positions: dict[tuple[str, str, str], int] = {}
    child_projected: list[tuple[Any, str, dict[str, Any]]] = []
    for row in visible_child_rows:
        if str(row.id) in consumed_child_ids:
            continue
        entry = _serialize_child_message(row)
        anchor_id = str(_metadata(row).get("turn_anchor_id") or "")
        anchor_key = (row.conversation_id, anchor_id)
        group_anchor_id = group_id_by_anchor.get(anchor_key)
        producer_scope = f"project:{row.conversation_id}:{anchor_id}"
        entry["metadata"] = {
            **entry["metadata"],
            "turn_anchor_id": group_anchor_id,
            "producer_scope": producer_scope,
            "project_timeline": {
                "group_message_id": group_anchor_id,
                "project_run_ids": sorted(run_ids_by_anchor.get(anchor_key, set())),
                "subagent_session_id": row.conversation_id,
            },
        }
        entry["message_meta"] = entry["metadata"]
        entry["turnAnchorId"] = group_anchor_id
        entry["producerScope"] = producer_scope
        entry["producer_scope"] = producer_scope
        item = (row.created_at, str(row.id), entry)
        if row.role != "tool_call":
            child_projected.append(item)
            continue
        tool_key = (
            row.conversation_id,
            anchor_id,
            str(entry.get("toolCallId") or row.id),
        )
        previous_position = tool_positions.get(tool_key)
        if previous_position is None:
            tool_positions[tool_key] = len(child_projected)
            child_projected.append(item)
            continue
        previous = child_projected[previous_position]
        if previous[2].get("toolStatus") == "done" and entry.get("toolStatus") == "running":
            continue
        entry["created_at"] = previous[2].get("created_at") or entry.get("created_at")
        child_projected[previous_position] = (previous[0], previous[1], entry)

    projected.extend(child_projected)
    projected.sort(key=lambda item: (item[0], item[1]))
    # Keep every child event inside the Human message partition that caused
    # its ProjectRun. A later Human message may join the same lifecycle cohort
    # while an older producer is still emitting tools/chunks; global timestamp
    # order would otherwise place those older-turn events after the new anchor.
    for group_anchor in [row for row in group_messages if row.role == "user"]:
        anchor_id = str(group_anchor.id)
        scoped = [
            item
            for item in projected
            if item[2].get("role") != "user"
            and str(
                item[2].get("turnAnchorId")
                or dict(item[2].get("metadata") or {}).get("turn_anchor_id")
                or ""
            )
            == anchor_id
        ]
        if not scoped:
            continue
        scoped_ids = {id(item) for item in scoped}
        remaining = [item for item in projected if id(item) not in scoped_ids]
        anchor_index = next(
            (
                index
                for index, item in enumerate(remaining)
                if str(item[2].get("id") or "") == anchor_id
            ),
            -1,
        )
        if anchor_index < 0:
            continue
        next_user_index = next(
            (
                index
                for index in range(anchor_index + 1, len(remaining))
                if remaining[index][2].get("role") == "user"
            ),
            len(remaining),
        )
        projected = [
            *remaining[:next_user_index],
            *scoped,
            *remaining[next_user_index:],
        ]
    return [entry for _created_at, _message_id, entry in projected]
