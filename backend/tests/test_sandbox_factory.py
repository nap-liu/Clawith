"""Tests for the sandbox backend factory.

These don't spin up real docker or real bwrap: they just verify the
selection logic and singleton caching.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.sandbox.factory import get_sandbox_backend


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """Each test starts with a clean factory cache.

    The factory memoises with ``functools.cache`` so returns are stable
    across a process — which is what we want in prod, but not between
    tests that mock out dependencies.
    """
    get_sandbox_backend.cache_clear()
    yield
    get_sandbox_backend.cache_clear()


def test_docker_backend_singleton():
    """Two calls with 'docker' return the *same* instance (no per-tool rebuild)."""
    # Avoid hitting a real docker daemon by mocking the SDK entrypoint.
    with patch(
        "app.services.sandbox.local.binary_runner.docker.from_env",
        return_value=object(),
    ):
        a = get_sandbox_backend("docker")
        b = get_sandbox_backend("docker")
    assert a is b


def test_bwrap_backend_raises_on_non_linux():
    """BubblewrapBackend must refuse to construct when bwrap is missing.

    The factory deliberately does NOT fall back to docker: if a tool author
    explicitly asked for 'bwrap', a missing bwrap is an environment bug
    and should surface loudly, not silently demote to docker.
    """
    with patch("app.services.sandbox.local.bwrap_backend.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="bwrap"):
            get_sandbox_backend("bwrap")


def test_unknown_backend_name_raises():
    """Typos / future backends must not silently default to docker."""
    with pytest.raises(ValueError, match="unknown sandbox backend"):
        get_sandbox_backend("vm")


def test_bwrap_backend_singleton_when_available():
    """When bwrap is available (mocked), repeated calls return the same instance."""
    with patch("app.services.sandbox.local.bwrap_backend.shutil.which", return_value="/usr/bin/bwrap"):
        a = get_sandbox_backend("bwrap")
        b = get_sandbox_backend("bwrap")
    assert a is b
    # Sanity: it really is the bubblewrap impl, not the docker fallback.
    from app.services.sandbox.local.bwrap_backend import BubblewrapBackend
    assert isinstance(a, BubblewrapBackend)
