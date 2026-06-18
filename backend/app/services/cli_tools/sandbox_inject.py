"""Build the CLI-tool sandbox injection (identity-carrying PATH wrappers).

Each type='cli' tool (e.g. `svc`) is exposed to the agent's sandbox as a real
command on PATH via a wrapper script that carries the caller's identity as an
``env`` prefix on the exec line:

    <bindir>/svc  ->  #!/bin/sh
                      exec env YYBPC_CLI_USER_PHONE='138...' YYBPC_CLI_HOME='...' \\
                          '/data/cli_binaries/<...>/<sha>.bin' "$@"

Why identity lives in the WRAPPER, not the session env
------------------------------------------------------
The shell session is per-conversation and persistent (so the agent's own
``export``/``cd`` survive across calls — a documented session semantic). If we
exported identity into that session it would persist too, and in a multi-user
group IM conversation a later sender (or an exec whose injection couldn't be
built) would inherit the previous sender's identity — cross-user impersonation
with zero malice. Putting identity on the wrapper's exec line instead means:

  * the identity is scoped to the binary process (and its pipes / subprocesses),
    never the session env — `env` / logs never show another user's phone;
  * every exec REWRITES the wrapper with the *current* sender's identity, so a
    prior sender's identity cannot persist (correct-by-construction, not
    "re-export wins");
  * fail-safe: if the injection can't be built (DB blip, deleted binary), NO
    wrapper is written → `svc` is simply `command not found`, never run under a
    stale identity.

Per-conversation bindir
-----------------------
The wrapper is written to a per-conversation directory (``bindir``, keyed by a
hash of the session anchor) that the caller prepends to PATH. A per-agent shared
path would race across concurrent conversations (two senders rewriting the same
``svc`` file). The bindir is computed by the backend (which knows the anchor)
and passed in.

Trust model (current): the wrapper file holds the identity in cleartext on disk;
a malicious agent in the sandbox could `cat` it (and historical per-anchor
wrappers, bounded by the LRU). Accepted for the trusted-agent scenario (the
agent can sudo anyway, spec v4 §1.3); what this design closes is *automatic*
cross-user leakage. Hardening = per-conversation signed token, tracked
separately.

Paths need no translation: backend and sandbox mount the same named volumes at
the same paths (/data/cli_binaries ro, /data/cli_state rw).

Split: pure rendering functions here (unit-tested); the DB-touching builder
(`build_cli_injection`) lives in agent_tools.
"""

from __future__ import annotations

import base64
import re

from app.services.cli_tools.placeholders import PlaceholderContext, resolve

# Strict shell-identifier — for ENV KEYS, which become `export KEY=` / `env KEY=`
# on the wrapper exec line and so must be valid shell identifiers (no dashes).
_FUNC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# CLI tool / wrapper / LLM-function names. A hyphen is allowed here (e.g. a tool
# named `my-cli`): it is a valid PATH command + filename, and a valid
# OpenAI/Anthropic/qwen function name (`^[a-zA-Z0-9_-]{1,64}$`). The leading char
# is still restricted so the name can never be parsed as a flag (`-x`).
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# Per-conversation wrapper directories live under this dir in the sandbox user's
# home (resolved at runtime — bash: $HOME; python: expanduser). The leaf is a
# hash of the session anchor (computed by the backend), giving each conversation
# its own `svc` so concurrent conversations don't race on one shared file.
_WRAPPER_BIN_ROOT = ".clawith-bin"

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
    if not _TOOL_NAME_RE.fullmatch(name):
        raise ValueError(f"unsafe CLI tool name: {name!r}")


def _validate_env_keys(env: dict[str, str]) -> None:
    for key in env:
        if not _FUNC_NAME_RE.fullmatch(key):
            raise ValueError(f"unsafe env key: {key!r}")


def _wrapper_content(binary_path: str, env: dict[str, str]) -> str:
    """The wrapper script body: `exec [env K='v'...] '<binary>' "$@"`.

    Identity rides as an ``env`` prefix so it is scoped to the binary process
    (and its pipes / subprocesses), never the shell session. Empty env → a plain
    ``exec`` (identity-less; the binary reports its own NOT_LOGGED_IN).
    """
    _validate_env_keys(env)
    if env:
        assigns = " ".join(f"{k}={shell_quote(v)}" for k, v in env.items())
        exec_line = f"exec env {assigns} {shell_quote(binary_path)} \"$@\""
    else:
        exec_line = f"exec {shell_quote(binary_path)} \"$@\""
    return f"#!/bin/sh\n{exec_line}\n"


def build_wrapper_write_sh(
    *, name: str, binary_path: str, env: dict[str, str], bindir: str
) -> str:
    """Bash that (re)writes one tool's identity-carrying wrapper into ``bindir``.

    ``bindir`` is the per-conversation wrapper dir (already quoted for bash, e.g.
    ``"$HOME/.clawith-bin/<hash>"``) that the caller prepends to PATH. Rewritten
    every exec with the *current* sender's ``env`` so a prior sender's identity
    cannot persist. Content is base64'd to avoid quoting pitfalls; ``name`` and
    every env key are validated so they're safe as a filename / env assignment.
    """
    _validate_name(name)
    b64 = base64.b64encode(_wrapper_content(binary_path, env).encode()).decode()
    return (
        f"mkdir -p {bindir} && "
        f"echo {b64} | base64 -d > {bindir}/{name} && "
        f"chmod 755 {bindir}/{name}"
    )


def build_python_prelude(wrappers: list[dict], bindir: str) -> str:
    """Python prepended to a python exec: write the identity-carrying wrappers
    into ``bindir`` and prepend ``bindir`` to PATH.

    Identity is NOT written to ``os.environ`` — it rides inside each wrapper (env
    prefix), so a persistent kernel's ``os.environ`` can never leak a prior
    sender's identity. ``subprocess.run(['svc', ...])`` finds `svc` on PATH and
    the wrapper supplies the identity. ``bindir`` is an expanduser-style path
    (e.g. ``~/.clawith-bin/<hash>``); ``_``-prefixed locals avoid clashing with
    the user's code.
    """
    lines = [
        "import os as _os, base64 as _b64, shutil as _shutil",
        f"_bindir = _os.path.expanduser({bindir!r})",
        # Clear any PRIOR sender's wrappers first — the wrapper set must reflect
        # THIS exec's sender (no wrappers below → empty dir → svc not found).
        "_shutil.rmtree(_bindir, ignore_errors=True)",
        "_os.makedirs(_bindir, exist_ok=True)",
        "_os.environ['PATH'] = _bindir + ':' + _os.environ.get('PATH', '')",
    ]
    for w in wrappers:
        name = w["name"]
        _validate_name(name)
        content = _wrapper_content(w["binary_path"], w.get("env") or {})
        b64 = base64.b64encode(content.encode()).decode()
        lines.append(f"_p = _os.path.join(_bindir, {name!r})")
        lines.append(f"open(_p, 'wb').write(_b64.b64decode({b64!r}))")
        lines.append("_os.chmod(_p, 0o755)")
    return "\n".join(lines)
