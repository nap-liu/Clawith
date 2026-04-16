"""Whitelist-based placeholder substitution for CLI-tool args and env values.

Allowed placeholders (all single-dotted):
  {user.id}, {user.phone}, {user.email}
  {agent.id}
  {tenant.id}
  {params.<name>}  — where <name> is a key present in the caller params

A literal `{{` renders as `{`, `}}` as `}` (doubled-brace escape).
Anything else in braces raises InvalidPlaceholderError.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WHITELIST: dict[str, set[str]] = {
    "user": {"id", "phone", "email"},
    "agent": {"id"},
    "tenant": {"id"},
}

# Matches one placeholder `{root.key}` — doubled braces are pre-escaped before.
_PLACEHOLDER_RE = re.compile(r"\{([a-z]+)\.([a-z_][a-z0-9_]*)\}")


class InvalidPlaceholderError(ValueError):
    """Raised when a template uses a placeholder outside the whitelist."""


@dataclass(frozen=True)
class PlaceholderContext:
    user: dict[str, str] = field(default_factory=dict)
    agent: dict[str, str] = field(default_factory=dict)
    tenant: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)


def render(template: str, ctx: PlaceholderContext) -> str:
    """Substitute whitelisted placeholders in `template`."""
    # Protect doubled braces, then substitute, then unprotect.
    protected = template.replace("{{", "\x00OPEN\x00").replace("}}", "\x00CLOSE\x00")

    def _sub(match: re.Match[str]) -> str:
        root, key = match.group(1), match.group(2)
        if root == "params":
            if key not in ctx.params:
                raise InvalidPlaceholderError(f"params.{key} not provided")
            return ctx.params[key]
        allowed = _WHITELIST.get(root)
        if allowed is None or key not in allowed:
            raise InvalidPlaceholderError(f"{root}.{key} is not a recognised placeholder")
        values = getattr(ctx, root)
        if key not in values:
            raise InvalidPlaceholderError(f"{root}.{key} not provided in context")
        return values[key]

    substituted = _PLACEHOLDER_RE.sub(_sub, protected)
    return substituted.replace("\x00OPEN\x00", "{").replace("\x00CLOSE\x00", "}")
