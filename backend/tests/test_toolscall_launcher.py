from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from app.services.cli_tools.sandbox_inject import build_python_execution
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend
from app.services.toolscall.capability import (
    TOOLSCALL_CONTEXT_ENV,
    prepare_toolscall_launcher,
    verify_toolscall_context,
)

_SIGNING_SEED = b"t" * 32


class _BridgeHandler(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict]] = []
    response_body = b'{"raw":true}'

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        type(self).calls.append(
            {
                "path": self.path,
                "authorization": self.headers.get("authorization"),
                "body": json.loads(body),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, *_args):
        return


@pytest.fixture
def bridge_server():
    _BridgeHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/internal/toolscall/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _prepared(endpoint: str, *, native: list[str] | None = None) -> dict[str, str]:
    return prepare_toolscall_launcher(_wrapper(endpoint, native=native), ttl_seconds=60)


def _wrapper(endpoint: str, *, native: list[str] | None = None) -> dict:
    return {
            "kind": "toolscall",
            "name": "toolscall",
            "endpoint": endpoint,
            "signing_seed": _SIGNING_SEED,
            "scope": "scope-launcher",
            "agent": "00000000-0000-0000-0000-000000000001",
            "user": "00000000-0000-0000-0000-000000000002",
            "session": "session-1",
            "turn": "",
            "standard": {
                "sample": {
                    "count": "integer",
                    "enabled": "boolean",
                    "payload": "object",
                    "text": "string",
                }
            },
            "native": native or [],
        }


def _write_launcher(tmp_path, prepared: dict[str, str]):
    path = tmp_path / "toolscall"
    path.write_text(prepared["launcher"])
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(launcher, prepared, *args, input_bytes=b"", env=None):
    child_env = os.environ.copy()
    child_env[TOOLSCALL_CONTEXT_ENV] = prepared["context_token"]
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, str(launcher), *args],
        input=input_bytes,
        capture_output=True,
        env=child_env,
        check=False,
    )


def test_standard_flags_are_mechanically_typed_and_output_is_raw(
    tmp_path, bridge_server
):
    prepared = _prepared(bridge_server)
    launcher = _write_launcher(tmp_path, prepared)

    result = _run(
        launcher,
        prepared,
        "sample",
        "--count",
        "7",
        "--enabled",
        "true",
        "--payload={\"items\":[1,2]}",
        "--text",
        "hello",
    )

    assert result.returncode == 0
    assert result.stdout == _BridgeHandler.response_body
    assert result.stderr == b""
    assert _BridgeHandler.calls == [
        {
            "path": "/api/internal/toolscall/v1/call/sample",
            "authorization": f"Bearer {prepared['context_token']}",
            "body": {
                "count": 7,
                "enabled": True,
                "payload": {"items": [1, 2]},
                "text": "hello",
            },
        }
    ]


def test_standard_stdin_accepts_one_json_object(tmp_path, bridge_server):
    prepared = _prepared(bridge_server)
    launcher = _write_launcher(tmp_path, prepared)

    result = _run(
        launcher,
        prepared,
        "sample",
        input_bytes='{"text":"你好","count":3}'.encode(),
    )

    assert result.returncode == 0
    assert _BridgeHandler.calls[0]["body"] == {"text": "你好", "count": 3}


def test_standard_empty_stdin_defaults_to_empty_object(tmp_path, bridge_server):
    prepared = _prepared(bridge_server)
    launcher = _write_launcher(tmp_path, prepared)

    result = _run(launcher, prepared, "sample")

    assert result.returncode == 0
    assert _BridgeHandler.calls[0]["body"] == {}


def test_standard_rejects_mixed_stdin_and_flags(tmp_path, bridge_server):
    prepared = _prepared(bridge_server)
    launcher = _write_launcher(tmp_path, prepared)

    result = _run(
        launcher,
        prepared,
        "sample",
        "--text",
        "flags",
        input_bytes=b'{"text":"stdin"}',
    )

    assert result.returncode == 2
    assert b"cannot be used together" in result.stderr
    assert b"pipe one complete JSON object and remove all flags" in result.stderr
    assert _BridgeHandler.calls == []


def test_native_cli_preserves_argv_stdin_and_exit_code(tmp_path, bridge_server):
    native = tmp_path / "native-tool"
    native.write_text('#!/bin/sh\nprintf "argv:%s|" "$*"\ncat\nexit 23\n')
    native.chmod(native.stat().st_mode | stat.S_IXUSR)
    prepared = _prepared(bridge_server, native=["native-tool"])
    launcher = _write_launcher(tmp_path, prepared)

    result = _run(
        launcher,
        prepared,
        "native-tool",
        "--raw",
        "two words",
        input_bytes=b"stdin-bytes",
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
    )

    assert result.returncode == 23
    assert result.stdout == b"argv:--raw two words|stdin-bytes"
    assert result.stderr == b""
    assert _BridgeHandler.calls == []


def test_context_is_signed_and_scoped(bridge_server):
    prepared = _prepared(bridge_server)
    payload = verify_toolscall_context(
        prepared["context_token"], signing_seed=_SIGNING_SEED
    )

    assert payload["aud"] == "toolscall"
    assert payload["scope"] == "scope-launcher"
    assert "standard" not in payload
    assert "native" not in payload
    assert "outer" not in payload
    assert "nonce" not in payload
    assert len(prepared["context_token"]) < 1000
    assert prepared["launcher_relpath"].startswith(
        ".cache/aio/toolscall/scopes/scope-launcher/"
    )
    assert prepared["launcher_compression"] == "gzip"
    with pytest.raises(ValueError, match="invalid toolscall"):
        verify_toolscall_context(
            prepared["context_token"] + "corrupt",
            signing_seed=_SIGNING_SEED,
        )
    with pytest.raises(ValueError, match="invalid toolscall"):
        verify_toolscall_context(
            prepared["context_token"],
            signing_seed=b"x" * 32,
        )


def test_jupyter_scope_is_visible_only_during_cell_and_destroyed(
    tmp_path, monkeypatch, bridge_server
):
    monkeypatch.setenv("HOME", str(tmp_path))
    previous_path = os.environ.get("PATH")
    wrapped = build_python_execution(
        [_wrapper(bridge_server)],
        "import shutil\ntoolscall_path = shutil.which('toolscall')",
        ttl_seconds=60,
    )
    scope: dict = {}

    exec(wrapped, scope, scope)  # noqa: S102 - executing generated cell wrapper

    assert scope["toolscall_path"].endswith("/scope-launcher/bin/toolscall")
    assert not (tmp_path / ".cache/aio/toolscall/scopes/scope-launcher").exists()
    assert os.environ.get("PATH") == previous_path
    assert TOOLSCALL_CONTEXT_ENV not in os.environ


@pytest.mark.parametrize("background_job", [False, True])
def test_shell_scope_is_private_to_execution_and_destroyed(
    tmp_path, bridge_server, background_job
):
    user_code = (
        f"command -v toolscall > {tmp_path / 'toolscall-path.txt'}\n"
        'test -n "$TOOLSCALL_CONTEXT"'
    )
    command = AioSandboxBackend._compose_shell_command(
        cwd=str(tmp_path),
        code=user_code,
        language="bash",
        inject={"platform_wrappers": [_wrapper(bridge_server)]},
        background_job=background_job,
        context_ttl_seconds=60,
    )
    encoded_script = command.split("echo ", 1)[1].split(" | base64", 1)[0]
    script_path = tmp_path / "execution.sh"
    user_script_path = tmp_path / "user.sh"
    user_script_path.write_text(user_code)
    decoded_script = base64.b64decode(encoded_script).decode()
    encoded_user = base64.b64encode(user_code.encode()).decode()
    decoded_script = decoded_script.replace(
        f"source <(echo {encoded_user} | base64 -d)",
        f'source "{user_script_path}"',
    )
    script_path.write_text(decoded_script)

    result = subprocess.run(
        ["bash", "-c", 'source "$1"', "bash", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    tool_path = (tmp_path / "toolscall-path.txt").read_text().strip()
    assert "/.cache/aio/toolscall/scopes/scope-launcher/bin/toolscall" in tool_path
    assert not (tmp_path / ".cache/aio/toolscall/scopes/scope-launcher").exists()
    assert not (tmp_path / ".local/bin/toolscall").exists()
