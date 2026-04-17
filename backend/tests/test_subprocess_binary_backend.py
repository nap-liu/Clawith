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
