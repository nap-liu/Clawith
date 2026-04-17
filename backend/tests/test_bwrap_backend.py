"""Integration-ish tests for BubblewrapBackend.

These exercise the real bwrap binary against small shebang scripts. The
whole module is skipped when bwrap is not installed — macOS / Windows
dev environments automatically pass (0 tests collected for this file).
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not shutil.which("bwrap"),
    reason="bwrap not installed; BubblewrapBackend is Linux-only",
)


def _script(tmp_path: Path, body: str) -> Path:
    """Write an executable shebang script to ``tmp_path`` and return its path."""
    script = tmp_path / "runme.sh"
    script.write_text(textwrap.dedent(body).lstrip())
    script.chmod(0o555)
    return script


@pytest.mark.asyncio
async def test_bwrap_happy_path(tmp_path):
    """Basic sanity: stdout captured, exit=0, no flags tripped."""
    from app.services.sandbox.local.bwrap_backend import BubblewrapBackend

    script = _script(tmp_path, """\
        #!/bin/sh
        echo hi
    """)
    backend = BubblewrapBackend()
    result = await backend.run(
        binary_host_path=str(script),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="512m",
        network=False,
        pids_limit=100,
        tmpfs_size="64m",
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "hi"
    assert result.sandbox_failed is False
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_bwrap_home_mount_is_rw(tmp_path):
    """When home_host_path is bind-mounted, writes into HOME persist on the host."""
    from app.services.sandbox.local.bwrap_backend import BubblewrapBackend

    script = _script(tmp_path, """\
        #!/bin/sh
        echo "persisted" > "$HOME/marker"
    """)
    home = tmp_path / "home"
    home.mkdir()

    backend = BubblewrapBackend()
    result = await backend.run(
        binary_host_path=str(script),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=str(home),
        image=None,
        cpu_limit="1.0",
        memory_limit="512m",
        network=False,
        pids_limit=100,
        tmpfs_size="64m",
    )
    assert result.exit_code == 0
    assert (home / "marker").read_text().strip() == "persisted"


@pytest.mark.asyncio
async def test_bwrap_network_disabled(tmp_path):
    """With network=False, outbound TCP must fail (unreachable / refused)."""
    from app.services.sandbox.local.bwrap_backend import BubblewrapBackend

    script = _script(tmp_path, """\
        #!/usr/bin/env python3
        import socket, sys
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=2)
            sys.exit(0)
        except OSError:
            sys.exit(42)
    """)
    backend = BubblewrapBackend()
    result = await backend.run(
        binary_host_path=str(script),
        args=[],
        env={},
        timeout_seconds=10,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="512m",
        network=False,
        pids_limit=100,
        tmpfs_size="64m",
    )
    # The namespaced process either fails to connect (exit 42) or the
    # runtime can't even resolve the interface (non-zero exit). Both
    # prove network isolation; what we must NOT see is a successful
    # connection (exit 0).
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_bwrap_missing_binary(tmp_path):
    """Non-existent host path short-circuits to sandbox_failed=True."""
    from app.services.sandbox.local.bwrap_backend import BubblewrapBackend

    backend = BubblewrapBackend()
    result = await backend.run(
        binary_host_path=str(tmp_path / "does-not-exist"),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="512m",
        network=False,
        pids_limit=100,
        tmpfs_size="64m",
    )
    assert result.sandbox_failed is True
    assert result.exit_code == 1
    assert "not found" in result.error.lower()
