"""Tests for SubprocessBinaryBackend — runs a host-side binary directly
as a child process. No docker, no bwrap, no namespaces."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from app.services.sandbox.local.subprocess_binary_backend import (
    SubprocessBinaryBackend,
)


def _make_binary(tmp_path: Path, script: str) -> Path:
    """Write a tiny shell script and mark it executable."""
    p = tmp_path / "tool"
    p.write_text(textwrap.dedent(script))
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


@pytest.mark.asyncio
async def test_run_returns_stdout_and_zero_exit(tmp_path):
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        echo hello
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.sandbox_failed is False
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_run_times_out_and_kills_process(tmp_path):
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        sleep 10
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=1,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    assert result.timed_out is True
    assert result.exit_code != 0
    assert result.duration_ms < 8000  # killed well before the 10s sleep would finish


@pytest.mark.asyncio
async def test_run_captures_stderr(tmp_path):
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        echo oops >&2
        exit 3
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    assert result.exit_code == 3
    assert "oops" in result.stderr


@pytest.mark.asyncio
async def test_run_env_is_passed_to_child(tmp_path):
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        printf '%s' "$CLAWITH_FOO"
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={"CLAWITH_FOO": "bar"},
        timeout_seconds=5,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    assert result.stdout == "bar"


@pytest.mark.asyncio
async def test_run_env_does_not_inherit_parent(tmp_path):
    """Env must be exactly what the caller passed — no parent leakage."""
    os.environ["CLAWITH_LEAK_MARKER"] = "leaked"
    try:
        binary = _make_binary(tmp_path, """\
            #!/bin/sh
            printf '%s' "${CLAWITH_LEAK_MARKER:-absent}"
        """)
        backend = SubprocessBinaryBackend()
        result = await backend.run(
            binary_host_path=str(binary),
            args=[],
            env={},
            timeout_seconds=5,
            home_host_path=None,
            image=None,
            cpu_limit="1.0",
            memory_limit="256m",
            network=True,
        )
        assert result.stdout == "absent"
    finally:
        os.environ.pop("CLAWITH_LEAK_MARKER", None)


@pytest.mark.asyncio
async def test_run_with_home_sets_cwd_and_home_env(tmp_path):
    home = tmp_path / "persistent_home"
    home.mkdir()
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        pwd
        echo "$HOME"
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=str(home),
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    lines = result.stdout.strip().splitlines()
    # macOS: pwd may return the resolved path (/var → /private/var).
    assert os.path.realpath(lines[0]) == os.path.realpath(str(home))
    # $HOME is echoed verbatim as we set it.
    assert lines[1] == str(home)


@pytest.mark.asyncio
async def test_run_without_home_does_not_set_home_env(tmp_path):
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        echo "${HOME:-NO_HOME}"
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    # No HOME passed — child sees literal default from the shell fallback.
    assert result.stdout.strip() == "NO_HOME"


@pytest.mark.asyncio
async def test_run_without_home_inherits_parent_cwd(tmp_path, monkeypatch):
    """When home_host_path is None, the child inherits the parent's cwd."""
    monkeypatch.chdir(tmp_path)
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        pwd
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=5,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    # pwd resolves symlinks on macOS (/var → /private/var), so use realpath.
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(str(tmp_path))


@pytest.mark.asyncio
async def test_run_truncates_huge_stdout(tmp_path):
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        # 2 MiB of 'a'
        yes a | head -c 2097152
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=10,
        home_host_path=None,
        image=None,
        cpu_limit="1.0",
        memory_limit="256m",
        network=True,
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert len(result.stdout) == 1 << 20  # truncated to 1 MiB
    # Confirm we kept the *prefix* (not a suffix or a middle slice): the
    # child streamed 'a\n' pairs (yes(1) appends newlines), so the truncated
    # output must be exactly the 'a\n' pattern from the start.
    assert result.stdout == "a\n" * (1 << 19)


@pytest.mark.skipif(sys.platform != "linux", reason="rlimit is Linux-specific")
@pytest.mark.asyncio
async def test_run_enforces_cpu_rlimit_on_linux(tmp_path):
    """CPU quota '1' means rlimit RLIMIT_CPU=1s → busy loop dies within ~2s."""
    binary = _make_binary(tmp_path, """\
        #!/bin/sh
        # Busy loop
        awk 'BEGIN{for(;;);}'
    """)
    backend = SubprocessBinaryBackend()
    result = await backend.run(
        binary_host_path=str(binary),
        args=[],
        env={},
        timeout_seconds=10,
        home_host_path=None,
        image=None,
        cpu_limit="1",
        memory_limit="256m",
        network=True,
    )
    # Child is SIGXCPU-killed at ~1s of CPU, well before timeout_seconds=10
    assert result.duration_ms < 5000
    assert result.timed_out is False
    assert result.exit_code != 0
