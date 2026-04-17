"""Tests for the sandbox backend factory."""

from __future__ import annotations

import pytest

from app.services.sandbox.factory import get_sandbox_backend
from app.services.sandbox.local.subprocess_binary_backend import (
    SubprocessBinaryBackend,
)


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    get_sandbox_backend.cache_clear()
    yield
    get_sandbox_backend.cache_clear()


def test_default_backend_is_subprocess():
    assert isinstance(get_sandbox_backend(), SubprocessBinaryBackend)


def test_subprocess_backend_is_singleton():
    assert get_sandbox_backend("subprocess") is get_sandbox_backend("subprocess")


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        get_sandbox_backend("docker")
    with pytest.raises(ValueError):
        get_sandbox_backend("bwrap")
