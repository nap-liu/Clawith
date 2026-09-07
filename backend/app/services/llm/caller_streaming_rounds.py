"""Round helpers for the streaming caller loop."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.llm.caller_context import *  # noqa: F401,F403
from app.services.llm.caller_shared import *  # noqa: F401,F403
from app.services.llm.caller_streaming_support import *  # noqa: F401,F403
from app.services.llm.caller_tooling import *  # noqa: F401,F403


@dataclass
class _CallLlmRoundOutcome:
    response: Any
    complete_response_content: str
    recovery_prefix_messages: list[dict[str, str]]
    accumulated_partials: list[str]
    durable_partial_count: int
    round_visible_joiner: str
    terminal_result: str | None = None


@dataclass
class _CallLlmPlainTextResult:
    advance_round: bool
    result: str | None = None
    skip_before_round_once: bool = False


async def _call_llm_resume_truncated_response(
    state: CallLlmState,
    client,
    response,
    round_i: int,
) -> _CallLlmRoundOutcome:
    accumulated_partials: list[str] = []
    recovery_prefix_messages: list[dict[str, str]] = []
    recovery_count_this_round = 0
    durable_partial_count = 0
    # Only already-completed plain replies participate in the terminal return
    # prefix. Tool-round narration is durable on its tool row but is excluded
    # from the terminal reply, so it must not influence tail splitting.
    round_visible_joiner = "\n\n" if state.terminal_response_segments else ""

    while _response_was_truncated_by_length(response) and state.max_output_recoveries < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
        state.max_output_recoveries += 1
        recovery_count_this_round += 1
        partial_text = response.content or ""
        partial_persisted = await _call_llm_persist_intermediate_segment(
            state,
            partial_text,
            visible_joiner_before=(round_visible_joiner if recovery_count_this_round == 1 else ""),
            max_output_resume_prompt=RESUME_PROMPT,
            thinking=response.reasoning_content,
        )
        accumulated_partials.append(partial_text)
        if not partial_persisted:
            recovery_prefix_messages.extend(
                [
                    {"role": "assistant", "content": partial_text},
                    {"role": "user", "content": RESUME_PROMPT},
                ]
            )
        else:
            durable_partial_count += 1
        state.api_messages.append(
            LLMMessage(
                role="assistant",
                content=partial_text,
            )
        )
        state.api_messages.append(
            LLMMessage(
                role="user",
                content=RESUME_PROMPT,
            )
        )
        logger.info(
            f"[LLM] max_output_tokens hit; resume attempt "
            f"{state.max_output_recoveries}/{MAX_OUTPUT_TOKENS_RECOVERY_LIMIT} "
            f"(round {round_i + 1}, this-round {recovery_count_this_round})"
        )

        dispatch_messages = list(state.api_messages)
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
            return _CallLlmRoundOutcome(
                response=response,
                complete_response_content="",
                recovery_prefix_messages=recovery_prefix_messages,
                accumulated_partials=accumulated_partials,
                durable_partial_count=durable_partial_count,
                round_visible_joiner=round_visible_joiner,
                terminal_result=context_stop,
            )
        resume_budget = measure_dispatch(
            model=state.model,
            messages=dispatch_messages,
            tools=state.tools_for_llm if state.tools_for_llm else None,
            max_output_tokens=state.max_tokens,
        )
        response, dispatch_messages, resume_budget = await _call_llm_dispatch_round_with_context_recovery(
            state,
            client,
            dispatch_messages,
            resume_budget,
            round_i + 1,
        )
        await _call_llm_track_response_usage(state, response, round_i + 1)

    if _response_was_truncated_by_length(response):
        logger.error(
            f"[LLM] Output token limit not recoverable after {MAX_OUTPUT_TOKENS_RECOVERY_LIMIT} resume attempts"
        )
        if state.agent_id and state.unsaved_usage.total_tokens > 0:
            await record_token_usage(state.agent_id, state.unsaved_usage)
        await state.client_guard.close()
        _call_llm_log_turn_timing(state, "output_limit", round_i + 1)
        return _CallLlmRoundOutcome(
            response=response,
            complete_response_content="",
            recovery_prefix_messages=recovery_prefix_messages,
            accumulated_partials=accumulated_partials,
            durable_partial_count=durable_partial_count,
            round_visible_joiner=round_visible_joiner,
            terminal_result="[LLM Error] Output token limit exceeded after 3 resume attempts",
        )

    return _CallLlmRoundOutcome(
        response=response,
        complete_response_content="".join(accumulated_partials) + (response.content or ""),
        recovery_prefix_messages=recovery_prefix_messages,
        accumulated_partials=accumulated_partials,
        durable_partial_count=durable_partial_count,
        round_visible_joiner=round_visible_joiner,
    )


async def _call_llm_handle_plain_text_round(
    state: CallLlmState,
    response,
    round_outcome: _CallLlmRoundOutcome,
    dispatch_budget: DispatchBudget,
    round_i: int,
) -> _CallLlmPlainTextResult:
    has_next_round = round_i + 1 < state.max_tool_rounds
    intermediate_persisted = bool(
        round_outcome.accumulated_partials
        and round_outcome.durable_partial_count == len(round_outcome.accumulated_partials)
        and not (response.content or "").strip()
    )

    async def _persist_before_late_injection(
        *,
        created_at=None,
        _response_content=response.content or "",
        _has_accumulated_partials=bool(round_outcome.accumulated_partials),
        _complete_response_content=round_outcome.complete_response_content,
        _visible_joiner=round_outcome.round_visible_joiner,
        _thinking=response.reasoning_content,
    ):
        nonlocal intermediate_persisted
        if intermediate_persisted:
            return
        late_segment = _response_content if _has_accumulated_partials else _complete_response_content
        intermediate_persisted = await _call_llm_persist_intermediate_segment(
            state,
            late_segment,
            visible_joiner_before=("" if _has_accumulated_partials else _visible_joiner),
            thinking=_thinking,
            created_at=created_at,
        )

    late_injected = (
        await _invoke_before_round(
            state.before_round,
            round_i + 1,
            before_injection=_persist_before_late_injection,
        )
        if state.before_round is not None and has_next_round
        else []
    )
    if late_injected:
        from app.services.image_context import prepare_messages_for_model

        if (
            state.turn_anchor_id is not None
            and state.anchor_agent_id is not None
            and state.durable_user_id is not None
            and state.session_id
            and not intermediate_persisted
        ):
            raise RuntimeError("late injection claimed before intermediate assistant persistence")

        state.api_messages.append(
            LLMMessage(
                role="assistant",
                content=response.content or None,
                reasoning_content=response.reasoning_content,
            )
        )
        prepared_injected = await prepare_messages_for_model(
            late_injected,
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
        if round_outcome.complete_response_content.strip():
            state.terminal_response_segments.append(
                round_outcome.complete_response_content
            )
        return _CallLlmPlainTextResult(
            advance_round=True,
            skip_before_round_once=True,
        )

    if state.last_authoritative_prompt_tokens is not None and state.context_recovery is not None:
        from app.services.llm.compactor import should_compact

        post_round_required, _, _ = should_compact(
            model=state.model,
            last_prompt_tokens=state.last_authoritative_prompt_tokens,
            pre_flight_estimate=None,
        )
        if post_round_required:
            post_round_budget = replace(
                dispatch_budget,
                authoritative_prompt_tokens=state.last_authoritative_prompt_tokens,
                count_source="provider_usage",
                token_overflow=True,
            )
            recovered = await state.context_recovery(state.model, post_round_budget)
            if recovered is None:
                logger.error(
                    "[context_guard] post-round compaction exhausted "
                    f"session={state.session_id} provider_tokens="
                    f"{state.last_authoritative_prompt_tokens}"
                )

    if state.agent_id and state.unsaved_usage.total_tokens > 0:
        await record_token_usage(state.agent_id, state.unsaved_usage)
    await state.client_guard.close()
    _call_llm_log_turn_timing(state, "reply", round_i + 1)
    return _CallLlmPlainTextResult(
        advance_round=False,
        # A response carrying tool calls is an intermediate protocol round, not
        # part of the terminal user reply. Its exact assistant text remains in
        # the durable tool-call row and provider history; only this final plain
        # text round is returned for terminal assistant persistence/delivery.
        result=(
            "\n\n".join(
                segment
                for segment in (
                    *state.terminal_response_segments,
                    round_outcome.complete_response_content,
                )
                if segment and segment.strip()
            )
            or make_llm_failure(
                code="empty_model_response",
                message_key="errors.emptyModelResponse",
                details={"round": round_i + 1},
            )
        ),
    )


async def _call_llm_execute_tool_round(
    state: CallLlmState,
    response,
    recovery_prefix_messages: list[dict[str, str]],
    round_i: int,
) -> str | None:
    logger.info(f"[LLM] Round {round_i + 1}: {len(response.tool_calls)} tool call(s)")
    sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
    if retry_instruction:
        if state.invalid_tool_call_retries >= 1:
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "invalid_tool_call", round_i + 1)
            return make_llm_failure(
                code="invalid_tool_call_stream",
                message_key="errors.invalidToolCallStream",
                details={"round": round_i + 1},
            )
        state.invalid_tool_call_retries += 1
        state.api_messages.append(LLMMessage(role="user", content=retry_instruction))
        return None
    state.invalid_tool_call_retries = 0

    conf_call = find_request_confirmation_call(sanitized_tool_calls)
    if conf_call is not None:
        _conf_action_tool = (conf_call.action or {}).get("tool") if conf_call.action else None
        if conf_call.valid and (_conf_action_tool is None or _conf_action_tool in state.allowed_tool_names):
            from app.services import confirmation_service

            await confirmation_service.suspend_for_confirmation(
                agent_id=state.agent_id,
                conversation_id=state.session_id,
                chat_session_id=None,
                source_channel="web",
                user_id=state.user_id,
                intro_text=_latest_visible_response_segment(*state.visible_response_segments),
                title=conf_call.title,
                summary=conf_call.summary,
                action=conf_call.action,
                risk_level=conf_call.risk_level,
                buttons=conf_call.buttons,
                force_confirmation=conf_call.force_confirmation,
                turn_anchor_id=state.turn_anchor_id,
                assistant_content=response.content or None,
                recovery_prefix_messages=recovery_prefix_messages,
                reasoning_content=response.reasoning_content,
                round_id=(
                    f"{state.turn_anchor_id or state.session_id}:{state.turn_execution_id}:round:{round_i + 1}"
                    if state.turn_anchor_id or state.session_id
                    else f"round:{round_i + 1}"
                ),
            )
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "confirmation_suspended", round_i + 1)
            return ""
        if not conf_call.valid:
            _conf_reason = conf_call.error or "request_confirmation 参数无效"
        else:
            _conf_reason = f"工具 {_conf_action_tool} 未对该 agent 启用,无法挟带"
        state.api_messages.append(
            LLMMessage(
                role="assistant",
                content=response.content or None,
                tool_calls=sanitized_tool_calls,
                reasoning_content=response.reasoning_content,
            )
        )
        state.api_messages.append(
            LLMMessage(
                role="tool",
                content=f"❌ {_conf_reason}",
                tool_call_id=conf_call.call_id,
            )
        )
        return None

    repeat_period = _repeating_tool_period(state.tool_round_history, sanitized_tool_calls)
    if repeat_period is not None:
        logger.warning(
            f"[LLM] Repeated tool-call guard tripped (period={repeat_period}, "
            f"round {round_i + 1}, agent={state.agent_id}); stopping loop gracefully."
        )
        if state.agent_id and state.unsaved_usage.total_tokens > 0:
            await record_token_usage(state.agent_id, state.unsaved_usage)
        await state.client_guard.close()
        _call_llm_log_turn_timing(state, f"tool_loop_period_{repeat_period}", round_i + 1)
        return make_llm_failure(
            code=f"tool_loop_period_{repeat_period}",
            message_key="errors.toolLoopStopped",
            details={"period": repeat_period, "round": round_i + 1},
        )

    await _call_llm_before_tool_execution_guard(state)

    fresh_start = len(state.api_messages)
    state.api_messages.append(
        LLMMessage(
            role="assistant",
            content=response.content or None,
            tool_calls=sanitized_tool_calls,
            reasoning_content=response.reasoning_content,
        )
    )

    full_reasoning_content = response.reasoning_content or ""
    round_done_records: list[_RoundDoneToolCall] = []
    durable_round_id = (
        f"{state.turn_anchor_id or state.session_id}:{state.turn_execution_id}:round:{round_i + 1}"
        if state.turn_anchor_id or state.session_id
        else f"round:{round_i + 1}"
    )
    running_events: list[dict[str, Any]] = []
    for tool_index, tc in enumerate(sanitized_tool_calls or []):
        args = _canonicalize_tc_arguments(tc, state.session_id)
        running_events.append(
            {
                "name": tc["function"]["name"],
                "call_id": tc.get("id", ""),
                "args": args,
                "status": "running",
                "round_id": durable_round_id,
                "round_tool_index": tool_index,
                "reasoning_content": full_reasoning_content,
                "assistant_content": (response.content or None) if tool_index == 0 else None,
                "recovery_prefix_messages": (recovery_prefix_messages if tool_index == 0 else []),
            }
        )
    try:
        persisted_running = await _persist_tool_call_events_strict(
            running_events,
            agent_id=state.anchor_agent_id,
            user_id=state.user_id,
            session_id=state.session_id,
            turn_anchor_id=state.turn_anchor_id,
        )
    except Exception as e:
        logger.exception(f"[LLM] Failed to persist complete planned tool round: {e}")
        if state.agent_id and state.unsaved_usage.total_tokens > 0:
            await record_token_usage(state.agent_id, state.unsaved_usage)
        await state.client_guard.close()
        _call_llm_log_turn_timing(state, "tool_round_persist_error", round_i + 1)
        return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"
    for event in running_events:
        durable_message_id = (
            persisted_running.get(str(event.get("call_id") or ""))
            if isinstance(persisted_running, dict)
            else None
        )
        if durable_message_id is not None:
            event["_durable_persisted"] = True
            event["_durable_message_id"] = str(durable_message_id)
        if state.on_tool_call is not None:
            try:
                await state.on_tool_call(event)
            except Exception:
                pass

    for tool_index, tc in enumerate(sanitized_tool_calls or []):
        await _call_llm_before_tool_execution_guard(state)
        try:
            tool_error = await _process_tool_call(
                tc=tc,
                api_messages=state.api_messages,
                agent_id=state.agent_id,
                user_id=state.user_id,
                session_id=state.session_id,
                supports_vision=state.supports_vision,
                on_tool_call=state.on_tool_call,
                on_code_output=state.on_code_output,
                full_reasoning_content=full_reasoning_content,
                allowed_tool_names=state.allowed_tool_names,
                tools_for_llm=state.tools_for_llm,
                emit_running=False,
                turn_anchor_id=state.turn_anchor_id,
                before_execute=lambda: _call_llm_admit_tool_execution(state),
                round_done_records=round_done_records,
                round_id=durable_round_id,
                round_tool_index=tool_index,
                assistant_content=(response.content or None) if tool_index == 0 else None,
                recovery_prefix_messages=(recovery_prefix_messages if tool_index == 0 else None),
                durable_agent_id=state.anchor_agent_id,
            )
        except Exception as e:
            logger.exception(f"[LLM] Tool execution or durable result persistence failed: {e}")
            await _emit_round_done_events(round_done_records, state.on_tool_call)
            if state.agent_id and state.unsaved_usage.total_tokens > 0:
                await record_token_usage(state.agent_id, state.unsaved_usage)
            await state.client_guard.close()
            _call_llm_log_turn_timing(state, "tool_result_persist_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"
        if tool_error:
            state.api_messages.append(
                LLMMessage(
                    role="tool",
                    content=tool_error,
                    tool_call_id=tc.get("id", ""),
                )
            )

    try:
        rewrites = await enforce_message_budget(
            state.api_messages,
            fresh_start_idx=fresh_start,
            agent_id=state.agent_id,
            session_id=state.session_id,
        )
    except Exception as e:
        logger.exception(f"[LLM] Fresh tool round exceeds the hard message budget: {e}")
        await _emit_round_done_events(round_done_records, state.on_tool_call)
        if state.agent_id and state.unsaved_usage.total_tokens > 0:
            await record_token_usage(state.agent_id, state.unsaved_usage)
        await state.client_guard.close()
        _call_llm_log_turn_timing(state, "tool_result_budget_error", round_i + 1)
        return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"
    try:
        await _reconcile_round_tool_outputs(
            rewrites,
            round_done_records,
            agent_id=state.anchor_agent_id,
            user_id=state.user_id,
            session_id=state.session_id,
            turn_anchor_id=state.turn_anchor_id,
        )
    except Exception as e:
        logger.exception(f"[LLM] Failed to reconcile durable tool results: {e}")
        await _emit_round_done_events(round_done_records, state.on_tool_call)
        if state.agent_id and state.unsaved_usage.total_tokens > 0:
            await record_token_usage(state.agent_id, state.unsaved_usage)
        await state.client_guard.close()
        _call_llm_log_turn_timing(state, "tool_result_reconcile_error", round_i + 1)
        return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

    await _emit_round_done_events(round_done_records, state.on_tool_call)

    observation = _tool_round_observation(
        sanitized_tool_calls or [],
        [record.provider_content for record in round_done_records],
    )
    if observation:
        state.tool_round_history = [*state.tool_round_history, observation][-6:]
    if _repeating_tool_period(state.tool_round_history) is not None:
        state.api_messages.append(LLMMessage(role="user", content=REPEAT_TOOL_CALL_NUDGE_PROMPT))

    return None


__all__ = [name for name in globals() if name != "__all__" and not name.startswith("__")]
