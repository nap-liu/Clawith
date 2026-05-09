"""${root.field} placeholder engine — pure functions, no DB / no HTTP.

Used by the MCP server config flow to interpolate platform-provided
identity (user.id, agent.name, ...) into URLs, headers, api keys,
and prompt-block text. CLI tools migrate to this engine in P4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


PROMPT_SAFE_ROOTS = frozenset({"agent", "tenant", "session", "channel"})
ALL_ROOTS = frozenset({"user", "agent", "tenant", "session", "channel", "params"})

_TOKEN_RE = re.compile(r"\$\{([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\}")


class PlaceholderError(Exception):
    """Base for engine errors."""


class DisallowedPlaceholderError(PlaceholderError):
    """Token's root is not in allowed_roots for this rendering site."""


class UnknownPlaceholderError(PlaceholderError):
    """Token's root.field could not be resolved from the context."""


@dataclass(frozen=True)
class PlaceholderContext:
    user: dict[str, Any] = field(default_factory=dict)
    agent: dict[str, Any] = field(default_factory=dict)
    tenant: dict[str, Any] = field(default_factory=dict)
    session: dict[str, Any] = field(default_factory=dict)
    channel: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def lookup(self, root: str, field_name: str) -> Any:
        bucket = getattr(self, root, None)
        if not isinstance(bucket, dict):
            return None
        return bucket.get(field_name)
