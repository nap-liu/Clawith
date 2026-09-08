"""High-level agent LLM entry points."""

from app.services.llm.caller_context import *  # noqa: F401,F403
from app.services.llm.caller_failover import call_llm_with_failover
from app.services.llm.caller_shared import *  # noqa: F401,F403
from app.services.llm.caller_tooling import *  # noqa: F401,F403
from app.services.agent_execution.runtime import isolate_agent_execution


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


@isolate_agent_execution
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
    """Call agent LLM with tool-calling loop (for background services)."""
    from app.models.agent import Agent
    from app.services.chat_model_selection import (
        BackgroundModelUnavailableError,
        MODEL_OVERRIDE_NONE,
        MODEL_OVERRIDE_OK,
        resolve_runtime_models,
    )

    # Load agent and models
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 未找到数字员工"
    from app.core.okr_feature import is_retired_okr_agent

    if await is_retired_okr_agent(db, agent):
        return "⚠️ 未找到数字员工"

    if execution_user_id is None:
        # Rolling-upgrade compatibility for legacy background call sites.
        execution_user_id = agent.creator_id
    else:
        from app.services.execution_identity import resolve_execution_user_id

        execution_user_id = await resolve_execution_user_id(
            db,
            agent,
            execution_user_id,
        )

    resolved_models = await resolve_runtime_models(
        db,
        agent=agent,
        override_model_id=model_override_id,
        override_temperature=temperature_override,
        override_reasoning_effort=reasoning_effort_override,
    )
    if model_override_id and resolved_models.override_status not in {
        MODEL_OVERRIDE_NONE,
        MODEL_OVERRIDE_OK,
    }:
        raise BackgroundModelUnavailableError("后台任务指定的模型不可用")
    primary_model = resolved_models.primary_model
    fallback_model = resolved_models.fallback_model

    if not primary_model:
        raise BackgroundModelUnavailableError(f"{agent.name} 未配置可用的 LLM 模型")

    await ensure_active_turn(
        owner_user_id=execution_user_id,
        agent_id=agent_id,
        session_id=session_id or f"{turn_type}:{uuid.uuid4()}",
        turn_type=turn_type,
        title=user_prompt.strip()[:40] or None,
    )

    if _same_model_record(primary_model, fallback_model):
        logger.info("[call_agent_llm_with_tools] Primary and fallback reference the same model id; skipping fallback")
        fallback_model = None

    # Build messages
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    allowed_tool_names = _allowed_tool_names(tools_for_llm)
    # ``session_id`` is stable for schedulers and heartbeats. A fresh execution
    # id prevents separate runs at the same round number from being grouped as
    # one historical multi-tool response.
    background_execution_id = uuid.uuid4()

    # Everything needed by the provider/tool loop is now an immutable snapshot.
    # End the read transaction before waiting for provider capacity or remote
    # model I/O so hundreds of queued turns do not pin one database connection
    # each. ``expire_on_commit=False`` keeps the loaded agent/model values usable.
    await db.commit()

    async def _try_model(model, *, allow_provider_retries: bool = True) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        _unsaved_usage = TokenUsage()
        tool_executed = False
        visible_response_segments: list[str] = []
        client_guard: LLMClientCloseGuard | None = None
        api_messages = list(messages)
        current_round = 1
        try:
            client = create_llm_client(
                provider=model.provider,
                api_key=get_model_api_key(model),
                model=model.model,
                base_url=model.base_url,
                timeout=_get_model_timeout(model),
                provider_managed_timeout=True,
            )
            client_guard = LLMClientCloseGuard(client)

            max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))

            # Tool-calling loop
            _tool_round_history: list[tuple[tuple[tuple[str, str], str], ...]] = []
            _invalid_tool_call_retries = 0
            for round_i in range(max_rounds):
                current_round = round_i + 1
                # Check token usage limit mid-loop (every 3 rounds)
                if round_i > 0 and round_i % 3 == 0:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                        _unsaved_usage = TokenUsage()
                        _, _token_limit_msg = await _get_agent_config(agent_id)
                        if _token_limit_msg:
                            logger.warning(
                                f"[call_agent_llm_with_tools] Token limit exceeded mid-loop: {_token_limit_msg}"
                            )
                            await client_guard.close()
                            return _token_limit_msg, False, tool_executed

                context_stop = await _guard_provider_dispatch(
                    model=model,
                    messages=api_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    max_output_tokens=max_tokens,
                    session_id=session_id,
                )
                if context_stop:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client_guard.close()
                    return context_stop, False, tool_executed

                try:
                    response = await _complete_with_throttle_retry(
                        client,
                        model=model,
                        round_i=round_i + 1,
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        reasoning_effort=getattr(model, "reasoning_effort", None),
                        max_tokens=max_tokens,
                        on_status=on_status,
                        allow_retries=allow_provider_retries,
                    )
                except Exception as e:
                    logger.error(f"[call_agent_llm_with_tools] Agent {agent_id}: LLM call error: {e}")
                    await client_guard.close()
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    raise

                # Track tokens for this round
                _usage_this_round = _usage_from_response(response)
                _accumulated_usage.add(_usage_this_round)
                _unsaved_usage.add(_usage_this_round)

                if response.content and response.content.strip():
                    visible_response_segments.append(response.content)

                # Plain assistant text (no tool calls) ends the turn — it IS the reply.
                if not response.tool_calls:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client_guard.close()
                    return (
                        response.content or "[Empty response]",
                        True,
                        tool_executed,
                    )

                # Execute tool calls
                # Sanitize first — invalid tool args become a retry user message
                # (mirrors the streaming path's behavior in the main _try_model).
                sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction:
                    if _invalid_tool_call_retries >= 1:
                        if agent_id and _unsaved_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _unsaved_usage)
                        await client_guard.close()
                        return (
                            make_llm_failure(
                                code="invalid_tool_call_stream",
                                message_key="errors.invalidToolCallStream",
                                details={"round": round_i + 1},
                            ),
                            False,
                            tool_executed,
                        )
                    _invalid_tool_call_retries += 1
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue
                _invalid_tool_call_retries = 0

                # request_confirmation handling (twin of the main call_llm loop):
                # valid → SUSPEND the turn on this tool_call (pending row + card; resumed on
                # the user's click); invalid / tool not enabled → surface error and loop.
                conf_call = find_request_confirmation_call(sanitized_tool_calls)
                if conf_call is not None:
                    _conf_action_tool = (conf_call.action or {}).get("tool") if conf_call.action else None
                    if conf_call.valid and (_conf_action_tool is None or _conf_action_tool in allowed_tool_names):
                        from app.services import confirmation_service  # lazy import — avoid circular

                        await confirmation_service.suspend_for_confirmation(
                            agent_id=agent_id,
                            conversation_id=session_id,
                            chat_session_id=None,
                            source_channel="web",
                            # Bind background confirmation to the configured executor.
                            user_id=execution_user_id,
                            intro_text=_latest_visible_response_segment(*visible_response_segments),
                            title=conf_call.title,
                            summary=conf_call.summary,
                            action=conf_call.action,
                            risk_level=conf_call.risk_level,
                            buttons=conf_call.buttons,
                            force_confirmation=conf_call.force_confirmation,
                            turn_anchor_id=None,
                            assistant_content=response.content or None,
                            recovery_prefix_messages=[],
                            reasoning_content=response.reasoning_content,
                            round_id=(
                                f"{session_id}:background:{background_execution_id}:round:{round_i + 1}"
                            ),
                        )
                        if agent_id and _unsaved_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _unsaved_usage)
                        await client_guard.close()
                        # Suspended: intro text + card already persisted/delivered. Return ""
                        # so the channel handler doesn't re-persist a duplicate reply.
                        return "", True, True
                    else:
                        if not conf_call.valid:
                            _conf_reason = conf_call.error or "request_confirmation 参数无效"
                        else:
                            _conf_reason = f"工具 {_conf_action_tool} 未对该 agent 启用,无法挟带"
                        # A role="tool" reply must be preceded by the assistant
                        # message carrying its tool_calls, or strict providers
                        # reject the orphaned tool message.
                        fresh_start = len(api_messages)
                        api_messages.append(
                            LLMMessage(
                                role="assistant",
                                content=response.content or None,
                                tool_calls=sanitized_tool_calls,
                                reasoning_content=response.reasoning_content,
                            )
                        )
                        api_messages.append(
                            LLMMessage(
                                role="tool",
                                content=f"❌ {_conf_reason}",
                                tool_call_id=conf_call.call_id,
                            )
                        )
                        await enforce_message_budget(
                            api_messages,
                            fresh_start_idx=fresh_start,
                            agent_id=agent_id,
                            session_id=session_id,
                        )
                        continue

                repeat_period = _repeating_tool_period(_tool_round_history, sanitized_tool_calls)
                if repeat_period is not None:
                    logger.warning(
                        f"[call_agent_llm_with_tools] Repeated tool-call guard tripped "
                        f"(period={repeat_period}, round {round_i + 1}, agent={agent_id})."
                    )
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client_guard.close()
                    return (
                        make_llm_failure(
                            code=f"tool_loop_period_{repeat_period}",
                            message_key="errors.toolLoopStopped",
                            details={"period": repeat_period, "round": round_i + 1},
                        ),
                        True,
                        tool_executed,
                    )

                # Only this newly appended round may be materialized. Earlier
                # rounds were already dispatched and are an immutable cache
                # prefix.
                fresh_start = len(api_messages)

                # Add assistant message with tool calls.
                # NB: tc["function"] is shared by reference with _canonicalize_tc_arguments's
                # in-place canonicalization — must stay a reference (no deepcopy), or
                # history entries will carry the pre-repair malformed arguments.
                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )

                round_results: list[Any] = []
                for tc in sanitized_tool_calls or []:
                    args = _canonicalize_tc_arguments(tc, session_id)
                    tool_name = tc["function"]["name"]

                    tool_executed = True
                    if tool_name not in allowed_tool_names:
                        logger.warning(
                            f"[call_agent_llm_with_tools] Blocked disabled tool call: {tool_name} agent_id={agent_id}"
                        )
                        result = _tool_not_enabled_message(tool_name)
                    else:
                        _tool_t0 = perf_counter()
                        result = await execute_tool(
                            tool_name,
                            args,
                            agent_id=agent_id,
                            user_id=execution_user_id,
                            session_id=session_id,
                            tool_call_id=str(tc.get("id") or ""),
                            tools_for_llm=tools_for_llm,
                        )
                        logger.info(
                            f"[LLM Timing] tool={tool_name} exec={perf_counter() - _tool_t0:.2f}s agent={agent_id}"
                        )
                    llm_view = await finalize_tool_output(
                        result,
                        tool_name=tool_name,
                        agent_id=agent_id,
                        session_id=session_id,
                        tool_call_id=str(tc.get("id") or ""),
                    )
                    round_results.append(llm_view)
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tc["id"],
                            content=llm_view,
                        )
                    )

                rewrites = await enforce_message_budget(
                    api_messages,
                    fresh_start_idx=fresh_start,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                rewritten_results = {
                    rewrite.tool_call_id: rewrite.final_content for rewrite in rewrites
                }
                round_results = [
                    rewritten_results.get(str(tc.get("id") or ""), result)
                    for tc, result in zip(
                        sanitized_tool_calls or [],
                        round_results,
                        strict=True,
                    )
                ]

                observation = _tool_round_observation(sanitized_tool_calls or [], round_results)
                if observation:
                    _tool_round_history = [*_tool_round_history, observation][-6:]
                if _repeating_tool_period(_tool_round_history) is not None:
                    api_messages.append(LLMMessage(role="user", content=REPEAT_TOOL_CALL_NUDGE_PROMPT))

            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            return (
                make_llm_failure(
                    code="tool_round_limit",
                    message_key="errors.toolRoundLimit",
                    details={"round": max_rounds},
                ),
                False,
                tool_executed,
            )

        except Exception as e:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            if client_guard is not None:
                await client_guard.close()
            if _as_model_response_idle_timeout(e) is not None:
                return model_response_idle_timeout_failure(), False, tool_executed
            if isinstance(e, LLMError):
                return (
                    _provider_failure_outcome(
                        model=model,
                        round_number=current_round,
                        error=e,
                        messages=api_messages,
                        had_tool_side_effect=tool_executed,
                    ),
                    False,
                    tool_executed,
                )
            return (
                make_llm_failure(
                    code="model_turn_failed",
                    message_key="errors.modelTurnFailed",
                    details=_llm_failure_details(
                        model=model,
                        round_number=current_round,
                        error=e,
                        messages=api_messages,
                        had_tool_side_effect=tool_executed,
                    ),
                ),
                False,
                tool_executed,
            )

    # Try primary model
    reply, success, primary_tool_executed = await _try_model(primary_model)
    if success:
        return reply
    if getattr(reply, "allow_failover", True) is False:
        return reply

    # Primary failed - check if retryable
    error_type = classify_error(Exception(reply))
    if error_type == FailoverErrorType.NON_RETRYABLE or not fallback_model:
        return reply

    if primary_tool_executed:
        logger.warning("[call_agent_llm_with_tools] Blocked fallback: side-effecting tool already executed")
        return reply

    # Try fallback model
    logger.info(f"[call_agent_llm_with_tools] Retrying with fallback: {fallback_model.model}")
    reply2, success2, _fallback_tool_executed = await _try_model(
        fallback_model,
        allow_provider_retries=False,
    )
    if success2:
        return reply2

    if reply == PROVIDER_CONTEXT_BLOCKED_MESSAGE and reply2 == PROVIDER_CONTEXT_BLOCKED_MESSAGE:
        return PROVIDER_CONTEXT_BLOCKED_MESSAGE

    if isinstance(reply2, LLMFailure):
        return _combined_model_failure(reply, reply2)
    return f"⚠️ Both models failed | Primary: {reply[:80]} | Fallback: {reply2[:80]}"
