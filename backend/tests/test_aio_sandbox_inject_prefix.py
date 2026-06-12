"""Pure tests for AioSandboxBackend shell command composition.

Contract (per-session isolation): _compose_shell_command(*, cwd, code, language,
inject, bindir) returns a single-line base64 transport
`source <(echo <B64> | base64 -d)`. Identity rides INSIDE each wrapper (an `env`
prefix on its exec line) written to the per-conversation `bindir`, and `bindir`
is prepended to PATH — identity is NEVER a session-level `export`.
"""
import base64
import re

from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend

_BINDIR = "$HOME/.clawith-bin/abc123"


def _decode_cmd(cmd: str) -> str:
    """Extract and decode the base64 payload from `source <(echo <B64> | base64 -d)`."""
    m = re.search(r"source <\(echo ([A-Za-z0-9+/=]+) \| base64 -d\)", cmd)
    assert m, f"command does not match expected single-line b64 transport:\n  {cmd!r}"
    return base64.b64decode(m.group(1)).decode()


def _wrapper_payloads(script: str) -> list[str]:
    """Decode every base64 wrapper-write block in the script."""
    return [base64.b64decode(m).decode() for m in re.findall(r"echo ([A-Za-z0-9+/=]+) \| base64 -d >", script)]


def test_compose_without_inject_is_single_line_b64():
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="echo hi", language="bash", inject=None, bindir=_BINDIR
    )
    assert cmd.startswith("source <(echo ")
    assert "| base64 -d)" in cmd
    script = _decode_cmd(cmd)
    assert "cd '/data/agents/a1'" in script
    assert "export HOME=" in script
    assert "echo hi" in script
    # No wrapper writes and no bindir on PATH when inject is None.
    assert "base64 -d >" not in script
    assert ".clawith-bin" not in script


def test_compose_identity_lives_in_wrapper_not_session_export():
    """The crux: a sender's identity must NOT be a session-level export (which
    would persist via `source` and leak to the next sender). It appears ONLY
    inside the wrapper's base64 content, as an `env` prefix."""
    inject = {"wrappers": [{
        "name": "svc",
        "binary_path": "/data/cli_binaries/b.bin",
        "env": {"YYBPC_CLI_USER_PHONE": "13800000000"},
    }]}
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="svc report list | head", language="bash",
        inject=inject, bindir=_BINDIR,
    )
    script = _decode_cmd(cmd)
    # Identity must NOT appear as a bare session export.
    assert "export YYBPC_CLI_USER_PHONE" not in script
    assert "13800000000" not in script.replace(_wrapper_payloads(script)[0], "")
    # Identity DOES appear inside the wrapper, as an env prefix on exec.
    wrapper = _wrapper_payloads(script)[0]
    assert "exec env YYBPC_CLI_USER_PHONE='13800000000' '/data/cli_binaries/b.bin' \"$@\"" in wrapper


def test_compose_with_inject_order_and_path_prepend():
    """Order: cd/HOME reset < wrapper write (into bindir) < PATH prepend < user code."""
    inject = {"wrappers": [{
        "name": "svc", "binary_path": "/data/cli_binaries/b.bin", "env": {},
    }]}
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="svc report list | head", language="bash",
        inject=inject, bindir=_BINDIR,
    )
    script = _decode_cmd(cmd)
    cd_pos = script.index("cd '/data/agents/a1'")
    wrapper_pos = script.index("base64 -d >")
    path_pos = script.index('export PATH="$HOME/.clawith-bin/abc123:$PATH"')
    user_pos = script.index("svc report list | head")
    assert cd_pos < wrapper_pos < path_pos < user_pos, (
        "expected: cd+HOME < wrapper_write < PATH prepend < user_code"
    )
    # Wrapper is written into the per-conversation bindir.
    assert '"$HOME/.clawith-bin/abc123"/svc' in script


def test_compose_bash_comment_and_multiline_survive():
    code = "# this is a comment\necho 'line1'\necho 'line2'"
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="bash", inject=None, bindir=_BINDIR
    )
    script = _decode_cmd(cmd)
    assert "# this is a comment\necho 'line1'" in script
    assert "echo 'line2'" in script


def test_compose_node_with_inject_heredoc_intact():
    inject = {"wrappers": [{"name": "svc", "binary_path": "/data/cli_binaries/b.bin", "env": {}}]}
    code = "console.log(1)\nconsole.log(2)"
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="node", inject=inject, bindir=_BINDIR,
    )
    script = _decode_cmd(cmd)
    assert "base64 -d >" in script
    assert "node <<'" in script
    assert "console.log(1)\nconsole.log(2)" in script
    assert script.index("base64 -d >") < script.index("node <<'")


def test_compose_reset_wrappers_clears_stale_dir_even_without_inject():
    """A prior sender wrote a wrapper into bindir (persisted by `source`). When
    THIS sender has no injection, the compose must still CLEAR the bindir so the
    stale wrapper can't be used — fail-safe against group-IM impersonation."""
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="svc", language="bash",
        inject=None, bindir=_BINDIR, reset_wrappers=True,
    )
    script = _decode_cmd(cmd)
    # The bindir is wiped (rm -rf) before the user code runs.
    assert 'rm -rf "$HOME/.clawith-bin/abc123"' in script
    # No wrapper is written (no inject), so svc is gone.
    assert "base64 -d >" not in script


def test_compose_no_reset_no_inject_leaves_bindir_untouched():
    """A pure non-CLI exec (never had wrappers) must not pay for a clear."""
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="echo hi", language="bash",
        inject=None, bindir=_BINDIR, reset_wrappers=False,
    )
    script = _decode_cmd(cmd)
    assert "rm -rf" not in script
    assert ".clawith-bin" not in script


def test_compose_inject_none_still_materializes_user_code():
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="print('hello')", language="bash", inject=None, bindir=_BINDIR
    )
    script = _decode_cmd(cmd)
    assert "print('hello')" in script
    assert "export NO_COLOR=1" in script
