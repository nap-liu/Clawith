"""Canonical agent-relative paths for persisted tool results."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_PATH_COMPONENT_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_tool_result_component(value: str, *, fallback: str = "unnamed") -> str:
    """Return one safe, bounded path component for tool-produced artifacts."""
    cleaned = _PATH_COMPONENT_SAFE.sub("_", str(value or "")).strip("_.")
    return (cleaned or fallback)[:80]


def tool_result_session_dir(session_id: str | None) -> PurePosixPath:
    """Return the canonical agent-relative result directory for one session."""
    session_component = sanitize_tool_result_component(
        str(session_id or "nosession"),
        fallback="nosession",
    )
    return PurePosixPath(".tool_results") / session_component
