"""Build the CLI-tool sandbox injection (wrappers + identity env).

Each type='cli' tool (e.g. `svc`) is exposed to the agent's sandbox as a real
command on PATH via an **identity-agnostic wrapper script**:

    /home/gem/.local/bin/svc  ->  #!/bin/sh
                                   exec '/data/cli_binaries/<...>/<sha>.bin' "$@"

The wrapper carries NO identity — it just execs the binary, inheriting whatever
env its caller has. Identity (phone / tokens / state dir) is injected per-exec
as environment variables scoped to that single execution:

  * bash: exported inside the per-exec child shell (never the persistent
    session) so it cannot leak into another call / conversation (no 串台);
  * python: `os.environ.update(...)` prepended to the user's code.

The wrapper lives in the sandbox user's own ``~/.local/bin`` (resolved at runtime
from ``$HOME`` / ``expanduser`` — never a hardcoded path), which is on PATH for
both the shell and the jupyter kernel. So the agent can use `svc` transparently
from bash, pipes, `subprocess.run(['svc'])`, xargs, etc. — as it would expect.

Trust model (current): identity rides on inheritable env, so a malicious agent
could re-export it. Accepted for now (trusted agent); hardening = per-conversation
signed token, tracked separately.

Paths need no translation: backend and sandbox mount the same named volumes at
the same paths (/data/cli_binaries ro, /data/cli_state rw).

Split: pure rendering functions here (unit-tested); the DB-touching builder
(`build_cli_injection`) lives in agent_tools.
"""

from __future__ import annotations

import base64
import json
import re

from app.services.cli_tools.placeholders import PlaceholderContext, resolve

# Shell-identifier-safe names (no dashes) — used for both CLI tool/wrapper names
# and env keys (so they're safe as `export KEY=` and as a filename on PATH).
_FUNC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Wrapper scripts go in the sandbox user's own ~/.local/bin, resolved at runtime
# (bash: $HOME; python: expanduser) — NOT a hardcoded path. That dir is on PATH
# for the conventional pip-user layout, used by both the shell and jupyter.
_WRAPPER_BIN_REL = ".local/bin"

# Env entries referencing these roots are identity-scoped: when the caller
# supplied no user context (placeholder resolves to itself) they are skipped
# so the CLI sees no identity at all (and reports its own NOT_LOGGED_IN)
# instead of a fake empty one. The caller (build_cli_injection) decides the
# user context from the call origin — web/IM = conversation user, trigger/cron
# = agent creator, A2A consult = source agent owner.
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


def _validate_name(name: str) -> None:
    if not _FUNC_NAME_RE.fullmatch(name):
        raise ValueError(f"unsafe CLI tool name: {name!r}")


def _validate_env_keys(env: dict[str, str]) -> None:
    for key in env:
        if not _FUNC_NAME_RE.fullmatch(key):
            raise ValueError(f"unsafe env key: {key!r}")


def build_wrapper_write_sh(*, name: str, binary_path: str) -> str:
    """Bash that (re)writes the identity-agnostic PATH wrapper for one tool.

    Writes to ``$HOME/.local/bin/<name>`` — ``$HOME`` is resolved by the sandbox
    at runtime (no hardcoded path). Must run while ``$HOME`` is still the sandbox
    user's native home (i.e. before any HOME reset), so the wrapper lands in the
    dir that's on PATH for both the shell and jupyter. Idempotent — safe every
    exec (cheap; also picks up a new binary version). Content is base64'd to
    avoid quoting pitfalls; ``name`` is validated so it's safe inside the path.
    """
    _validate_name(name)
    content = f'#!/bin/sh\nexec {shell_quote(binary_path)} "$@"\n'
    b64 = base64.b64encode(content.encode()).decode()
    bindir = f'"$HOME/{_WRAPPER_BIN_REL}"'
    return (
        f"mkdir -p {bindir} && "
        f"echo {b64} | base64 -d > {bindir}/{name} && "
        f"chmod 755 {bindir}/{name}"
    )


def build_env_exports_sh(env: dict[str, str]) -> str:
    """`export K='v' ...` for the per-exec identity env (empty string if none)."""
    _validate_env_keys(env)
    if not env:
        return ""
    assigns = " ".join(f"{k}={shell_quote(v)}" for k, v in env.items())
    return f"export {assigns}"


def build_python_prelude(wrappers: list[dict], env: dict[str, str]) -> str:
    """Python prepended to a python exec: write the PATH wrappers + set os.environ.

    Makes `subprocess.run(['svc', ...])` (and any child process) in jupyter find
    `svc` on PATH and inherit the identity env. Uses `_`-prefixed names so it
    won't clash with the user's code.
    """
    _validate_env_keys(env)
    lines = [
        "import os as _os, base64 as _b64",
        f"_bindir = _os.path.expanduser({('~/' + _WRAPPER_BIN_REL)!r})",
        "_os.makedirs(_bindir, exist_ok=True)",
        "_os.environ['PATH'] = _bindir + ':' + _os.environ.get('PATH', '')",
    ]
    for w in wrappers:
        name = w["name"]
        _validate_name(name)
        content = f'#!/bin/sh\nexec {shell_quote(w["binary_path"])} "$@"\n'
        b64 = base64.b64encode(content.encode()).decode()
        lines.append(f"_p = _os.path.join(_bindir, {name!r})")
        lines.append(f"open(_p, 'wb').write(_b64.b64decode({b64!r}))")
        lines.append("_os.chmod(_p, 0o755)")
    lines.append(f"_os.environ.update({json.dumps(env)})")
    return "\n".join(lines)
