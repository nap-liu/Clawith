"""High-level agent LLM entry points."""

import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.llm.caller_failover import call_llm_with_failover


async def call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[dict] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    on_status=None,
) -> str:
    """Call the agent's LLM with automatic failover support."""
    from app.core.permissions import is_agent_expired
    from app.models.agent import Agent
    from app.services.chat_model_selection import resolve_runtime_models

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 数字员工未找到"
    from app.core.okr_feature import is_retired_okr_agent

    if await is_retired_okr_agent(db, agent):
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "数字员工已过期并停止服务，请联系管理员延长有效期。"

    runtime_models = await resolve_runtime_models(db, agent=agent)
    primary_model = runtime_models.primary_model
    fallback_model = runtime_models.fallback_model

    if not primary_model:
        return f"⚠️ {agent.name} 未配置 LLM 模型，请在管理后台设置。"

    # Build conversation messages
    messages: list[dict] = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    # Use unified call_llm_with_failover
    try:
        reply = await call_llm_with_failover(
            primary_model=primary_model,
            fallback_model=fallback_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
            on_status=on_status,
            turn_anchor_id=None,
        )
        return reply
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[call_agent_llm] Unexpected error: {error_msg}")
        return f"⚠️ 调用模型出错: {error_msg[:150]}"


async def call_agent_llm_with_tools(
    db: AsyncSession,
    agent_id: uuid.UUID,
    system_prompt: str,
    user_prompt: str,
    max_rounds: int = 50,
    session_id: str = "",
    execution_user_id: uuid.UUID | None = None,
    turn_type: str = "background",
    model_override_id: uuid.UUID | str | None = None,
    temperature_override: float | None = None,
    reasoning_effort_override: str | None = None,
    on_status=None,
) -> str:
    """Accept a durable background invocation through the ordinary turn owner."""
    from app.models.agent import Agent
    from app.services.background_task_admission import create_background_turn
    from app.services.background_turns import run_background_turn
    from app.services.llm.failure_outcome import make_llm_failure

    agent = await db.get(Agent, agent_id)
    if agent is None:
        return make_llm_failure(code="model_turn_failed", message_key="errors.modelTurnFailed")
    from app.core.okr_feature import is_retired_okr_agent

    if await is_retired_okr_agent(db, agent):
        return make_llm_failure(code="model_turn_failed", message_key="errors.modelTurnFailed")
    anchor = await create_background_turn(
        db, agent=agent, kind="background", reference_id=uuid.uuid4(),
        user_prompt=user_prompt, execution_user_id=execution_user_id,
        settings={
            "model_override_id": str(model_override_id) if model_override_id else None,
            "temperature_override": temperature_override,
            "reasoning_effort_override": reasoning_effort_override,
            "max_tool_rounds_override": max_rounds,
            "prepared_turn_context": [system_prompt, ""],
        },
        completion={"requested_turn_type": turn_type, "resource_id": session_id},
    )
    anchor_id = anchor.id
    await db.commit()
    return await run_background_turn(anchor_id) or ""
