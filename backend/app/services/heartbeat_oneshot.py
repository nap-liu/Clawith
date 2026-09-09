"""One-shot automation as an ordinary durable background turn."""

from __future__ import annotations

import uuid

from loguru import logger

from app.core.logging_config import new_trace_id
from app.database import async_session
from app.models.agent import Agent
from app.services.background_task_admission import create_background_turn
from app.services.execution_identity import resolve_execution_user_id


async def run_agent_oneshot(
    agent_id: uuid.UUID,
    prompt: str,
    triggered_by_user_id: uuid.UUID | None = None,
    max_rounds: int = 40,
) -> str:
    """Save the original prompt, then use shared execution and recovery."""
    from app.core.okr_feature import is_retired_okr_agent
    from app.services.background_turns import run_background_turn

    new_trace_id()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        if agent is None or await is_retired_okr_agent(db, agent):
            return ""
        execution_user_id = await resolve_execution_user_id(
            db, agent, triggered_by_user_id, legacy_user_id=agent.creator_id,
        )
        anchor = await create_background_turn(
            db, agent=agent, kind="oneshot", reference_id=uuid.uuid4(),
            user_prompt=prompt, execution_user_id=execution_user_id,
            settings={"max_tool_rounds_override": max_rounds},
            completion={
                "triggered_by_user_id": str(triggered_by_user_id) if triggered_by_user_id else None,
                "agent_name": agent.name,
            },
        )
        anchor_id = anchor.id
        await db.commit()
    try:
        return await run_background_turn(anchor_id) or ""
    except Exception:
        logger.exception("Oneshot {} interrupted; its durable turn remains recoverable", anchor_id)
        return ""
