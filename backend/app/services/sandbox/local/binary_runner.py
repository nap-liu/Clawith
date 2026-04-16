"""Run a host-side binary inside an ephemeral docker sandbox.

Used by the CLI tools subsystem. The binary is bind-mounted read-only at
`/binary` inside a `clawith-cli-sandbox` container; the container drops all
capabilities, runs as `nobody`, has a read-only rootfs with a tmpfs `/tmp`,
and optionally no network.

This is *not* a replacement for `DockerBackend.execute` (which runs
source code in language-specific images). It is a focused runner for the
narrow "execute this uploaded binary" use case.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import docker
from docker.errors import APIError, ImageNotFound

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinaryRunResult:
    """Outcome of a single sandboxed binary execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    sandbox_failed: bool = False
    error: str = ""


class BinaryRunner:
    """Execute a mounted binary inside an ephemeral sandbox container."""

    def __init__(
        self,
        image: str,
        *,
        cpu_limit: str = "1.0",
        memory_limit: str = "512m",
        pids_limit: int = 100,
        network: bool = False,
        tmpfs_size: str = "64m",
    ) -> None:
        self.image = image
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.pids_limit = pids_limit
        self.network = network
        self.tmpfs_size = tmpfs_size
        self._client = docker.from_env()

    async def run(
        self,
        binary_host_path: str,
        args: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: int = 30,
    ) -> BinaryRunResult:
        """Execute `binary_host_path` inside a one-shot sandbox container.

        Args:
            binary_host_path: absolute path on the host to the binary. Must
                be readable + executable. Mounted read-only at /binary.
            args: arguments passed to the binary.
            env: environment variables passed to the container.
            timeout_seconds: kill the container if it runs longer than this.
        """
        host_path = Path(binary_host_path).resolve()
        if not host_path.is_file():
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=0,
                sandbox_failed=True,
                error=f"binary not found on host: {host_path}",
            )

        start = time.monotonic()
        try:
            inner = await asyncio.to_thread(
                self._run_blocking,
                str(host_path),
                list(args),
                dict(env),
                timeout_seconds,
            )
        except ImageNotFound as exc:
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - start) * 1000),
                sandbox_failed=True,
                error=f"sandbox image missing: {exc}",
            )
        except APIError as exc:
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - start) * 1000),
                sandbox_failed=True,
                error=f"docker api error: {exc}",
            )

        return BinaryRunResult(
            exit_code=inner.exit_code,
            stdout=inner.stdout,
            stderr=inner.stderr,
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=inner.timed_out,
            sandbox_failed=inner.sandbox_failed,
            error=inner.error,
        )

    def _run_blocking(
        self,
        host_path: str,
        args: list[str],
        env: dict[str, str],
        timeout_seconds: int,
    ) -> BinaryRunResult:
        """Synchronous docker-SDK invocation; called in a thread."""
        container = self._client.containers.create(
            image=self.image,
            command=["/binary", *args],
            environment=env,
            network_disabled=not self.network,
            read_only=True,
            tmpfs={"/tmp": f"rw,size={self.tmpfs_size},mode=1777"},
            mem_limit=self.memory_limit,
            nano_cpus=int(float(self.cpu_limit) * 1_000_000_000),
            pids_limit=self.pids_limit,
            user="65534:65534",
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],
            volumes={host_path: {"bind": "/binary", "mode": "ro"}},
        )

        timed_out = False
        exit_code = -1
        try:
            container.start()
            try:
                status = container.wait(timeout=timeout_seconds)
                exit_code = int(status.get("StatusCode", -1))
            except Exception:
                try:
                    container.kill()
                except APIError:
                    pass
                timed_out = True
                exit_code = -1

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        finally:
            try:
                container.remove(force=True)
            except APIError:
                logger.warning("failed to remove container %s", container.id, exc_info=True)

        return BinaryRunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=0,  # filled in by the async caller
            timed_out=timed_out,
        )
