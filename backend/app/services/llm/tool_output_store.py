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
    "execute_code": 30_000,
    "run_command": 30_000,
    "bash": 30_000,
    "grep": 20_000,
    "search_files": 20_000,
    "read_file": float("inf"),
    "list_files": 50_000,
    "_default": 50_000,
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
    except Exception as exc:
        logger.warning(
            f"[tool_output_store] materialize failed tool={tool_name} "
            f"err={type(exc).__name__}: {exc}; falling back to inline shape"
        )
        shaped, _ = shape_tool_result(result, int(budget))
        return shaped
