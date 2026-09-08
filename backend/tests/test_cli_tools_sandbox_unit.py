"""Pure-function tests for identity-safe aio CLI launchers."""

import os
import subprocess

import pytest

from app.services.cli_tools.placeholders import PlaceholderContext
from app.services.cli_tools.sandbox_inject import (
    build_python_execution,
    prepare_launchers,
    render_env,
    shell_quote,
)


def test_shell_quote_wraps_and_escapes():
    assert shell_quote("abc") == "'abc'"
    assert shell_quote("a'b") == "'a'\\''b'"


def test_signed_context_executes_binary_and_is_not_forwarded(tmp_path):
    binary = tmp_path / "binary"
    binary.write_text(
        "#!/bin/sh\nprintf '%s|' \"$IDENTITY\"\n"
        "env | grep '^AIO_CLI_CONTEXT_' || true\n"
    )
    binary.chmod(0o755)
    launcher = prepare_launchers([{
        "name": "svc", "binary_path": str(binary), "env": {"IDENTITY": "user-a"},
    }], ttl_seconds=60)[0]
    launcher_path = tmp_path / "svc"
    launcher_path.write_text(launcher["launcher"])
    launcher_path.chmod(0o755)
    env = os.environ.copy()
    env[launcher["context_env"]] = launcher["context_token"]
    result = subprocess.run([str(launcher_path)], env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert result.stdout == "user-a|"


def test_launcher_fails_closed_for_missing_or_tampered_context(tmp_path):
    prepared = prepare_launchers([{
        "name": "svc", "binary_path": "/bin/true", "env": {},
    }], ttl_seconds=60)[0]
    launcher_path = tmp_path / "svc"
    launcher_path.write_text(prepared["launcher"])
    launcher_path.chmod(0o755)
    missing = subprocess.run([str(launcher_path)], text=True, capture_output=True)
    assert missing.returncode == 126
    env = os.environ.copy()
    env[prepared["context_env"]] = prepared["context_token"] + "x"
    tampered = subprocess.run([str(launcher_path)], env=env, text=True, capture_output=True)
    assert tampered.returncode == 126


@pytest.mark.parametrize("name", ["bad name; rm", "svc\n"])
def test_prepare_launchers_rejects_unsafe_name(name):
    with pytest.raises(ValueError):
        prepare_launchers([{"name": name, "binary_path": "/x", "env": {}}], ttl_seconds=60)


def test_prepare_launchers_rejects_unsafe_env_key():
    with pytest.raises(ValueError):
        prepare_launchers([{
            "name": "svc", "binary_path": "/x", "env": {"A; rm -rf /": "v"},
        }], ttl_seconds=60)


def test_python_execution_uses_local_bin_and_restores_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    binary = tmp_path / "binary"
    binary.write_text("#!/bin/sh\nprintf '%s' \"$MY_KEY\"\n")
    binary.chmod(0o755)
    wrapped = build_python_execution(
        [{"name": "my-cli", "binary_path": str(binary), "env": {"MY_KEY": "my_val"}}],
        "import subprocess\nanswer = subprocess.check_output(['my-cli'], text=True)",
        ttl_seconds=60,
    )
    scope = {}
    exec(wrapped, scope, scope)
    assert scope["answer"] == "my_val"
    assert (tmp_path / ".local" / "bin" / "my-cli").is_file()
    assert not any(key.startswith("AIO_CLI_CONTEXT_") for key in os.environ)


def test_render_env_resolves_placeholders_and_skips_userless_identity():
    ctx_with_user = PlaceholderContext(
        user={"id": "u1", "phone": "138", "email": ""},
        state={"dir": "/data/cli_state/t/tool/u1"},
    )
    env = {"YYBPC_CLI_USER_PHONE": "$user.phone", "YYBPC_CLI_HOME": "$state.dir", "FIXED": "1"}
    assert render_env(env, ctx_with_user) == {
        "YYBPC_CLI_USER_PHONE": "138",
        "YYBPC_CLI_HOME": "/data/cli_state/t/tool/u1",
        "FIXED": "1",
    }
    # No user in context → $user.* / $state.* entries are skipped entirely
    # (svc then runs identity-less and reports NOT_LOGGED_IN itself).
    assert render_env(env, PlaceholderContext()) == {"FIXED": "1"}


def test_prepare_launchers_still_rejects_hyphenated_env_key():
    """Relaxing the NAME rule must NOT relax env-key validation: an env key with a
    hyphen is invalid as `export`/`env KEY=` and must still raise."""
    with pytest.raises(ValueError):
        prepare_launchers([{
            "name": "my-cli", "binary_path": "/x", "env": {"BAD-KEY": "v"},
        }], ttl_seconds=60)
