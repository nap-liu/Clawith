"""One-shot agent execution used by heartbeat-adjacent automation."""

import json
from time import perf_counter
from types import SimpleNamespace
import uuid

from loguru import logger
from sqlalchemy import select

from app.core.logging_config import new_trace_id
from app.services.llm.utils import parse_tool_arguments


async def _notify_oneshot_error(
    triggered_by_user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    agent_name: str,
    error_msg: str,
) -> None:
    """Create a platform notification for the admin who triggered a failed oneshot task."""
    if not triggered_by_user_id:
        return
    try:
        from app.database import async_session
        from app.models.notification import Notification
        async with async_session() as db:
            db.add(Notification(
                user_id=triggered_by_user_id,
                type="system",
                title=f"{agent_name} task failed",
                body=error_msg[:500],
                link=f"/agents/{agent_id}#chat",
                ref_id=agent_id,
                sender_name=agent_name,
            ))
            await db.commit()
        logger.info(f"[Oneshot] Notified user {triggered_by_user_id} about {agent_name} failure")
    except Exception as e:
        logger.warning(f"[Oneshot] Failed to create error notification: {e}")


async def run_agent_oneshot(
    agent_id: uuid.UUID,
    prompt: str,
    triggered_by_user_id: uuid.UUID | None = None,
    max_rounds: int = 40,
) -> str:
    """Run an agent with a specific one-shot task prompt.

    Reuses the same LLM + tools infrastructure as the heartbeat, but:
    - Accepts an arbitrary task prompt instead of the HEARTBEAT.md instruction
    - Does NOT update last_heartbeat_at
    - Does NOT check active hours
    - Configurable max_rounds to handle multi-member outreach tasks
    - Sends a platform notification to the triggering user on failure

    Returns the final reply string (for logging purposes).
    """
    new_trace_id()
    client_guard = None
    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.llm import LLMModel
        from app.services.llm import get_model_api_key

        # ── Phase 1: Read agent + model config (short DB transaction) ──────────
        agent_name = ""
        agent_role = ""
        execution_user_id = None
        model_provider = ""
        model_api_key = ""
        model_api_key_encrypted = ""
        model_model = ""
        model_base_url = None
        model_temperature = None
        model_max_output_tokens = None
        model_request_timeout = None
        model_context_window = 0
        model_context_usage_ratio = 0.7

        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"[Oneshot] Agent {agent_id} not found — aborting")
                return ""
            from app.core.okr_feature import is_retired_okr_agent

            if await is_retired_okr_agent(db, agent):
                logger.info(f"[Oneshot] Retired system Agent {agent_id} skipped")
                return ""

            model_id = agent.primary_model_id or agent.fallback_model_id
            if not model_id:
                msg = "Agent has no LLM model configured. Please assign a model in Agent Settings."
                logger.warning(f"[Oneshot] Agent {agent_id} has no model configured — aborting")
                await _notify_oneshot_error(triggered_by_user_id, agent_id, agent_name or str(agent_id), msg)
                return ""

            model_result = await db.execute(select(LLMModel).where(LLMModel.id == model_id))
            model = model_result.scalar_one_or_none()
            if not model:
                msg = f"The configured LLM model ({model_id}) was not found. Please check Agent Settings."
                logger.warning(f"[Oneshot] Model {model_id} not found — aborting")
                await _notify_oneshot_error(triggered_by_user_id, agent_id, agent_name or str(agent_id), msg)
                return ""

            agent_name = agent.name
            agent_role = agent.role_description or ""
            from app.services.execution_identity import resolve_execution_user_id

            execution_user_id = await resolve_execution_user_id(
                db,
                agent,
                triggered_by_user_id,
                legacy_user_id=agent.creator_id,
            )
            model_provider = model.provider
            model_api_key = get_model_api_key(model)
            model_api_key_encrypted = model.api_key_encrypted
            model_model = model.model
            model_base_url = model.base_url
            model_temperature = agent.temperature if agent.temperature is not None else model.temperature
            model_max_output_tokens = getattr(model, "max_output_tokens", None)
            model_request_timeout = getattr(model, "request_timeout", None)
            model_context_window = getattr(model, "context_window", 0)
            model_context_usage_ratio = getattr(model, "context_usage_ratio", 0.7)

            # Build agent identity context (system prompt + dynamic context)
            from app.services.agent_context import build_agent_context
            static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent_role)

            await db.commit()
        # DB session is now closed — connection returned to pool

        from app.services.active_turns import ensure_active_turn

        await ensure_active_turn(
            owner_user_id=execution_user_id,
            agent_id=agent_id,
            session_id=f"oneshot:{uuid.uuid4()}",
            turn_type="oneshot",
            title=prompt.strip()[:40] or None,
        )

        # ── Phase 2: LLM tool-call loop (no DB connection held) ────────────────
        from app.services.llm import (
            LLMClientCloseGuard,
            create_llm_client,
            get_max_tokens,
            LLMMessage,
            LLMError,
        )
        from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
        from app.services.llm.caller import _complete_with_throttle_retry, _guard_provider_dispatch
        from app.services.llm.tool_output_store import enforce_message_budget, finalize_tool_output
        from app.services.token_tracker import (
            TokenUsage,
            record_token_usage,
            extract_token_usage,
        )

        try:
            client = create_llm_client(
                provider=model_provider,
                api_key=model_api_key,
                model=model_model,
                base_url=model_base_url,
                timeout=float(model_request_timeout or 120.0),
                provider_managed_timeout=True,
            )
            client_guard = LLMClientCloseGuard(client)
        except Exception as e:
            msg = f"Failed to initialise the LLM client: {e}"
            logger.error(f"[Oneshot] Failed to create LLM client for {agent_name}: {e}")
            await _notify_oneshot_error(triggered_by_user_id, agent_id, agent_name, msg)
            return ""

        tools_for_llm = await get_agent_tools_for_llm(agent_id)
        runtime_model = SimpleNamespace(
            provider=model_provider,
            model=model_model,
            base_url=model_base_url,
            api_key_encrypted=model_api_key_encrypted,
            request_timeout=model_request_timeout,
            context_window=model_context_window,
            context_usage_ratio=model_context_usage_ratio,
        )
        llm_messages = [
            LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt),
            LLMMessage(role="user", content=prompt),
        ]

        reply = ""
        accumulated_usage = TokenUsage()
        unsaved_usage = TokenUsage()

        for round_i in range(max_rounds):
            # Check token usage limit mid-loop (every 3 rounds)
            if round_i > 0 and round_i % 3 == 0:
                if agent_id and unsaved_usage.total_tokens > 0:
                    try:
                        await record_token_usage(agent_id, unsaved_usage)
                    except Exception as e:
                        logger.warning(f"[Oneshot] Failed to record token usage mid-loop: {e}")
                    unsaved_usage = TokenUsage()
                    from app.services.llm.caller import _get_agent_config
                    _, _token_limit_msg = await _get_agent_config(agent_id)
                    if _token_limit_msg:
                        logger.warning(f"[Oneshot] Token limit exceeded mid-loop: {_token_limit_msg}")
                        await client_guard.close()
                        reply = _token_limit_msg
                        break

            try:
                _round_t0 = perf_counter()
                round_max_tokens = get_max_tokens(model_provider, model_model, model_max_output_tokens)
                context_stop = await _guard_provider_dispatch(
                    model=runtime_model,
                    messages=llm_messages,
                    tools=tools_for_llm,
                    max_output_tokens=round_max_tokens,
                    session_id=f"oneshot:{agent_id}",
                )
                if context_stop:
                    reply = context_stop
                    break
                response = await _complete_with_throttle_retry(
                    client,
                    model=runtime_model,
                    round_i=round_i + 1,
                    messages=llm_messages,
                    tools=tools_for_llm,
                    temperature=model_temperature,
                    max_tokens=round_max_tokens,
                )
                logger.info(
                    f"[LLM Timing] round={round_i + 1} model={model_model} "
                    f"llm_call={perf_counter() - _round_t0:.2f}s (oneshot) agent={agent_id}"
                )
            except LLMError as e:
                logger.error(f"[Oneshot] LLM error (round {round_i}): {e}")
                await _notify_oneshot_error(
                    triggered_by_user_id, agent_id, agent_name,
                    f"LLM call failed (round {round_i}): {e}",
                )
                break
            except Exception as e:
                logger.error(f"[Oneshot] Unexpected LLM error (round {round_i}): {e}")
                await _notify_oneshot_error(
                    triggered_by_user_id, agent_id, agent_name,
                    f"Unexpected error during LLM call (round {round_i}): {e}",
                )
                break

            # Track token usage
            usage = extract_token_usage(response.usage)
            if not usage:
                usage = TokenUsage()
            accumulated_usage.add(usage)
            unsaved_usage.add(usage)

            if response.tool_calls:
                fresh_start = len(llm_messages)
                llm_messages.append(LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=[{
                        "id": tc["id"],
                        "type": "function",
                        "function": tc["function"],
                    } for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                ))

                for tc in response.tool_calls:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError:
                        args = {}

                    logger.info(f"[Oneshot:{agent_name}] Tool call: {tool_name}({list(args.keys())})")
                    tool_result = await execute_tool(
                        tool_name, args, agent_id, execution_user_id,
                        tool_call_id=tc["id"],
                        tools_for_llm=tools_for_llm,
                    )
                    llm_view = await finalize_tool_output(
                        tool_result,
                        tool_name=tool_name,
                        agent_id=agent_id,
                        session_id=f"oneshot:{agent_id}",
                        tool_call_id=tc["id"],
                    )

                    llm_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=tc["id"],
                        content=llm_view,
                    ))
                await enforce_message_budget(
                    llm_messages,
                    fresh_start_idx=fresh_start,
                    agent_id=agent_id,
                    session_id=f"oneshot:{agent_id}",
                )
            else:
                # No more tool calls — agent has finished
                reply = response.content or ""
                break

        await client_guard.close()

        # ── Phase 3: Record token usage (best-effort) ───────────────────────────
        if unsaved_usage.total_tokens > 0:
            try:
                await record_token_usage(agent_id, unsaved_usage)
            except Exception as e:
                logger.warning(f"[Oneshot] Failed to record token usage: {e}")

        # Log activity
        if reply:
            try:
                from app.services.activity_logger import log_activity
                await log_activity(
                    agent_id, "oneshot_task",
                    f"Oneshot task completed: {reply[:80]}",
                    detail={"reply": reply[:500], "triggered_by": str(triggered_by_user_id)},
                )
            except Exception:
                pass

        # ── Phase 4: Clear any previous error notifications ──────────
        if triggered_by_user_id:
            try:
                from sqlalchemy import delete
                from app.models.notification import Notification
                async with async_session() as db:
                    await db.execute(
                        delete(Notification).where(
                            Notification.user_id == triggered_by_user_id,
                            Notification.ref_id == agent_id,
                            Notification.type == "system",
                            Notification.title.contains("task failed")
                        )
                    )
                    await db.commit()
            except Exception as e:
                logger.warning(f"[Oneshot] Failed to clear error notifications: {e}")

        logger.info(f"[Oneshot] {agent_name} completed ({round_i + 1} rounds, {accumulated_usage.total_tokens} tokens)")
        return reply

    except Exception as e:
        logger.exception(f"[Oneshot] Unexpected error for agent {agent_id}: {e}")
        return ""
    finally:
        if client_guard is not None:
            await client_guard.close()
