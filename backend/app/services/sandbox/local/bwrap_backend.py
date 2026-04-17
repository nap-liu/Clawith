"""Bubblewrap-based sandbox backend. Linux-only, fast, weaker isolation.

BubblewrapBackend
=================

Linux-only. Uses ``bwrap(1)`` for fast namespace isolation.

Trade-off vs :class:`DockerSandboxBackend`:

- 10x faster cold start (~30ms vs ~300ms) — no container create/remove,
  no image pull, just ``execve(bwrap, …)``.
- Same kernel as the host, not a separate one — so the isolation is
  namespace-level and whatever seccomp/cap-drop we manage to set, rather
  than hypervisor-level.
- Not suitable for fully untrusted binaries; suitable for first-party or
  reviewed tools where the latency win matters.

Requires
--------

- ``bwrap`` binary installed in the backend container
  (``apt install bubblewrap``). The Dockerfile takes care of this for
  the main backend image.
- Linux host — macOS dev environments don't have bwrap, so the
  constructor raises ``RuntimeError``. The factory in
  ``app.services.sandbox.factory`` **does not** transparently fall back
  to docker when bwrap is missing: if the tool author explicitly chose
  ``backend: "bwrap"`` we want the failure to surface so it can be fixed,
  rather than silently switching to a backend they didn't configure for.

v1 limitations (documented trade-offs)
--------------------------------------

- **No seccomp BPF filter.** Installing a libseccomp-based allow-list is
  a followup; see ``_seccomp.py``. v1 relies on ``--unshare-all`` +
  ``--cap-drop ALL`` + ``--new-session`` + no-new-privileges.
- **No CPU/memory cgroup limits.** bwrap(1) itself does not configure
  cgroups; the recommended production setup is to run the backend under
  ``systemd-run --scope -p MemoryMax=… -p CPUQuota=…`` or under a
  cgroupsv2-aware supervisor. The ``cpu_limit`` / ``memory_limit``
  kwargs are accepted (so the interface matches the docker backend)
  but only logged at WARNING level — they do not take effect.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Mapping, Sequence

from app.services.sandbox.local.binary_runner import BinaryRunResult

logger = logging.getLogger(__name__)


_DEFAULT_ENV: dict[str, str] = {
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "XDG_CACHE_HOME": "/tmp/.cache",
    "XDG_CONFIG_HOME": "/tmp/.config",
    "XDG_DATA_HOME": "/tmp/.local/share",
    "XDG_STATE_HOME": "/tmp/.local/state",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}

_PERSISTENT_HOME_ENV: dict[str, str] = {
    "HOME": "/home/sandbox",
    "XDG_CACHE_HOME": "/home/sandbox/.cache",
    "XDG_CONFIG_HOME": "/home/sandbox/.config",
    "XDG_DATA_HOME": "/home/sandbox/.local/share",
    "XDG_STATE_HOME": "/home/sandbox/.local/state",
}


class BubblewrapBackend:
    """Namespace-isolated binary runner via ``bwrap(1)``.

    Matches the :class:`app.services.sandbox.backend.SandboxBackend`
    Protocol — one instance serves all tools, all per-call parameters
    flow through :meth:`run`.

    Egress allowlist: bwrap has no native "allow some hosts" primitive —
    ``--unshare-net`` is all-or-nothing and ``--share-net`` reopens the
    host network. The executor therefore passes the allowlist through as
    the ``CLAWITH_EGRESS_ALLOWLIST`` env var for cooperative CLIs, but
    this backend does NOT enforce it at the kernel level. Real
    enforcement requires an external network namespace with nftables
    rules and is tracked in
    ``docs/superpowers/TODO-egress-enforcement.md``.
    """

    def __init__(self, *, default_image: str = "n/a") -> None:
        """Verify bwrap is available; raise RuntimeError if not.

        ``default_image`` is accepted for interface parity with
        :class:`DockerSandboxBackend` but is unused — bwrap doesn't run
        images.
        """
        if not shutil.which("bwrap"):
            raise RuntimeError(
                "bwrap not installed; BubblewrapBackend is Linux-only. "
                "Install with `apt-get install bubblewrap` (or use the "
                "docker backend on macOS/Windows dev environments)."
            )
        self.default_image = default_image

    async def run(
        self,
        binary_host_path: str,
        args: Sequence[str],
        env: Mapping[str, str],
        *,
        timeout_seconds: int = 30,
        home_host_path: str | None = None,
        image: str | None = None,  # accepted for Protocol parity, unused
        cpu_limit: str = "1.0",
        memory_limit: str = "512m",
        network: bool = False,
        pids_limit: int = 100,  # accepted for Protocol parity, unused in v1
        tmpfs_size: str = "64m",
    ) -> BinaryRunResult:
        """Execute ``binary_host_path`` inside a bwrap sandbox.

        See :class:`app.services.sandbox.backend.SandboxBackend` for the
        contract. CPU/memory limits are **not enforced** in v1 (see module
        docstring); passing non-default values logs a warning.
        """
        start = time.monotonic()

        # Missing binary shortcut — same error shape as the docker backend
        # so upstream `_classify_failure` treats both uniformly.
        bin_path = Path(binary_host_path).resolve()
        if not bin_path.is_file():
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - start) * 1000),
                sandbox_failed=True,
                error=f"binary not found on host: {bin_path}",
            )

        if cpu_limit != "1.0" or memory_limit != "512m":
            logger.warning(
                "bwrap backend: cpu_limit=%s memory_limit=%s requested but "
                "bwrap(1) does not enforce cgroup limits; use systemd-run "
                "or a cgroupsv2 wrapper if you need hard resource caps.",
                cpu_limit, memory_limit,
            )

        # Merge env the same way the docker backend does so tools see a
        # consistent baseline regardless of which sandbox they land in.
        merged_env: dict[str, str] = {**_DEFAULT_ENV}
        if home_host_path:
            merged_env.update(_PERSISTENT_HOME_ENV)
        merged_env.update(env)

        cmd = self._build_bwrap_argv(
            bin_host_path=str(bin_path),
            bin_args=list(args),
            home_host_path=home_host_path,
            network=network,
            tmpfs_size=tmpfs_size,
        )

        # `env=…` here replaces the parent env wholesale rather than
        # layering on top: the sandboxed process should only see what we
        # explicitly expose, not whatever the backend container has in
        # its environment (DATABASE_URL, SECRET_KEY, …).
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=merged_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # bwrap verified at __init__ time, but the interpreter may
            # have been restarted / PATH mutated since. Surface as
            # sandbox_failed so the caller doesn't confuse it with a
            # binary exit failure.
            return BinaryRunResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - start) * 1000),
                sandbox_failed=True,
                error=f"bwrap exec failed: {exc}",
            )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
            exit_code = proc.returncode if proc.returncode is not None else -1
        except asyncio.TimeoutError:
            timed_out = True
            # SIGKILL the whole bwrap process; --die-with-parent plus the
            # fresh PID namespace (from --unshare-all) should take the
            # sandboxed process down with it.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout_bytes, stderr_bytes = await proc.communicate()
            except Exception:
                stdout_bytes, stderr_bytes = b"", b""
            exit_code = -1

        return BinaryRunResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=timed_out,
        )

    # ─── argv construction ─────────────────────────────────────────────

    def _build_bwrap_argv(
        self,
        *,
        bin_host_path: str,
        bin_args: list[str],
        home_host_path: str | None,
        network: bool,
        tmpfs_size: str,
    ) -> list[str]:
        """Return the full ``bwrap`` argv for one sandboxed invocation.

        Kept as a pure function of its inputs so tests can assert on the
        exact flag layout without spawning a subprocess.
        """
        argv: list[str] = [
            "bwrap",
            # Drop every namespace by default; `--share-net` puts the net
            # namespace back when the tool asked for network.
            "--unshare-all",
            # Fresh session + die-with-parent so a runaway binary can't
            # outlive the bwrap invocation.
            "--new-session",
            "--die-with-parent",
            # Belt-and-braces: deny additional privileges even if
            # somehow a setuid bit slips through.
            "--cap-drop", "ALL",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--unshare-cgroup-try",
            # Standard read-only filesystem view. /usr, /bin, /lib* come
            # from the host so dynamic binaries (e.g. glibc) resolve.
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib",
        ]

        # /lib64 only exists on x86_64 glibc hosts; tolerate absence.
        if os.path.isdir("/lib64"):
            argv += ["--ro-bind", "/lib64", "/lib64"]

        argv += [
            "--ro-bind", "/etc/alternatives", "/etc/alternatives",
            "--proc", "/proc",
            "--dev", "/dev",
            # tmpfs /tmp so intermediate writes go somewhere writable but
            # vanish at the end of the run.
            "--tmpfs", "/tmp",
            # Mount the binary read-only at a known path. Using /binary
            # mirrors the docker backend so argv templates are portable.
            "--ro-bind", bin_host_path, "/binary",
        ]

        if home_host_path:
            argv += ["--bind", home_host_path, "/home/sandbox"]
        else:
            argv += ["--tmpfs", "/home/sandbox"]

        # Network: bwrap --unshare-all already dropped the net ns. Only
        # if the caller asked for network do we put it back with
        # --share-net. (Note: bwrap has no single flag for "allow net
        # but unshare everything else" — you re-share by naming it.)
        if network:
            argv += ["--share-net"]

        # tmpfs_size isn't directly configurable on bwrap's --tmpfs in
        # older releases; newer bwrap accepts --size=. Pass best-effort
        # with the modern flag; if this bwrap is too old it simply
        # ignores unknown size and uses kernel defaults (which is fine).
        # Deliberately not asserting — this is an "advisory" knob.
        _ = tmpfs_size  # acknowledge the parameter; see note above.

        argv += ["/binary", *bin_args]
        return argv


__all__ = ["BubblewrapBackend"]
