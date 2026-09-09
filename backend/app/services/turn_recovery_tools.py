"""Durable tool-tail completion for interrupted turn recovery."""

from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.chat_history import (
    load_recoverable_messages_for_turn,
    persist_tool_call_row,
    rewrite_tool_call_done_results,
)
from app.services.llm.tool_output_store import finalize_tool_output
from app.services.media_ai_jobs import recover_generation
from app.services.media_ai_sessions import recover_media_submission
from app.services.media_ai_io import MediaAIError
from app.services.media_ai_tools import error_result
from app.services.turn_recovery import (
    RECOVERY_TOOL_MATERIALIZE_TIMEOUT_SECONDS,
    _RecoveryOrigin,
    _recovery_origin_matches,
    _tool_payload,
    _unfinished_tool_call_rows,
)


async def _complete_unfinished_tool_calls(
    db,
    anchor: ChatMessage,
    *,
    ctx_size: int,
    expected_origin: _RecoveryOrigin,
    execution_agent_id: uuid.UUID | None = None,
    release_db_before_execution: bool = False,
) -> int:
    execution_agent_id = execution_agent_id or anchor.agent_id
    rows = await load_recoverable_messages_for_turn(
        db,
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        turn_anchor_id=anchor.id,
        ctx_size=ctx_size,
    )
    unfinished = _unfinished_tool_call_rows(rows, turn_anchor_id=anchor.id)
    if release_db_before_execution:
        await db.close()
    completed = 0
    for _row, payload, key in unfinished:
        if not await _recovery_origin_matches(anchor, expected_origin):
            return completed
        name = str(payload.get("name") or payload.get("tool_name") or "")
        if not name:
            continue
        args = payload.get("args")
        if args is None:
            args = payload.get("arguments")
        if args is None:
            args = {}
        result_text = (
            "[Recovery blocked automatic replay] The previous execution was "
            "interrupted before its durable result was committed. The platform "
            "did not execute it again. Inspect the target state before choosing "
            "a safe next action."
        )
        submitted_media = await recover_media_submission(_row) if name in {"generate_media", "read_media"} else None
        if submitted_media is not None:
            result_text = submitted_media
        elif name == "generate_media":
            try:
                result_text = await recover_generation(
                    _row, execution_agent_id=execution_agent_id,
                    guard=lambda: _recovery_origin_matches(anchor, expected_origin),
                ) or result_text
            except MediaAIError as exc:
                result_text = error_result(exc)
        if not await _recovery_origin_matches(anchor, expected_origin):
            return completed
        llm_view = await finalize_tool_output(
            result_text,
            tool_name=name,
            agent_id=execution_agent_id,
            session_id=anchor.conversation_id,
            tool_call_id=key,
        )
        async with async_session() as done_db:
            await persist_tool_call_row(
                done_db,
                agent_id=anchor.agent_id,
                user_id=anchor.user_id,
                conversation_id=anchor.conversation_id,
                evt={
                    "name": name,
                    "call_id": key,
                    "args": args,
                    "status": "done",
                    "result": llm_view,
                    "reasoning_content": payload.get("reasoning_content"),
                    "assistant_content": payload.get("assistant_content"),
                    "responses_snapshot": (_row.message_meta or {}).get("responses_snapshot"),
                    "recovery_prefix_messages": payload.get("recovery_prefix_messages") or [],
                    "round_id": payload.get("round_id"),
                    "round_tool_index": payload.get("round_tool_index"),
                },
                turn_anchor_id=anchor.id,
            )
            await done_db.commit()
        completed += 1
    return completed


async def _normalize_completed_tool_rounds_for_recovery(
    anchor: ChatMessage,
    *,
    execution_agent_id: uuid.UUID,
) -> None:
    """Apply the live 64K round budget to crash-interrupted durable rows.

    Done rows are committed after each tool so recovery never repeats a side
    effect. A crash can therefore occur before the live loop's end-of-round
    rewrite. ``round_id`` makes that window recoverable without guessing from
    timestamps or adjacent rows.
    """
    from app.services.llm.client import LLMMessage
    from app.services.llm.tool_output_store import enforce_message_budget

    async with async_session() as read_db:
        rows = list(
            (
                await read_db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == anchor.agent_id,
                        ChatMessage.conversation_id == anchor.conversation_id,
                        ChatMessage.role == "tool_call",
                        ChatMessage.message_meta["turn_anchor_id"].as_string()
                        == str(anchor.id),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        await read_db.commit()

    grouped: dict[str, list[tuple[uuid.UUID, dict]]] = {}
    for row in rows:
        payload = _tool_payload(row)
        if not payload or payload.get("status") != "done":
            continue
        round_id = str(payload.get("round_id") or "")
        if round_id:
            grouped.setdefault(round_id, []).append((row.id, dict(payload)))

    replacements: dict[uuid.UUID, tuple[str, str]] = {}
    expected_results: dict[uuid.UUID, str] = {}
    for round_rows in grouped.values():
        round_rows.sort(
            key=lambda item: (
                item[1].get("round_tool_index")
                if isinstance(item[1].get("round_tool_index"), int)
                else 2**31,
                str(item[0]),
            )
        )
        tool_calls: list[dict] = []
        messages: list[LLMMessage] = []
        for row_id, payload in round_rows:
            call_id = str(payload.get("call_id") or payload.get("tool_call_id") or row_id)
            args = payload.get("args")
            if args is None:
                args = payload.get("arguments") or {}
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(payload.get("name") or payload.get("tool_name") or "unknown"),
                        "arguments": json.dumps(args, ensure_ascii=False, default=str),
                    },
                }
            )
        messages.append(LLMMessage(role="assistant", content=None, tool_calls=tool_calls))
        for row_id, payload in round_rows:
            call_id = str(payload.get("call_id") or payload.get("tool_call_id") or row_id)
            messages.append(
                LLMMessage(
                    role="tool",
                    tool_call_id=call_id,
                    content=str(payload.get("result") or ""),
                )
            )
        rewrites = await asyncio.wait_for(
            enforce_message_budget(
                messages,
                fresh_start_idx=0,
                agent_id=execution_agent_id,
                session_id=anchor.conversation_id,
            ),
            timeout=RECOVERY_TOOL_MATERIALIZE_TIMEOUT_SECONDS,
        )
        row_by_call_id = {
            str(payload.get("call_id") or payload.get("tool_call_id") or row_id): (row_id, payload)
            for row_id, payload in round_rows
        }
        for rewrite in rewrites:
            matched = row_by_call_id.get(rewrite.tool_call_id)
            if matched is None:
                raise RuntimeError(
                    f"recovery tool rewrite missing row call_id={rewrite.tool_call_id!r}"
                )
            row_id, payload = matched
            replacements[row_id] = (rewrite.tool_call_id, rewrite.final_content)
            expected_results[row_id] = str(payload.get("result") or "")

    if replacements:
        async with async_session() as write_db:
            await rewrite_tool_call_done_results(
                write_db,
                agent_id=anchor.agent_id,
                user_id=anchor.user_id,
                conversation_id=anchor.conversation_id,
                replacements=replacements,
                turn_anchor_id=anchor.id,
                expected_results=expected_results,
            )
            await write_db.commit()
