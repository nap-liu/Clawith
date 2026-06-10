"""Per-(tenant, tool, user) persistent HOME for stateful CLIs.

Scope choice — why (tenant, tool, user):
  * user-only would let svc read git's config and vice versa. It
    corrupts cache semantics and makes upgrade stories (tool-specific
    cache invalidation) impossible.
  * (tool, user) is the correct cache unit: same person using the same
    tool in two conversations should keep their login; different tools
    must not share each other's caches.
  * tenant is the outer namespace to prevent ID collisions when the same
    tool ID or user ID appears across different companies.

  NOTE on isolation: this layout provides cache-correctness scoping, NOT
  security isolation. Every leaf is chown'd to the same sandbox uid
  (gem=1000) and the volume is mounted whole into the shared sandbox, so
  any process there can read every other user's leaf. This is an accepted
  boundary for the trusted-agent use-case (spec v4 §1.3): agents already
  run with sudo inside the sandbox, so per-user DAC isolation is not a
  meaningful guarantee here.

Layout on disk (inside the backend container; volume is bind-mounted at
`/data/cli_state`; the aio-sandbox container mounts the same volume at
`/data/cli_state` (same path as backend)):

    /data/cli_state/
      <tenant_id>/
        <tool_id>/
          <user_id>/           <- HOME for the sandbox shell
            .config/...        <- whatever the binary decides to write
            .cache/...

Permission model:
  * The backend process runs as root (uid 0) and can `mkdir`/`chown`
    new subtrees freely.
  * Each leaf directory is chown'd to 1000:1000 (aio-sandbox user 'gem')
    so the sandbox shell can write token caches inside it.
  * Intermediate tenant/tool directories use mode 2775 (other=r-x), so
    gem can traverse and enumerate them. This is intentional: per-user
    DAC isolation is not enforced at this layer (see NOTE above).
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_SANDBOX_UID = 1000  # aio-sandbox shell user 'gem'
_SANDBOX_GID = 1000


def _dir_size_bytes(path: Path) -> int:
    """Sum file sizes under `path`, tolerating missing entries / races."""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except (FileNotFoundError, OSError):
                continue
    except (FileNotFoundError, OSError):
        return 0
    return total


class StateStorage:
    """Resolve and ensure the on-disk state directory for a given (tool, user).

    The returned path is container-visible and needs no translation: the
    aio-sandbox container mounts the same volume at the same path
    (/data/cli_state), so paths rendered here are valid inside the sandbox.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or Path(
            os.environ.get("CLI_STATE_ROOT", "/data/cli_state")
        )

    def ensure_home(
        self,
        *,
        tenant_id: str | uuid.UUID | None,
        tool_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
    ) -> Path:
        """Create the `<tenant>/<tool>/<user>` dir if missing, return it.

        Also chowns the leaf to 1000:1000 (aio-sandbox user 'gem') so
        the sandbox shell can write token caches inside it. Intermediate
        tenant/tool directories use mode 2775 and are traversable by gem;
        see module docstring NOTE on isolation for why that is accepted.
        """
        # UUIDs are the only values we accept; stringify defensively.
        tenant_segment = str(tenant_id) if tenant_id is not None else "_global"
        tool_segment = str(tool_id)
        user_segment = str(user_id)

        leaf = (self._root / tenant_segment / tool_segment / user_segment).resolve()
        # Defense in depth against traversal: resolved path must still be
        # under the configured root.
        try:
            leaf.relative_to(self._root.resolve())
        except ValueError as exc:
            raise ValueError(f"state path escaped root: {leaf}") from exc

        # `mkdir` + `chmod` rather than passing mode= directly, because
        # `mode` is masked by the process umask and we need group-write
        # reliably. 0o2775 (setgid + owner/group rwx, other=r-x) makes
        # all levels traversable by the sandbox user gem (uid 1000).
        for path in (
            self._root / tenant_segment,
            self._root / tenant_segment / tool_segment,
            leaf,
        ):
            path.mkdir(exist_ok=True)
            try:
                path.chmod(0o2775)
            except PermissionError:
                # Not our directory (e.g. created by a prior run under a
                # different config). Leave it; the sandbox will fail
                # loudly on first write if the perms are actually wrong,
                # which is easier to debug than a silent fix here.
                logger.debug("cli-tools: chmod skipped for %s", path)
        try:
            os.chown(leaf, _SANDBOX_UID, _SANDBOX_GID)
        except PermissionError:
            # Non-root dev environments can't chown; the sandbox will
            # fail loudly on first write if perms are actually wrong,
            # which is easier to debug than a silent fix here.
            logger.debug("cli-tools: chown skipped for %s", leaf)
        return leaf

    def _leaf_path(
        self,
        *,
        tenant_id: str | uuid.UUID | None,
        tool_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
    ) -> Path:
        """Compute the `<tenant>/<tool>/<user>` path without creating it."""
        tenant_segment = str(tenant_id) if tenant_id is not None else "_global"
        return self._root / tenant_segment / str(tool_id) / str(user_id)

    def get_home_usage_bytes(
        self,
        tenant_id: str | uuid.UUID | None,
        tool_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
    ) -> int:
        """Recursively sum file sizes under the per-(tool, user) HOME.

        Returns 0 if the directory does not exist yet. No caching: most
        HOMEs are small (config files, a token or two, a few MB of cache)
        and `os.walk` over them is well under 1ms. Revisit if profiling
        ever says otherwise.
        """
        leaf = self._leaf_path(tenant_id=tenant_id, tool_id=tool_id, user_id=user_id)
        if not leaf.exists():
            return 0
        return _dir_size_bytes(leaf)

    def check_quota(
        self,
        tenant_id: str | uuid.UUID | None,
        tool_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
        limit_mb: int,
    ) -> tuple[bool, int]:
        """Check whether current HOME usage is within `limit_mb`.

        Returns `(within_limit, current_bytes)`. `limit_mb == 0` short-
        circuits to `(True, 0)` — the caller opted out of the check. The
        comparison is `<=` so the limit is inclusive: a HOME sitting at
        exactly `limit_mb * 1024 * 1024` bytes is still allowed to run.
        """
        if limit_mb == 0:
            return True, 0
        current = self.get_home_usage_bytes(tenant_id, tool_id, user_id)
        within = current <= limit_mb * 1024 * 1024
        return within, current

    def clear_home(
        self,
        tenant_id: str | uuid.UUID | None,
        tool_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
    ) -> int:
        """Hard-delete the `<tenant>/<tool>/<user>/` leaf. Returns bytes freed.

        Used by the quota-reset admin endpoint. Tolerates a missing
        directory (returns 0) and never raises: `ignore_errors=True`
        mirrors the other delete_* helpers.
        """
        leaf = self._leaf_path(tenant_id=tenant_id, tool_id=tool_id, user_id=user_id)
        if not leaf.exists():
            return 0
        freed = _dir_size_bytes(leaf)
        shutil.rmtree(leaf, ignore_errors=True)
        if leaf.exists():
            logger.warning(
                "cli-tools.quota: failed to fully remove %s (partial rmtree)", leaf
            )
        return freed

    def delete_tool(
        self,
        tenant_id: str | uuid.UUID | None,
        tool_id: str | uuid.UUID,
    ) -> int:
        """Hard-delete `<tenant>/<tool>/` subtree. Returns bytes freed.

        Tolerates a missing directory (returns 0). Errors during rmtree are
        swallowed (ignore_errors=True); a lingering directory afterwards is
        logged as a warning so stale state can't silently outlive its Tool.
        """
        tenant_segment = str(tenant_id) if tenant_id is not None else "_global"
        target = self._root / tenant_segment / str(tool_id)
        if not target.exists():
            return 0
        freed = _dir_size_bytes(target)
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            logger.warning(
                "cli-tools.gc: failed to fully remove %s (partial rmtree)", target
            )
        return freed

    def delete_tenant(self, tenant_id: str | uuid.UUID) -> int:
        """Hard-delete the entire `<tenant>/` subtree. Returns bytes freed."""
        target = self._root / str(tenant_id)
        if not target.exists():
            return 0
        freed = _dir_size_bytes(target)
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            logger.warning(
                "cli-tools.gc: failed to fully remove %s (partial rmtree)", target
            )
        return freed

    def delete_user(self, user_id: str | uuid.UUID) -> int:
        """Hard-delete every `<tenant>/<tool>/<user>/` subdir for this user.

        Walks all tenants/tools under the root. Returns bytes freed across
        all matches. Only a function for now — no user-deletion endpoint
        exists to hook it into; call from that endpoint once it ships.
        """
        user_segment = str(user_id)
        if not self._root.exists():
            return 0
        total_freed = 0
        # `<root>/<tenant>/<tool>/<user>` — glob matches only depth-3 dirs.
        for candidate in self._root.glob(f"*/*/{user_segment}"):
            if not candidate.is_dir():
                continue
            freed = _dir_size_bytes(candidate)
            shutil.rmtree(candidate, ignore_errors=True)
            if candidate.exists():
                logger.warning(
                    "cli-tools.gc: failed to fully remove %s (partial rmtree)",
                    candidate,
                )
                continue
            total_freed += freed
        return total_freed
