"""Tool output materialization.

Large tool results are written to a per-agent, per-session file under
`.tool_results/` in the agent workspace. The LLM sees a compact
`<persisted-output>` block with a short preview plus a file reference,
and can call `read_file` (or other workspace tools) to fetch the rest
on demand.

This is the single write path for tool output. DB persistence, LLM
context replay, and the frontend all observe the same string — the
"llm_view" produced here. Downstream code never re-shapes it, so the
messages sequence becomes append-only (good for prompt caching).

The module intentionally does only two things:
  1. Decide inline vs. materialize for a given (tool, result).
  2. Produce a deterministic llm_view string.

Everything else (caching, budgets, message layout) is layered above it.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from loguru import logger

from app.config import get_settings
from .tool_result_shaping import shape_tool_result


PERSISTED_OPEN = "<persisted-output>"
PERSISTED_CLOSE = "</persisted-output>"
PREVIEW_CHARS = 2_000

TOOL_OUTPUT_MAX_CHARS: dict[str, int | float] = {
    # Sized for ~128K-token models (qwen3.5-plus, qwen-max-latest). Common
    # report payloads (svc report query, search results, JSON dumps) sit
    # in the 60–95 KB range; the previous 50 KB / 30 KB buckets pushed
    # them through persisted-output → read_file → re-persisted loops.
    # Doubled across the board so the typical query lands inline in one
    # tool call. Truly oversized output (the long tail) still gets
    # materialized to disk.
    "execute_code": 60_000,
    "run_command": 60_000,
    "bash": 60_000,
    "grep": 40_000,
    "search_files": 40_000,
    # read_document returns full extracted text (no tool-layer truncation); a
    # modest budget makes large documents overflow to a .tool_results/ file early
    # so the agent pages through them via read_file instead of flooding context.
    "read_document": 40_000,
    "read_file": float("inf"),
    "list_files": 100_000,
    "_default": 100_000,
}

ENV_OVERRIDE = "CLAWITH_TOOL_OUTPUT_MAX_CHARS"

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _default_budget() -> int:
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        try:
            return int(override)
        except ValueError:
            logger.warning(f"[tool_output_store] invalid {ENV_OVERRIDE}={override!r}, ignoring")
    fallback = TOOL_OUTPUT_MAX_CHARS["_default"]
    return int(fallback) if fallback != float("inf") else 50_000


def budget_for(tool_name: str) -> int | float:
    """Return the char budget for a tool, honoring env override on the default bucket."""
    if tool_name in TOOL_OUTPUT_MAX_CHARS:
        return TOOL_OUTPUT_MAX_CHARS[tool_name]
    return _default_budget()


def _sanitize(name: str) -> str:
    """Make a string safe for use as a filename component."""
    cleaned = _FILENAME_SAFE.sub("_", name).strip("_.") or "unnamed"
    return cleaned[:80]


def _store_dir(agent_id, session_id: str) -> Path:
    settings = get_settings()
    return (
        Path(settings.AGENT_DATA_DIR)
        / str(agent_id)
        / ".tool_results"
        / _sanitize(session_id or "nosession")
    )


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _looks_like_json(s: str) -> bool:
    stripped = s.lstrip()
    if not stripped:
        return False
    if stripped[0] not in "{[":
        return False
    try:
        json.loads(s)
        return True
    except (ValueError, TypeError):
        return False


def _render_empty(tool_name: str) -> str:
    return f"({tool_name} completed with no output)"


def _render_persisted(
    *,
    tool_name: str,
    rel_path: str,
    size_bytes: int,
    preview: str,
) -> str:
    return (
        f"{PERSISTED_OPEN}\n"
        f"Output too large ({_format_size(size_bytes)}). "
        f"Full output saved to: {rel_path}\n\n"
        f"Preview (first {PREVIEW_CHARS:,} chars):\n"
        f"{preview}\n"
        f"{'...' if size_bytes > len(preview) else ''}\n\n"
        f"Use read_file to access full content, "
        f"or grep/search_files to find specific content.\n"
        f"{PERSISTED_CLOSE}"
    )


def _materialize_to_file(
    result: str,
    *,
    tool_name: str,
    agent_id,
    session_id: str,
    tool_call_id: str,
) -> str:
    """Write ``result`` to the agent workspace and return the llm_view.

    Shared internal helper. Raises on any failure (missing agent_id,
    unwritable filesystem, …); the two public entrypoints
    (:func:`finalize_tool_output`, :func:`force_materialize_tool_output`)
    decide how to recover.
    """
    if not agent_id:
        raise ValueError("missing agent_id")
    store_dir = _store_dir(agent_id, session_id)
    store_dir.mkdir(parents=True, exist_ok=True)

    ext = "json" if _looks_like_json(result) else "txt"
    tc_id = _sanitize(tool_call_id) or uuid.uuid4().hex[:12]
    filename = f"{_sanitize(tool_name)}_{tc_id}.{ext}"
    full_path = store_dir / filename

    full_path.write_text(result, encoding="utf-8")

    rel_path = f".tool_results/{_sanitize(session_id or 'nosession')}/{filename}"
    preview = result[:PREVIEW_CHARS]
    view = _render_persisted(
        tool_name=tool_name,
        rel_path=rel_path,
        size_bytes=len(result),
        preview=preview,
    )
    logger.info(
        f"[tool_output_store] materialized tool={tool_name} "
        f"size={len(result)} path={rel_path}"
    )
    return view


def finalize_tool_output(
    result,
    *,
    tool_name: str,
    agent_id,
    session_id: str,
    tool_call_id: str,
) -> str:
    """Produce the llm_view string for a completed tool call.

    Non-string content (vision payloads: list[dict] of text+image blocks)
    is returned unchanged — vision injection is responsible for its own
    shape. Only raw string tool output is subject to materialization.

    Empty output is normalized so the LLM never sees a blank tool result.

    Writes to the agent workspace when the result exceeds the per-tool
    budget. If the write fails (missing agent/session, disk error), falls
    back to a bounded head+tail shape so the caller always gets a usable
    string — we never silently drop a tool result.
    """
    if not isinstance(result, str):
        return result

    if not result:
        return _render_empty(tool_name)

    budget = budget_for(tool_name)
    if budget == float("inf") or len(result) <= budget:
        return result

    try:
        return _materialize_to_file(
            result,
            tool_name=tool_name,
            agent_id=agent_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    except Exception as exc:
        logger.warning(
            f"[tool_output_store] materialize failed tool={tool_name} "
            f"err={type(exc).__name__}: {exc}; falling back to inline shape"
        )
        shaped, _ = shape_tool_result(result, int(budget))
        return shaped


def force_materialize_tool_output(
    result: str,
    *,
    tool_name: str,
    agent_id,
    session_id: str,
    tool_call_id: str,
) -> str:
    """Always materialize to disk regardless of tool budget.

    Unlike :func:`finalize_tool_output` which checks per-tool budget
    first, this is the escape hatch used by the message-level enforcer
    when the sum of multiple in-budget results blows past the message
    cap.

    Same storage layout, same ``<persisted-output>`` render format. On
    failure (disk error, missing agent_id), falls back to inline
    :func:`shape_tool_result` bounded at the per-tool budget — we never
    silently drop a tool result.
    """
    if not isinstance(result, str):
        return result

    if not result:
        return _render_empty(tool_name)

    try:
        return _materialize_to_file(
            result,
            tool_name=tool_name,
            agent_id=agent_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    except Exception as exc:
        logger.warning(
            f"[tool_output_store] force_materialize failed tool={tool_name} "
            f"err={type(exc).__name__}: {exc}; falling back to inline shape"
        )
        budget = budget_for(tool_name)
        bound = int(budget) if budget != float("inf") else 50_000
        shaped, _ = shape_tool_result(result, bound)
        return shaped


# ─────────────────────────────────────────────────────────────────────────────
# Message-level (cross-tool) budget enforcement
# ─────────────────────────────────────────────────────────────────────────────

MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 240_000
MSG_BUDGET_ENV_OVERRIDE = "CLAWITH_MSG_TOOL_BUDGET"


def _message_budget(default: int = MAX_TOOL_RESULTS_PER_MESSAGE_CHARS) -> int:
    """Return the per-message tool-result char budget, honoring env override."""
    override = os.environ.get(MSG_BUDGET_ENV_OVERRIDE)
    if override:
        try:
            return int(override)
        except ValueError:
            logger.warning(
                f"[tool_output_store] invalid {MSG_BUDGET_ENV_OVERRIDE}={override!r}, ignoring"
            )
    return default


def _tool_message_size(msg) -> int:
    """Character size a tool message contributes to the message-level budget.

    Only string content counts. Vision list[dict] payloads are excluded
    (base64 image bytes have their own transport cost and are not
    re-materializable into text files anyway).
    """
    if getattr(msg, "role", None) != "tool":
        return 0
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return len(content)
    return 0


def _tool_name_for_call_id(api_messages, tool_idx: int, tool_call_id: str) -> str:
    """Look up the tool name for a tool message by scanning backwards.

    Walks ``api_messages[:tool_idx]`` in reverse until an assistant
    message carrying ``tool_calls`` containing ``tool_call_id`` is
    found. Returns ``"unknown"`` when no match (e.g. pathological
    history with a tool message unpaired from its assistant).
    """
    if not tool_call_id:
        return "unknown"
    for i in range(tool_idx - 1, -1, -1):
        m = api_messages[i]
        if getattr(m, "role", None) != "assistant":
            continue
        tcs = getattr(m, "tool_calls", None) or []
        for tc in tcs:
            if tc.get("id") == tool_call_id:
                fn = tc.get("function") or {}
                name = fn.get("name")
                if name:
                    return name
        # Stop at the first assistant message scanned — tool messages
        # always follow their own round's assistant.
        break
    return "unknown"


def enforce_message_budget(
    api_messages: list,
    *,
    fresh_start_idx: int,
    agent_id,
    session_id: str,
    max_chars: int | None = None,
) -> None:
    """Keep the total tool-message char count across ``api_messages`` under
    ``max_chars`` by force-materializing the largest fresh inline tool
    messages until we are within budget.

    Strategy:
      1. Sum ``len(content)`` across *all* tool messages with string
         content (budget is a global ceiling on the current dispatch).
      2. While over budget, pick the LARGEST fresh (index >=
         ``fresh_start_idx``) tool message whose content is a plain
         string AND does not already contain a ``<persisted-output>``
         block, force-materialize it, and replace the ``LLMMessage`` in
         place (new instance — no in-place attribute mutation).
      3. Stop when we are under budget OR no more candidates exist.

    Append-only invariant: ``api_messages[:fresh_start_idx]`` is NEVER
    mutated. Those are historical messages — changing them would
    invalidate the prefix cache. If we run out of fresh candidates while
    still over budget, we log a warning and return.

    Vision list-content tool messages are excluded from both the size
    calculation and the materialization candidate pool.
    """
    if max_chars is None:
        max_chars = _message_budget()

    def _total() -> int:
        return sum(_tool_message_size(m) for m in api_messages)

    total = _total()
    if total <= max_chars:
        return

    # Build (size, index) list over fresh inline tool candidates.
    def _fresh_candidates() -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for i in range(fresh_start_idx, len(api_messages)):
            m = api_messages[i]
            if getattr(m, "role", None) != "tool":
                continue
            c = getattr(m, "content", None)
            if not isinstance(c, str):
                continue
            if PERSISTED_OPEN in c:
                continue
            out.append((len(c), i))
        # Largest first.
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    # LLMMessage import is local to avoid a cycle at module import time.
    from .client import LLMMessage

    while total > max_chars:
        candidates = _fresh_candidates()
        if not candidates:
            logger.warning(
                f"[tool_output_store] message budget still exceeded after "
                f"exhausting fresh candidates: total={total} cap={max_chars} "
                f"fresh_start_idx={fresh_start_idx}"
            )
            return

        _, idx = candidates[0]
        orig = api_messages[idx]
        content_str = orig.content  # guaranteed str by candidate filter
        tool_call_id = getattr(orig, "tool_call_id", "") or ""
        tool_name = _tool_name_for_call_id(api_messages, idx, tool_call_id)

        new_content = force_materialize_tool_output(
            content_str,
            tool_name=tool_name,
            agent_id=agent_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )

        # Replace with a fresh LLMMessage — never mutate the existing
        # instance. Even though this entry is "fresh" for this round,
        # mutation would be a landmine if callers ever reorder the
        # assignment.
        api_messages[idx] = LLMMessage(
            role=orig.role,
            content=new_content,
            tool_calls=orig.tool_calls,
            tool_call_id=orig.tool_call_id,
            reasoning_content=orig.reasoning_content,
            reasoning_signature=orig.reasoning_signature,
        )

        new_total = _total()
        logger.info(
            f"[tool_output_store] message budget: materialized fresh idx={idx} "
            f"tool={tool_name} shrink={len(content_str)}->{len(new_content)} "
            f"total={total}->{new_total} cap={max_chars}"
        )
        # Forward progress guard: if total did not decrease, bail to
        # avoid a tight loop on a pathological materialize output.
        if new_total >= total:
            logger.warning(
                f"[tool_output_store] message budget: no progress after "
                f"materialize idx={idx}; stopping"
            )
            return
        total = new_total
