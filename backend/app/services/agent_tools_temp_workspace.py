from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.task import Task
from app.services.task_time_projection import serialize_tasks_for_agent
from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.agent_tools import (
    CORE_MEMORY_TEMPLATE,
    TEMP_WORKSPACE_DEFAULT_PATHS,
    TOOL_MATERIALIZE_MAX_FILE_BYTES,
    TOOL_MATERIALIZE_MAX_TOTAL_BYTES,
    _tool_storage_key,
    get_storage_backend,
    logger,
    normalize_storage_key,
)
from app.services.storage_runtime.base import WriteCondition, content_hash_bytes
from app.services.workspace_collaboration import normalize_workspace_path
from app.services.workspace_locking import workspace_locks


async def initialize_agent_workspace(agent_id: uuid.UUID) -> None:
    """Seed default workspace files into shared storage once at agent creation time."""
    storage = get_storage_backend()
    mem_key = normalize_storage_key(f"{agent_id}/memory/memory.md")
    if not await storage.is_file(mem_key):
        await storage.write_text(
            mem_key,
            CORE_MEMORY_TEMPLATE,
            encoding="utf-8",
        )

    soul_key = normalize_storage_key(f"{agent_id}/soul.md")
    if not await storage.is_file(soul_key):
        soul_content = "# Personality\n\n_Describe your role and responsibilities._\n"
        try:
            async with async_session() as db:
                result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                agent = result.scalar_one_or_none()
                if agent and agent.role_description:
                    soul_content = f"# Personality\n\n{agent.role_description}\n"
        except Exception:
            pass
        await storage.write_text(soul_key, soul_content, encoding="utf-8")


@dataclass
class TempWorkspaceManifestEntry:
    rel_path: str
    storage_key: str
    base_version_token: str
    base_hash: str
    size: int


@dataclass
class TempWorkspace:
    temp_dir: tempfile.TemporaryDirectory
    root: Path
    agent_id: uuid.UUID
    tenant_id: str | None
    selected_paths: list[str]
    manifest: dict[str, TempWorkspaceManifestEntry]

    def cleanup(self) -> None:
        self.temp_dir.cleanup()


async def _materialize_storage_workspace(storage, storage_key: str, local_root: Path) -> None:
    if not await storage.is_dir(storage_key):
        return
    for entry in await storage.list_dir(storage_key):
        await _materialize_storage_entry(storage, entry.key, storage_key, local_root)


async def _materialize_storage_entry(storage, entry_key: str, root_key: str, local_root: Path) -> None:
    rel = entry_key.removeprefix(root_key.rstrip("/") + "/")
    target = (local_root / rel).resolve()
    if not str(target).startswith(str(local_root.resolve())):
        return
    if await storage.is_dir(entry_key):
        target.mkdir(parents=True, exist_ok=True)
        for child in await storage.list_dir(entry_key):
            await _materialize_storage_entry(storage, child.key, root_key, local_root)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await storage.read_bytes(entry_key))


async def _prepare_temp_workspace(
    agent_id: uuid.UUID,
    tenant_id: str | None = None,
    paths: list[str] | None = None,
    max_file_bytes: int = TOOL_MATERIALIZE_MAX_FILE_BYTES,
) -> TempWorkspace:
    tmp = tempfile.TemporaryDirectory(prefix=f"clawith-agent-{str(agent_id)[:8]}-")
    temp_ws = Path(tmp.name)
    for folder in ("workspace", "memory", "skills"):
        (temp_ws / folder).mkdir(parents=True, exist_ok=True)

    storage = get_storage_backend()
    budget = {"total": 0}
    runtime_workspace = current_agent_runtime_workspace(agent_id)
    if paths is None and runtime_workspace.is_project:
        selected = ["workspace", "memory/memory.md", "soul.md"]
    else:
        selected = TEMP_WORKSPACE_DEFAULT_PATHS if paths is None else [path for path in paths if path]
    manifest: dict[str, TempWorkspaceManifestEntry] = {}
    for rel_path in selected:
        storage_key, normalized, is_enterprise = _tool_storage_key(agent_id, rel_path, tenant_id)
        if is_enterprise:
            continue
        await _materialize_storage_path_with_budget(
            storage,
            storage_key,
            normalized,
            temp_ws,
            budget,
            manifest,
            max_file_bytes=max_file_bytes,
        )
    return TempWorkspace(
        temp_dir=tmp,
        root=temp_ws,
        agent_id=agent_id,
        tenant_id=tenant_id,
        selected_paths=list(selected),
        manifest=manifest,
    )


async def _materialize_storage_path_with_budget(
    storage,
    storage_key: str,
    rel_path: str,
    local_root: Path,
    budget: dict,
    manifest: dict[str, TempWorkspaceManifestEntry],
    *,
    max_file_bytes: int = TOOL_MATERIALIZE_MAX_FILE_BYTES,
) -> None:
    if await storage.is_file(storage_key):
        version = await storage.get_version(storage_key)
        if version.size > max_file_bytes:
            return
        if budget["total"] + version.size > TOOL_MATERIALIZE_MAX_TOTAL_BYTES:
            return
        target = (local_root / rel_path).resolve()
        if not str(target).startswith(str(local_root.resolve())):
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        data = await storage.read_bytes(storage_key)
        target.write_bytes(data)
        normalized_rel = normalize_workspace_path(rel_path)
        manifest[normalized_rel] = TempWorkspaceManifestEntry(
            rel_path=normalized_rel,
            storage_key=storage_key,
            base_version_token=version.token,
            base_hash=content_hash_bytes(data),
            size=version.size,
        )
        budget["total"] += version.size
        return
    if await storage.is_dir(storage_key):
        (local_root / rel_path).mkdir(parents=True, exist_ok=True)
        for entry in await storage.list_dir(storage_key):
            child_rel = f"{rel_path.rstrip('/')}/{entry.name}" if rel_path else entry.name
            await _materialize_storage_path_with_budget(
                storage,
                entry.key,
                child_rel,
                local_root,
                budget,
                manifest,
                max_file_bytes=max_file_bytes,
            )


async def _sync_tasks_to_file(agent_id: uuid.UUID, ws: Path):
    """Sync tasks from DB to legacy tasks.json, if the file already exists."""
    tasks_path = ws / "tasks.json"
    if not tasks_path.exists():
        return

    try:
        async with async_session() as db:
            result = await db.execute(select(Task).where(Task.agent_id == agent_id).order_by(Task.created_at.desc()))
            tasks = result.scalars().all()

        task_list = await serialize_tasks_for_agent(agent_id, tasks)

        tasks_path.write_text(
            json.dumps(task_list, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"[AgentTools] Failed to sync tasks: {e}")


async def flush_temp_workspace(temp_workspace: TempWorkspace, conflict_mode: str = "fail") -> dict[str, list[str]]:
    """Flush local changes back to storage using manifest-based conflict checks."""
    storage = get_storage_backend()
    selected_paths = [normalize_workspace_path(path) for path in temp_workspace.selected_paths]
    manifest = temp_workspace.manifest
    local_files = _collect_temp_workspace_files(temp_workspace.root, selected_paths)

    updated: list[str] = []
    conflicted: list[str] = []
    deleted: list[str] = []
    skipped: list[str] = []
    runtime_workspace = current_agent_runtime_workspace(temp_workspace.agent_id)

    async with workspace_locks(temp_workspace.agent_id, selected_paths):
        for rel_path, local_path in local_files.items():
            if local_path.name.startswith("_exec_tmp") or "__pycache__" in local_path.parts:
                continue
            if runtime_workspace.is_agent_write_protected(rel_path):
                skipped.append(rel_path)
                continue
            data = local_path.read_bytes()
            current_hash = content_hash_bytes(data)
            entry = manifest.get(rel_path)
            if entry and entry.base_hash == current_hash:
                skipped.append(rel_path)
                continue
            condition = (
                WriteCondition(version_token=entry.base_version_token) if entry else WriteCondition(require_absent=True)
            )
            storage_key = entry.storage_key if entry else runtime_workspace.storage_key(rel_path)
            result = await storage.write_bytes_if_match(
                storage_key,
                data,
                condition=condition,
            )
            if not result.ok:
                conflicted.append(rel_path)
                if conflict_mode == "fail":
                    return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}
                continue
            updated.append(rel_path)

        for rel_path, entry in manifest.items():
            if rel_path in local_files:
                continue
            if runtime_workspace.is_agent_write_protected(rel_path):
                skipped.append(rel_path)
                continue
            result = await storage.delete_if_match(
                entry.storage_key,
                condition=WriteCondition(version_token=entry.base_version_token),
            )
            if not result.ok:
                conflicted.append(rel_path)
                if conflict_mode == "fail":
                    return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}
                continue
            deleted.append(rel_path)

    return {"updated": updated, "deleted": deleted, "conflicted": conflicted, "skipped": skipped}


def _collect_temp_workspace_files(root: Path, selected_paths: list[str]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    root_resolved = root.resolve()
    for selected in selected_paths:
        if not selected:
            continue
        target = (root_resolved / selected).resolve()
        if not str(target).startswith(str(root_resolved)):
            continue
        if target.is_file():
            files[normalize_workspace_path(selected)] = target
            continue
        if not target.exists() or not target.is_dir():
            continue
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            rel = path.resolve().relative_to(root_resolved).as_posix()
            files[normalize_workspace_path(rel)] = path
    return files
