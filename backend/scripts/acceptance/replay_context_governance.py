"""Replay production-derived conversations through the real compaction path.

This is an acceptance runner, not a fixture-based test. It expects a disposable
local Docker database populated from read-only production exports, invokes the
same compactor and history loader used by live channels, and writes compaction
state only to that disposable database and the mounted local agent workspace.

Message bodies and credentials are intentionally never printed. The JSON report
contains structural counts, hashes, authoritative provider observations (when
present), and recovery classifications. It does not locally estimate input size.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from loguru import logger

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.services.chat_history import load_history_for_llm
from app.services.llm.compactor import (
    _load_summary_sender_attribution,
    extract_objective_evidence,
    maybe_compact,
    objective_evidence_items_from_rows,
    objective_evidence_limit,
)
from app.services.llm.turn_partition import partition_turns
from app.services.session_token_usage import load_latest_round_context_usage
from app.services.storage import get_storage_backend
from app.services.agent_runtime_workspace import current_agent_runtime_workspace


DEFAULT_SESSION_IDS = (
    "f9914868-a61b-4a28-a723-66abcdb20d71",
    "e46697c0-5946-4f5b-93d8-99616019f34e",
    "3b45732e-3adf-41f5-a15c-5a11ccc33627",
    "57555f7f-697b-4fad-a1a6-ae35bdea1a91",
    "849df1d6-171e-415b-b4af-3316b028b11d",
    "fa6f9885-ea01-4fbd-8e5c-b5ebc7a2bfc7",
    "3c6d38ac-c36e-4b54-83c3-1618822b3ac6",
    "8692cf96-ec3e-41ef-9dca-57437d1a7fac",
    "71651ec0-46a7-4ec1-be02-1a9b798d0f3e",
)
_ARCHIVE_PATH_RE = re.compile(r"Full output saved to:\s*([^\s<>]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ReplayResult:
    session_id: str
    agent_id: str
    model: str
    source_channel: str
    is_group: bool
    rows_before: int
    compactable_rows: int
    protected_rows: int
    protected_closed_turns: int
    authoritative_prompt_tokens_before: int | None
    compacted_rows_after: int
    summary_epoch: int
    summary_tokens: int | None
    archive_materialized: bool
    provider_initial_call_returned: bool
    recovery_classes: list[str]
    protected_suffix_sha256: str
    sequence_valid: bool
    compaction_applied: bool
    passed: bool


def _sha256_rows(rows: list[ChatMessage]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(str(row.id).encode())
        digest.update(b"\0")
        digest.update(str(row.role).encode())
        digest.update(b"\0")
        digest.update((row.content or "").encode())
        digest.update(b"\0")
        digest.update(
            json.dumps(row.message_meta or {}, sort_keys=True, ensure_ascii=False).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_provider_sequence(messages: list[dict[str, Any]]) -> bool:
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            if pending:
                return False
            call_ids = [str(call.get("id") or "") for call in tool_calls]
            if not all(call_ids) or len(call_ids) != len(set(call_ids)):
                return False
            pending.update(call_ids)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in pending:
                return False
            pending.remove(call_id)
            continue
        if pending:
            # A pending confirmation is legal only as the final provider-visible
            # item; it must never be followed by another historical message.
            return False
    return True


def _recovery_classes(reason: str | None) -> list[str]:
    classes: list[str] = []
    for part in (reason or "").split("; "):
        part = part.strip()
        if not part:
            continue
        fields = part.split(":", 2)
        classes.append(":".join(fields[:2]))
    return classes


async def _load_rows(agent_id: uuid.UUID, session_id: str) -> list[ChatMessage]:
    async with async_session() as db:
        result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == session_id,
                ChatMessage.compacted_into.is_(None),
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
        return list(result.scalars().all())


async def _history(
    *, agent_id: uuid.UUID, session_id: str, is_group: bool
) -> list[dict[str, Any]]:
    async with async_session() as db:
        return await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=session_id,
            ctx_size=100,
            is_group=is_group,
        )


async def replay_session(session_id: str) -> ReplayResult:
    async with async_session() as db:
        session = await db.get(ChatSession, uuid.UUID(session_id))
        if session is None:
            raise AssertionError(f"session not found in local clone: {session_id}")
        agent = await db.get(Agent, session.agent_id)
        if agent is None or agent.primary_model_id is None:
            raise AssertionError(f"session has no runnable primary model: {session_id}")
        model = await db.get(LLMModel, agent.primary_model_id)
        if model is None:
            raise AssertionError(f"primary model missing from local clone: {session_id}")
        agent_id = agent.id
        is_group = bool(session.is_group)
        source_channel = session.source_channel

    rows_before = await _load_rows(agent_id, session_id)
    partition = partition_turns(rows_before, keep_recent_turns=model.keep_recent_turns)
    compactable = partition.compactable_rows
    protected = partition.protected_rows + (
        list(partition.current.rows) if partition.current is not None else []
    )
    protected_ids = {row.id for row in protected}
    protected_hash = _sha256_rows(protected)
    protected_closed_turns = sum(turn.closed for turn in partition.protected)

    history_before = await _history(
        agent_id=agent_id,
        session_id=session_id,
        is_group=is_group,
    )
    authoritative_before = await load_latest_round_context_usage(
        agent_id=agent_id,
        session_id=session_id,
        provider=str(model.provider or ""),
        model=str(model.model or ""),
        model_record_id=str(model.id),
        endpoint=str(model.base_url or ""),
    )
    sequence_valid = _validate_provider_sequence(history_before)
    if not sequence_valid:
        raise AssertionError(f"provider history sequence invalid before replay: {session_id}")

    if not compactable:
        async with async_session() as db:
            active_marker = (
                await db.execute(
                    select(ChatCompaction)
                    .where(
                        ChatCompaction.session_id == session_id,
                        ChatCompaction.superseded_by.is_(None),
                        ChatCompaction.summary_validation_passed.is_(True),
                    )
                    .order_by(ChatCompaction.epoch.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            compacted_count = len(
                (
                    await db.execute(
                        select(ChatMessage.id).where(
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == session_id,
                            ChatMessage.compacted_into.is_not(None),
                        )
                    )
                ).all()
            )
        return ReplayResult(
            session_id=session_id,
            agent_id=str(agent_id),
            model=model.model,
            source_channel=source_channel,
            is_group=is_group,
            rows_before=len(rows_before),
            compactable_rows=0,
            protected_rows=len(protected),
            protected_closed_turns=protected_closed_turns,
            authoritative_prompt_tokens_before=authoritative_before,
            compacted_rows_after=compacted_count,
            summary_epoch=active_marker.epoch if active_marker is not None else 0,
            summary_tokens=active_marker.summary_tokens if active_marker is not None else None,
            archive_materialized=False,
            provider_initial_call_returned=False,
            recovery_classes=["not_required:no_compactable_prefix"],
            protected_suffix_sha256=protected_hash,
            sequence_valid=True,
            compaction_applied=False,
            passed=True,
        )

    # The exact latest root/refinements that leave the raw suffix are pinned by
    # the production compactor itself. Recompute the same content-agnostic
    # evidence here and assert it survives the persisted summary.
    async with async_session() as db:
        wrap_user_names, name_map = await _load_summary_sender_attribution(
            db,
            agent_id=agent_id,
            conversation_id=session_id,
            rows=rows_before,
        )
        prior_marker = (
            await db.execute(
                select(ChatCompaction)
                .where(
                    ChatCompaction.session_id == session_id,
                    ChatCompaction.superseded_by.is_(None),
                    ChatCompaction.summary_validation_passed.is_(True),
                )
                .order_by(ChatCompaction.epoch.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    evidence_items = objective_evidence_items_from_rows(
        prior_summary=prior_marker.summary_text if prior_marker is not None else None,
        rows=compactable,
        wrap_user_names=wrap_user_names,
        name_map=name_map,
    )
    expected_evidence = extract_objective_evidence(
        "",
        max_chars=objective_evidence_limit(model.compact_summary_max_tokens),
        evidence_items=evidence_items,
    )

    result = await maybe_compact(
        agent_id=agent_id,
        conversation_id=session_id,
        model=model,
        last_prompt_tokens=authoritative_before,
        force_required=True,
    )
    if not result.triggered or result.summary_id is None:
        raise AssertionError(
            f"required compaction did not complete: {session_id} "
            f"reason={result.skipped_reason}"
        )

    async with async_session() as db:
        marker = await db.get(ChatCompaction, result.summary_id)
        if marker is None or not marker.summary_validation_passed:
            raise AssertionError(f"validated marker missing: {session_id}")
        compacted_count = len(
            (
                await db.execute(
                    select(ChatMessage.id).where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == session_id,
                        ChatMessage.compacted_into.is_not(None),
                    )
                )
            ).all()
        )
        protected_after = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.id.in_(protected_ids))
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            ).scalars()
        )
        if len(protected_after) != len(protected_ids):
            raise AssertionError(f"protected suffix rows disappeared: {session_id}")
        if any(row.compacted_into is not None for row in protected_after):
            raise AssertionError(f"protected suffix was compacted: {session_id}")
        if _sha256_rows(protected_after) != protected_hash:
            raise AssertionError(f"protected suffix bytes changed: {session_id}")
        if expected_evidence and expected_evidence not in marker.summary_text:
            raise AssertionError(f"objective evidence missing from summary: {session_id}")
        archive_paths = _ARCHIVE_PATH_RE.findall(marker.summary_text)
        recovery = _recovery_classes(marker.validation_failure_reason)
        marker_epoch = marker.epoch
        marker_tokens = marker.summary_tokens

    archive_materialized = False
    if archive_paths:
        storage = get_storage_backend()
        workspace = current_agent_runtime_workspace(agent_id)
        archive_materialized = all(
            [await storage.exists(workspace.storage_key(path)) for path in archive_paths]
        )
        if not archive_materialized:
            raise AssertionError(f"summary archive path is unreadable: {session_id}")

    history_after = await _history(
        agent_id=agent_id,
        session_id=session_id,
        is_group=is_group,
    )
    sequence_valid = _validate_provider_sequence(history_after)
    if not sequence_valid:
        raise AssertionError(f"provider history sequence invalid: {session_id}")
    return ReplayResult(
        session_id=session_id,
        agent_id=str(agent_id),
        model=model.model,
        source_channel=source_channel,
        is_group=is_group,
        rows_before=len(rows_before),
        compactable_rows=len(compactable),
        protected_rows=len(protected),
        protected_closed_turns=protected_closed_turns,
        authoritative_prompt_tokens_before=authoritative_before,
        compacted_rows_after=compacted_count,
        summary_epoch=marker_epoch,
        summary_tokens=marker_tokens,
        archive_materialized=archive_materialized,
        provider_initial_call_returned=not any(
            value.startswith("summary_llm_error:") for value in recovery
        ),
        recovery_classes=recovery,
        protected_suffix_sha256=protected_hash,
        sequence_valid=sequence_valid,
        compaction_applied=True,
        passed=True,
    )


async def run(session_ids: list[str], concurrency: int) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def bounded(session_id: str) -> ReplayResult:
        async with semaphore:
            return await replay_session(session_id)

    try:
        results = await asyncio.gather(*(bounded(session_id) for session_id in session_ids))
        return {
            "passed": True,
            "session_count": len(results),
            "provider_initial_call_returns": sum(
                result.compaction_applied and result.provider_initial_call_returned
                for result in results
            ),
            "provider_initial_call_failures": sum(
                result.compaction_applied and not result.provider_initial_call_returned
                for result in results
            ),
            "healthy_noops": sum(not result.compaction_applied for result in results),
            "authoritative_observations_present": sum(
                result.authoritative_prompt_tokens_before is not None
                for result in results
            ),
            "results": [asdict(result) for result in results],
        }
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_ids", nargs="*", default=list(DEFAULT_SESSION_IDS))
    parser.add_argument("--concurrency", type=int, default=2)
    return parser.parse_args()


if __name__ == "__main__":
    # Loguru defaults to DEBUG when the full ASGI logging bootstrap is absent.
    # Acceptance output must never echo production-derived message bodies.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    args = _parse_args()
    print(
        json.dumps(
            asyncio.run(run(args.session_ids, args.concurrency)),
            ensure_ascii=False,
            indent=2,
        )
    )
