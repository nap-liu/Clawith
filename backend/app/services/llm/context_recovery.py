"""One durable context-compaction loop for every conversation initiator."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from app.database import async_session
from app.services.llm.turn_partition import effective_keep_recent_turns


def build_context_recovery(
    *,
    agent_id,
    conversation_id: str,
    turn_anchor_id,
    ctx_size: int,
    primary_model,
    fallback_model=None,
    is_group: bool = False,
    include_thinking: bool = False,
    prefix_messages: list[dict] | None = None,
    normalize: Callable[[list[dict]], list[dict]] | None = None,
    on_recovered: Callable[[list[dict]], Any] | None = None,
):
    """Bind durable identity to the shared compact/reload/retry operation."""
    protected_keep_recent_turns = effective_keep_recent_turns(
        primary_model,
        fallback_model,
    )

    async def _recover(recovery_model, dispatch_budget):
        from app.services.chat_history import load_recoverable_history_for_turn
        from app.services.llm.compactor import (
            COMPACTION_NOT_APPLICABLE_REASONS,
            ContextRecoveryMessages,
            maybe_compact,
        )

        provider_overflow = bool(getattr(dispatch_budget, "provider_overflow", False))
        compacted = await maybe_compact(
            agent_id=agent_id,
            conversation_id=conversation_id,
            model=recovery_model,
            last_prompt_tokens=getattr(dispatch_budget, "authoritative_prompt_tokens", None),
            current_anchor_id=turn_anchor_id,
            force_required=provider_overflow,
            keep_recent_turns_override=(
                getattr(dispatch_budget, "keep_recent_turns_override", None)
                if provider_overflow
                else protected_keep_recent_turns
            ),
        )
        preflight_not_applicable = (
            not compacted.triggered
            and compacted.skipped_reason in COMPACTION_NOT_APPLICABLE_REASONS
            and dispatch_budget.fits
        )
        if not compacted.triggered and not preflight_not_applicable:
            logger.warning(
                "[context_recovery] could not compact session={}: {}",
                conversation_id,
                compacted.skipped_reason,
            )
            return None

        async with async_session() as recovery_db:
            recovered = await load_recoverable_history_for_turn(
                recovery_db,
                agent_id=agent_id,
                conversation_id=conversation_id,
                turn_anchor_id=turn_anchor_id,
                ctx_size=ctx_size,
                is_group=is_group,
                include_thinking=include_thinking,
            )
        if not recovered:
            logger.warning(
                "[context_recovery] lost latest-anchor race session={}",
                conversation_id,
            )
            return None
        recovered = normalize(recovered) if normalize is not None else recovered
        if on_recovered is not None:
            on_recovered(recovered)
        return ContextRecoveryMessages(
            [*(prefix_messages or []), *recovered],
            preflight_not_applicable=preflight_not_applicable,
            compacted=compacted.triggered,
        )

    return _recover
