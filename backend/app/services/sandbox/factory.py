"""Factory for :class:`SandboxBackend` singletons.

Only one backend is registered now: ``subprocess``. Docker and bwrap
were removed because the CLI-tools trust model (first-party / reviewed
binaries only) does not justify their operational cost. See
docs/cli-tools/AUTHOR_GUIDE.md §"Sandbox contract" for the threat
model this decision assumes.
"""

from __future__ import annotations

import functools
from typing import Literal

from app.services.sandbox.backend import SandboxBackend

SandboxBackendName = Literal["subprocess"]


@functools.cache
def get_sandbox_backend(backend_name: str = "subprocess") -> SandboxBackend:
    """Return the singleton SubprocessBinaryBackend.

    ``backend_name`` is accepted for call-site compatibility with legacy
    callers that still pass a value; anything other than ``"subprocess"``
    raises ``ValueError`` so config drift surfaces loudly instead of
    silently running under an unexpected backend.
    """
    if backend_name != "subprocess":
        raise ValueError(
            f"unknown sandbox backend: {backend_name!r}. "
            f"Only 'subprocess' is supported."
        )
    from app.services.sandbox.local.subprocess_binary_backend import (
        SubprocessBinaryBackend,
    )
    return SubprocessBinaryBackend()


__all__ = ["get_sandbox_backend", "SandboxBackendName"]
