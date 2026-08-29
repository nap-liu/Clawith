import shutil
import sys
from pathlib import Path

import pytest

from app.services import agent_tools
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.local.subprocess_backend import SubprocessBackend


pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="the hardened local sandbox requires bubblewrap",
)


async def test_hardened_workspace_exposes_only_the_bound_project_copy(tmp_path: Path):
    workspace = tmp_path / "repository"
    workspace.mkdir()
    (workspace / "input.txt").write_text("project input\n", encoding="utf-8")
    private_file = tmp_path / "private.txt"
    private_file.write_text("not visible\n", encoding="utf-8")
    runtime_temp = tmp_path / ".runtime-tmp"

    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    backend = SubprocessBackend(
        SandboxConfig(
            type=SandboxType.SUBPROCESS,
            hardened=True,
            allow_network=False,
            allow_unsafe_fallback_when_bwrap_missing=False,
        )
    )

    result = await backend.execute(
        code=(
            "set -eu\n"
            "test -f input.txt\n"
            "test ! -e /data/agents\n"
            f"test ! -e '{private_file}'\n"
            "printf 'sandbox output\\n' > output.txt\n"
            "printf 'runtime only\\n' > \"$TMPDIR/runtime.txt\"\n"
        ),
        language="bash",
        timeout=10,
        work_dir=str(workspace),
        venv_path_override=str(venv),
        runtime_temp_path_override=str(runtime_temp),
    )

    assert result.success is True, result.error or result.stderr
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "sandbox output\n"
    assert (runtime_temp / "runtime.txt").read_text(encoding="utf-8") == "runtime only\n"
    assert not (workspace / ".tmp" / "runtime.txt").exists()


async def test_hardened_workspace_applies_standard_output_file_limit(tmp_path: Path):
    workspace = tmp_path / "repository"
    workspace.mkdir()
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    backend = SubprocessBackend(
        SandboxConfig(
            type=SandboxType.SUBPROCESS,
            hardened=True,
            allow_network=False,
            allow_unsafe_fallback_when_bwrap_missing=False,
        )
    )

    result = await backend.execute(
        code=(
            "with open('too-large.bin', 'wb') as output:\n"
            "    for _ in range(11):\n"
            "        output.write(b'x' * 1024 * 1024)\n"
        ),
        language="python",
        timeout=10,
        work_dir=str(workspace),
        venv_path_override=str(venv),
        runtime_temp_path_override=str(tmp_path / ".runtime-tmp"),
    )

    assert result.success is False
    created = workspace / "too-large.bin"
    assert not created.exists() or created.stat().st_size <= 10 * 1024 * 1024


@pytest.mark.parametrize(
    ("error_type", "error_message"),
    [(ValueError, "invalid config"), (RuntimeError, "backend failed")],
)
async def test_hardened_project_execution_never_falls_back_to_host_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    error_message: str,
):
    workspace = tmp_path / "repository"
    workspace.mkdir()
    venv = tmp_path / ".venv"

    def reject_backend(_config):
        raise error_type(error_message)

    async def no_tool_config(*_args, **_kwargs):
        return {}

    async def forbidden_legacy(*_args, **_kwargs):
        raise AssertionError("hardened project execution called the legacy host executor")

    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", reject_backend)
    monkeypatch.setattr(agent_tools, "_get_tool_config", no_tool_config)
    monkeypatch.setattr(agent_tools, "_execute_code_legacy", forbidden_legacy)

    result = await agent_tools._execute_code(
        None,
        workspace,
        {"action": "execute", "language": "bash", "code": "pwd"},
        work_dir_override=workspace,
        hardened_workspace=True,
        venv_path_override=venv,
    )

    assert "Sandbox" in result
    assert error_message in result
