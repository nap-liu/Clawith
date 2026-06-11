"""Pure tests for AioSandboxBackend shell command composition.

The new API: _compose_shell_command(*, cwd, code, language, inject: dict | None)
returns a single-line base64 transport: `bash <(echo <B64> | base64 -d)`.
Decode the B64 block to assert on the materialized script contents.
"""
import base64
import re

from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


def _decode_cmd(cmd: str) -> str:
    """Extract and decode the base64 payload from `bash <(echo <B64> | base64 -d)`."""
    m = re.search(r"bash <\(echo ([A-Za-z0-9+/=]+) \| base64 -d\)", cmd)
    assert m, f"command does not match expected single-line b64 transport:\n  {cmd!r}"
    return base64.b64decode(m.group(1)).decode()


def test_compose_without_inject_is_single_line_b64():
    """No injection: command is the single-line base64 transport."""
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="echo hi", language="bash", inject=None
    )
    # Must be a single-line base64 transport.
    assert cmd.startswith("bash <(echo ")
    assert "| base64 -d)" in cmd
    # Decoded script must contain cwd setup and user code.
    script = _decode_cmd(cmd)
    assert "cd '/data/agents/a1'" in script
    assert "export HOME=" in script
    assert "echo hi" in script
    # No wrapper writes when inject is None.
    assert "() {" not in script
    assert "base64 -d >" not in script


def test_compose_with_inject_places_wrappers_before_user_code():
    """Inject with a wrapper: decoded script has wrapper write BEFORE cwd/env/code."""
    inject = {
        "wrappers": [{"name": "svc", "binary_path": "/data/cli_binaries/b.bin"}],
        "env": {"YYBPC_CLI_USER_PHONE": "13800000000"},
    }
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="svc report list | head", language="bash",
        inject=inject,
    )
    assert cmd.startswith("bash <(echo ")
    script = _decode_cmd(cmd)

    # Wrapper write must come first (before cd/HOME reset).
    wrapper_pos = script.index("base64 -d >")  # the wrapper-write line
    cd_pos = script.index("cd '/data/agents/a1'")
    env_pos = script.index("YYBPC_CLI_USER_PHONE='13800000000'")
    user_pos = script.index("svc report list | head")

    assert wrapper_pos < cd_pos < env_pos < user_pos, (
        "expected: wrapper_write < cd+HOME < identity_env < user_code"
    )


def test_compose_bash_comment_and_multiline_survive():
    """Comments and multi-line code survive the b64 transport unchanged."""
    code = "# this is a comment\necho 'line1'\necho 'line2'"
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="bash", inject=None
    )
    script = _decode_cmd(cmd)
    # All three lines must appear verbatim in the decoded script.
    assert "# this is a comment" in script
    assert "echo 'line1'" in script
    assert "echo 'line2'" in script
    # The comment and subsequent line must be on separate physical lines
    # (not joined with ';' which would break the comment).
    assert "# this is a comment\necho 'line1'" in script


def test_compose_node_with_inject_heredoc_intact():
    """node + inject: function block precedes the node heredoc; heredoc body is intact."""
    inject = {
        "wrappers": [{"name": "svc", "binary_path": "/data/cli_binaries/b.bin"}],
        "env": {},
    }
    code = "console.log(1)\nconsole.log(2)"
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="node", inject=inject,
    )
    script = _decode_cmd(cmd)

    # Wrapper write present.
    assert "base64 -d >" in script
    # heredoc form present.
    assert "node <<'" in script
    # Both console.log lines preserved verbatim and in order.
    assert "console.log(1)\nconsole.log(2)" in script

    wrapper_pos = script.index("base64 -d >")
    node_pos = script.index("node <<'")
    assert wrapper_pos < node_pos, "wrapper write must precede the node heredoc"


def test_compose_env_export_only_no_wrappers():
    """Inject with env but no wrappers: env export appears, no wrapper writes."""
    inject = {
        "wrappers": [],
        "env": {"MY_TOKEN": "secret123"},
    }
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="echo done", language="bash", inject=inject,
    )
    script = _decode_cmd(cmd)
    assert "MY_TOKEN='secret123'" in script
    assert "base64 -d >" not in script
    assert "echo done" in script


def test_compose_inject_none_still_materializes_user_code():
    """inject=None: the decoded script still contains the user code verbatim."""
    code = "print('hello')"
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="bash", inject=None
    )
    script = _decode_cmd(cmd)
    assert "print('hello')" in script
    assert "export NO_COLOR=1" in script
