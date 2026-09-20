from app.services.llm.compactor_shared import *  # noqa: F401,F403

async def _delete_uncommitted_archive(archive_materialized) -> None:
    """Do not delete a stable content-addressed archive after a failed commit.

    Another process may already have committed a summary referencing the same
    epoch/source object. The bounded orphan case is safe and retry-reusable;
    deleting a possibly shared object would break lossless history.
    """
    return None


# ─── Per-session lock ────────────────────────────────────────────────


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


def _current_anchor_owns_latest_logical_tail(
    rows: list[ChatMessage],
    current_anchor_id: uuid.UUID,
) -> bool:
    """Accept an anchored turn after it has accumulated tools/injections."""
    if not rows:
        return False
    partition = partition_turns(rows, current_anchor_id=str(current_anchor_id))
    current = partition.current
    return bool(
        current is not None
        and current.rows
        and str(getattr(current.rows[-1], "id", "")) == str(rows[-1].id)
    )


def _is_dryrun() -> bool:
    return os.environ.get("CLAWITH_COMPACT_DRYRUN", "").lower() in ("1", "true", "yes")


# ─── Summary LLM call ────────────────────────────────────────────────


async def _summarize_via_llm(
    *,
    span_text: str,
    prior_summary: str | None,
    model: LLMModel,
    retained_history_text: str | None = None,
    failed_summary: str | None = None,
    failure_reason: str | None = None,
    on_input_bounded: Callable[[bool], None] | None = None,
) -> tuple[str, dict | None]:
    """Run one streaming summary call and require a complete terminal response."""
    from app.services.llm import LLMMessage, create_llm_client, get_max_tokens, get_model_api_key
    from app.services.model_headers import resolve_model_headers

    def _messages(candidate_span: str) -> list[LLMMessage]:
        user_payload = []
        if prior_summary:
            user_payload.append("<prior-summary epoch_n_minus_1>\n" + prior_summary + "\n</prior-summary>\n")
        if failure_reason is not None:
            user_payload.append(
                "<repair-request>\n"
                f"The previous draft failed validation: {failure_reason}. "
                "Rewrite it once so it follows every required heading and preservation rule.\n"
                f"<failed-draft>\n{failed_summary or ''}\n</failed-draft>\n"
                "</repair-request>"
            )
        user_payload.append(
            "<replacement-history>\n" + candidate_span + "\n</replacement-history>"
        )
        if retained_history_text:
            user_payload.append(
                "<retained-history>\n"
                + retained_history_text
                + "\n</retained-history>"
            )
        return [
            LLMMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
            LLMMessage(role="user", content="\n\n".join(user_payload)),
        ]

    messages = _messages(span_text)
    if on_input_bounded is not None:
        on_input_bounded(False)

    api_key = get_model_api_key(model)
    client = create_llm_client(
        provider=model.provider,
        api_protocol=getattr(model, "api_protocol", None),
        base_url=model.base_url,
        api_key=api_key,
        model=model.model,
        timeout=float(getattr(model, "request_timeout", None) or 120.0),
        provider_managed_timeout=True,
        extra_headers=resolve_model_headers(model),
    )

    try:
        response = await client.stream(
            messages,
            max_tokens=get_max_tokens(
                model.provider, model.model, getattr(model, "max_output_tokens", None)
            ),
            temperature=0.2,
        )
        if getattr(response, "tool_calls", None):
            raise RuntimeError("summary_unexpected_tool_call")
        finish_reason = getattr(response, "finish_reason", None)
        if finish_reason != "stop":
            raise RuntimeError(f"summary_incomplete:{finish_reason or 'missing_terminal_status'}")
        if not str(getattr(response, "content", "") or "").strip():
            raise RuntimeError("summary_empty_response")
        return response.content or "", response.usage
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()

__all__ = (
    '_delete_uncommitted_archive',
    '_get_session_lock',
    '_current_anchor_owns_latest_logical_tail',
    '_is_dryrun',
    '_summarize_via_llm',
)
