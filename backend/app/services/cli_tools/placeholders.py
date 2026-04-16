"""Placeholder resolution for CLI-tool env values and args.

Project convention (matches the pre-M2 cli_tool_executor):
an env value or args entry that is a single `$name.field` token is
replaced wholesale with the context value; anything else passes through
literally. No template interpolation, no braces.

Recognised tokens:
  $user.id, $user.phone, $user.email
  $agent.id, $tenant.id
  $params.<name>
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlaceholderContext:
    user: dict[str, str] = field(default_factory=dict)
    agent: dict[str, str] = field(default_factory=dict)
    tenant: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)

    def lookup(self, key: str) -> str | None:
        """Resolve `user.phone` / `agent.id` / `params.<name>`. Returns None if unknown."""
        if "." not in key:
            return None
        root, field_name = key.split(".", 1)
        src: dict[str, Any]
        if root == "user":
            src = self.user
        elif root == "agent":
            src = self.agent
        elif root == "tenant":
            src = self.tenant
        elif root == "params":
            src = self.params
        else:
            return None
        value = src.get(field_name)
        return str(value) if value is not None else None


def resolve(value: str, ctx: PlaceholderContext) -> str:
    """Resolve a single value using the `$root.field` convention.

    If `value` is the exact token `$root.field` and the context has a
    mapping for it, the replacement string is returned. Otherwise the
    original value is returned unchanged (used verbatim).
    """
    if not isinstance(value, str):
        return value
    if value.startswith("$") and len(value) > 1:
        resolved = ctx.lookup(value[1:])
        if resolved is not None:
            return resolved
    return value
