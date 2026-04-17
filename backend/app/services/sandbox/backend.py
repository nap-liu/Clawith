"""Pluggable sandbox backend Protocol for the CLI-tools subsystem.

Only one implementation remains: SubprocessBinaryBackend. The protocol
is kept so tests and custom stacks can inject a fake runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable


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


@runtime_checkable
class SandboxBackend(Protocol):
    """Contract every sandbox implementation must satisfy.

    Stateless per-call: everything a run needs is passed to ``run()``.
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
    ) -> BinaryRunResult:
        ...


__all__ = ["SandboxBackend", "BinaryRunResult"]
