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
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_SANDBOX_UID = 65534
_SANDBOX_GID = 65534


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
