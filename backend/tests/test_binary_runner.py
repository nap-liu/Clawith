"""Tests for BinaryRunner — executes a host-side binary inside a docker sandbox.

The docker-SDK path is unit-tested with a mock `DockerClient`. A real
end-to-end integration test (marked `integration`) exists as well and is
skipped by default because the docker-in-docker path-mapping makes it
environment-sensitive — run it manually on a host with shared tmp mounts.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.sandbox.local.binary_runner import BinaryRunner, BinaryRunResult


def _shebang(tmp_path: Path) -> Path:
    script = tmp_path / "noop.sh"
    script.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "hi"
    """))
    script.chmod(0o555)
    return script


class _FakeContainer:
    def __init__(self, *, exit_code=0, stdout=b"", stderr=b"", timeout_raises=False):
        self.id = "fake"
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_raises = timeout_raises
        self.started = False
        self.killed = False
        self.removed = False

    def start(self):
        self.started = True

    def wait(self, timeout=None):
        if self._timeout_raises:
            raise TimeoutError("simulated")
        return {"StatusCode": self._exit_code}

    def kill(self):
        self.killed = True

    def logs(self, stdout=True, stderr=True):
        if stdout and not stderr:
            return self._stdout
        if stderr and not stdout:
            return self._stderr
        return self._stdout + self._stderr

    def remove(self, force=False):
        self.removed = True


def _fake_docker_client(container: _FakeContainer) -> MagicMock:
    client = MagicMock()
    client.containers.create = MagicMock(return_value=container)
    return client


@pytest.mark.asyncio
async def test_binary_runner_happy_path(tmp_path):
    """Successful run returns stdout, exit_code=0, no flags set."""
    script = _shebang(tmp_path)
    container = _FakeContainer(exit_code=0, stdout=b"args=hello world\ngreeting=hi\n")

    with patch("app.services.sandbox.local.binary_runner.docker.from_env",
               return_value=_fake_docker_client(container)):
        runner = BinaryRunner(image="clawith-cli-sandbox:local-test")
        result = await runner.run(
            binary_host_path=str(script),
            args=["hello", "world"],
            env={"GREETING": "hi"},
            timeout_seconds=5,
        )

    assert isinstance(result, BinaryRunResult)
    assert result.exit_code == 0
    assert "args=hello world" in result.stdout
    assert "greeting=hi" in result.stdout
    assert result.timed_out is False
    assert result.sandbox_failed is False
    assert container.started is True
    assert container.removed is True


@pytest.mark.asyncio
async def test_binary_runner_passes_sandbox_flags_to_docker(tmp_path):
    """The docker create call receives cap-drop, user, tmpfs, network-disabled etc."""
    script = _shebang(tmp_path)
    container = _FakeContainer(exit_code=0, stdout=b"", stderr=b"")
    client = _fake_docker_client(container)

    with patch("app.services.sandbox.local.binary_runner.docker.from_env", return_value=client):
        runner = BinaryRunner(
            image="clawith-cli-sandbox:local-test",
            cpu_limit="0.5", memory_limit="256m", pids_limit=50, network=False,
        )
        await runner.run(
            binary_host_path=str(script),
            args=["--flag"],
            env={"K": "v"},
            timeout_seconds=10,
        )

    create_kwargs = client.containers.create.call_args.kwargs
    assert create_kwargs["image"] == "clawith-cli-sandbox:local-test"
    assert create_kwargs["command"] == ["/binary", "--flag"]
    assert create_kwargs["environment"] == {"K": "v"}
    assert create_kwargs["network_disabled"] is True
    assert create_kwargs["read_only"] is True
    assert create_kwargs["user"] == "65534:65534"
    assert create_kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges" in create_kwargs["security_opt"]
    assert create_kwargs["mem_limit"] == "256m"
    assert create_kwargs["pids_limit"] == 50
    assert create_kwargs["nano_cpus"] == 500_000_000
    # The host path appears unchanged in the volumes map with mode=ro.
    assert create_kwargs["volumes"] == {str(script.resolve()): {"bind": "/binary", "mode": "ro"}}
    assert create_kwargs["tmpfs"] == {"/tmp": "rw,size=64m,mode=1777"}


@pytest.mark.asyncio
async def test_binary_runner_network_enabled_sets_network_disabled_false(tmp_path):
    script = _shebang(tmp_path)
    container = _FakeContainer(exit_code=0)
    client = _fake_docker_client(container)

    with patch("app.services.sandbox.local.binary_runner.docker.from_env", return_value=client):
        runner = BinaryRunner(image="img", network=True)
        await runner.run(binary_host_path=str(script), args=[], env={}, timeout_seconds=5)

    assert client.containers.create.call_args.kwargs["network_disabled"] is False


@pytest.mark.asyncio
async def test_binary_runner_timeout(tmp_path):
    """wait() raising (timeout) → kill, mark timed_out, exit_code=-1."""
    script = _shebang(tmp_path)
    container = _FakeContainer(timeout_raises=True)

    with patch("app.services.sandbox.local.binary_runner.docker.from_env",
               return_value=_fake_docker_client(container)):
        runner = BinaryRunner(image="img")
        result = await runner.run(
            binary_host_path=str(script),
            args=[],
            env={},
            timeout_seconds=2,
        )

    assert result.timed_out is True
    assert result.exit_code == -1
    assert container.killed is True
    assert container.removed is True


@pytest.mark.asyncio
async def test_binary_runner_missing_binary_surfaces_sandbox_failure(tmp_path):
    """Non-existent host path → sandbox_failed=True, no docker call at all."""
    with patch("app.services.sandbox.local.binary_runner.docker.from_env", return_value=MagicMock()):
        runner = BinaryRunner(image="img")
        result = await runner.run(
            binary_host_path=str(tmp_path / "does-not-exist"),
            args=[],
            env={},
            timeout_seconds=5,
        )

    assert result.sandbox_failed is True
    assert result.exit_code == 1
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_binary_runner_missing_image_surfaces_sandbox_failure(tmp_path):
    """ImageNotFound from docker SDK → sandbox_failed=True with helpful error."""
    from docker.errors import ImageNotFound

    script = _shebang(tmp_path)
    client = MagicMock()
    client.containers.create = MagicMock(side_effect=ImageNotFound("no such image"))

    with patch("app.services.sandbox.local.binary_runner.docker.from_env", return_value=client):
        runner = BinaryRunner(image="missing:tag")
        result = await runner.run(
            binary_host_path=str(script),
            args=[],
            env={},
            timeout_seconds=5,
        )

    assert result.sandbox_failed is True
    assert "image" in result.error.lower()


# ─────────────────────────────────────────────────────────────────────────
# Integration test: exercises the real docker daemon + sandbox image.
# Skipped by default — enable with `pytest -m integration` on a host where
# the docker-in-docker path mapping works (not typical in Docker Desktop
# on macOS without extra tmp mounts).
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_binary_runner_integration_with_real_docker(tmp_path):
    script = _shebang(tmp_path)
    runner = BinaryRunner(image="clawith-cli-sandbox:local-test")
    result = await runner.run(
        binary_host_path=str(script),
        args=[],
        env={},
        timeout_seconds=10,
    )
    assert result.exit_code == 0
    assert "hi" in result.stdout
    assert result.sandbox_failed is False
