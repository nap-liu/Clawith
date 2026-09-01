"""Support helpers for the streaming caller loop."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.services.llm.caller_context import *  # noqa: F401,F403
from app.services.llm.caller_shared import *  # noqa: F401,F403
from app.services.llm.caller_tooling import *  # noqa: F401,F403


@dataclass
class CallLlmState:
    model: "LLMModel"
    agent_name: str
    role_description: str
    agent_id: Any = None
    user_id: Any = None
    session_id: str = ""
    on_chunk: Any = None
    on_tool_call: Any = None
    on_tool_delta: Any = None
    on_thinking: Any = None
    on_usage: Any = None
    is_group: bool = False
    on_code_output: Any = None
    current_user_name_override: str | None = None
    channel_context: dict | None = None
    turn_anchor_id: uuid.UUID | None = None
    turn_anchor_agent_id: uuid.UUID | None = None
    turn_type: str | None = None
    context_recovery: Any = None
    before_round: Any = None
    before_tool_execution: Any = None
    include_soul: bool = True
    include_memory: bool = True
    supports_vision: bool = False
    max_tool_rounds: int = 50
    static_prompt: str = ""
    dynamic_prompt: str | None = None
    tools_for_llm: list[dict] = field(default_factory=list)
    allowed_tool_names: set[str] = field(default_factory=set)
    client_guard: LLMClientCloseGuard | None = None
    max_tokens: int = 0
    unsaved_usage: TokenUsage = field(default_factory=TokenUsage)
    last_authoritative_prompt_tokens: int | None = None
    anchor_agent_id: uuid.UUID | None = None
    agent_uuid: uuid.UUID | None = None
    durable_user_id: uuid.UUID | None = None
    turn_t0: float = 0.0
    max_output_recoveries: int = 0
    repeat_streaks: dict[tuple[str, str], int] = field(default_factory=dict)
    visible_response_segments: list[str] = field(default_factory=list)
    turn_execution_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    api_messages: list[LLMMessage] = field(default_factory=list)
    preflight_compaction_not_applicable: bool = False


async def _call_llm_assert_durable_turn_running(state: CallLlmState) -> None:
    durable_agent_id = state.turn_anchor_agent_id or state.agent_uuid
    if durable_agent_id is None or not state.session_id or state.turn_anchor_id is None:
        return
    from app.services.conversation_turn_lifecycle import assert_conversation_turn_running

    await assert_conversation_turn_running(
        agent_id=durable_agent_id,
        conversation_id=str(state.session_id),
        turn_anchor_id=state.turn_anchor_id,
    )


async def _call_llm_before_tool_execution_guard(state: CallLlmState) -> None:
    await _call_llm_assert_durable_turn_running(state)
    if state.before_tool_execution is not None:
        await state.before_tool_execution()


async def _call_llm_admit_tool_execution(state: CallLlmState) -> None:
    durable_agent_id = state.turn_anchor_agent_id or state.agent_uuid
    if durable_agent_id is not None and state.session_id and state.turn_anchor_id is not None:
        from app.services.conversation_turn_lifecycle import admit_conversation_turn_side_effect

        await admit_conversation_turn_side_effect(
            agent_id=durable_agent_id,
            conversation_id=str(state.session_id),
            turn_anchor_id=state.turn_anchor_id,
        )
    if state.before_tool_execution is not None:
        await state.before_tool_execution()


async def _call_llm_resolve_execution_identity_and_active_turn(state: CallLlmState) -> None:
    state.agent_uuid = _coerce_uuid(state.agent_id)
    viewer_uuid = _coerce_uuid(state.user_id)

    if state.agent_uuid is not None and viewer_uuid == state.agent_uuid:
        from app.models.agent import Agent as AgentModel

        async with async_session() as identity_db:
            creator_id = await identity_db.scalar(
                select(AgentModel.creator_id).where(AgentModel.id == state.agent_uuid)
            )
        if creator_id is not None:
            state.user_id = creator_id

    if state.agent_id and state.user_id and state.session_id:
        try:
            owner_uuid = uuid.UUID(str(state.user_id))
            agent_uuid = uuid.UUID(str(state.agent_id))
        except (TypeError, ValueError):
            pass
        else:
            await ensure_active_turn(
                owner_user_id=owner_uuid,
                agent_id=agent_uuid,
                session_id=str(state.session_id),
                turn_type=state.turn_type,
                turn_anchor_id=state.turn_anchor_id,
                turn_anchor_agent_id=state.turn_anchor_agent_id,
            )
            state.agent_uuid = agent_uuid

    state.durable_user_id = _coerce_uuid(state.user_id)


async def _call_llm_load_round_limit(state: CallLlmState, max_tool_rounds_override: int | None) -> str | None:
    state.supports_vision = bool(getattr(state.model, "supports_vision", False))
    state.max_tool_rounds, token_limit_msg = await _get_agent_config(state.agent_id)
    if token_limit_msg:
        return token_limit_msg
    if max_tool_rounds_override is not None:
        state.max_tool_rounds = max(1, min(200, int(max_tool_rounds_override)))
    return None


def _call_llm_default_on_tool_call(state: CallLlmState):
    if state.on_tool_call is not None or not state.session_id:
        return state.on_tool_call
    from app.services.chat_history import persist_tool_call

    async def _default_on_tool_call(data: dict):
        if data.get("status") in {"running", "done"} and (state.anchor_agent_id or state.agent_id):
            await persist_tool_call(
                async_session,
                agent_id=state.anchor_agent_id or state.agent_id,
                user_id=state.user_id,
                conversation_id=state.session_id,
                evt=data,
                turn_anchor_id=state.turn_anchor_id,
            )

    return _default_on_tool_call


async def _call_llm_prepare_turn_context(
    state: CallLlmState,
    prepared_turn_context: tuple[str, str] | None,
) -> None:
    if prepared_turn_context is None:
        state.static_prompt, state.dynamic_prompt = await _build_turn_context(
            agent_id=state.agent_id,
            agent_name=state.agent_name,
            role_description=state.role_description,
            user_id=state.user_id,
            current_user_name_override=state.current_user_name_override,
            is_group=state.is_group,
            session_id=state.session_id,
            channel_context=state.channel_context,
            include_soul=state.include_soul,
            include_memory=state.include_memory,
        )
    else:
        state.static_prompt, state.dynamic_prompt = prepared_turn_context


async def _call_llm_prepare_tools(
    state: CallLlmState,
    prepared_tools: list[dict] | None,
    skip_tools: bool,
) -> None:
    if prepared_tools is not None:
        state.tools_for_llm = list(prepared_tools)
    elif skip_tools:
        state.tools_for_llm = []
    else:
        from app.services.agent_tools import AGENT_TOOLS

        state.tools_for_llm = await get_agent_tools_for_llm(state.agent_id) if state.agent_id else AGENT_TOOLS
    if state.tools_for_llm:
        state.tools_for_llm = sorted(
            state.tools_for_llm,
            key=lambda t: t.get("function", {}).get("name", ""),
        )
    state.allowed_tool_names = _allowed_tool_names(state.tools_for_llm)


async def _assemble_api_messages_for_call_llm(
    source_messages: list[dict],
    *,
    agent_id,
    supports_vision: bool,
    static_prompt: str,
    dynamic_prompt: str | None,
) -> list[LLMMessage]:
    from app.services.image_context import prepare_messages_for_model

    prepared_messages = await prepare_messages_for_model(
        source_messages,
        agent_id=agent_id,
        supports_vision=supports_vision,
    )
    assembled = [LLMMessage(role="system", content=static_prompt)]
    for msg in prepared_messages:
        assembled.append(
            LLMMessage(
                role=msg.get("role", "user"),
                content=msg.get("content"),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
            )
        )
    assembled = _convert_messages_for_vision(assembled, supports_vision)
    return _attach_turn_context(assembled, dynamic_prompt)


async def _call_llm_prepare_client_and_messages(
    state: CallLlmState,
    messages: list[dict],
) -> tuple[Any | None, str | None]:
    state.api_messages = await _assemble_api_messages_for_call_llm(
        messages,
        agent_id=state.agent_id,
        supports_vision=state.supports_vision,
        static_prompt=state.static_prompt,
        dynamic_prompt=state.dynamic_prompt,
    )

    try:
        client = create_llm_client(
            provider=state.model.provider,
            api_key=get_model_api_key(state.model),
            model=state.model.model,
            base_url=state.model.base_url,
            timeout=_get_model_timeout(state.model),
            provider_managed_timeout=True,
        )
        state.client_guard = LLMClientCloseGuard(client)
    except Exception as e:
        return None, f"[Error] Failed to create LLM client: {e}"

    state.max_tokens = get_max_tokens(
        state.model.provider,
        state.model.model,
        getattr(state.model, "max_output_tokens", None),
    )
    state.anchor_agent_id = _coerce_uuid(state.turn_anchor_agent_id) or _coerce_uuid(state.agent_id)
    state.turn_t0 = perf_counter()

    if state.anchor_agent_id is not None and state.session_id:
        from app.services.session_token_usage import load_latest_round_context_usage

        try:
            state.last_authoritative_prompt_tokens = await load_latest_round_context_usage(
                agent_id=state.anchor_agent_id,
                session_id=state.session_id,
                provider=str(getattr(state.model, "provider", "") or ""),
                model=str(getattr(state.model, "model", "") or ""),
                model_record_id=str(getattr(state.model, "id", "") or ""),
                endpoint=str(getattr(state.model, "base_url", "") or ""),
            )
        except Exception as exc:
            logger.error(f"[context_usage] latest usage load failed session={state.session_id}: {exc}")

    return client, None


async def _call_llm_track_response_usage(
    state: CallLlmState,
    response,
    round_number: int,
) -> None:
    usage = _usage_from_response(response)
    state.unsaved_usage.add(usage)
    if state.on_usage is not None:
        try:
            await state.on_usage(usage)
        except Exception as exc:
            logger.warning(f"[LLM] usage callback failed (ignored): {exc}")

    raw_usage = getattr(response, "usage", None)
    authoritative = extract_token_usage(raw_usage)
    if authoritative is not None and authoritative.context_input_tokens > 0:
        state.last_authoritative_prompt_tokens = authoritative.context_input_tokens
        if state.anchor_agent_id is not None and state.turn_anchor_id is not None and state.session_id:
            from app.services.session_token_usage import persist_round_context_usage

            try:
                await persist_round_context_usage(
                    agent_id=state.anchor_agent_id,
                    session_id=state.session_id,
                    turn_anchor_id=state.turn_anchor_id,
                    input_tokens=authoritative.context_input_tokens,
                    output_tokens=authoritative.output_tokens,
                    provider=str(getattr(state.model, "provider", "") or ""),
                    model=str(getattr(state.model, "model", "") or ""),
                    model_record_id=str(getattr(state.model, "id", "") or ""),
                    endpoint=str(getattr(state.model, "base_url", "") or ""),
                    usage_details=_authoritative_usage_details(raw_usage),
                )
            except Exception as exc:
                logger.error(
                    "[context_usage] round usage backfill failed "
                    f"session={state.session_id} round={round_number}: {exc}"
                )
    ratio = _cache_hit_ratio(raw_usage)
    if ratio is not None and isinstance(raw_usage, dict):
        prompt_tokens = raw_usage.get("prompt_tokens")
        line = f"[LLM] Round {round_number} cache-hit-ratio={ratio} prompt={prompt_tokens}"
        if ratio < float(os.environ.get("CLAWITH_CACHE_HIT_WARN_RATIO", "0.7")):
            logger.warning(line + " (LOW — prefix cache may be missing the tool-loop tail)")
        else:
            logger.info(line)


def _call_llm_log_turn_timing(state: CallLlmState, outcome: str, rounds: int) -> None:
    logger.info(
        f"[LLM Timing] turn outcome={outcome} rounds={rounds} "
        f"total={perf_counter() - state.turn_t0:.2f}s agent={state.agent_id} session={state.session_id}"
    )


async def _call_llm_persist_intermediate_segment(
    state: CallLlmState,
    content: str,
    *,
    visible_joiner_before: str,
    max_output_resume_prompt: str | None = None,
    thinking: str | None = None,
    created_at=None,
) -> bool:
    if not (content or "").strip():
        return False
    if state.anchor_agent_id is None or state.durable_user_id is None or state.turn_anchor_id is None or not state.session_id:
        return False
    from app.services.chat_history import persist_intermediate_assistant_reply

    await persist_intermediate_assistant_reply(
        async_session,
        agent_id=state.anchor_agent_id,
        user_id=state.durable_user_id,
        conversation_id=state.session_id,
        content=content,
        thinking=thinking,
        turn_anchor_id=state.turn_anchor_id,
        visible_joiner_before=visible_joiner_before,
        max_output_resume_prompt=max_output_resume_prompt,
        created_at=created_at,
    )
    return True


async def _call_llm_dispatch_round_with_context_recovery(
    state: CallLlmState,
    client,
    current_messages: list[LLMMessage],
    current_budget: DispatchBudget,
    round_number: int,
):
    while True:
        meaningful_progress = False

        async def _chunk(text: str):
            nonlocal meaningful_progress
            meaningful_progress = meaningful_progress or bool(text)
            if state.on_chunk is not None:
                await state.on_chunk(text)

        async def _thinking(text: str):
            nonlocal meaningful_progress
            meaningful_progress = meaningful_progress or bool(text)
            if state.on_thinking is not None:
                await state.on_thinking(text)

        async def _tool_delta(data: dict):
            nonlocal meaningful_progress
            meaningful_progress = meaningful_progress or bool(data)
            if state.on_tool_delta is not None:
                await state.on_tool_delta(data)

        try:
            response = await _stream_with_throttle_retry(
                client,
                model=state.model,
                round_i=round_number,
                messages=current_messages,
                tools=state.tools_for_llm if state.tools_for_llm else None,
                temperature=state.model.temperature,
                max_tokens=state.max_tokens,
                on_chunk=_chunk,
                on_tool_delta=_tool_delta,
                on_thinking=_thinking,
            )
            return response, current_messages, current_budget
        except LLMError as exc:
            if meaningful_progress or not _is_provider_context_overflow(exc) or state.context_recovery is None:
                raise
            overflow_budget = replace(
                current_budget,
                provider_overflow=True,
                token_overflow=True,
                count_source="provider_context_rejection",
            )
            recovered_messages = await state.context_recovery(state.model, overflow_budget)
            if recovered_messages is None:
                raise
            state.api_messages = await _assemble_api_messages_for_call_llm(
                recovered_messages,
                agent_id=state.agent_id,
                supports_vision=state.supports_vision,
                static_prompt=state.static_prompt,
                dynamic_prompt=state.dynamic_prompt,
            )
            current_messages = list(state.api_messages)
            state.last_authoritative_prompt_tokens = None
            current_budget = measure_dispatch(
                model=state.model,
                messages=current_messages,
                tools=state.tools_for_llm if state.tools_for_llm else None,
                max_output_tokens=state.max_tokens,
            )
            logger.warning(
                "[context_guard] provider rejected context; compacted "
                f"and retrying the same round session={state.session_id} "
                f"model={getattr(state.model, 'model', '?')}"
            )


async def _call_llm_apply_preflight_context_recovery(
    state: CallLlmState,
    dispatch_messages: list[LLMMessage],
    dispatch_budget: DispatchBudget,
) -> tuple[list[LLMMessage], DispatchBudget, bool]:
    from app.services.llm.compactor import should_compact

    preflight_compaction_required, _, _ = should_compact(
        model=state.model,
        last_prompt_tokens=state.last_authoritative_prompt_tokens,
        pre_flight_estimate=None,
    )
    if (
        (not dispatch_budget.fits or preflight_compaction_required)
        and state.turn_anchor_id is not None
        and state.context_recovery is not None
    ):
        recovered_messages = await state.context_recovery(state.model, dispatch_budget)
        if recovered_messages is not None:
            state.preflight_compaction_not_applicable = bool(
                getattr(recovered_messages, "preflight_not_applicable", False)
            )
            state.api_messages = await _assemble_api_messages_for_call_llm(
                recovered_messages,
                agent_id=state.agent_id,
                supports_vision=state.supports_vision,
                static_prompt=state.static_prompt,
                dynamic_prompt=state.dynamic_prompt,
            )
            dispatch_messages = list(state.api_messages)
            state.last_authoritative_prompt_tokens = None
            dispatch_budget = measure_dispatch(
                model=state.model,
                messages=dispatch_messages,
                tools=state.tools_for_llm if state.tools_for_llm else None,
                max_output_tokens=state.max_tokens,
            )
    return dispatch_messages, dispatch_budget, preflight_compaction_required


__all__ = [name for name in globals() if name != "__all__" and not name.startswith("__")]
