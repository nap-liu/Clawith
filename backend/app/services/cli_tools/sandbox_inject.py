"""Render identity-safe CLI launchers for aio-sandbox executions.

CLI commands live in the agent's standard ``$HOME/.local/bin`` directory.  A
launcher is stable across conversations and contains no caller identity.  The
current caller's resolved environment is carried by a short-lived, signed
execution context in a process-local environment variable instead.

This keeps foreground shells, Jupyter kernels and managed background jobs in
the same filesystem/PATH namespace without allowing one sender's identity to
persist into the next execution.  A launcher without a valid context fails
closed with exit code 126.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import get_settings
from app.services.cli_tools.placeholders import PlaceholderContext, resolve

_FUNC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_IDENTITY_ROOTS = ("$user.", "$state.")
_LOCAL_BIN = "$HOME/.local/bin"
_MANAGED_MARKER = "# aio-managed-cli-launcher:v1"
_TOKEN_VERSION = 1


def shell_quote(value: str) -> str:
    """Single-quote ``value`` for bash, escaping embedded single quotes."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def render_env(env: dict[str, str], ctx: PlaceholderContext) -> dict[str, str]:
    """Resolve placeholder values; drop identity entries with no context."""
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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signing_key() -> Ed25519PrivateKey:
    # Deterministic deployment key: backend replicas using the same application
    # secret emit contexts accepted by the same identity-free launcher.
    seed = hashlib.sha256(
        (get_settings().SECRET_KEY + ":aio-cli-context:v1").encode()
    ).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def context_env_name(name: str) -> str:
    """Return the neutral, shell-safe execution-context variable for a tool."""
    _validate_name(name)
    suffix = hashlib.sha256(name.encode()).hexdigest()[:16].upper()
    return f"AIO_CLI_CONTEXT_{suffix}"


def _launcher_content(
    *, name: str, binary_path: str, context_env: str, public_key_b64: str
) -> str:
    """Build a static launcher containing no identity or per-exec secret."""
    _validate_name(name)
    # repr() values are backend-controlled after strict tool-name validation;
    # the result is Python source, not shell source.
    return f'''#!/usr/bin/env python3
{_MANAGED_MARKER}
import base64
import json
import os
import sys
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TOOL = {name!r}
BINARY = {binary_path!r}
CONTEXT_ENV = {context_env!r}
PUBLIC_KEY = {public_key_b64!r}

def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

def _fail(message):
    sys.stderr.write("aio cli: " + message + "\\n")
    raise SystemExit(126)

token = os.environ.get(CONTEXT_ENV)
if not token:
    _fail("valid execution context required for " + TOOL)
try:
    payload_part, signature_part = token.split(".", 1)
    payload = _decode(payload_part)
    Ed25519PublicKey.from_public_bytes(_decode(PUBLIC_KEY)).verify(
        _decode(signature_part), payload
    )
    context = json.loads(payload)
except Exception:
    _fail("invalid execution context for " + TOOL)

now = int(time.time())
if (
    context.get("v") != {_TOKEN_VERSION}
    or context.get("tool") != TOOL
    or context.get("binary") != BINARY
    or not isinstance(context.get("env"), dict)
    or int(context.get("iat", 0)) > now + 60
    or int(context.get("exp", 0)) < now
):
    _fail("expired or mismatched execution context for " + TOOL)

child_env = os.environ.copy()
child_env.pop(CONTEXT_ENV, None)
for key, value in context["env"].items():
    if not isinstance(key, str) or not isinstance(value, str):
        _fail("invalid environment in execution context for " + TOOL)
    child_env[key] = value
os.execve(BINARY, [BINARY, *sys.argv[1:]], child_env)
'''


def prepare_launchers(
    wrappers: list[dict], *, ttl_seconds: int
) -> list[dict[str, str]]:
    """Sign per-exec contexts and return identity-free launcher specs."""
    ttl = max(1, int(ttl_seconds))
    now = int(time.time())
    private_key = _signing_key()
    public_key_b64 = _b64url(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    prepared: list[dict[str, str]] = []
    for wrapper in wrappers:
        name = wrapper["name"]
        binary_path = str(wrapper["binary_path"])
        env = {str(k): str(v) for k, v in (wrapper.get("env") or {}).items()}
        _validate_name(name)
        _validate_env_keys(env)
        context_env = context_env_name(name)
        payload = json.dumps(
            {
                "v": _TOKEN_VERSION,
                "tool": name,
                "binary": binary_path,
                "env": env,
                "iat": now,
                "exp": now + ttl,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        prepared.append(
            {
                "name": name,
                "context_env": context_env,
                "context_token": (
                    f"{_b64url(payload)}.{_b64url(private_key.sign(payload))}"
                ),
                "launcher": _launcher_content(
                    name=name,
                    binary_path=binary_path,
                    context_env=context_env,
                    public_key_b64=public_key_b64,
                ),
            }
        )
    return prepared


def build_launcher_write_sh(launcher: dict[str, str]) -> str:
    """Bash that atomically installs one managed launcher in ``.local/bin``.

    A non-managed file with the same name is never overwritten.  The setup
    fails before user code runs so command resolution cannot silently fall
    through to a different executable.
    """
    name = launcher["name"]
    _validate_name(name)
    content = launcher["launcher"]
    encoded = base64.b64encode(content.encode()).decode()
    target = f'"{_LOCAL_BIN}/{name}"'
    temp = f'"{_LOCAL_BIN}/.{name}.aio-tmp-$$"'
    return (
        f"mkdir -p \"{_LOCAL_BIN}\" && "
        f"if [ -e {target} ] && ! grep -Fqx {_MANAGED_MARKER!r} {target}; then "
        f"echo 'aio cli: refusing to overwrite unmanaged {_LOCAL_BIN}/{name}' >&2; false; "
        f"else echo {encoded} | base64 -d > {temp} && chmod 755 {temp} && mv -f {temp} {target}; fi"
    )


def build_python_execution(
    wrappers: list[dict], code: str, *, ttl_seconds: int
) -> str:
    """Wrap one Jupyter cell with launcher setup and scoped identity context."""
    launchers = prepare_launchers(wrappers, ttl_seconds=ttl_seconds)
    user_code = base64.b64encode(code.encode()).decode()
    lines = [
        "import os as _aio_os, base64 as _aio_b64",
        "_aio_bindir = _aio_os.path.expanduser('~/.local/bin')",
        "_aio_os.makedirs(_aio_bindir, exist_ok=True)",
        "_aio_path = _aio_os.environ.get('PATH', '')",
        "if _aio_bindir not in _aio_path.split(':'):",
        "    _aio_os.environ['PATH'] = _aio_bindir + ':' + _aio_path",
    ]
    for launcher in launchers:
        name = launcher["name"]
        encoded = base64.b64encode(launcher["launcher"].encode()).decode()
        lines.extend(
            [
                f"_aio_target = _aio_os.path.join(_aio_bindir, {name!r})",
                "if _aio_os.path.exists(_aio_target):",
                "    with open(_aio_target, 'r', encoding='utf-8', errors='replace') as _aio_f:",
                f"        if {_MANAGED_MARKER!r} not in _aio_f.read().splitlines():",
                "            raise RuntimeError('aio cli: refusing to overwrite unmanaged ' + _aio_target)",
                "_aio_temp = _aio_target + '.aio-tmp-' + str(_aio_os.getpid())",
                f"with open(_aio_temp, 'wb') as _aio_f: _aio_f.write(_aio_b64.b64decode({encoded!r}))",
                "_aio_os.chmod(_aio_temp, 0o755)",
                "_aio_os.replace(_aio_temp, _aio_target)",
            ]
        )
    lines.append("_aio_previous = {}")
    lines.append("_aio_missing = object()")
    lines.append("try:")
    for launcher in launchers:
        key = launcher["context_env"]
        token = launcher["context_token"]
        lines.append(f"    _aio_previous[{key!r}] = _aio_os.environ.get({key!r}, _aio_missing)")
        lines.append(f"    _aio_os.environ[{key!r}] = {token!r}")
    lines.append(
        f"    exec(compile(_aio_b64.b64decode({user_code!r}), '<aio-cell>', 'exec'), globals(), globals())"
    )
    lines.append("finally:")
    lines.append("    for _aio_key, _aio_value in _aio_previous.items():")
    lines.append("        if _aio_value is _aio_missing: _aio_os.environ.pop(_aio_key, None)")
    lines.append("        else: _aio_os.environ[_aio_key] = _aio_value")
    return "\n".join(lines)
