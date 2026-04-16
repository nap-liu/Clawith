"""Run a host-side binary inside an ephemeral docker sandbox.

Used by the CLI tools subsystem. The binary is bind-mounted read-only at
`/binary` inside a `clawith-cli-sandbox` container; the container drops all
capabilities, runs as `nobody`, has a read-only rootfs with a tmpfs `/tmp`,
and optionally no network.

This is *not* a replacement for `DockerBackend.execute` (which runs
source code in language-specific images). It is a focused runner for the
narrow "execute this uploaded binary" use case.

Docker-out-of-docker path translation
-------------------------------------
When the backend itself runs inside a container (compose / k8s), the path
it sees for `/data/cli_binaries/...` is not the same path the host
docker daemon sees. The daemon needs the path from *its* filesystem.
`HostPathResolver` handles that: given the container path and the compose
volume name backing `/data/cli_binaries`, it asks the daemon where the
volume's `_data` lives and rewrites the prefix.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

import docker
from docker.errors import APIError, ImageNotFound, NotFound

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


class HostPathResolver:
    """Rewrite container-visible paths to their host-visible counterparts.

    Configured with a set of (container_root, volume_name) mappings. On
    first use, each mapping's named docker volume is inspected to
    discover where its `_data` lives on the host and that mountpoint is
    cached.

    `resolve(path)` tries each mapping; a path under an unmapped or
    unresolvable root is returned unchanged (works for bind mounts, k8s
    PVs, or non-container dev envs).
    """

    def __init__(self, client, mappings: dict[str, str | None]) -> None:
        self._client = client
        # Longest-prefix-first so a deeper mount wins over a shallower one.
        self._mappings: list[tuple[PurePosixPath, str | None]] = sorted(
            ((PurePosixPath(root), vol) for root, vol in mappings.items()),
            key=lambda m: len(str(m[0])),
            reverse=True,
        )
        self._host_roots: dict[PurePosixPath, PurePosixPath | None] = {}

    def _host_root_for(self, container_root: PurePosixPath,
                       volume_name: str | None) -> PurePosixPath | None:
        if container_root in self._host_roots:
            return self._host_roots[container_root]
        host_root: PurePosixPath | None = None
        if volume_name:
            try:
                vol = self._client.volumes.get(volume_name)
                mountpoint = vol.attrs.get("Mountpoint")
                if mountpoint:
                    host_root = PurePosixPath(mountpoint)
                    logger.info("cli-tools host-path remap: %s -> %s",
                                container_root, host_root)
            except NotFound:
                logger.info("cli-tools volume %r not found; skipping remap",
                            volume_name)
        self._host_roots[container_root] = host_root
        return host_root

    def resolve(self, container_path: str) -> str:
        p = PurePosixPath(container_path)
        for container_root, volume_name in self._mappings:
            try:
                relative = p.relative_to(container_root)
            except ValueError:
                continue
            host_root = self._host_root_for(container_root, volume_name)
            if host_root is None:
                return container_path
            return str(host_root / relative)
        return container_path


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
        self._host_path_resolver = HostPathResolver(
            client=self._client,
            mappings={
                os.environ.get("CLI_BINARIES_ROOT", "/data/cli_binaries"):
                    os.environ.get("CLI_BINARIES_VOLUME_NAME"),
                os.environ.get("CLI_STATE_ROOT", "/data/cli_state"):
                    os.environ.get("CLI_STATE_VOLUME_NAME"),
            },
        )

    async def run(
        self,
        binary_host_path: str,
        args: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: int = 30,
        home_host_path: str | None = None,
    ) -> BinaryRunResult:
        """Execute `binary_host_path` inside a one-shot sandbox container.

        Args:
            binary_host_path: absolute path on the host to the binary. Must
                be readable + executable. Mounted read-only at /binary.
            args: arguments passed to the binary.
            env: environment variables passed to the container.
            timeout_seconds: kill the container if it runs longer than this.
            home_host_path: absolute path on the host to a per-(tool,user)
                directory that will be mounted rw at /home/sandbox and
                used as HOME. Persists across runs. Omit for stateless
                tools — they get an ephemeral /tmp HOME.
        """
        container_path = Path(binary_host_path).resolve()
        if not container_path.is_file():
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=0,
                sandbox_failed=True,
                error=f"binary not found on host: {container_path}",
            )

        # The path the backend container sees is not the path the docker
        # daemon sees when we're running docker-out-of-docker. Translate.
        daemon_path = self._host_path_resolver.resolve(str(container_path))
        daemon_home = (
            self._host_path_resolver.resolve(home_host_path)
            if home_host_path
            else None
        )

        start = time.monotonic()
        try:
            inner = await asyncio.to_thread(
                self._run_blocking,
                daemon_path,
                list(args),
                dict(env),
                timeout_seconds,
                daemon_home,
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

    # Baseline env every sandbox binary sees. The rootfs is read-only and
    # nobody's HOME in /etc/passwd is `/nonexistent`, so any runtime that
    # touches HOME (Bun, Node npm, pip, …) crashes with EROFS. Pointing
    # HOME and XDG_* into the /tmp tmpfs makes those write attempts land
    # somewhere writable — per-invocation, ephemeral. Stateful tools
    # override HOME via `home_mount` (see _run_blocking).
    _DEFAULT_ENV: dict[str, str] = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/tmp/.cache",
        "XDG_CONFIG_HOME": "/tmp/.config",
        "XDG_DATA_HOME": "/tmp/.local/share",
        "XDG_STATE_HOME": "/tmp/.local/state",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }

    # When a persistent HOME is bind-mounted, it replaces the tmpfs
    # defaults. XDG_* also move under the persistent root so caches and
    # config land in the same place the tool thinks is "home".
    _PERSISTENT_HOME_ENV: dict[str, str] = {
        "HOME": "/home/sandbox",
        "XDG_CACHE_HOME": "/home/sandbox/.cache",
        "XDG_CONFIG_HOME": "/home/sandbox/.config",
        "XDG_DATA_HOME": "/home/sandbox/.local/share",
        "XDG_STATE_HOME": "/home/sandbox/.local/state",
    }

    def _run_blocking(
        self,
        host_path: str,
        args: list[str],
        env: dict[str, str],
        timeout_seconds: int,
        home_host_path: str | None = None,
    ) -> BinaryRunResult:
        """Synchronous docker-SDK invocation; called in a thread."""
        # Layer: defaults < persistent-home defaults (if any) < tool env.
        # Operators always win — if they pin HOME somewhere unusual,
        # that's their call.
        merged_env: dict[str, str] = {**self._DEFAULT_ENV}
        if home_host_path:
            merged_env.update(self._PERSISTENT_HOME_ENV)
        merged_env.update(env)

        volumes: dict[str, dict[str, str]] = {
            host_path: {"bind": "/binary", "mode": "ro"},
        }
        if home_host_path:
            # rw so the tool can write login tokens / caches; no-new-
            # privileges + cap-drop are still in force so this can't be
            # used to escape the sandbox.
            volumes[home_host_path] = {"bind": "/home/sandbox", "mode": "rw"}

        container = self._client.containers.create(
            image=self.image,
            command=["/binary", *args],
            environment=merged_env,
            network_disabled=not self.network,
            read_only=True,
            tmpfs={"/tmp": f"rw,size={self.tmpfs_size},mode=1777"},
            mem_limit=self.memory_limit,
            nano_cpus=int(float(self.cpu_limit) * 1_000_000_000),
            pids_limit=self.pids_limit,
            user="65534:65534",
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],
            volumes=volumes,
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
