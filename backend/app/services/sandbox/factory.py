"""Factory for :class:`SandboxBackend` singletons, keyed by backend name.

Each process keeps one instance per backend name (via ``functools.cache``).
Backends are stateless per-call so sharing across coroutines is safe; and
since both concrete implementations hold expensive handles (docker
client / the bwrap availability check), caching them avoids per-tool
reinstantiation overhead.

There is no transparent fallback between backends. If the tool author
selected ``"bwrap"`` and bwrap is not installed, the factory raises
``RuntimeError`` so the environment issue surfaces at startup instead of
silently running under a different isolation model than configured.
"""

from __future__ import annotations

import functools
import os
from typing import Literal

from app.services.sandbox.backend import SandboxBackend


SandboxBackendName = Literal["docker", "bwrap"]


def _default_sandbox_image() -> str:
    """The image DockerSandboxBackend should fall back to when a tool
    doesn't pin its own ``sandbox.image``. Environment-overridable so
    staging/prod can pin a tagged image while dev uses ``:stable``.
    """
    return os.environ.get("CLI_SANDBOX_IMAGE", "clawith-cli-sandbox:stable")


@functools.cache
def get_sandbox_backend(backend_name: str) -> SandboxBackend:
    """Return a singleton backend by name.

    Valid names:

    - ``"docker"`` — default, works everywhere docker works. ~300ms cold
      start. Strong isolation.
    - ``"bwrap"`` — Linux-only. ~30ms cold start. Weaker isolation.
      Raises :class:`RuntimeError` if bwrap is not installed on the
      current host.

    Unknown names raise :class:`ValueError` — we refuse to guess.

    The result is cached per-process. Tests that need to reset between
    cases can call ``get_sandbox_backend.cache_clear()``.
    """
    if backend_name == "docker":
        # Deferred import keeps this module importable on systems
        # without the docker SDK installed, as long as no-one actually
        # requests the docker backend.
        from app.services.sandbox.local.binary_runner import DockerSandboxBackend
        return DockerSandboxBackend(default_image=_default_sandbox_image())

    if backend_name == "bwrap":
        from app.services.sandbox.local.bwrap_backend import BubblewrapBackend
        # BubblewrapBackend.__init__ raises RuntimeError when bwrap is
        # missing — that's what we want. Don't catch it: forcing a hard
        # failure is the whole point of not silently falling back.
        return BubblewrapBackend()

    raise ValueError(
        f"unknown sandbox backend: {backend_name!r}. "
        f"Valid choices: 'docker', 'bwrap'."
    )


__all__ = ["get_sandbox_backend", "SandboxBackendName"]
