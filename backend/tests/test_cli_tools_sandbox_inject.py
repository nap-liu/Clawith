"""Pure-function tests for the sandbox CLI injection block builder."""
import pytest

from app.services.cli_tools.sandbox_inject import (
    build_cli_function,
    render_env,
    shell_quote,
)
from app.services.cli_tools.placeholders import PlaceholderContext


def test_shell_quote_wraps_and_escapes():
    assert shell_quote("abc") == "'abc'"
    assert shell_quote("a'b") == "'a'\\''b'"


def test_build_cli_function_binds_env_inside_function():
    text = build_cli_function(
        name="svc",
        binary_path="/data/cli_binaries/_global/t1/aa.bin",
        env={"YYBPC_CLI_USER_PHONE": "13800000000", "YYBPC_CLI_HOME": "/data/cli_state/x"},
    )
    # One function definition, env as command-prefix assignments (NOT export).
    assert text.startswith("svc() {")
    assert "export" not in text
    assert "YYBPC_CLI_USER_PHONE='13800000000'" in text
    assert "YYBPC_CLI_HOME='/data/cli_state/x'" in text
    assert "'/data/cli_binaries/_global/t1/aa.bin' \"$@\"" in text
    assert text.rstrip().endswith("}")


def test_build_cli_function_rejects_unsafe_name():
    with pytest.raises(ValueError):
        build_cli_function(name="bad name; rm", binary_path="/x", env={})


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


def test_build_cli_function_rejects_unsafe_env_key():
    with pytest.raises(ValueError):
        build_cli_function(name="svc", binary_path="/x", env={"A; touch /tmp/P; B": "v"})


def test_build_cli_function_rejects_trailing_newline_name():
    with pytest.raises(ValueError):
        build_cli_function(name="svc\n", binary_path="/x", env={})
