"""Build the CLI shell-function injection block for the aio-sandbox.

Each type='cli' tool becomes one bash function injected into every
shell exec for the agent's sandbox session:

    svc() { YYBPC_CLI_USER_PHONE='138...' YYBPC_CLI_HOME='/data/cli_state/...' \
            '/data/cli_binaries/<tenant>/<tool_id>/<sha256>.bin' "$@"; }

Why command-prefix assignments instead of `export`:
  * they take precedence over any same-name variable the agent exported
    in the session — the platform-injected identity always wins;
  * they live only in the binary's process env, invisible to `env`
    in the surrounding shell.

Paths need no translation: the sandbox container mounts the same named
volumes at the same paths as the backend (/data/cli_binaries ro,
/data/cli_state rw).

Split: pure functions (quote/render/function text) are unit-tested;
the DB-touching builder lives in agent_tools (caller side).

Note: the rendered function text may span multiple physical lines when an env
value contains a newline, but it is still a single valid bash function
definition; callers must inject it as a whole block and must not filter by line.
"""

from __future__ import annotations

import re

from app.services.cli_tools.placeholders import PlaceholderContext, resolve

# bash function names: keep it conservative (no dashes — POSIX-safe).
_FUNC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Env entries referencing these roots are identity-scoped: when the caller
# supplied no user context (placeholder resolves to itself) they are skipped
# so the CLI sees no identity at all (and reports its own NOT_LOGGED_IN)
# instead of a fake empty one. The caller (build_cli_inject_prefix) decides
# the user context from the call origin — web/IM = conversation user,
# trigger/cron = agent creator, A2A consult = source agent owner — so an
# autonomous run is normally creator-bound, not identity-less.
_IDENTITY_ROOTS = ("$user.", "$state.")


def shell_quote(value: str) -> str:
    """Single-quote `value` for bash, escaping embedded single quotes."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def render_env(env: dict[str, str], ctx: PlaceholderContext) -> dict[str, str]:
    """Resolve placeholder values; drop identity entries with no context.

    An identity-prefixed entry ($user./$state.) is dropped when its value does
    not resolve (token comes back unchanged) — e.g. no user in context, or a
    typo'd field.
    """
    out: dict[str, str] = {}
    for key, raw in env.items():
        resolved = resolve(raw, ctx)
        if resolved == raw and any(raw.startswith(p) for p in _IDENTITY_ROOTS):
            continue
        out[key] = resolved
    return out


def build_cli_function(*, name: str, binary_path: str, env: dict[str, str]) -> str:
    """Render one bash function exposing `binary_path` as command `name`."""
    if not _FUNC_NAME_RE.fullmatch(name):
        raise ValueError(f"unsafe CLI function name: {name!r}")
    for key in env:
        if not _FUNC_NAME_RE.fullmatch(key):
            raise ValueError(f"unsafe env key: {key!r}")
    assigns = " ".join(f"{k}={shell_quote(v)}" for k, v in env.items())
    prefix = f"{assigns} " if assigns else ""
    return f'{name}() {{ {prefix}{shell_quote(binary_path)} "$@"; }}'
