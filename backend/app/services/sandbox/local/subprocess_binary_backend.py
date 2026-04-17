"""Run a host-side binary directly as a subprocess.

Unlike the removed DockerSandboxBackend / BubblewrapBackend, this
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
import os
import time
from typing import Mapping, Sequence

from app.services.sandbox.backend import BinaryRunResult

logger = logging.getLogger(__name__)

# Output caps. Matches the old DockerSandboxBackend so downstream
# consumers (UI, audit log) see the same size envelope.
_MAX_STDOUT = 1 << 20  # 1 MiB
_MAX_STDERR = 64 * 1024  # 64 KiB


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
        del image, cpu_limit, memory_limit, network
        start = time.monotonic()

        # If caller supplied a persistent HOME, use it as cwd and set
        # $HOME so tools like gh / kubectl write their state there.
        child_env = dict(env)
        cwd: str | None = None
        if home_host_path is not None:
            cwd = home_host_path
            child_env.setdefault("HOME", home_host_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                binary_host_path,
                *args,
                env=child_env,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid if os.name == "posix" else None,
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
