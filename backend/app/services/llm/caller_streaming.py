from __future__ import annotations

"""Streaming caller entry point."""

from app.services.llm.caller_context import *  # noqa: F401,F403
from app.services.llm.caller_shared import *  # noqa: F401,F403
from app.services.llm.caller_streaming_rounds import *  # noqa: F401,F403
from app.services.llm.caller_streaming_support import *  # noqa: F401,F403
from app.services.llm.caller_tooling import *  # noqa: F401,F403


@serialize_conversation_execution
async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_tool_call=None,
    on_tool_delta=None,
    on_thinking=None,
    on_usage=None,
    max_tool_rounds_override: int | None = None,
    skip_tools: bool = False,
    is_group: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    channel_context: dict | None = None,
    turn_anchor_id: uuid.UUID | None = None,
    turn_anchor_agent_id: uuid.UUID | None = None,
    turn_type: str | None = None,
    prepared_turn_context: tuple[str, str] | None = None,
    prepared_tools: list[dict] | None = None,
    context_recovery=None,
    before_round=None,
    before_tool_execution=None,
    include_soul: bool = True,
    include_memory: bool = True,
) -> str:
    state = CallLlmState(
        model=model,
        agent_name=agent_name,
        role_description=role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=on_chunk,
        on_tool_call=on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        on_usage=on_usage,
        is_group=is_group,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        channel_context=channel_context,
        turn_anchor_id=turn_anchor_id,
        turn_anchor_agent_id=turn_anchor_agent_id,
        turn_type=turn_type,
        context_recovery=context_recovery,
        before_round=before_round,
        before_tool_execution=before_tool_execution,
        include_soul=include_soul,
        include_memory=include_memory,
    )

    await _call_llm_resolve_execution_identity_and_active_turn(state)
    token_limit_msg = await _call_llm_load_round_limit(state, max_tool_rounds_override)
    if token_limit_msg:
        return token_limit_msg

    state.on_tool_call = _call_llm_default_on_tool_call(state)
    await _call_llm_prepare_turn_context(state, prepared_turn_context)
    await _call_llm_prepare_tools(state, prepared_tools, skip_tools)
    client, client_error = await _call_llm_prepare_client_and_messages(state, messages)
    if client_error is not None:
        return client_error

    skip_before_round_once = False
    for round_i in range(state.max_tool_rounds):
        await _call_llm_assert_durable_turn_running(state)
        if skip_before_round_once:
            skip_before_round_once = False
        elif state.before_round is not None:
            injected = await _invoke_before_round(state.before_round, round_i)
            if injected:
                from app.services.image_context import prepare_messages_for_model

                prepared_injected = await prepare_messages_for_model(
                    injected,
                    agent_id=state.agent_id,
                    supports_vision=state.supports_vision,
                )
                state.api_messages.extend(
                    LLMMessage(
                        role=msg.get("role", "user"),
                        content=msg.get("content"),
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                    )
                    for msg in prepared_injected
                )

        _warn_threshold_80 = int(state.max_tool_rounds * 0.8)
        _warn_threshold_96 = state.max_tool_rounds - 2
        if round_i == _warn_threshold_80:
            state.api_messages.append(
                LLMMessage(
                    role="user",
                    content=(
                        f"⚠️ 你已使用 {round_i}/{state.max_tool_rounds} 轮工具调用。"
                        "如果当前任务尚未完成，请尽快使用 upsert_focus_item 保存进度，"
                        "并使用 set_trigger 设置续接触发器，在剩余轮次中做好收尾。"
                    ),
                )
            )
        elif round_i == _warn_threshold_96:
            state.api_messages.append(
                LLMMessage(
                    role="user",
                    content="🚨 仅剩 2 轮工具调用。请立即使用 upsert_focus_item 保存进度并设置续接触发器。",
                )
            )

        dispatch_messages = list(state.api_messages)

        if round_i > 0 and round_i % 3 == 0:
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
                state.unsaved_usage = TokenUsage()
                _, token_limit_msg = await _get_agent_config(state.agent_id)
                if token_limit_msg:
                    logger.warning(f"[LLM] Token limit exceeded mid-loop: {token_limit_msg}")
                    await state.client_guard.close()
                    _call_llm_log_turn_timing(state, "token_limit", round_i + 1)
                    return token_limit_msg

        dispatch_budget = measure_dispatch(
            model=state.model,
            messages=dispatch_messages,
            tools=state.tools_for_llm if state.tools_for_llm else None,
            max_output_tokens=state.max_tokens,
            authoritative_prompt_tokens=state.last_authoritative_prompt_tokens,
            count_source=(
                "previous_provider_usage"
                if state.last_authoritative_prompt_tokens is not None
                else "unavailable"
            ),
        )
        dispatch_messages, dispatch_budget, preflight_compaction_required = (
            await _call_llm_apply_preflight_context_recovery(
                state,
                dispatch_messages,
                dispatch_budget,
            )
        )

        if preflight_compaction_required and not (
            state.last_authoritative_prompt_tokens is None
            or (state.preflight_compaction_not_applicable and dispatch_budget.fits)
        ):
            logger.error(
                "[context_guard] required compaction did not produce a safe prompt "
                f"session={state.session_id} provider_tokens={state.last_authoritative_prompt_tokens}"
            )
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "required_compaction_failed", round_i + 1)
            return PROVIDER_CONTEXT_BLOCKED_MESSAGE

        context_stop = None
        if not dispatch_budget.fits:
            context_stop = await _guard_provider_dispatch(
                model=state.model,
                messages=dispatch_messages,
                tools=state.tools_for_llm if state.tools_for_llm else None,
                max_output_tokens=state.max_tokens,
                session_id=state.session_id,
            )
        if context_stop:
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "context_blocked", round_i + 1)
            return context_stop

        try:
            response, dispatch_messages, dispatch_budget = await _call_llm_dispatch_round_with_context_recovery(
                state,
                client,
                dispatch_messages,
                dispatch_budget,
                round_i + 1,
            )
            await _call_llm_track_response_usage(state, response, round_i + 1)
            round_outcome = await _call_llm_resume_truncated_response(
                state,
                client,
                response,
                round_i,
            )
            if round_outcome.terminal_result is not None:
                return round_outcome.terminal_result
            response = round_outcome.response
        except ProviderThrottleExhausted as e:
            logger.error(
                f"[LLM] Provider throttle exhausted: "
                f"provider={getattr(state.model, 'provider', '?')} model={getattr(state.model, 'model', '?')} {e}"
            )
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "throttle_exhausted", round_i + 1)
            return PROVIDER_THROTTLE_USER_MESSAGE
        except LLMError as e:
            if _as_model_response_idle_timeout(e) is not None:
                logger.error(
                    "[LLM] model response idle timeout: "
                    f"provider={getattr(state.model, 'provider', '?')} "
                    f"model={getattr(state.model, 'model', '?')}"
                )
                if state.agent_id and state.unsaved_usage.total_tokens > 0:
                    await record_token_usage(state.agent_id, state.unsaved_usage)
                await state.client_guard.close()
                _call_llm_log_turn_timing(state, "response_idle_timeout", round_i + 1)
                return model_response_idle_timeout_failure()
            logger.error(
                f"[LLM] LLMError: provider={getattr(state.model, 'provider', '?')} model={getattr(state.model, 'model', '?')} {e}"
            )
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "llm_error", round_i + 1)
            return f"[LLM Error] {e}"
        except Exception as e:
            if _as_model_response_idle_timeout(e) is not None:
                logger.error(
                    "[LLM] model response idle timeout: "
                    f"provider={getattr(state.model, 'provider', '?')} "
                    f"model={getattr(state.model, 'model', '?')}"
                )
                if state.agent_id and state.unsaved_usage.total_tokens > 0:
                    await record_token_usage(state.agent_id, state.unsaved_usage)
                await state.client_guard.close()
                _call_llm_log_turn_timing(state, "response_idle_timeout", round_i + 1)
                return model_response_idle_timeout_failure()
            logger.exception(f"[LLM] Unexpected error: {type(e).__name__}: {str(e)[:300]}")
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "call_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        if round_outcome.complete_response_content and round_outcome.complete_response_content.strip():
            state.visible_response_segments.append(round_outcome.complete_response_content)

        if not response.tool_calls:
            plain_text_result = await _call_llm_handle_plain_text_round(
                state,
                response,
                round_outcome,
                dispatch_budget,
                round_i,
            )
            if plain_text_result.advance_round:
                skip_before_round_once = plain_text_result.skip_before_round_once
                continue
            return plain_text_result.result or "[LLM returned empty content]"

        tool_round_result = await _call_llm_execute_tool_round(
            state,
            response,
            round_outcome.recovery_prefix_messages,
            round_i,
        )
        if tool_round_result is not None:
            return tool_round_result

    if state.agent_id and state.unsaved_usage.total_tokens > 0:
        await record_token_usage(state.agent_id, state.unsaved_usage)
    await state.client_guard.close()
    _call_llm_log_turn_timing(state, "round_limit", state.max_tool_rounds)
    return "[Error] Too many tool call rounds"
