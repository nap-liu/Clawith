"""Pure tests for aio shell composition with standard local-bin launchers."""
import os
import subprocess

from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


def test_bash_without_inject_preserves_multiline_code_and_environment(tmp_path):
    command = AioSandboxBackend._compose_shell_command(
        cwd=str(tmp_path),
        code='# comment must not swallow the next line\nprintf "%s|%s|%s|" "$HOME" "$NO_COLOR" "$PIP_USER"\necho first\necho second',
        language="bash",
        inject=None,
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{tmp_path}|1|1|first\nsecond\n"


def test_node_with_inject_delivers_multiline_stdin(tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    node = local_bin / "node"
    node.write_text("#!/bin/sh\ncat\n")
    node.chmod(0o755)
    code = "console.log(1)\nconsole.log(2)"
    command = AioSandboxBackend._compose_shell_command(
        cwd=str(tmp_path), code=code, language="node",
        inject={"wrappers": [{"name": "svc", "binary_path": "/bin/true", "env": {}}]},
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == code + "\n"


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
