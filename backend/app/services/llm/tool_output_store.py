"""Tool output materialization.

Large tool results are written to a per-agent, per-session file under
`.tool_results/` in the agent workspace. The LLM sees a compact
`<persisted-output>` block with a short preview plus a file reference,
and can call `read_file` (or other workspace tools) to fetch the rest
on demand.

This is the single shaping path for tool output. DB persistence, LLM
context replay, and the frontend all observe the same string — the
"llm_view" produced here. Downstream code never re-shapes it, so the
messages sequence becomes append-only (good for prompt caching).

The module intentionally does only two things:
  1. Decide inline vs. materialize for a given (tool, result). If the
     configured backend has no locally readable write path, keep a bounded
     inline result instead of claiming an unreadable file was saved.
  2. Produce a deterministic llm_view string.

Everything else (caching, budgets, message layout) is layered above it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass

from loguru import logger

from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.storage import get_storage_backend
from app.services.tool_result_paths import (
    sanitize_tool_result_component,
    tool_result_session_dir,
)

PERSISTED_OPEN = "<persisted-output>"
PERSISTED_CLOSE = "</persisted-output>"
MIN_PERSISTED_VIEW_CHARS = 1_024

# One platform-wide inline ceiling keeps tool behavior predictable and leaves
# room for the system prompt, recent turns, and model output. 32K characters
# covers ordinary command/search/read results; larger payloads remain lossless
# through persisted-output and targeted follow-up reads.
DEFAULT_TOOL_OUTPUT_MAX_CHARS = 32_000

ENV_OVERRIDE = "CLAWITH_TOOL_OUTPUT_MAX_CHARS"
_SAVED_PATH_RE = re.compile(r"Full output saved to:\s*([^\s<>]+)", re.IGNORECASE)
_TRUNCATION_LINE_RE = re.compile(r"^TRUNCATED:[^\n]*", re.MULTILINE)
_INLINE_PREVIEW_RE = re.compile(
    r"Inline preview:\n(?P<preview>.*?)\n\.\.\.\[truncated; read the saved file for the remainder\]\.\.\.",
    re.DOTALL,
)

def _default_budget() -> int:
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        try:
            value = int(override)
            if value >= MIN_PERSISTED_VIEW_CHARS:
                return value
            logger.warning(
                f"[tool_output_store] {ENV_OVERRIDE}={value} is too small to include "
                f"truncation metadata and a readable path; ignoring"
            )
        except ValueError:
            logger.warning(f"[tool_output_store] invalid {ENV_OVERRIDE}={override!r}, ignoring")
    return DEFAULT_TOOL_OUTPUT_MAX_CHARS


def budget_for(tool_name: str) -> int | float:
    """Return the normalized tool-output budget, honoring one env override."""
    return _default_budget()


def _sanitize(name: str) -> str:
    """Make a string safe for use as a filename component."""
    return sanitize_tool_result_component(name)


class ToolOutputMaterializationError(RuntimeError):
    """The full result could not be saved to a path readable by agent tools."""


class ToolOutputBudgetExceeded(RuntimeError):
    """A fresh tool round cannot fit beneath the configured hard ceiling."""


@dataclass(frozen=True)
class MaterializedToolOutput:
    llm_view: str
    relative_path: str
    storage_key: str


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
    result: str,
    max_view_chars: int,
) -> str:
    bounded_read_hint = (
        "For ordinary multi-line text, use read_file with a small line range. "
        if tool_name != "read_file"
        else ""
    )
    limit = max(MIN_PERSISTED_VIEW_CHARS, int(max_view_chars))

    def _compose(preview_chars: int) -> str:
        preview = result[:preview_chars]
        return (
            f"{PERSISTED_OPEN}\n"
            f"TRUNCATED: original output contains {len(result):,} characters "
            f"({_format_size(size_bytes)}). The inline view retains the first "
            f"{len(preview):,} characters within the {limit:,}-character limit.\n"
            f"Full output saved to: {rel_path}\n\n"
            f"Inline preview:\n{preview}\n...[truncated; read the saved file for the remainder]...\n\n"
            f"{bounded_read_hint}Use grep/search_files for targeted lookup, or execute_code_aio to "
            f"process the referenced output or original source file directly. "
            f"Keep code output bounded to summaries, validation results, and file paths; "
            f"do not read the full bulk output back into chat.\n"
            f"{PERSISTED_CLOSE}"
        )

    # Metadata/path length varies, so calculate the largest preview that keeps
    # the complete LLM view within the configured ceiling.
    preview_chars = min(len(result), limit)
    for _ in range(4):
        rendered = _compose(preview_chars)
        overflow = len(rendered) - limit
        if overflow <= 0:
            return rendered
        preview_chars = max(0, preview_chars - overflow)
    rendered = _compose(preview_chars)
    if len(rendered) > limit:
        raise ValueError(f"persisted output metadata exceeds view limit ({len(rendered)} > {limit})")
    return rendered


def _shrink_persisted_view(view: str, *, max_view_chars: int) -> str | None:
    """Shrink an existing envelope without touching its full-output object."""
    if not view.startswith(PERSISTED_OPEN) or PERSISTED_CLOSE not in view:
        return None
    path_match = _SAVED_PATH_RE.search(view)
    truncation_match = _TRUNCATION_LINE_RE.search(view)
    if path_match is None or truncation_match is None:
        return None
    preview_match = _INLINE_PREVIEW_RE.search(view)
    preview_source = preview_match.group("preview") if preview_match else ""
    limit = max(MIN_PERSISTED_VIEW_CHARS, int(max_view_chars))
    original_descriptor = truncation_match.group(0).split(". The inline view", 1)[0].rstrip(".")

    def _compose(preview_chars: int) -> str:
        return (
            f"{PERSISTED_OPEN}\n"
            f"{original_descriptor}. This re-rendered inline view retains the first "
            f"{preview_chars:,} characters within the {limit:,}-character limit.\n"
            f"Full output saved to: {path_match.group(1)}\n\n"
            f"Inline preview:\n{preview_source[:preview_chars]}\n"
            "...[truncated; read the saved file for the remainder]...\n\n"
            "Use read_file with a small line range or grep/search_files on the saved path. "
            "Do not load the full bulk output back into chat.\n"
            f"{PERSISTED_CLOSE}"
        )

    preview_chars = min(len(preview_source), limit)
    for _ in range(4):
        rendered = _compose(preview_chars)
        overflow = len(rendered) - limit
        if overflow <= 0:
            return rendered
        preview_chars = max(0, preview_chars - overflow)
    rendered = _compose(preview_chars)
    return rendered if len(rendered) <= limit else None


async def materialize_tool_output_strict(
    result: str,
    *,
    tool_name: str,
    agent_id,
    session_id: str,
    tool_call_id: str,
    max_view_chars: int,
) -> MaterializedToolOutput:
    """Persist every byte through the configured storage backend.

    The returned path is the same agent-relative path accepted by ``read_file``.
    Failure is explicit: callers must never replace a durable result with an
    inline truncation that has no readable recovery path.
    """
    if not agent_id:
        raise ToolOutputMaterializationError("missing agent_id")

    ext = "json" if _looks_like_json(result) else "txt"
    tc_id = (_sanitize(tool_call_id) or "call")[:60]
    # Provider call IDs are not guaranteed unique across rounds. Content-
    # addressed identity prevents cross-round overwrite while making retries
    # idempotent instead of leaving a new random orphan after every attempt.
    materialization_id = hashlib.sha256(result.encode("utf-8")).hexdigest()[:12]
    filename = f"{_sanitize(tool_name)}_{tc_id}_{materialization_id}.{ext}"
    rel_path = (tool_result_session_dir(session_id) / filename).as_posix()
    storage_key = current_agent_runtime_workspace(agent_id).storage_key(rel_path)
    # Render first.  A malformed/oversized metadata envelope must fail before
    # the durable write, otherwise the caller sees an exception while an
    # unreferenced object is left behind in local/S3 storage.
    view = _render_persisted(
        tool_name=tool_name,
        rel_path=rel_path,
        size_bytes=len(result.encode("utf-8")),
        result=result,
        max_view_chars=max_view_chars,
    )
    storage = get_storage_backend()
    try:
        if await storage.exists(storage_key):
            existing = await storage.read_text(
                storage_key,
                encoding="utf-8",
                errors="strict",
            )
            if existing != result:
                raise ToolOutputMaterializationError(
                    "content-addressed tool output path contains different bytes"
                )
            logger.info(
                f"[tool_output_store] reused materialized tool={tool_name} "
                f"size={len(result)} path={rel_path}"
            )
            return MaterializedToolOutput(
                llm_view=view,
                relative_path=rel_path,
                storage_key=storage_key,
            )
    except ToolOutputMaterializationError:
        raise
    except Exception as exc:
        raise ToolOutputMaterializationError(
            f"failed to verify existing tool output: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        await storage.write_text(storage_key, result, encoding="utf-8")
    except BaseException as exc:
        # Never delete a content-addressed target here. Another retry/process
        # may already have committed the same stable object and an unconditional
        # cleanup would destroy an older history row's recovery path. Failed
        # writes are never referenced; a later retry verifies exact bytes.
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ToolOutputMaterializationError(
            f"failed to persist full tool output: {type(exc).__name__}: {exc}"
        ) from exc
    logger.info(
        f"[tool_output_store] materialized tool={tool_name} "
        f"size={len(result)} path={rel_path}"
    )
    return MaterializedToolOutput(
        llm_view=view,
        relative_path=rel_path,
        storage_key=storage_key,
    )


async def finalize_tool_output(
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

    Writes to the configured agent workspace when the result exceeds the
    per-tool budget. Persistence failure raises instead of manufacturing an
    unrecoverable truncation without a readable path.
    """
    if not isinstance(result, str):
        return result

    if not result:
        return _render_empty(tool_name)

    budget = budget_for(tool_name)
    if budget == float("inf") or len(result) <= budget:
        return result

    materialized = await materialize_tool_output_strict(
        result,
        tool_name=tool_name,
        agent_id=agent_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        max_view_chars=int(budget),
    )
    return materialized.llm_view


async def force_materialize_tool_output(
    result: str,
    *,
    tool_name: str,
    agent_id,
    session_id: str,
    tool_call_id: str,
    max_view_chars: int | None = None,
) -> str:
    """Always materialize to disk regardless of tool budget.

    Unlike :func:`finalize_tool_output` which checks the normalized budget
    first, this is the escape hatch used by the message-level enforcer
    when the sum of multiple in-budget results blows past the message
    cap.

    Same storage layout and ``<persisted-output>`` format. Failure is strict so
    a caller cannot mistake an old nested envelope for this write's success.
    """
    if not isinstance(result, str):
        return result

    if not result:
        return _render_empty(tool_name)

    budget = budget_for(tool_name)
    bound = int(budget) if budget != float("inf") else DEFAULT_TOOL_OUTPUT_MAX_CHARS
    view_limit = max_view_chars if max_view_chars is not None else bound

    materialized = await materialize_tool_output_strict(
        result,
        tool_name=tool_name,
        agent_id=agent_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        max_view_chars=view_limit,
    )
    return materialized.llm_view


# ─────────────────────────────────────────────────────────────────────────────
# Message-level (cross-tool) budget enforcement
# ─────────────────────────────────────────────────────────────────────────────

MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 64_000
MSG_BUDGET_ENV_OVERRIDE = "CLAWITH_MSG_TOOL_BUDGET"


@dataclass(frozen=True)
class ToolOutputRewrite:
    """One fresh tool result changed by the round-level budget enforcer."""

    tool_call_id: str
    final_content: str


def _message_budget(default: int = MAX_TOOL_RESULTS_PER_MESSAGE_CHARS) -> int:
    """Return the per-message tool-result char budget, honoring env override."""
    override = os.environ.get(MSG_BUDGET_ENV_OVERRIDE)
    if override:
        try:
            value = int(override)
            if value >= MIN_PERSISTED_VIEW_CHARS:
                return value
            logger.warning(
                f"[tool_output_store] {MSG_BUDGET_ENV_OVERRIDE}={value} is too small "
                f"for a recoverable persisted-output envelope; ignoring"
            )
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


async def enforce_message_budget(
    api_messages: list,
    *,
    fresh_start_idx: int,
    agent_id,
    session_id: str,
    max_chars: int | None = None,
) -> list[ToolOutputRewrite]:
    """Keep this fresh round's total tool-message characters under
    ``max_chars`` by force-materializing the largest fresh inline tool
    messages until we are within budget.

    Strategy:
      1. Sum ``len(content)`` across fresh tool messages with string
         content (the ceiling is independent for every tool-call round).
      2. While over budget, pick the LARGEST fresh (index >=
         ``fresh_start_idx``) tool message whose content is a plain
         string AND does not already contain a ``<persisted-output>``
         block, force-materialize it, and replace the ``LLMMessage`` in
         place (new instance — no in-place attribute mutation).
      3. Stop only when the hard postcondition is satisfied. If even minimal
         recoverable envelopes cannot fit, raise before provider dispatch.

    Append-only invariant: ``api_messages[:fresh_start_idx]`` is NEVER
    mutated. Those are historical messages — changing them would
    invalidate the prefix cache. If we run out of fresh candidates while
    still over budget, fail closed rather than treating the ceiling as advice.

    Vision list-content tool messages are excluded from both the size
    calculation and the materialization candidate pool. The returned rewrite
    records let the caller reconcile the already-durable done rows before the
    next provider dispatch.
    """
    if max_chars is None:
        max_chars = _message_budget()

    def _total() -> int:
        return sum(_tool_message_size(m) for m in api_messages[fresh_start_idx:])

    total = _total()
    if total <= max_chars:
        return []

    rewrites: list[ToolOutputRewrite] = []
    rewritten_indices: set[int] = set()

    # Build (size, index) list over fresh inline tool candidates.
    def _fresh_candidates() -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for i in range(fresh_start_idx, len(api_messages)):
            if i in rewritten_indices:
                continue
            m = api_messages[i]
            if getattr(m, "role", None) != "tool":
                continue
            c = getattr(m, "content", None)
            if not isinstance(c, str):
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
            raise ToolOutputBudgetExceeded(
                "fresh tool results cannot fit the message budget after "
                f"lossless materialization: total={total} cap={max_chars} "
                f"fresh_start_idx={fresh_start_idx}"
            )

        _, idx = candidates[0]
        orig = api_messages[idx]
        content_str = orig.content  # guaranteed str by candidate filter
        tool_call_id = getattr(orig, "tool_call_id", "") or ""
        tool_name = _tool_name_for_call_id(api_messages, idx, tool_call_id)

        # Retain as much of this result as the remaining cumulative capacity
        # permits. The rendered envelope, explicit truncation notice, path and
        # preview together stay within this target.
        remaining_without_candidate = total - len(content_str)
        target_view_chars = max(
            MIN_PERSISTED_VIEW_CHARS,
            min(
                int(budget_for(tool_name)),
                max_chars - remaining_without_candidate,
            ),
        )
        new_content = _shrink_persisted_view(
            content_str,
            max_view_chars=target_view_chars,
        )
        if new_content is None:
            new_content = await force_materialize_tool_output(
                content_str,
                tool_name=tool_name,
                agent_id=agent_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                max_view_chars=target_view_chars,
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
        rewrites.append(
            ToolOutputRewrite(
                tool_call_id=tool_call_id,
                final_content=new_content,
            )
        )
        rewritten_indices.add(idx)

        new_total = _total()
        logger.info(
            f"[tool_output_store] message budget: materialized fresh idx={idx} "
            f"tool={tool_name} shrink={len(content_str)}->{len(new_content)} "
            f"total={total}->{new_total} cap={max_chars}"
        )
        # Forward progress guard: if total did not decrease, bail to
        # avoid a tight loop on a pathological materialize output.
        if new_total >= total:
            raise ToolOutputBudgetExceeded(
                "tool-result materialization made no budget progress: "
                f"idx={idx} total={total} cap={max_chars}"
            )
        total = new_total

    if total > max_chars:  # defensive assertion for future loop changes
        raise ToolOutputBudgetExceeded(
            f"fresh tool results exceed hard budget: total={total} cap={max_chars}"
        )
    return rewrites
