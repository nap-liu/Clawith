"""Run a host-side binary directly as a subprocess.

Unlike the removed container-based sandbox backends, this
backend provides *no isolation*: the child inherits the backend
process's filesystem, network, and (by default) capabilities. Linux
applies soft rlimits for cpu/memory; macOS has no equivalent and
ignores those parameters.

This backend is only appropriate when the uploaded binaries are
trusted (reviewed / whitelisted). A hostile binary can read every
file the backend can read and connect to any host the backend can
reach.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from typing import Mapping, Sequence

from app.services.sandbox.backend import BinaryRunResult

logger = logging.getLogger(__name__)

# Output caps. Matches the old DockerSandboxBackend so downstream
# consumers (UI, audit log) see the same size envelope.
_MAX_STDOUT = 1 << 20  # 1 MiB
_MAX_STDERR = 64 * 1024  # 64 KiB


def _parse_cpu_limit(raw: str) -> int | None:
    """Parse the tool's cpu_limit string into a CPU-seconds budget.

    Subprocess mode cannot enforce the per-core quota semantics docker's
    ``--cpus`` had. We reinterpret ``cpu_limit`` as a *total CPU-seconds
    budget* for a single invocation (RLIMIT_CPU takes whole seconds,
    rounded up to at least 1):

    - ``"0.5"`` → 1s (ceil; RLIMIT_CPU minimum is 1)
    - ``"1"`` / ``"1.0"`` → 1s
    - ``"2"`` → 2s
    - ``"2.5"`` → 3s
    - ``"0"`` / ``""`` / negative → None (no limit; symmetric with
      _parse_memory_limit)

    Callers treat ``None`` as "no limit".
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return max(1, math.ceil(v))


def _parse_memory_limit(raw: str) -> int | None:
    """Parse '256m' / '1g' / '512M' → bytes.

    Requires an explicit unit suffix (k/m/g). Unit-less strings like
    '256' are rejected with a warning because they almost always mean
    "256 MB" (docker muscle memory) — treating them as 256 bytes would
    OOM the child before it started.

    Returns None on parse failure or missing unit → callers treat as
    "no limit".
    """
    if not raw:
        return None
    unit = raw[-1].lower()
    if unit not in "kmg":
        logger.warning(
            "memory_limit=%r missing unit (k/m/g); treating as no limit",
            raw,
        )
        return None
    try:
        amount = float(raw[:-1])
    except (TypeError, ValueError):
        return None
    multiplier = {"k": 1024, "m": 1024**2, "g": 1024**3}[unit]
    return int(amount * multiplier)


def _make_preexec(cpu_seconds: int | None, mem_bytes: int | None):
    """Build the preexec_fn that applies rlimits + process-group creation.

    Linux-only rlimit path; on other POSIX platforms only os.setsid runs.
    """
    def _preexec():
        os.setsid()
        if sys.platform == "linux":
            import resource  # local import: non-Linux POSIX has it too, but keep the platform guard tight
            if cpu_seconds is not None:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            if mem_bytes is not None:
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    return _preexec


class SubprocessBinaryBackend:
    """Stateless subprocess-based binary runner."""

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
        del image, network  # subprocess mode ignores these (no isolation to toggle)
        start = time.monotonic()

        # If caller supplied a persistent HOME, use it as cwd and set
        # $HOME so tools like gh / kubectl write their state there.
        child_env = dict(env)
        cwd: str | None = None
        if home_host_path is not None:
            cwd = home_host_path
            # home_host_path is the infrastructure contract; tool-config env
            # must not override it. Otherwise $HOME and cwd could desync.
            child_env["HOME"] = home_host_path

        cpu_seconds = _parse_cpu_limit(cpu_limit) if sys.platform == "linux" else None
        mem_bytes = _parse_memory_limit(memory_limit) if sys.platform == "linux" else None
        preexec = _make_preexec(cpu_seconds, mem_bytes) if os.name == "posix" else None

        try:
            proc = await asyncio.create_subprocess_exec(
                binary_host_path,
                *args,
                env=child_env,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec,
            )
        except FileNotFoundError as exc:
            return BinaryRunResult(
                exit_code=127,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - start) * 1000),
                sandbox_failed=True,
                error=f"binary not found: {exc}",
            )

        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except ProcessLookupError:
                # Child already exited between timeout and killpg; nothing
                # to kill, but we still need to drain the pipes below.
                pass
            except PermissionError:
                # e.g. child changed uid and we can't signal its group.
                # Fall back to the direct kill, which may also fail but
                # gives us one more chance.
                proc.kill()
            stdout_b, stderr_b = await proc.communicate()

        return BinaryRunResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b[:_MAX_STDOUT].decode("utf-8", errors="replace"),
            stderr=stderr_b[:_MAX_STDERR].decode("utf-8", errors="replace"),
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=timed_out,
        )


__all__ = ["SubprocessBinaryBackend"]
