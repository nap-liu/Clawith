"""Failover wrapper for caller entry points."""

from app.services.llm.caller_context import *  # noqa: F401,F403
from app.services.llm.caller_shared import *  # noqa: F401,F403
from app.services.llm.caller_streaming import call_llm
from app.services.llm.caller_tooling import *  # noqa: F401,F403
from app.services.turn_tool_settings import with_scene_tool_settings
from app.services.agent_execution.runtime import isolate_agent_execution


@serialize_conversation_execution
@isolate_agent_execution
@with_scene_tool_settings
async def call_llm_with_failover(
    primary_model,
    fallback_model,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    on_status=None,
    on_usage=None,
    on_tool_call=None,
    on_tool_delta=None,
    on_failover=None,
    skip_tools: bool = False,
    is_group: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    channel_context: dict | None = None,
    turn_anchor_id: uuid.UUID | None = None,
    turn_anchor_agent_id: uuid.UUID | None = None,
    turn_type: str | None = None,
    context_recovery=None,
    prepared_tools: list[dict] | None = None,
    before_round=None,
    before_tool_execution=None,
    include_soul: bool = True,
    include_memory: bool = True,
    max_tool_rounds_override: int | None = None,
) -> str:
    """Call LLM with automatic failover support."""
    guard = FailoverGuard()
    include_soul = include_soul and (channel_context or {}).get("scene_include_soul", True)
    include_memory = include_memory and (channel_context or {}).get("scene_include_memory", True)

    # Config-level fallback: if no primary, use fallback directly
    if primary_model is None and fallback_model is not None:
        logger.info("[Failover] Primary model not configured, using fallback directly")
        primary_model = fallback_model
        fallback_model = None

    if primary_model is None:
        return "⚠️ 未配置 LLM 模型"

    if _same_model_record(primary_model, fallback_model):
        logger.info("[Failover] Primary and fallback reference the same model id; skipping fallback")
        fallback_model = None

    turn_messages = list(messages)
    injected_turn_messages: list[dict] = []
    from app.services.llm.turn_partition import effective_keep_recent_turns

    initial_provider_overflow_keep = effective_keep_recent_turns(
        primary_model,
        fallback_model,
    )

    def _recovery_message_key(message: dict) -> str:
        """Canonical equality for one live-vs-durable injected message.

        Durable history includes ``attachments=[]`` when the row metadata owns
        that key; live IM/subagent injection omits it when there are no files.
        Normalize only that representational difference. A counted comparison
        below preserves two legitimate identical messages instead of treating
        membership as a set.
        """
        normalized = dict(message)
        if normalized.get("attachments") == []:
            normalized.pop("attachments", None)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _surviving_baseline_suffix_keys(recovered: list[dict]) -> list[str]:
        """Return the longest contiguous baseline suffix present in recovery.

        Compaction can replace an arbitrary old prefix, but the uncompressed
        current-turn baseline survives as a contiguous suffix.  Reserving only
        that observed suffix prevents an older identical message from being
        mistaken for a newly injected message that is absent from a racing
        recovery read.
        """
        baseline_keys = [_recovery_message_key(message) for message in messages]
        recovered_keys = [_recovery_message_key(message) for message in recovered]
        if not baseline_keys or not recovered_keys:
            return []

        longest = 0
        last_baseline_key = baseline_keys[-1]
        for recovered_end, key in enumerate(recovered_keys):
            if key != last_baseline_key:
                continue
            matched = 1
            max_match = min(len(baseline_keys), recovered_end + 1)
            while (
                matched < max_match
                and baseline_keys[-1 - matched] == recovered_keys[recovered_end - matched]
            ):
                matched += 1
            longest = max(longest, matched)
            if longest == len(baseline_keys):
                break
        return baseline_keys[-longest:] if longest else []

    next_provider_overflow_keep = initial_provider_overflow_keep
    provider_overflow_exhausted = False

    async def _recover_once(model, budget):
        nonlocal turn_messages, next_provider_overflow_keep, provider_overflow_exhausted
        if context_recovery is None:
            return None

        provider_overflow = bool(getattr(budget, "provider_overflow", False))
        if provider_overflow and provider_overflow_exhausted:
            return None
        recovered = None
        while True:
            attempt_budget = budget
            attempted_keep: int | None = None
            if provider_overflow:
                attempted_keep = max(0, next_provider_overflow_keep)
                attempt_budget = replace(
                    budget,
                    keep_recent_turns_override=attempted_keep,
                )
            try:
                recovered = await context_recovery(model, attempt_budget)
            except Exception as exc:
                logger.error(
                    "[context_guard] safe context recovery failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                return None

            if provider_overflow:
                # A provider rejection permits one explicit protection level.
                # If that level has no expired turn, step down locally until
                # useful work is possible. A second provider rejection resumes
                # at N-1, eventually reaching current-turn-only (zero history).
                if attempted_keep == 0:
                    provider_overflow_exhausted = True
                else:
                    next_provider_overflow_keep = attempted_keep - 1
            if recovered is not None or not provider_overflow or attempted_keep == 0:
                break
        if recovered is not None:
            preflight_not_applicable = bool(
                getattr(recovered, "preflight_not_applicable", False)
            )
            # Recovery normally reloads the complete durable current-turn tail,
            # including already-delivered round-boundary injections. Preserve
            # any injection that is not present in that snapshot as well: a
            # read immediately following the standard ordered commits must not
            # drop a trailing user message that this logical turn consumed.
            # Canonical occurrence counts keep duplicate texts and the normal
            # durable path both correct.
            turn_messages = list(recovered)
            recovered_counts: dict[str, int] = {}
            for message in turn_messages:
                key = _recovery_message_key(message)
                recovered_counts[key] = recovered_counts.get(key, 0) + 1
            # Only recovered occurrences beyond the actually surviving
            # pre-injection suffix can satisfy live injections. This remains
            # correct when an older protected message has the same text as a
            # trailing injection and when compaction has replaced older rows.
            for baseline_key in _surviving_baseline_suffix_keys(turn_messages):
                recovered_counts[baseline_key] = max(
                    0,
                    recovered_counts.get(baseline_key, 0) - 1,
                )
            for injected in injected_turn_messages:
                key = _recovery_message_key(injected)
                if recovered_counts.get(key, 0) > 0:
                    recovered_counts[key] -= 1
                    continue
                turn_messages.append(injected)
            if preflight_not_applicable:
                from app.services.llm.compactor import ContextRecoveryMessages

                return ContextRecoveryMessages(
                    turn_messages,
                    preflight_not_applicable=True,
                )
            return turn_messages
        return None

    # Freeze one context snapshot for the whole logical turn.  Runtime
    # failover is a provider retry, not a new Agent turn, so both models must
    # see the same memory, clock, trigger and relationship state.
    prepared_turn_context = await _build_turn_context(
        agent_id=agent_id,
        agent_name=agent_name,
        role_description=role_description,
        user_id=user_id,
        current_user_name_override=current_user_name_override,
        is_group=is_group,
        session_id=session_id,
        channel_context=channel_context,
        include_soul=include_soul,
        include_memory=include_memory,
    )
    if prepared_tools is not None:
        prepared_tools = sorted(
            prepared_tools,
            key=lambda tool: tool.get("function", {}).get("name", ""),
        )
    elif skip_tools:
        prepared_tools = []
    else:
        from app.services.agent_tools import AGENT_TOOLS

        prepared_tools = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
        prepared_tools = sorted(
            prepared_tools or [],
            key=lambda tool: tool.get("function", {}).get("name", ""),
        )

    async def _wrapped_before_round(round_i: int, *, before_injection=None):
        if before_round is None:
            return []
        injected = await _invoke_before_round(
            before_round,
            round_i,
            before_injection=before_injection,
        )
        if injected:
            normalized = [dict(message) for message in injected]
            injected_turn_messages.extend(normalized)
            turn_messages.extend(normalized)
        return injected

    # Wrapper callbacks to track state for guard checks
    async def _wrapped_on_chunk(text: str):
        guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _wrapped_on_thinking(text: str):
        guard.mark_streaming_started()
        if on_thinking:
            await on_thinking(text)

    async def _wrapped_on_tool_delta(data: dict):
        guard.mark_streaming_started()
        if on_tool_delta:
            await on_tool_delta(data)

    async def _wrapped_on_tool_call(data: dict):
        if data.get("status") in {"running", "done"}:
            guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    # Try primary model
    primary_result = await call_llm(
        primary_model,
        turn_messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_wrapped_on_chunk,
        on_tool_call=_wrapped_on_tool_call,
        on_tool_delta=_wrapped_on_tool_delta,
        on_thinking=_wrapped_on_thinking,
        on_status=on_status,
        on_usage=on_usage,
        skip_tools=skip_tools,
        is_group=is_group,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        channel_context=channel_context,
        turn_anchor_id=turn_anchor_id,
        turn_anchor_agent_id=turn_anchor_agent_id,
        turn_type=turn_type,
        prepared_turn_context=prepared_turn_context,
        prepared_tools=prepared_tools,
        context_recovery=_recover_once,
        before_round=_wrapped_before_round,
        before_tool_execution=before_tool_execution,
        max_tool_rounds_override=max_tool_rounds_override,
    )

    if getattr(primary_result, "allow_failover", True) is False:
        return primary_result

    # Check if we need to failover
    if not is_retryable_error(primary_result):
        # A non-error result is just a normal reply — no failover needed, and
        # nothing to warn about. Only a genuine error string that classifies as
        # non-retryable is worth surfacing.
        if is_error_result(primary_result):
            logger.warning(f"[Failover] Skipped: primary model returned a non-retryable error: {primary_result[:150]}")
        return primary_result

    # Check guard conditions
    if not guard.can_failover():
        if guard.tool_executed:
            logger.warning("[Failover] Blocked: side-effecting tool already executed")
        elif guard.streaming_started:
            logger.warning("[Failover] Blocked: streaming already started")
        elif guard.failover_done:
            logger.warning("[Failover] Blocked: failover already done once")
        return primary_result

    # No fallback available
    if fallback_model is None:
        logger.warning("[Failover] No fallback model available")
        return primary_result

    # Runtime failover: retry with fallback model
    logger.info(f"[Failover] Retrying with fallback model: {fallback_model.provider}/{fallback_model.model}")

    if on_failover:
        try:
            await on_failover(f"Switched to fallback model: {fallback_model.model}")
        except Exception:
            pass

    guard.mark_failover_done()

    # Call fallback with fresh callbacks
    fallback_guard = FailoverGuard()
    fallback_guard.mark_failover_done()

    async def _fallback_on_chunk(text: str):
        fallback_guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _fallback_on_thinking(text: str):
        fallback_guard.mark_streaming_started()
        if on_thinking:
            await on_thinking(text)

    async def _fallback_on_tool_delta(data: dict):
        fallback_guard.mark_streaming_started()
        if on_tool_delta:
            await on_tool_delta(data)

    async def _fallback_on_tool_call(data: dict):
        if data.get("status") in {"running", "done"}:
            fallback_guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    fallback_result = await call_llm(
        fallback_model,
        turn_messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_fallback_on_chunk,
        on_tool_call=_fallback_on_tool_call,
        on_tool_delta=_fallback_on_tool_delta,
        on_thinking=_fallback_on_thinking,
        on_status=on_status,
        on_usage=on_usage,
        skip_tools=skip_tools,
        is_group=is_group,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        channel_context=channel_context,
        turn_anchor_id=turn_anchor_id,
        turn_anchor_agent_id=turn_anchor_agent_id,
        turn_type=turn_type,
        prepared_turn_context=prepared_turn_context,
        prepared_tools=prepared_tools,
        # A normal primary provider request has already occurred. Even when it
        # failed before yielding output, fallback is a retry and may not mutate
        # persisted history or trigger compaction after that first dispatch.
        context_recovery=None,
        before_round=_wrapped_before_round,
        before_tool_execution=before_tool_execution,
        max_tool_rounds_override=max_tool_rounds_override,
        provider_retries_enabled=False,
    )

    if primary_result == PROVIDER_CONTEXT_BLOCKED_MESSAGE and fallback_result == PROVIDER_CONTEXT_BLOCKED_MESSAGE:
        return PROVIDER_CONTEXT_BLOCKED_MESSAGE

    # Combine error messages if fallback also failed
    if is_retryable_error(fallback_result) or fallback_result.startswith("⚠️") or fallback_result.startswith("[Error]"):
        if isinstance(fallback_result, LLMFailure):
            return _combined_model_failure(primary_result, fallback_result)
        return f"⚠️ 调用模型出错: Primary: {primary_result[:80]} | Fallback: {fallback_result[:80]}"

    return fallback_result
