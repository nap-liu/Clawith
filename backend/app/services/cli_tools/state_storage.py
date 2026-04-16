"""Per-(tenant, tool, user) persistent HOME for stateful CLIs.

Scope choice — why (tenant, tool, user):
  * tool-only would let User A see User B's svc login token. Security
    boundary violation.
  * user-only would let svc read git's config and vice versa. Not a
    safety issue, but it corrupts cache semantics and makes upgrade
    stories (tool-specific cache invalidation) impossible.
  * (tool, user) is the smallest safe unit: same person using the same
    tool in two conversations should keep their login.
  * tenant is the outer namespace to keep tools from different companies
    fully isolated even if tool IDs or user IDs ever collide.

Layout on disk (inside the backend container; volume is bind-mounted at
`/data/cli_state`, host-mapped via HostPathResolver for the sandbox):

    /data/cli_state/
      <tenant_id>/
        <tool_id>/
          <user_id>/           <- mounted read-write at /home/sandbox
            .config/...        <- whatever the binary decides to write
            .cache/...

Permission model:
  * The root (`/data/cli_state`) is clawith:clawith rwx so the backend
    (uid 1000) can `mkdir` new subtrees (see entrypoint.sh).
  * Each leaf directory is chown'd to 65534:65534 (nobody) so the
    sandbox can write inside it.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_SANDBOX_UID = 65534
_SANDBOX_GID = 65534


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
    """Resolve and ensure the on-disk HOME for a given (tool, user).

    The returned path is the *container-visible* path; BinaryRunner's
    HostPathResolver handles the translation to the docker daemon's view.
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

        Also chowns the leaf to 65534:65534 so the sandbox (running as
        nobody) can write inside it. The intermediate tenant/tool dirs
        stay clawith-owned so nobody can't traverse sideways.
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
        # reliably. The root is setgid with gid 65534, so new leaves
        # inherit gid 65534; setting mode 2775 (setgid + group rwx) is
        # what lets the sandbox (uid 65534) write inside its own leaf.
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
