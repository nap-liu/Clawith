"""Caller tool-event and persistence helpers."""

from app.services.llm.caller_shared import *  # noqa: F401,F403


def _tool_call_signature(tc: dict) -> tuple[str, str]:
    """Stable ``(name, canonical-args)`` identity for a tool call.

    Arguments are JSON-normalised (sorted keys) so semantically identical calls
    compare equal regardless of key order / whitespace — at least as strict as
    the provider's own "identical arguments" check. Falls back to the trimmed
    raw string when the arguments are not valid JSON.
    """
    fn = (tc or {}).get("function") or {}
    name = fn.get("name") or ""
    raw = fn.get("arguments")
    if raw is None or raw == "":
        return (name, "")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        args_key = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args_key = raw.strip() if isinstance(raw, str) else str(raw)
    return (name, args_key)


def _update_repeat_streaks(
    prev_streaks: dict[tuple[str, str], int],
    round_signatures: list[tuple[str, str]],
) -> dict[tuple[str, str], int]:
    """Consecutive-round streak counts after one round of tool calls.

    A signature seen this round extends its prior streak (+1); any signature NOT
    seen this round drops out (streak resets to 0). Pure — does not mutate
    ``prev_streaks``. Identical calls within the *same* round count once (the
    provider's 400 is about repetition *across* rounds).
    """
    return {sig: prev_streaks.get(sig, 0) + 1 for sig in round_signatures}


# ═══════════════════════════════════════════════════════════════════════════════
# Failover Guard
# ═══════════════════════════════════════════════════════════════════════════════


class FailoverGuard:
    """Guard state for failover decisions."""

    def __init__(self):
        self.tool_executed = False
        self.streaming_started = False
        self.failover_done = False

    def mark_tool_executed(self):
        """Mark that a side-effecting tool has been executed."""
        self.tool_executed = True

    def mark_streaming_started(self):
        """Mark that streaming output has started."""
        self.streaming_started = True

    def mark_failover_done(self):
        """Mark that failover has already happened once."""
        self.failover_done = True

    def can_failover(self) -> bool:
        """Check if failover is allowed based on guard rules."""
        if self.failover_done:
            return False  # Only failover once
        if self.tool_executed:
            return False  # Don't failover after side effects
        if self.streaming_started:
            return False  # Don't failover after streaming started
        return True


def is_error_result(result: str) -> bool:
    """True when *result* is an error sentinel string, not a normal model reply.

    The LLM/tool layer signals failures by returning a string prefixed with one
    of these markers instead of raising, so a successful reply never matches.
    """
    return result == PROVIDER_CONTEXT_BLOCKED_MESSAGE or result.startswith(
        ("[LLM Error]", "[LLM call error]", "[Error]")
    )


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.

    Uses unified classification from failover.py.
    """
    if not is_error_result(result):
        return False

    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE


def _same_model_record(primary_model, fallback_model) -> bool:
    """True only for the same configured DB model record.

    Two records with the same provider/model name may intentionally use a
    different endpoint or credential and remain a valid fallback.
    """
    if primary_model is None or fallback_model is None:
        return False
    primary_id = getattr(primary_model, "id", None)
    fallback_id = getattr(fallback_model, "id", None)
    return primary_id is not None and fallback_id is not None and primary_id == fallback_id


def _get_model_timeout(model: "LLMModel") -> float:
    """Return the effective request timeout for a model."""
    return float(getattr(model, "request_timeout", None) or 120.0)


def _usage_from_response(response) -> TokenUsage:
    usage = extract_token_usage(response.usage)
    if usage:
        return usage
    # Missing provider usage is unknown. Never synthesize input tokens from
    # text length, bytes, image placeholders, or a local tokenizer.
    return TokenUsage()


def _authoritative_usage_details(raw_usage: dict | None) -> dict:
    """Return provider modality/cache detail fields without inventing values."""
    if not isinstance(raw_usage, dict):
        return {}
    details: dict = {}
    for key in (
        "prompt_tokens_details",
        "input_tokens_details",
        "completion_tokens_details",
        "output_tokens_details",
        "usageMetadata",
    ):
        value = raw_usage.get(key)
        if isinstance(value, dict):
            details[key] = value
    return details


def _is_provider_context_overflow(error: Exception) -> bool:
    """Recognize explicit provider context-limit rejections only.

    This intentionally excludes generic HTTP 400 responses.  Retrying an
    authentication/schema/safety error after compaction would be both wasteful
    and capable of masking the real failure.
    """
    structured = " ".join(
        str(value or "").lower()
        for value in (
            getattr(error, "error_code", None),
            getattr(error, "error_type", None),
        )
    )
    structured_markers = (
        "context_length_exceeded",
        "context_window_exceeded",
        "max_tokens_exceeded",
        "request_too_large",
    )
    if any(marker in structured for marker in structured_markers):
        return True
    if getattr(error, "status_code", None) == 413:
        return True

    value = str(error).lower()
    markers = (
        "context_length_exceeded",
        "maximum context length",
        "max context length",
        "context window",
        "input length exceeds",
        "input is too long",
        "too many tokens",
        "reduce the length of the messages",
        "prompt is too long",
    )
    return any(marker in value for marker in markers)


def _coerce_uuid(value) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


_SENSITIVE_RESULT_TOOLS = {"list_installed_mcp_servers"}


def _observable_tool_args(tool_name: str, args: dict[str, Any]) -> str:
    """Return useful tool-call metadata without logging private media URLs."""
    if tool_name != "send_media":
        return json.dumps(args, ensure_ascii=False, default=str)
    summary = {
        "keys": sorted(str(key) for key in args),
        "media_type": args.get("media_type"),
        "source": "url" if args.get("url") else "file_path" if args.get("file_path") else None,
        "url_mode": args.get("url_mode"),
        "has_session_id": bool(args.get("session_id")),
        "has_user_id": bool(args.get("user_id")),
        "channel": args.get("channel"),
        "has_message": bool(args.get("message")),
        "has_cover_image_path": bool(args.get("cover_image_path")),
    }
    return json.dumps(summary, ensure_ascii=False, default=str)


def _observable_tool_result(tool_name: str, result: str) -> str:
    """Mask credential values while preserving the complete diagnostic shape."""
    if tool_name == "send_media":
        try:
            payload = json.loads(result)
            if not isinstance(payload, dict):
                return '{"type": "media_delivery_result", "status": "unparseable"}'
            safe_keys = (
                "type",
                "version",
                "status",
                "code",
                "media_kind",
                "source_mode",
                "channel",
                "http_status",
                "actual_kind",
                "size",
                "mime_type",
                "caption_sent",
            )
            summary = {key: payload[key] for key in safe_keys if key in payload}
            summary["has_url"] = bool(payload.get("url"))
            summary["has_path"] = bool(payload.get("path") or payload.get("managed_path"))
            return json.dumps(summary, ensure_ascii=False, default=str)
        except Exception:
            return '{"type": "media_delivery_result", "status": "unparseable"}'
    if tool_name not in _SENSITIVE_RESULT_TOOLS:
        return result
    try:
        from app.utils.sanitize import sanitize_sensitive_values

        payload = json.loads(result)
        return json.dumps(
            sanitize_sensitive_values(payload),
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        return result


def _send_media_result_is_durable_in_current_session(tool_name: str, result: str, session_id: str) -> bool:
    """True when send_media already updated the current Session's tool row."""
    if tool_name != "send_media":
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("type")
        in {
            "platform_media_delivery",
            "media_delivery_result",
        }
        and payload.get("status")
        in {
            "sent",
            "already_sent",
            "failed",
            "unsupported",
            "unknown",
        }
        and str(payload.get("session_id") or "") == str(session_id or "")
        and str(payload.get("message_id") or "")
    )


def _durable_tool_result_row_id(tool_name: str, result: str, session_id: str) -> uuid.UUID | None:
    """Resolve a tool-owned durable result row when the tool persisted it itself."""
    if not _send_media_result_is_durable_in_current_session(tool_name, result, session_id):
        return None
    try:
        payload = json.loads(result)
        return uuid.UUID(str(payload.get("message_id") or ""))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


@dataclass
class _RoundDoneToolCall:
    event: dict[str, Any]
    row_id: uuid.UUID | None


async def _persist_tool_call_events_strict(
    events: list[dict],
    *,
    agent_id,
    user_id,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> dict[str, uuid.UUID]:
    """Durably append tool-call markers before the loop relies on them.

    Returns an empty mapping for non-persistable test/background calls without UUID ids.
    For real chat turns, DB errors propagate and stop execution before side
    effects can happen without a recovery marker.
    """
    if not events or not session_id:
        return {}
    agent_uuid = _coerce_uuid(agent_id)
    user_uuid = _coerce_uuid(user_id)
    if agent_uuid is None or user_uuid is None:
        return {}

    from app.services.chat_history import persist_tool_call_row
    from app.services.conversation_turn_lifecycle import (
        lock_conversation_turn_running,
    )

    persisted: dict[str, uuid.UUID] = {}
    async with async_session() as db:
        await lock_conversation_turn_running(
            db,
            agent_id=agent_uuid,
            conversation_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        for evt in events:
            row_id = await persist_tool_call_row(
                db,
                agent_id=agent_uuid,
                user_id=user_uuid,
                conversation_id=session_id,
                evt=evt,
                turn_anchor_id=turn_anchor_id,
                turn_fence_locked=True,
            )
            if row_id is not None:
                persisted[str(evt.get("call_id") or "")] = row_id
        await db.commit()
    return persisted


async def _reconcile_round_tool_outputs(
    rewrites: list[ToolOutputRewrite],
    done_records: list[_RoundDoneToolCall],
    *,
    agent_id,
    user_id,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> None:
    """Make durable done rows match the fresh provider transcript exactly."""
    if not rewrites:
        return

    records_by_call_id: dict[str, _RoundDoneToolCall] = {}
    for record in done_records:
        call_id = str(record.event.get("call_id") or "")
        if not call_id or call_id in records_by_call_id:
            raise RuntimeError(f"ambiguous durable tool result call_id: {call_id!r}")
        records_by_call_id[call_id] = record

    final_by_call_id: dict[str, str] = {}
    for rewrite in rewrites:
        if not rewrite.tool_call_id or rewrite.tool_call_id in final_by_call_id:
            raise RuntimeError(f"ambiguous tool output rewrite call_id: {rewrite.tool_call_id!r}")
        if rewrite.tool_call_id not in records_by_call_id:
            raise RuntimeError(f"tool output rewrite has no done result: {rewrite.tool_call_id!r}")
        final_by_call_id[rewrite.tool_call_id] = rewrite.final_content

    agent_uuid = _coerce_uuid(agent_id)
    user_uuid = _coerce_uuid(user_id)
    durable_turn = bool(session_id and agent_uuid is not None and user_uuid is not None)
    if durable_turn:
        replacements: dict[uuid.UUID, tuple[str, str]] = {}
        for call_id, final_content in final_by_call_id.items():
            row_id = records_by_call_id[call_id].row_id
            if row_id is None:
                raise RuntimeError(f"durable tool result row is missing for call_id={call_id!r}")
            replacements[row_id] = (call_id, final_content)

        from app.services.chat_history import rewrite_tool_call_done_results

        async with async_session() as db:
            await rewrite_tool_call_done_results(
                db,
                agent_id=agent_uuid,
                user_id=user_uuid,
                conversation_id=session_id,
                replacements=replacements,
                turn_anchor_id=turn_anchor_id,
            )
            await db.commit()

    # Mutate callback payloads only after the durable transaction commits. On
    # failure they retain the original values that are still authoritative in DB.
    for call_id, final_content in final_by_call_id.items():
        records_by_call_id[call_id].event["result"] = final_content


async def _emit_round_done_events(
    done_records: list[_RoundDoneToolCall],
    on_tool_call,
) -> None:
    if on_tool_call is None:
        return
    for record in done_records:
        try:
            await on_tool_call(record.event)
        except Exception:
            pass


__all__ = [name for name in globals() if name != "__all__" and not name.startswith("__")]
