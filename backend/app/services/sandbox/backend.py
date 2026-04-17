"""Pluggable sandbox backend Protocol for the CLI-tools subsystem.

A ``SandboxBackend`` executes a single host-side binary inside some form
of isolated environment and returns a ``BinaryRunResult``. Implementations
trade off isolation strength against cold-start latency:

- ``DockerSandboxBackend``  — full container per call, ~300 ms cold start,
  strong isolation (separate namespaces, read-only rootfs, cap-drop,
  docker's default seccomp). Works everywhere docker works.
- ``BubblewrapBackend``     — Linux namespaces via bwrap(1), ~30 ms cold
  start, weaker isolation (shared kernel, no per-call seccomp BPF in the
  v1 implementation). Linux-only. Intended for first-party / reviewed
  tools where the ~10x latency win matters (e.g. hot-path agent tools).

The Protocol is deliberately narrow: it exposes exactly what the CLI
executor needs (``run()``), nothing else. Backends can add implementation-
specific helpers but the executor never calls them.
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, runtime_checkable

from app.services.sandbox.local.binary_runner import BinaryRunResult


@runtime_checkable
class SandboxBackend(Protocol):
    """Contract every sandbox implementation must satisfy.

    Implementations are stateless per-call: everything a run needs is
    passed to ``run()``. A single instance can serve every tool and
    every tenant.
    """

    async def run(
        self,
        binary_host_path: str,
        args: Sequence[str],
        env: Mapping[str, str],
        *,
        timeout_seconds: int,
        home_host_path: str | None,
        image: str | None,
        cpu_limit: str,
        memory_limit: str,
        network: bool,
        pids_limit: int,
        tmpfs_size: str,
    ) -> BinaryRunResult:
        """Execute ``binary_host_path`` inside the sandbox and return the outcome.

        Args:
            binary_host_path: absolute path on the host (readable + executable).
            args: argv passed to the binary.
            env: environment variables exposed inside the sandbox.
            timeout_seconds: kill the process group after this many seconds.
            home_host_path: optional rw-mounted HOME for persistent tools.
            image: sandbox image override (docker-only; bwrap ignores).
            cpu_limit: per-call CPU quota ("1.0", "0.5", …). Some backends
                may not enforce this — check the backend docstring.
            memory_limit: per-call memory ("512m", "1g"). May be advisory.
            network: True → allow network. Default is deny.
            pids_limit: max processes inside the sandbox.
            tmpfs_size: size of the /tmp tmpfs mount.
        """
        ...


__all__ = ["SandboxBackend", "BinaryRunResult"]
