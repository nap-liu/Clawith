"""Placeholder resolution for CLI-tool env values and args.

Project convention (matches the pre-M2 cli_tool_executor):
an env value or args entry that is a single `$name.field` token is
replaced wholesale with the context value; anything else passes through
literally. No template interpolation, no braces.

Recognised tokens:
  $user.id, $user.phone, $user.email
  $agent.id, $tenant.id
  $params.<name>

`$params.<name>` may resolve to either a scalar or a list. Scalars are
used as-is. Lists are meaningful only for the args_template (they
expand into multiple argv entries at the executor layer); environment
variable values are always scalars — if the param is a list, json.dumps
it so the representation is unambiguous.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlaceholderContext:
    user: dict[str, str] = field(default_factory=dict)
    agent: dict[str, str] = field(default_factory=dict)
    tenant: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def lookup(self, key: str) -> Any:
        """Resolve `user.phone` / `agent.id` / `params.<name>`.

        Returns the raw context value (may be str, list, int, …) or
        `None` if the key is unknown. Callers decide how to stringify.
        """
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
        return src.get(field_name)


def _token_name(value: Any) -> str | None:
    """Return `<root>.<field>` if `value` is a bare `$<root>.<field>` token."""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        return value[1:]
    return None


def resolve(value: str, ctx: PlaceholderContext) -> str:
    """Resolve a single value to a string using the `$root.field` convention.

    Used for env values and for non-list args. If the resolved context
    value is a list, it is JSON-dumped — env keys must be strings.
    Unknown tokens pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    name = _token_name(value)
    if name is not None:
        resolved = ctx.lookup(name)
        if resolved is None:
            return value
        if isinstance(resolved, list):
            return json.dumps(resolved, ensure_ascii=False)
        return str(resolved)
    return value


def resolve_args(template: list[str], ctx: PlaceholderContext) -> list[str]:
    """Render an args_template to a flat argv list.

    Rule: a template entry that is a bare token `$params.X` and whose
    context value is a list is expanded in place — each list element
    becomes its own argv. Every other entry goes through `resolve()`
    (scalar string replacement). This is what lets agents drive
    multi-segment CLIs (`git commit -m ...`, `svc report list`, …)
    without a shell.
    """
    out: list[str] = []
    for entry in template:
        name = _token_name(entry)
        if name is not None:
            raw = ctx.lookup(name)
            if isinstance(raw, list):
                out.extend(str(x) for x in raw)
                continue
        out.append(resolve(entry, ctx))
    return out
