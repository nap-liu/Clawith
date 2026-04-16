"""Seccomp BPF filter scaffolding for BubblewrapBackend.

Spec §task-2 called for an allow-list of syscalls compiled to BPF and fed
to bwrap via ``--seccomp-bpf``. Doing that properly requires the
``libseccomp-python`` package and a kernel with CONFIG_SECCOMP=y, which
adds a non-trivial build/runtime dep for the backend container.

For v1 we opted for the "simple" variant documented in the task:
``BubblewrapBackend`` does **not** load a BPF filter — it relies on
``--unshare-all`` + ``--cap-drop ALL`` + ``--new-session`` for isolation.
That's weaker than docker's default seccomp and is the reason bwrap is
not a drop-in for untrusted code — see the backend's class docstring.

``BPF_FILTER`` is kept as an empty ``bytes`` so callers that feature-gate
on ``len(BPF_FILTER)`` see zero, and so a future PR can drop in a real
filter without changing call-sites.
"""

from __future__ import annotations

# Intentionally empty in v1 — see module docstring for rationale.
BPF_FILTER: bytes = b""

__all__ = ["BPF_FILTER"]
