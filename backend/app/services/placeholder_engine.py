"""${root.field} placeholder engine — pure functions, no DB / no HTTP.

Used by the MCP server config flow to interpolate platform-provided
identity (user.id, agent.name, ...) into URLs, headers, api keys,
and prompt-block text. CLI tools migrate to this engine in P4.
"""

from __future__ import annotations

import json
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


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render(
    template: str,
    ctx: PlaceholderContext,
    allowed_roots: frozenset[str] = ALL_ROOTS,
    *,
    on_unknown: Literal["raise", "keep_literal"] = "raise",
) -> str:
    """Replace every ``${root.field}`` in *template* using *ctx*.

    * ``root`` not in ``allowed_roots`` → DisallowedPlaceholderError
    * ``root.field`` resolves to None and ``on_unknown='raise'`` →
      UnknownPlaceholderError
    * ``on_unknown='keep_literal'`` → unresolved tokens stay as
      literal ``${root.field}`` in the output
    """

    def repl(m: re.Match[str]) -> str:
        root, field_name = m.group(1), m.group(2)
        if root not in allowed_roots:
            raise DisallowedPlaceholderError(
                f"placeholder root '{root}' not allowed here "
                f"(allowed: {sorted(allowed_roots)})"
            )
        value = ctx.lookup(root, field_name)
        if value is None:
            if on_unknown == "raise":
                raise UnknownPlaceholderError(f"placeholder ${{{root}.{field_name}}} unresolved")
            return m.group(0)  # keep literal
        return _stringify(value)

    return _TOKEN_RE.sub(repl, template)


def render_dict(
    data: dict[str, Any],
    ctx: PlaceholderContext,
    allowed_roots: frozenset[str] = ALL_ROOTS,
    *,
    on_unknown: Literal["raise", "keep_literal"] = "raise",
) -> dict[str, Any]:
    """Render every string value in *data*. Non-strings pass through."""
    return {
        k: render(v, ctx, allowed_roots, on_unknown=on_unknown) if isinstance(v, str) else v
        for k, v in data.items()
    }


def detect_used_roots(template: str) -> set[str]:
    """Static scan: return set of root names appearing in *template*."""
    return {m.group(1) for m in _TOKEN_RE.finditer(template)}
