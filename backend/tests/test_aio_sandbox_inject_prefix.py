"""Pure tests for AioSandboxBackend shell command composition."""
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


def test_compose_without_prefix_keeps_existing_shape():
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="echo hi", language="bash", inject_prefix=None
    )
    assert cmd.startswith("cd '/data/agents/a1' && export HOME=")
    assert cmd.endswith("echo hi")
    assert "() {" not in cmd


def test_compose_with_prefix_places_functions_before_user_code():
    prefix = "svc() { X='1' '/data/cli_binaries/b.bin' \"$@\"; }"
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="svc report list | head", language="bash",
        inject_prefix=prefix,
    )
    fn_pos = cmd.index("svc() {")
    user_pos = cmd.index("svc report list | head")
    export_pos = cmd.index("export NO_COLOR=1")
    assert export_pos < fn_pos < user_pos
    # Functions and user code on separate lines; aio-sandbox >= 1.9.3
    # normalizes the newlines at its shell-exec layer.
    assert "\nsvc report list | head" in cmd


def test_compose_node_with_prefix_keeps_heredoc_intact_after_functions():
    """node + inject_prefix: the function block precedes the node heredoc, and
    the `node <<'DELIM' ... DELIM` heredoc body is preserved verbatim (1.9.3's
    bash-aware split must not corrupt it)."""
    prefix = "svc() { X='1' '/data/cli_binaries/b.bin' \"$@\"; }"
    code = "console.log(1)\nconsole.log(2)"
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="node", inject_prefix=prefix,
    )
    fn_pos = cmd.index("svc() {")
    node_pos = cmd.index("node <<'AIOSB_NODE_")
    assert fn_pos < node_pos  # function block before the node heredoc
    # heredoc body preserved verbatim (both console.log lines, in order)
    assert "console.log(1)\nconsole.log(2)" in cmd
    # prefix and the node command on separate physical lines
    assert "}\nnode <<'AIOSB_NODE_" in cmd
