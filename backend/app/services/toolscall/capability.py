"""Execution-scoped launcher and compact context for ``toolscall``.

The launcher is a transport adapter only. Standard builtin/MCP tools send one
JSON object to the backend's existing ``execute_tool`` path. Native CLI tools
are exec'd directly so argv, stdin, stdout, stderr, exit status, and signals
retain ordinary process semantics.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from app.services.toolscall.runtime import (
    ToolscallRuntimeUnavailable,
    initialize_toolscall_runtime,
    register_toolscall_scope,
)

TOOLSCALL_COMMAND = "toolscall"
TOOLSCALL_AUDIENCE = "toolscall"
TOOLSCALL_CONTEXT_ENV = "TOOLSCALL_CONTEXT"
TOOLSCALL_TOKEN_VERSION = 1
_LOCAL_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

TOOLSCALL_USAGE_DESCRIPTION = (
    "\n\nBash tool composition with toolscall (enabled for this Agent):\n"
    "- `toolscall` is a CLI executable, not a Python or JavaScript function. "
    "Prefer `language=\"bash\"`; from another language invoke it as a subprocess.\n"
    "- `toolscall <tool> --key value` invokes any standard builtin or MCP "
    "tool already present in the current turn. For structured or batch "
    "arguments, pipe exactly one complete JSON object to stdin, for example "
    "`jq -c '{table_id: 18, rows: .}' workspace/batch.json | "
    "toolscall <tool>`. Never combine stdin JSON with flags, `--stdin`, or "
    "`--key/--value` pairs. Check the command exit status before reporting success.\n"
    "- `toolscall <cli-tool> <argv...>` invokes an available native CLI "
    "tool with ordinary argv/stdin/stdout/stderr and exit-code semantics.\n"
    "- `toolscall` has no list or describe operation because the current "
    "tool schemas are already visible. Its stdout is the original tool output, "
    "so it can be piped directly to jq, rg, files, or another command."
)


ToolscallUnavailable = ToolscallRuntimeUnavailable


def toolscall_enabled_for_agent(config: dict[str, Any] | None) -> bool:
    """Return the Agent-level switch, defaulting missing config to enabled."""
    values = config or {}
    if "toolscall_enabled" not in values:
        return True
    return values["toolscall_enabled"] is True


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode_b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signing_key(seed: bytes) -> Ed25519PrivateKey:
    if len(seed) != 32:
        raise ToolscallUnavailable("invalid toolscall runtime signing material")
    derived = hashlib.sha256(seed + b":toolscall-context:v1").digest()
    return Ed25519PrivateKey.from_private_bytes(derived)


def _sign(payload: dict[str, Any], *, signing_seed: bytes) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64url(raw)}.{_b64url(_signing_key(signing_seed).sign(raw))}"


def verify_toolscall_context(
    token: str,
    *,
    signing_seed: bytes,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify and decode one backend-issued execution context."""
    public_key = _signing_key(signing_seed).public_key()
    try:
        payload_part, signature_part = token.split(".", 1)
        raw = _decode_b64url(payload_part)
        public_key.verify(_decode_b64url(signature_part), raw)
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError("invalid toolscall execution context") from exc

    current = int(time.time()) if now is None else int(now)
    if (
        payload.get("v") != TOOLSCALL_TOKEN_VERSION
        or payload.get("aud") != TOOLSCALL_AUDIENCE
        or not isinstance(payload.get("scope"), str)
        or not payload.get("scope")
        or int(payload.get("iat", 0)) > current + 60
        or int(payload.get("exp", 0)) < current
    ):
        raise ValueError("expired or mismatched toolscall execution context")
    return payload


def _schema_types(schema: Any) -> str | list[str] | None:
    if not isinstance(schema, dict):
        return None
    direct = schema.get("type")
    if isinstance(direct, str):
        return direct
    if isinstance(direct, list):
        values = [value for value in direct if isinstance(value, str)]
        return values or None

    variants: list[str] = []
    for keyword in ("anyOf", "oneOf"):
        for option in schema.get(keyword) or []:
            option_type = _schema_types(option)
            values = option_type if isinstance(option_type, list) else [option_type]
            for value in values:
                if value and value not in variants:
                    variants.append(value)
    return variants or None


def _top_level_types(tool: dict[str, Any]) -> dict[str, str | list[str] | None]:
    parameters = ((tool.get("function") or {}).get("parameters") or {})
    properties = parameters.get("properties") or {}
    if not isinstance(properties, dict):
        return {}
    return {
        str(name): _schema_types(schema)
        for name, schema in properties.items()
        if isinstance(name, str)
    }


async def build_toolscall_wrapper(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
    tools_for_llm: list[dict[str, Any]],
    aio_base_url: str,
    ttl_seconds: int,
    native_tool_names: set[str] | None = None,
) -> dict[str, Any] | None:
    """Build unsigned launcher data from the exact current-turn tool list."""
    visible: dict[str, dict[str, Any]] = {}
    for tool in tools_for_llm:
        name = str((tool.get("function") or {}).get("name") or "").strip()
        if name:
            visible[name] = tool

    # Control-flow tools and code executors cannot be recursively invoked from
    # inside the same AIO foreground execution.
    excluded = {
        "request_confirmation",
        "execute_code",
        "execute_code_e2b",
        "execute_code_aio",
        TOOLSCALL_COMMAND,
    }
    names = set(visible) - excluded
    if not names:
        return None

    # Endpoint type comes from the native wrappers prepared for this exact
    # execution environment, not from a second Tool-table lookup. Names are
    # still intersected with the immutable current-turn schema snapshot.
    native = sorted(names & set(native_tool_names or set()))
    standard = {
        name: _top_level_types(visible[name])
        for name in sorted(names - set(native))
    }
    if not standard and not native:
        return None

    runtime = await initialize_toolscall_runtime(aio_base_url=aio_base_url)
    scope_id = await register_toolscall_scope(
        standard_tool_names=set(standard),
        ttl_seconds=ttl_seconds,
    )

    return {
        "kind": "toolscall",
        "name": TOOLSCALL_COMMAND,
        "endpoint": runtime.bridge_url,
        "signing_seed": runtime.signing_seed,
        "scope": scope_id,
        "agent": str(agent_id),
        "user": str(user_id),
        "session": str(session_id or ""),
        "turn": str(turn_anchor_id or ""),
        "standard": standard,
        "native": native,
    }


def prepare_toolscall_launcher(
    wrapper: dict[str, Any], *, ttl_seconds: int
) -> dict[str, str]:
    """Sign a wrapper scope and return an isolated installable launcher."""
    if wrapper.get("kind") != "toolscall" or wrapper.get("name") != TOOLSCALL_COMMAND:
        raise ValueError("invalid toolscall wrapper")
    ttl = max(1, int(ttl_seconds))
    now = int(time.time())
    payload = {
        "v": TOOLSCALL_TOKEN_VERSION,
        "aud": TOOLSCALL_AUDIENCE,
        "agent": str(wrapper["agent"]),
        "user": str(wrapper["user"]),
        "session": str(wrapper.get("session") or ""),
        "turn": str(wrapper.get("turn") or ""),
        "scope": str(wrapper["scope"]),
        "iat": now,
        "exp": now + ttl,
    }
    endpoint = str(wrapper["endpoint"]).rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("toolscall bridge URL must use http or https")
    signing_seed = wrapper.get("signing_seed")
    if not isinstance(signing_seed, bytes):
        raise ToolscallUnavailable("missing toolscall runtime signing material")
    scope_id = str(wrapper["scope"])
    if not _LOCAL_SCOPE_ID_RE.fullmatch(scope_id):
        raise ValueError("invalid toolscall scope identifier")
    scope_root = f".cache/aio/toolscall/scopes/{scope_id}"
    token = _sign(payload, signing_seed=signing_seed)
    return {
        "name": TOOLSCALL_COMMAND,
        "context_env": TOOLSCALL_CONTEXT_ENV,
        "context_token": token,
        "launcher_relpath": f"{scope_root}/bin/{TOOLSCALL_COMMAND}",
        "scope_root_relpath": scope_root,
        "launcher_compression": "gzip",
        "launcher": _launcher_content(
            endpoint=endpoint,
            standard=wrapper.get("standard") or {},
            native=list(wrapper.get("native") or []),
        ),
    }


def _launcher_content(
    *,
    endpoint: str,
    standard: dict[str, Any],
    native: list[str],
) -> str:
    # The launcher deliberately treats the bearer as opaque. The bridge already
    # verifies its signature, expiry and Redis scope; doing that again here only
    # adds a heavyweight import to every pipeline stage.
    standard_data = base64.b64encode(
        gzip.compress(
            json.dumps(
                standard,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
            compresslevel=1,
        )
    ).decode()
    return f'''#!/usr/bin/env python3
# aio-managed-cli-launcher:v1
import os
import sys

COMMAND = {TOOLSCALL_COMMAND!r}
CONTEXT_ENV = {TOOLSCALL_CONTEXT_ENV!r}
ENDPOINT = {endpoint!r}
STANDARD_DATA = {standard_data!r}
NATIVE = {native!r}

def _fail(message, code=2):
    sys.stderr.write(COMMAND + ": " + message + "\\n")
    raise SystemExit(code)

token = os.environ.get(CONTEXT_ENV)
if not token:
    _fail("valid execution context required", 126)
if len(sys.argv) < 2:
    _fail("tool name is required")
tool = sys.argv[1]

if tool in NATIVE:
    child_env = os.environ.copy()
    child_env.pop(CONTEXT_ENV, None)
    try:
        os.execvpe(tool, [tool, *sys.argv[2:]], child_env)
    except FileNotFoundError:
        _fail("native CLI tool not found: " + tool, 127)

import base64
import gzip
import json
import urllib.error
import urllib.parse
import urllib.request

try:
    STANDARD = json.loads(gzip.decompress(base64.b64decode(STANDARD_DATA)))
except Exception:
    _fail("invalid embedded tool schema", 126)

def _types(spec):
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, list):
        return [item for item in spec if isinstance(item, str)]
    return []

def _convert(raw, spec):
    declared = _types(spec)
    if not declared or declared == ["string"]:
        return raw
    if declared == ["integer"]:
        try:
            return int(raw, 10)
        except ValueError:
            _fail("expected an integer, got " + repr(raw))
    if declared == ["number"]:
        try:
            value = json.loads(raw)
        except Exception:
            _fail("expected a number, got " + repr(raw))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail("expected a number, got " + repr(raw))
        return value
    if declared == ["boolean"]:
        if raw == "true":
            return True
        if raw == "false":
            return False
        _fail("expected true or false, got " + repr(raw))
    if declared == ["null"]:
        if raw == "null":
            return None
        _fail("expected null, got " + repr(raw))
    if declared == ["object"] or declared == ["array"]:
        try:
            value = json.loads(raw)
        except Exception:
            _fail("expected JSON " + declared[0] + ", got " + repr(raw))
        expected = dict if declared[0] == "object" else list
        if not isinstance(value, expected):
            _fail("expected JSON " + declared[0] + ", got " + repr(raw))
        return value
    try:
        value = json.loads(raw)
    except Exception:
        if "string" in declared:
            return raw
        _fail("value does not match declared JSON types: " + ",".join(declared))
    actual = (
        "null" if value is None else
        "boolean" if isinstance(value, bool) else
        "integer" if isinstance(value, int) else
        "number" if isinstance(value, float) else
        "array" if isinstance(value, list) else
        "object" if isinstance(value, dict) else
        "string"
    )
    if actual not in declared and not (actual == "integer" and "number" in declared):
        _fail("value does not match declared JSON types: " + ",".join(declared))
    return value

def _flags(argv, schema):
    result = {{}}
    index = 0
    while index < len(argv):
        item = argv[index]
        if not item.startswith("--") or item == "--":
            _fail("standard tools accept only --key value or --key=value arguments")
        token = item[2:]
        if "=" in token:
            key, raw = token.split("=", 1)
        else:
            key = token
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                _fail("missing value for --" + key)
            else:
                index += 1
                raw = argv[index]
        if not key:
            _fail("empty argument name")
        if key in result:
            _fail("duplicate argument --" + key)
        result[key] = _convert(raw, schema.get(key))
        index += 1
    return result

schema = STANDARD.get(tool)
if schema is None:
    _fail("tool is not available in this execution context: " + tool, 126)

stdin_data = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()
if stdin_data.strip() and len(sys.argv) > 2:
    _fail("stdin JSON and CLI arguments cannot be used together; pipe one complete JSON object and remove all flags")
if stdin_data.strip():
    try:
        arguments = json.loads(stdin_data)
    except Exception as exc:
        _fail("stdin must contain one JSON object: " + str(exc))
    if not isinstance(arguments, dict):
        _fail("stdin must contain one JSON object")
elif len(sys.argv) > 2:
    arguments = _flags(sys.argv[2:], schema)
else:
    arguments = {{}}

request = urllib.request.Request(
    ENDPOINT + "/call/" + urllib.parse.quote(tool, safe=""),
    data=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode(),
    method="POST",
    headers={{
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }},
)
try:
    with urllib.request.urlopen(request) as response:
        body = response.read()
except urllib.error.HTTPError as exc:
    detail = exc.read()
    if detail:
        sys.stderr.buffer.write(detail)
        if not detail.endswith(b"\\n"):
            sys.stderr.buffer.write(b"\\n")
    else:
        sys.stderr.write(COMMAND + ": bridge HTTP " + str(exc.code) + "\\n")
    raise SystemExit(70)
except Exception as exc:
    _fail("bridge transport failed: " + str(exc), 70)
sys.stdout.buffer.write(body)
'''
