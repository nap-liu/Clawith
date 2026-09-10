"""Caller context and tool-processing helpers."""

from dataclasses import replace

from app.services.llm.caller_shared import *  # noqa: F401,F403
from app.services.llm.caller_tooling import *  # noqa: F401,F403


async def _get_agent_config(agent_id) -> tuple[int, str | None]:
    """Get agent config: max_tool_rounds and token limit status."""
    if not agent_id:
        return 50, None

    try:
        from app.models.agent import Agent as AgentModel

        async with async_session() as _db:
            _ar = await _db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            _agent = _ar.scalar_one_or_none()
            if _agent:
                max_rounds = _agent.max_tool_rounds or 50
                if _agent.max_tokens_per_day and _agent.tokens_used_today >= _agent.max_tokens_per_day:
                    return (
                        max_rounds,
                        f"⚠️ Daily token usage has reached the limit ({_agent.tokens_used_today:,}/{_agent.max_tokens_per_day:,}). Please try again tomorrow or ask admin to increase the limit.",
                    )
                if _agent.max_tokens_per_month and _agent.tokens_used_month >= _agent.max_tokens_per_month:
                    return (
                        max_rounds,
                        f"⚠️ Monthly token usage has reached the limit ({_agent.tokens_used_month:,}/{_agent.max_tokens_per_month:,}). Please ask admin to increase the limit.",
                    )
                return max_rounds, None
    except Exception:
        pass
    return 50, None


async def _get_user_name(user_id) -> str | None:
    """Get user's display name for personalized context."""
    if not user_id:
        return None
    try:
        from app.models.agent import Agent as _AgentModel
        from app.models.user import User as _UserModel

        async with async_session() as _udb:
            _ur = await _udb.execute(select(_UserModel).where(_UserModel.id == user_id))
            _u = _ur.scalar_one_or_none()
            if _u:
                return _u.display_name or _u.username
            # Check Agent name fallback (A2A: the "user" may be a peer agent)
            _ar = await _udb.execute(select(_AgentModel).where(_AgentModel.id == user_id))
            _a = _ar.scalar_one_or_none()
            if _a:
                return _a.name
    except Exception:
        pass
    return None


def _convert_messages_for_vision(api_messages: list, supports_vision: bool) -> list:
    """Convert image markers to vision format if supported, or strip them."""
    import copy
    import re as _re_v

    # Deep copy to avoid modifying the original list in place
    new_messages = copy.deepcopy(api_messages)

    if supports_vision:
        # Vision format: convert image markers in strings to OpenAI Vision API list format
        for i, msg in enumerate(new_messages):
            if msg.role != "user" or not msg.content or not isinstance(msg.content, str):
                continue

            content_str = msg.content
            pattern = r"\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]"
            images = _re_v.findall(pattern, content_str)

            if not images:
                continue

            text = _re_v.sub(pattern, "", content_str).strip()
            parts = [{"type": "image_url", "image_url": {"url": img}} for img in images]
            if text:
                # Per OpenAI spec, text part should come after image parts
                parts.append({"type": "text", "text": text})

            new_messages[i] = type(msg)(
                role=msg.role, content=parts, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
            )
    else:
        # Non-vision format: ensure content is a string for all roles, stripping image data.
        _img_marker_pattern = r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]"
        for i, msg in enumerate(new_messages):
            if isinstance(msg.content, list):
                # It's a list, join all text parts. This handles user messages
                # with vision content and tool messages from vision_inject.
                text_parts = [part.get("text", "") for part in msg.content if part.get("type") == "text"]
                content_str = "\n".join(text_parts).strip()
                new_messages[i] = type(msg)(
                    role=msg.role, content=content_str, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

            elif isinstance(msg.content, str) and "[image_data:" in msg.content:
                # It's a string with image markers, strip them
                _n_imgs = len(_re_v.findall(_img_marker_pattern, msg.content))
                cleaned = _re_v.sub(_img_marker_pattern, "", msg.content).strip()
                if _n_imgs > 0:
                    cleaned += f"\n[用户发送了 {_n_imgs} 张图片，但当前模型不支持视觉，无法查看图片内容]"
                new_messages[i] = type(msg)(
                    role=msg.role, content=cleaned, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

    return new_messages


def _attach_turn_context(api_messages: list, dynamic_prompt: str | None) -> list:
    """Attach one immutable runtime-context snapshot to the current turn.

    Layout after injection:
        last_user.content = f"<context>\\n{dynamic_prompt}\\n</context>\\n\\n{original}"

    The returned list becomes the caller's canonical in-memory message list for
    the complete tool loop.  Every later LLM request therefore sees the same
    memory/runtime snapshot, while assistant/tool messages are appended after
    it without changing any previously-dispatched bytes.

    Why wrap only a *tail* user instead of searching history?
    - A tail user is the current turn and can safely receive volatile context.
    - A non-user tail means the execution is resuming from assistant/tool
      history (for example after confirmation); rewriting an older user would
      corrupt persisted-history semantics and invalidate prefix caching.
    - Leaves the stable system + persisted conversation prefix untouched.
    - Places turn-volatile data at the tail, after the cacheable history.
    - Does not persist the wrapper to ChatMessage; the next turn rebuilds a
      fresh snapshot around its own current user message.

    A tail tool result is a suspended tool-call continuation.  Keep that result
    as the final message so the provider resumes the same turn instead of
    interpreting runtime context as new user input.  In that shape the dynamic
    snapshot travels on the system message's dedicated ``dynamic_content``
    field.  This is tool-agnostic: confirmation cards are only one producer of
    externally completed tool results.

    When there is no user or tool result, append a context-only user message so
    unattended executions still receive the snapshot.  With no dynamic context,
    return a shallow copy unchanged.  The input list is never mutated.

    The returned list contains fresh ``LLMMessage`` instances for any message
    we modify, so the caller's ``api_messages`` stays byte-identical for the
    next round's cache prefix.
    """
    out = list(api_messages)
    if not dynamic_prompt:
        return out

    if out and out[-1].role == "tool":
        system_idx = next(
            (idx for idx, message in enumerate(out) if message.role == "system"),
            None,
        )
        if system_idx is None:
            out.insert(
                0,
                LLMMessage(
                    role="system",
                    content="",
                    dynamic_content=dynamic_prompt,
                ),
            )
        else:
            system_message = out[system_idx]
            existing_dynamic = system_message.dynamic_content
            out[system_idx] = replace(
                system_message,
                dynamic_content=(
                    f"{existing_dynamic}\n\n{dynamic_prompt}"
                    if existing_dynamic
                    else dynamic_prompt
                ),
            )
        return out

    # Only the final message can be the current user turn. Non-tool unattended
    # paths may need a context-only user tail; completed tool continuations were
    # handled above and must retain their tool result as the tail.
    if not out or out[-1].role != "user":
        out.append(
            LLMMessage(
                role="user",
                content=f"<context>\n{dynamic_prompt}\n</context>",
            )
        )
        return out

    last_user_idx = len(out) - 1
    original = out[last_user_idx]
    original_content = original.content
    # If content is a vision list, wrap the trailing text part (vision_convert
    # already places text after images); if there's no text part, append one.
    if isinstance(original_content, list):
        new_parts = [dict(p) for p in original_content]
        text_idx = -1
        for i in range(len(new_parts) - 1, -1, -1):
            if new_parts[i].get("type") == "text":
                text_idx = i
                break
        wrapper_prefix = f"<context>\n{dynamic_prompt}\n</context>\n\n"
        if text_idx >= 0:
            existing_text = new_parts[text_idx].get("text", "")
            new_parts[text_idx] = {
                "type": "text",
                "text": f"{wrapper_prefix}{existing_text}",
            }
        else:
            new_parts.append({"type": "text", "text": wrapper_prefix.rstrip()})
        new_content: str | list = new_parts
    else:
        base = original_content or ""
        new_content = f"<context>\n{dynamic_prompt}\n</context>\n\n{base}"

    out[last_user_idx] = LLMMessage(
        role=original.role,
        content=new_content,
        tool_calls=original.tool_calls,
        tool_call_id=original.tool_call_id,
        reasoning_content=original.reasoning_content,
        reasoning_signature=original.reasoning_signature,
    )
    return out


async def _build_turn_context(
    *,
    agent_id,
    agent_name: str,
    role_description: str,
    user_id,
    current_user_name_override: str | None,
    is_group: bool,
    session_id: str,
    channel_context: dict | None,
    include_soul: bool = True,
    include_memory: bool = True,
) -> tuple[str, str]:
    """Build the immutable static/dynamic context pair for one logical turn."""
    if current_user_name_override:
        user_name = current_user_name_override
    else:
        user_name = await _get_user_name(user_id)

    from app.services.agent_context import build_agent_context

    runtime_channel_context = dict(channel_context or {})
    if session_id:
        runtime_channel_context["session_id"] = session_id

    return await build_agent_context(
        agent_id,
        agent_name,
        role_description,
        current_user_name=user_name,
        current_user_id=None if current_user_name_override else user_id,
        is_group=is_group,
        channel_context=runtime_channel_context,
        include_soul=include_soul,
        include_memory=include_memory,
    )


def _check_tool_requires_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """Check if tool requires arguments and return (should_execute, result_or_error)."""
    if not args and tool_name in TOOLS_REQUIRING_ARGS:
        return (
            False,
            f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments.",
        )
    return True, ""


# ─── Upstream-imported helpers ────────────────────────────────────────────────
# Restored after the take-ours conflict resolution dropped them: the call sites
# (_process_tool_call, the tool execution loops at ~700 and ~1100) were merged
# from upstream and still reference these symbols, so the definitions must
# coexist with the fork's _canonicalize_tc_arguments / _shape_tool_content_for_context.


def _sanitize_tool_calls_for_context(tool_calls: list[dict]) -> tuple[list[dict] | None, str | None]:
    """Validate one provider tool-call round atomically before use or persistence."""
    if not tool_calls or len(tool_calls) > 128:
        return None, (
            "Your previous tool-call response was incomplete. Retry once with between 1 and 128 "
            "complete function calls. Do not explain; only return valid tool calls."
        )

    sanitized: list[dict] = []
    call_ids: set[str] = set()
    for tc in tool_calls:
        if not isinstance(tc, dict):
            return None, (
                "Your previous tool-call response was malformed. Retry once with complete function "
                "calls. Do not explain; only return valid tool calls."
            )
        fn = tc.get("function")
        if tc.get("type") not in (None, "function") or not isinstance(fn, dict):
            return None, (
                "Your previous tool-call response was malformed. Retry once with complete function "
                "calls. Do not explain; only return valid tool calls."
            )
        call_id = str(tc.get("id") or "").strip()
        tool_name = str(fn.get("name") or "").strip()
        if not call_id or call_id in call_ids or not tool_name:
            return None, (
                "Your previous tool-call response had a missing or duplicate call ID, or a missing "
                "function name. Retry once with complete, uniquely identified function calls. "
                "Do not explain; only return valid tool calls."
            )
        call_ids.add(call_id)
        raw_args = fn.get("arguments", "{}")

        if raw_args is None or raw_args == "":
            args_str = "{}"
        elif isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[LLM] Invalid tool arguments JSON for {}: {} at pos {}",
                    tool_name or "<unknown>",
                    exc.msg,
                    exc.pos,
                )
                return None, (
                    "Your previous tool call arguments were not valid JSON. "
                    f"The affected tool was `{tool_name or 'unknown'}`. "
                    "Retry the tool call now with `function.arguments` as one valid JSON object string. "
                    "Escape all quotes and newlines inside long HTML, CSS, JavaScript, or markdown content. "
                    "Do not explain; only retry with a valid tool call."
                )
            if not isinstance(parsed_args, dict):
                return None, (
                    "Your previous tool call arguments were valid JSON but not a JSON object. "
                    f"The affected tool was `{tool_name}`. Retry once with "
                    "`function.arguments` as one JSON object string."
                )
            args_str = raw_args
        elif isinstance(raw_args, dict):
            args_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            return None, (
                "Your previous tool call arguments had an unsupported type. "
                f"The affected tool was `{tool_name or 'unknown'}`. "
                "Retry the tool call with `function.arguments` as one valid JSON object string."
            )

        new_tc = {
            "id": call_id,
            "type": tc.get("type") or "function",
            "function": {
                "name": tool_name,
                "arguments": args_str,
            },
        }
        if "_gemini_extra" in tc:
            new_tc["_gemini_extra"] = tc["_gemini_extra"]
        sanitized.append(new_tc)

    return sanitized, None


def _allowed_tool_names(tools_for_llm: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools_for_llm or []:
        name = ((tool.get("function") or {}).get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _tool_not_enabled_message(tool_name: str) -> str:
    return (
        f"Tool `{tool_name}` is not enabled for this agent. "
        "Do not call it again. Use only the tools currently available to you, "
        "or explain that the required capability is not enabled."
    )


def _canonicalize_tc_arguments(tc: dict, session_id: str) -> dict[str, Any]:
    """Canonicalize ``tc['function']['arguments']`` in place and return the parsed dict.

    The canonical JSON is written back to ``tc['function']['arguments']`` so that
    any subsequent LLM round receiving this ``tc`` in conversation history will
    pass DashScope's ``function.arguments must be in JSON format`` validation.
    Used by both in-flight tool loops (_process_tool_call and _try_model).
    """
    fn = tc["function"]
    tool_name = fn["name"]
    raw_args = fn.get("arguments", "{}")
    args, canonical_args, repair_method = canonicalize_tool_arguments(raw_args)
    fn["arguments"] = canonical_args
    if repair_method != "clean":
        logger.warning(
            f"[LLM] tool_call args repaired: tool={tool_name} method={repair_method} "
            f"orig_len={len(raw_args)} new_len={len(canonical_args)} session={session_id}"
        )
    return args


async def _process_tool_call(
    tc: dict,
    api_messages: list,
    agent_id,
    user_id,
    session_id: str,
    supports_vision: bool,
    on_tool_call,
    full_reasoning_content: str,
    allowed_tool_names: set[str],
    tools_for_llm: list[dict] | None = None,
    on_code_output=None,
    emit_running: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
    before_execute=None,
    round_done_records: list[_RoundDoneToolCall] | None = None,
    round_id: str | None = None,
    round_tool_index: int | None = None,
    assistant_content: str | None = None,
    responses_snapshot: dict | None = None,
    recovery_prefix_messages: list[dict[str, str]] | None = None,
    durable_agent_id=None,
) -> str:
    """Process a single tool call and return result."""
    persistence_agent_id = durable_agent_id or agent_id
    args = _canonicalize_tc_arguments(tc, session_id)
    tool_name = tc["function"]["name"]
    logger.info(f"[LLM] Calling tool: {tool_name}({_observable_tool_args(tool_name, args)[:500]})")

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        done_evt = {
            "name": tool_name,
            "call_id": tc.get("id", ""),
            "args": args,
            "status": "done",
            "round_id": round_id,
            "round_tool_index": round_tool_index,
            "result": error_msg,
            "reasoning_content": full_reasoning_content,
            "assistant_content": assistant_content,
            "responses_snapshot": responses_snapshot,
            "recovery_prefix_messages": recovery_prefix_messages or [],
        }
        persisted = await _persist_tool_call_events_strict(
            [done_evt],
            agent_id=persistence_agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        row_id = persisted.get(str(done_evt.get("call_id") or "")) if persisted else None
        if persisted:
            done_evt["_durable_persisted"] = True
        record = _RoundDoneToolCall(event=done_evt, row_id=row_id, provider_content=error_msg)
        if round_done_records is not None:
            round_done_records.append(record)
        else:
            await _emit_round_done_events([record], on_tool_call)
        return error_msg

    permission_name = "read_media" if tool_name == "read_image" else tool_name
    if permission_name not in allowed_tool_names:
        result = _tool_not_enabled_message(tool_name)
        logger.warning(f"[LLM] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
        done_evt = {
            "name": tool_name,
            "call_id": tc.get("id", ""),
            "args": args,
            "status": "done",
            "round_id": round_id,
            "round_tool_index": round_tool_index,
            "result": result,
            "reasoning_content": full_reasoning_content,
            "assistant_content": assistant_content,
            "responses_snapshot": responses_snapshot,
            "recovery_prefix_messages": recovery_prefix_messages or [],
        }
        persisted = await _persist_tool_call_events_strict(
            [done_evt],
            agent_id=persistence_agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        row_id = persisted.get(str(done_evt.get("call_id") or "")) if persisted else None
        if persisted:
            done_evt["_durable_persisted"] = True
        record = _RoundDoneToolCall(event=done_evt, row_id=row_id, provider_content=result)
        if round_done_records is not None:
            round_done_records.append(record)
        else:
            await _emit_round_done_events([record], on_tool_call)
        api_messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=tc["id"],
                content=result,
            )
        )
        return ""

    # Notify client about tool call (in-progress). The normal multi-tool loop
    # pre-emits running markers for the whole round before executing any tool;
    # direct helper callers keep the historical behavior through emit_running=True.
    if emit_running:
        running_evt = {
            "name": tool_name,
            "call_id": tc.get("id", ""),
            "args": args,
            "status": "running",
            "round_id": round_id,
            "round_tool_index": round_tool_index,
            "reasoning_content": full_reasoning_content,
            "assistant_content": assistant_content,
            "responses_snapshot": responses_snapshot,
            "recovery_prefix_messages": recovery_prefix_messages or [],
        }
        persisted_running = await _persist_tool_call_events_strict(
            [running_evt],
            agent_id=persistence_agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        running_row_id = (
            persisted_running.get(str(running_evt.get("call_id") or ""))
            if isinstance(persisted_running, dict)
            else None
        )
        if running_row_id is not None:
            running_evt["_durable_persisted"] = True
            running_evt["_durable_message_id"] = str(running_row_id)
    else:
        running_evt = None

    if running_evt is not None and on_tool_call:
        try:
            await on_tool_call({k: v for k, v in running_evt.items() if k != "responses_snapshot"})
        except Exception:
            pass

    # A durable reference is display metadata, never a partial model result.
    session_ref = None

    async def on_progress(reference):
        nonlocal session_ref
        session_ref = dict(reference)
        event = {
            "name": tool_name,
            "call_id": tc.get("id", ""),
            "args": args,
            "status": "running",
            "session_ref": session_ref,
            "round_id": round_id,
            "round_tool_index": round_tool_index,
            "reasoning_content": full_reasoning_content,
            "assistant_content": assistant_content,
            "responses_snapshot": responses_snapshot,
            "recovery_prefix_messages": recovery_prefix_messages or [],
        }
        persisted = await _persist_tool_call_events_strict(
            [event],
            agent_id=persistence_agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        row_id = persisted.get(str(event["call_id"]))
        if row_id is not None:
            event["_durable_persisted"] = True
            event["_durable_message_id"] = str(row_id)
        if on_tool_call:
            try:
                await on_tool_call({k: v for k, v in event.items() if k != "responses_snapshot"})
            except Exception:
                pass

    # Execute tool — pass on_output for execute_code streaming
    if before_execute is not None:
        await before_execute()
    _on_output = on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    _tool_t0 = perf_counter()
    result = await execute_tool(
        tool_name,
        args,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        tool_call_id=str(tc.get("id") or ""),
        turn_anchor_id=turn_anchor_id,
        on_output=_on_output,
        on_progress=on_progress,
        tools_for_llm=tools_for_llm,
    )
    logger.info(f"[LLM Timing] tool={tool_name} exec={perf_counter() - _tool_t0:.2f}s agent={agent_id}")
    observable_result = _observable_tool_result(tool_name, result)
    logger.debug(f"[LLM] Tool result: {observable_result[:100]}")

    # Materialize oversize output and produce the canonical llm_view string.
    # This is the single shape point — DB, WS live stream, historical replay
    # all consume this string, keeping the messages sequence append-only.
    llm_view = await finalize_tool_output(
        result,
        tool_name=tool_name,
        agent_id=agent_id,
        session_id=session_id,
        tool_call_id=tc["id"],
    )

    # Vision injection is a one-shot enhancement for the current turn only.
    # Historical replay sees the string llm_view; only this round gets the
    # richer image payload.
    tool_content: str | list = llm_view
    if supports_vision and agent_id:
        try:
            from app.services.agent_runtime_workspace import current_agent_runtime_workspace
            from app.services.vision_inject import try_inject_screenshot_vision

            ws_path = current_agent_runtime_workspace(agent_id).local_root
            vision_content = try_inject_screenshot_vision(tool_name, str(result), ws_path)
            if vision_content:
                tool_content = vision_content
                logger.info(f"[LLM] Injected screenshot vision for {tool_name}")
        except Exception as e:
            logger.warning(f"[LLM] Vision injection failed for {tool_name}: {e}")

    # Notify client (for WS live stream and DB persistence) with the
    # llm_view — never the raw result. Three-way consistency: DB view,
    # LLM replay view, and the value the frontend receives all match.
    done_evt = {
        "name": tool_name,
        "call_id": tc.get("id", ""),
        "args": args,
        "status": "done",
        "round_id": round_id,
        "round_tool_index": round_tool_index,
        "result": llm_view,
        "reasoning_content": full_reasoning_content,
        "assistant_content": assistant_content,
        "responses_snapshot": responses_snapshot,
        "recovery_prefix_messages": recovery_prefix_messages or [],
    }
    done_row_id = _durable_tool_result_row_id(tool_name, str(llm_view), session_id)
    if session_ref is not None:
        done_evt["session_ref"] = session_ref
    if done_row_id is not None:
        done_evt["_durable_persisted"] = True
    else:
        persisted_done_rows = await _persist_tool_call_events_strict(
            [done_evt],
            agent_id=persistence_agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        if persisted_done_rows:
            done_evt["_durable_persisted"] = True
            if isinstance(persisted_done_rows, dict):
                done_row_id = persisted_done_rows.get(str(done_evt.get("call_id") or ""))

    done_record = _RoundDoneToolCall(
        event=done_evt,
        row_id=done_row_id,
        provider_content=tool_content,
    )
    if round_done_records is not None:
        round_done_records.append(done_record)
    else:
        await _emit_round_done_events([done_record], on_tool_call)

    api_messages.append(
        LLMMessage(
            role="tool",
            tool_call_id=tc["id"],
            content=tool_content,
        )
    )
    return ""


__all__ = [name for name in globals() if name != "__all__" and not name.startswith("__")]
