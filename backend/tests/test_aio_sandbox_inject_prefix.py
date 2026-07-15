"""Pure tests for aio shell composition with standard local-bin launchers."""
import base64
import json
import os
import re
import subprocess

from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


def _decode_cmd(cmd: str) -> str:
    match = re.search(r"source <\(echo ([A-Za-z0-9+/=]+) \| base64 -d\)", cmd)
    assert match, cmd
    return base64.b64decode(match.group(1)).decode()


def _launcher_payloads(script: str) -> list[str]:
    return [
        base64.b64decode(value).decode()
        for value in re.findall(r"echo ([A-Za-z0-9+/=]+) \| base64 -d >", script)
    ]


def _scoped_user_code(script: str) -> str:
    match = re.search(r"source <\(echo ([A-Za-z0-9+/=]+) \| base64 -d\)", script)
    assert match, script
    return base64.b64decode(match.group(1)).decode()


def _context_payload(script: str) -> dict:
    match = re.search(r"local -x AIO_CLI_CONTEXT_[A-F0-9]+='([^']+)'", script)
    assert match, script
    payload = match.group(1).split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_compose_without_inject_uses_standard_local_bin_once():
    cmd = AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="echo hi", language="bash", inject=None
    )
    assert "\n" not in cmd
    script = _decode_cmd(cmd)
    assert "cd '/data/agents/a1'" in script
    assert 'export HOME=' in script
    assert '$HOME/.local/bin' in script
    assert "echo hi" in script
    assert ".jobs" not in script
    assert ".clawith-bin" not in script
    assert "local -x AIO_CLI_CONTEXT_" not in script


def test_identity_is_signed_context_not_launcher_or_session_export():
    inject = {"wrappers": [{
        "name": "svc",
        "binary_path": "/data/cli_binaries/b.bin",
        "env": {"YYBPC_CLI_USER_PHONE": "13800000000"},
    }]}
    script = _decode_cmd(AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1",
        code="svc report list | head",
        language="bash",
        inject=inject,
    ))
    launcher = _launcher_payloads(script)[0]
    assert "13800000000" not in launcher
    assert "YYBPC_CLI_USER_PHONE" not in launcher
    assert "export YYBPC_CLI_USER_PHONE" not in script
    assert _context_payload(script)["env"] == {
        "YYBPC_CLI_USER_PHONE": "13800000000"
    }
    assert "svc report list | head" in _scoped_user_code(script)


def test_launcher_setup_order_and_no_runtime_bin_directory():
    inject = {"wrappers": [{
        "name": "svc", "binary_path": "/data/cli_binaries/b.bin", "env": {},
    }]}
    script = _decode_cmd(AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="svc", language="bash", inject=inject
    ))
    assert script.index("cd '/data/agents/a1'") < script.index(".local/bin/svc")
    assert '"$HOME/.local/bin/svc"' in script
    assert ".jobs" not in script
    assert ".clawith-bin" not in script
    assert "foreground/bin" not in script


def test_bash_comment_and_multiline_survive_without_inject():
    code = "# this is a comment\necho 'line1'\necho 'line2'"
    script = _decode_cmd(AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="bash", inject=None
    ))
    assert code in script


def test_node_with_inject_heredoc_survives_inside_scope():
    inject = {"wrappers": [{
        "name": "svc", "binary_path": "/data/cli_binaries/b.bin", "env": {},
    }]}
    code = "console.log(1)\nconsole.log(2)"
    script = _decode_cmd(AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code=code, language="node", inject=inject
    ))
    scoped = _scoped_user_code(script)
    assert "node <<'" in scoped
    assert code in scoped


def test_injectless_execution_cannot_reuse_a_stale_launcher_identity():
    script = _decode_cmd(AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="svc", language="bash", inject=None
    ))
    # The identity-free launcher may remain in .local/bin, but no execution
    # context is present, so it fails closed rather than reusing a prior sender.
    assert "local -x AIO_CLI_CONTEXT_" not in script
    assert "svc" in script
    assert "rm -rf" not in script


def test_inject_none_still_materializes_user_code_and_permission_env():
    script = _decode_cmd(AioSandboxBackend._compose_shell_command(
        cwd="/data/agents/a1", code="print('hello')", language="bash", inject=None
    ))
    assert "print('hello')" in script
    assert "export NO_COLOR=1" in script
    assert "export PIP_USER=1" in script


def test_real_bash_two_identities_share_launcher_without_cross_talk(tmp_path):
    binary = tmp_path / "identity-bin"
    binary.write_text("#!/bin/sh\nprintf '%s' \"$IDENTITY\"\n")
    binary.chmod(0o755)

    def command(identity: str) -> str:
        return AioSandboxBackend._compose_shell_command(
            cwd=str(tmp_path),
            code="sleep 0.05; svc",
            language="bash",
            inject={"wrappers": [{
                "name": "svc",
                "binary_path": str(binary),
                "env": {"IDENTITY": identity},
            }]},
        )

    env = os.environ.copy()
    first = subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-c", command("user-a")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-c", command("user-b")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_out, first_err = first.communicate(timeout=10)
    second_out, second_err = second.communicate(timeout=10)
    assert (first.returncode, first_out, first_err) == (0, "user-a", "")
    assert (second.returncode, second_out, second_err) == (0, "user-b", "")

    launcher = (tmp_path / ".local" / "bin" / "svc").read_text()
    assert "user-a" not in launcher
    assert "user-b" not in launcher
    stale = subprocess.run(
        [str(tmp_path / ".local" / "bin" / "svc")],
        env=env,
        text=True,
        capture_output=True,
    )
    assert stale.returncode == 126
    assert "valid execution context required" in stale.stderr


def test_real_bash_user_exports_persist_but_cli_context_does_not(tmp_path):
    binary = tmp_path / "identity-bin"
    binary.write_text("#!/bin/sh\nprintf '%s' \"$IDENTITY\"\n")
    binary.chmod(0o755)
    first = AioSandboxBackend._compose_shell_command(
        cwd=str(tmp_path),
        code="export USER_SETTING=kept; svc",
        language="bash",
        inject={"wrappers": [{
            "name": "svc", "binary_path": str(binary), "env": {"IDENTITY": "current"},
        }]},
    )
    # A second call in the same persistent shell sees the user's export, while
    # the AIO context variable created by the first function scope is gone.
    second = AioSandboxBackend._compose_shell_command(
        cwd=str(tmp_path),
        code="printf '|%s|' \"$USER_SETTING\"; env | grep '^AIO_CLI_CONTEXT_' || true",
        language="bash",
        inject=None,
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", first + "\n" + second],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout == "current|kept|"


def test_unmanaged_local_bin_collision_fails_before_user_code(tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    existing = local_bin / "svc"
    existing.write_text("#!/bin/sh\necho unmanaged\n")
    existing.chmod(0o755)
    command = AioSandboxBackend._compose_shell_command(
        cwd=str(tmp_path),
        code="echo SHOULD_NOT_RUN",
        language="bash",
        inject={"wrappers": [{
            "name": "svc", "binary_path": "/bin/true", "env": {},
        }]},
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "refusing to overwrite unmanaged" in result.stderr
    assert "SHOULD_NOT_RUN" not in result.stdout
    assert existing.read_text() == "#!/bin/sh\necho unmanaged\n"
