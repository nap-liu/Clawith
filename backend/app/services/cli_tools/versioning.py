"""CLI-tool binary version history + rollback service.

Three-verb API:

- :func:`record_new_version` — called from the upload endpoint after the
  new ``.bin`` lands on disk. Inserts a new row, flips the previous
  current off, and evicts the oldest version past the retention cap
  (along with its on-disk file).
- :func:`rollback_to` — flips the current flag to the requested historical
  version. Does **not** evict anything; the previously-current row stays
  in history so the admin can roll forward again.
- :func:`list_versions` — ``uploaded_at DESC`` listing for the UI.

Invariants:

- ``Tool.config.binary`` (the cheap projection) is always rewritten to
  match the current version. Callers that read ``Tool.config.binary``
  don't need to know the history table exists.
- At most one row per ``tool_id`` has ``is_current=True``. Enforced by
  the partial unique index ``uq_cli_tool_binary_versions_current`` from
  the alembic migration, and by this module flipping the old flag off
  *before* turning the new one on.
- :data:`MAX_RETAINED_VERSIONS` is a soft constant. Raise it to keep more
  history on disk; lower it to save disk at the cost of fewer rollback
  points. Must be >= 2 or rollback becomes pointless (only the current
  row exists).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cli_tool_binary import CliToolBinaryVersion
from app.models.tool import Tool
from app.services.cli_tools.schema import BinaryMetadata, CliToolConfig
from app.services.cli_tools.storage import BinaryStorage

logger = logging.getLogger(__name__)


# How many versions (including the current one) we keep on disk + in the
# DB for each CLI tool. Rows older than this are hard-deleted along with
# their ``.bin`` file. Configurable here so operators can trade disk for
# deeper history; the default of 5 is a heuristic — enough to cover a
# "ship, notice regression, ship fix, roll back, ship again" churn cycle
# without letting a runaway upload loop fill the binaries volume.
MAX_RETAINED_VERSIONS = 5


def _tenant_key(tool: Tool) -> str:
    """Match the convention used by the upload endpoint / BinaryStorage."""
    return str(tool.tenant_id) if tool.tenant_id is not None else "_global"


def _project_binary_into_config(tool: Tool, version: CliToolBinaryVersion) -> None:
    """Rewrite ``tool.config.binary`` to mirror ``version``.

    Runtime / sandbox subtrees are preserved byte-for-byte — this is the
    only way we ever set binary metadata, and it must never clobber
    admin-owned config.
    """
    existing = CliToolConfig.model_validate(tool.config or {})
    tool.config = CliToolConfig(
        binary=BinaryMetadata(
            sha256=version.sha256,
            size=version.size,
            original_name=version.original_name,
            uploaded_at=version.uploaded_at,
        ),
        runtime=existing.runtime,
        sandbox=existing.sandbox,
    ).model_dump(mode="json")


async def _evict_oldest_past_cap(
    db: AsyncSession,
    tool: Tool,
    *,
    binary_storage: Optional[BinaryStorage],
) -> list[str]:
    """Delete version rows (and files) past ``MAX_RETAINED_VERSIONS``.

    Returns the sha256s that were evicted, for logging.

    Strategy:
      1. Load every version for the tool, newest first.
      2. Keep the first N; mark the rest for deletion.
      3. For each deletion, hard-delete the row and ask ``binary_storage``
         to remove the file. The file removal is best-effort — if it
         fails, the row still goes so we don't leak DB rows; the nightly
         orphan sweep will pick up the file.

    We never evict a row with ``is_current=True`` even if it is somehow
    past the cap (shouldn't happen, but defensive).
    """
    rows = (
        await db.execute(
            select(CliToolBinaryVersion)
            .where(CliToolBinaryVersion.tool_id == tool.id)
            .order_by(CliToolBinaryVersion.uploaded_at.desc())
        )
    ).scalars().all()

    if len(rows) <= MAX_RETAINED_VERSIONS:
        return []

    evicted_shas: list[str] = []
    tenant_key = _tenant_key(tool)
    for victim in rows[MAX_RETAINED_VERSIONS:]:
        if victim.is_current:
            # Pathological: current row somehow fell off the retention
            # window. Skip it and log — losing the current binary would
            # brick the tool.
            logger.warning(
                "cli_tools.versioning: refusing to evict current version %s "
                "for tool %s (retention cap misconfigured?)",
                victim.id,
                tool.id,
            )
            continue
        evicted_shas.append(victim.sha256)
        await db.delete(victim)
        if binary_storage is not None:
            binary_storage.delete_version(tenant_key, str(tool.id), victim.sha256)

    if evicted_shas:
        logger.info(
            "cli_tools.versioning: evicted %d old version(s) for tool %s",
            len(evicted_shas),
            tool.id,
        )
    return evicted_shas


async def record_new_version(
    db: AsyncSession,
    tool: Tool,
    *,
    sha256: str,
    size: int,
    original_name: str,
    user_id: Optional[uuid.UUID],
    notes: Optional[str] = None,
    binary_storage: Optional[BinaryStorage] = None,
    uploaded_at: Optional[datetime] = None,
) -> CliToolBinaryVersion:
    """Persist a freshly-uploaded binary as the new current version.

    Side effects (all in the caller's transaction):

    - Flips every existing ``is_current=True`` row for this tool to False.
    - Inserts a new row with ``is_current=True`` pointing at the new sha.
    - Rewrites ``tool.config.binary`` to the new metadata.
    - Evicts any version rows past ``MAX_RETAINED_VERSIONS`` (oldest
      first), hard-deleting their ``.bin`` file when ``binary_storage``
      is supplied.

    The caller (upload endpoint) owns ``db.commit()``.
    """
    # Clear the previous current first. We must run this *before* adding
    # the new row so the partial unique index doesn't trip; Postgres
    # evaluates unique constraints at statement boundary, but belt + braces.
    await db.execute(
        update(CliToolBinaryVersion)
        .where(
            CliToolBinaryVersion.tool_id == tool.id,
            CliToolBinaryVersion.is_current.is_(True),
        )
        .values(is_current=False)
    )

    version = CliToolBinaryVersion(
        id=uuid.uuid4(),
        tool_id=tool.id,
        sha256=sha256,
        size=size,
        original_name=original_name[:255],
        uploaded_at=uploaded_at or datetime.now(timezone.utc),
        uploaded_by_user_id=user_id,
        is_current=True,
        notes=(notes or None),
    )
    db.add(version)
    await db.flush()

    _project_binary_into_config(tool, version)

    await _evict_oldest_past_cap(db, tool, binary_storage=binary_storage)
    return version


async def rollback_to(
    db: AsyncSession,
    tool: Tool,
    version_id: uuid.UUID,
) -> CliToolBinaryVersion:
    """Make ``version_id`` the current version of ``tool``.

    Raises :class:`LookupError` if the version doesn't exist or belongs
    to a different tool (the API layer turns this into a 404).

    The previously-current row stays in history with ``is_current=False``
    so the caller can roll forward again. File eviction does not run on
    rollback — every retained version must remain playable.
    """
    target = (
        await db.execute(
            select(CliToolBinaryVersion).where(
                CliToolBinaryVersion.id == version_id,
                CliToolBinaryVersion.tool_id == tool.id,
            )
        )
    ).scalar_one_or_none()

    if target is None:
        raise LookupError(f"binary version {version_id} not found for tool {tool.id}")

    # If the target is already current, treat this as a noop — same
    # contract as "POST rollback to v1 twice" producing one audit row
    # from the caller but no DB change on the second call.
    if target.is_current:
        return target

    await db.execute(
        update(CliToolBinaryVersion)
        .where(
            CliToolBinaryVersion.tool_id == tool.id,
            CliToolBinaryVersion.is_current.is_(True),
        )
        .values(is_current=False)
    )
    target.is_current = True
    await db.flush()

    _project_binary_into_config(tool, target)
    return target


async def list_versions(
    db: AsyncSession,
    tool: Tool,
) -> list[CliToolBinaryVersion]:
    """Return every version for ``tool``, newest upload first."""
    rows = (
        await db.execute(
            select(CliToolBinaryVersion)
            .where(CliToolBinaryVersion.tool_id == tool.id)
            .order_by(CliToolBinaryVersion.uploaded_at.desc())
        )
    ).scalars().all()
    return list(rows)


# Convenience export for callers that iterate over version file paths
# (mostly tests). Kept out of the public surface above because production
# code should go through ``BinaryStorage.resolve`` directly.
def iter_version_paths(
    binary_storage: BinaryStorage,
    tool: Tool,
    versions: Iterable[CliToolBinaryVersion],
) -> Iterable[Path]:
    tenant_key = _tenant_key(tool)
    for v in versions:
        yield binary_storage.resolve(tenant_key, str(tool.id), v.sha256)
