"""Isolated, project-scoped Agent workspaces.

A project Agent is a snapshot of a standard AgentDir. Only its soul, primary
memory file, and ordinary workspace assets cross the boundary in either
direction. Runtime state and credentials remain outside the project tree.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.services.agent_manager import agent_manager
from app.services.storage import agent_storage_key, get_storage_backend
from app.services.workspace_paths import WorkspacePathError, resolve_path_within_root

DEFAULT_PROJECT_SOUL = "# Project Agent\n"
DEFAULT_PROJECT_MEMORY = "# Project Memory\n"

_SENSITIVE_COMPONENTS = {
    ".aws",
    ".cache",
    ".credentials",
    ".docker",
    ".git",
    ".gnupg",
    ".openclaw",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".session",
    ".sessions",
    ".ssh",
    "__pycache__",
    "cache",
    "caches",
    "credentials",
    "node_modules",
    "secrets",
    "session",
    "sessions",
}
_SENSITIVE_FILENAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "cookies.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "session.json",
    "sessions.json",
    "token.json",
    "tokens.json",
    ".coverage",
}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}


@dataclass(frozen=True, slots=True)
class ProjectAgentWorkspace:
    """Canonical paths for one project Agent snapshot."""

    root: Path
    soul: Path
    memory: Path
    workspace: Path


@dataclass(frozen=True, slots=True)
class ProjectAgentWorkspaceCopyResult:
    """Observable result of an allowlisted workspace copy."""

    workspace: ProjectAgentWorkspace
    copied: tuple[str, ...]
    skipped_sensitive: tuple[str, ...]
    skipped_existing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectAgentPromotionResult:
    """Assets copied from a project snapshot into a standard AgentDir."""

    target_agent_dir: Path
    copied: tuple[str, ...]
    skipped_sensitive: tuple[str, ...]
    skipped_existing: tuple[str, ...]


def build_project_agent_identity_defaults(
    *,
    project_name: str,
    project_goal: str,
    success_criteria: list[object] | tuple[object, ...],
    agent_name: str,
    role_description: str,
) -> tuple[str, str]:
    """Build durable defaults for a newly created project Agent.

    These files are project assets and are only used when the corresponding
    source or owner-authored identity file does not already exist.
    """

    normalized_project_name = project_name.strip() or "未命名项目"
    normalized_agent_name = agent_name.strip() or "项目成员"
    normalized_goal = project_goal.strip() or f"完成项目“{normalized_project_name}”的既定目标。"
    normalized_role = role_description.strip() or f"承担“{normalized_agent_name}”对应的专业岗位职责。"
    criteria = [str(item).strip() for item in success_criteria if str(item).strip()]
    criteria_text = (
        "\n".join(f"{index}. {criterion}" for index, criterion in enumerate(criteria, start=1))
        if criteria
        else "1. 以项目负责人确认的交付结果与质量要求为准。"
    )

    soul = f"""# {normalized_agent_name}

你是项目“{normalized_project_name}”的专用 Agent。

## 岗位职责

{normalized_role}

## 项目使命

{normalized_goal}

## 工作准则

- 始终以岗位专业标准分析、执行和交付，不退化为通用事务处理角色。
- 所有结论、进度和交付物都应关联项目工作区中的真实证据。
- 主动对齐项目验收条件；发现缺口时及时向项目负责人说明并推进闭环。
- `soul.md` 与核心项目记忆由项目负责人维护，不得自行修改。
"""
    memory = f"""# Project Memory

## 项目上下文

- 项目名称：{normalized_project_name}
- 项目目标：{normalized_goal}

## 验收条件

{criteria_text}

## 岗位上下文

- Agent：{normalized_agent_name}
- 岗位职责：{normalized_role}

## 持久约束

- 仅在当前项目工作区内维护过程材料与交付物。
- 以项目目标、验收条件和岗位职责作为每次执行与复核的共同基线。
- 核心项目记忆由项目负责人维护，不得自行修改。
"""
    return soul, memory


def project_agent_workspace(project_root: Path, agent_id: uuid.UUID | str) -> ProjectAgentWorkspace:
    """Resolve one project Agent directory without allowing root escape."""

    normalized_agent_id = uuid.UUID(str(agent_id))
    project_root = Path(project_root).expanduser().resolve()
    if not project_root.is_dir():
        raise FileNotFoundError(f"Project root does not exist: {project_root}")
    agents_root = project_root / ".agents"
    if agents_root.is_symlink():
        raise WorkspacePathError("Project Agent root cannot be a symbolic link")
    if agents_root.exists() and not agents_root.is_dir():
        raise WorkspacePathError("Project Agent root must be a directory")

    unresolved_root = agents_root / str(normalized_agent_id)
    if unresolved_root.is_symlink():
        raise WorkspacePathError("Project Agent directory cannot be a symbolic link")
    root = resolve_path_within_root(
        project_root,
        f".agents/{normalized_agent_id}",
        label="project Agent directory",
    )
    return ProjectAgentWorkspace(
        root=root,
        soul=root / "soul.md",
        memory=root / "memory.md",
        workspace=root / "workspace",
    )


def resolve_project_agent_path(
    project_root: Path,
    agent_id: uuid.UUID | str,
    relative_path: str,
    *,
    allow_root: bool = True,
) -> Path:
    """Resolve an untrusted path beneath one project Agent snapshot."""

    layout = project_agent_workspace(project_root, agent_id)
    _reject_symlink_path(layout.root, layout.root / relative_path)
    target = resolve_path_within_root(
        layout.root,
        relative_path,
        allow_root=allow_root,
        label="project Agent asset",
    )
    _reject_symlink_path(layout.root, target)
    return target


async def create_project_agent_workspace(
    project_root: Path,
    agent_id: uuid.UUID | str,
    *,
    source_agent_id: uuid.UUID | str | None = None,
    overwrite: bool = False,
    default_soul: str = DEFAULT_PROJECT_SOUL,
    default_memory: str = DEFAULT_PROJECT_MEMORY,
) -> ProjectAgentWorkspaceCopyResult:
    """Create a project snapshot, optionally seeded from a standard AgentDir."""

    layout = project_agent_workspace(project_root, agent_id)
    source_dir = None
    if source_agent_id is not None:
        source_dir = await agent_manager._materialize_agent_dir(uuid.UUID(str(source_agent_id)))
    return await asyncio.to_thread(
        _create_local_snapshot,
        layout,
        source_dir,
        overwrite,
        default_soul,
        default_memory,
    )


def deactivate_project_agent_workspace(
    project_root: Path,
    agent_id: uuid.UUID | str,
) -> ProjectAgentWorkspace:
    """Retain and return a deactivated Agent snapshot without deleting data."""

    layout = project_agent_workspace(project_root, agent_id)
    if not layout.root.is_dir():
        raise FileNotFoundError(f"Project Agent workspace does not exist: {layout.root}")
    return layout


async def promote_project_agent_workspace(
    project_root: Path,
    agent_id: uuid.UUID | str,
    target_agent_id: uuid.UUID | str,
    *,
    overwrite: bool = False,
) -> ProjectAgentPromotionResult:
    """Copy allowlisted project assets into a standard shared AgentDir."""

    layout = project_agent_workspace(project_root, agent_id)
    if not layout.root.is_dir():
        raise FileNotFoundError(f"Project Agent workspace does not exist: {layout.root}")

    target_agent_id = uuid.UUID(str(target_agent_id))
    assets, skipped_sensitive = await asyncio.to_thread(_collect_project_assets, layout)
    storage = get_storage_backend()
    copied: list[str] = []
    skipped_existing: list[str] = []
    for relative_path, source in assets:
        storage_key = agent_storage_key(target_agent_id, relative_path)
        if not overwrite and await storage.exists(storage_key):
            skipped_existing.append(relative_path)
            continue
        await storage.write_bytes(storage_key, await asyncio.to_thread(source.read_bytes))
        copied.append(relative_path)

    target_dir = await agent_manager._materialize_agent_dir(target_agent_id)
    (target_dir / "workspace").mkdir(parents=True, exist_ok=True)
    return ProjectAgentPromotionResult(
        target_agent_dir=target_dir,
        copied=tuple(copied),
        skipped_sensitive=tuple(skipped_sensitive),
        skipped_existing=tuple(skipped_existing),
    )


def _create_local_snapshot(
    layout: ProjectAgentWorkspace,
    source_dir: Path | None,
    overwrite: bool,
    default_soul: str,
    default_memory: str,
) -> ProjectAgentWorkspaceCopyResult:
    layout.root.mkdir(parents=True, exist_ok=True)
    _reject_symlink_path(layout.root, layout.root)
    layout.workspace.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped_sensitive: list[str] = []
    skipped_existing: list[str] = []

    if source_dir is not None:
        source_dir = source_dir.resolve()
        _copy_if_allowed(
            source_dir / "soul.md",
            layout.soul,
            layout.root,
            "soul.md",
            overwrite,
            copied,
            skipped_existing,
        )
        source_memory = source_dir / "memory" / "memory.md"
        if not source_memory.is_file():
            source_memory = source_dir / "memory.md"
        _copy_if_allowed(
            source_memory,
            layout.memory,
            layout.root,
            "memory.md",
            overwrite,
            copied,
            skipped_existing,
        )
        source_workspace = source_dir / "workspace"
        if source_workspace.is_dir() and not source_workspace.is_symlink():
            for source, relative_path in _iter_allowed_files(source_workspace, skipped_sensitive):
                project_relative = f"workspace/{relative_path.as_posix()}"
                _copy_if_allowed(
                    source,
                    layout.workspace / relative_path,
                    layout.root,
                    project_relative,
                    overwrite,
                    copied,
                    skipped_existing,
                )

    _write_default_if_missing(layout.root, layout.soul, default_soul)
    _write_default_if_missing(layout.root, layout.memory, default_memory)
    return ProjectAgentWorkspaceCopyResult(
        workspace=layout,
        copied=tuple(copied),
        skipped_sensitive=tuple(skipped_sensitive),
        skipped_existing=tuple(skipped_existing),
    )


def _collect_project_assets(layout: ProjectAgentWorkspace) -> tuple[list[tuple[str, Path]], list[str]]:
    _reject_symlink_path(layout.root, layout.root)
    assets: list[tuple[str, Path]] = []
    skipped_sensitive: list[str] = []
    if _is_safe_regular_file(layout.soul):
        assets.append(("soul.md", layout.soul))
    if _is_safe_regular_file(layout.memory):
        assets.append(("memory/memory.md", layout.memory))
    if layout.workspace.is_dir() and not layout.workspace.is_symlink():
        for source, relative_path in _iter_allowed_files(layout.workspace, skipped_sensitive):
            assets.append((f"workspace/{relative_path.as_posix()}", source))
    return assets, skipped_sensitive


def _iter_allowed_files(root: Path, skipped_sensitive: list[str]):
    for source in sorted(root.rglob("*")):
        relative_path = source.relative_to(root)
        if source.is_symlink():
            skipped_sensitive.append(relative_path.as_posix())
            continue
        if source.is_dir():
            continue
        if not source.is_file() or _is_sensitive_asset(relative_path):
            skipped_sensitive.append(relative_path.as_posix())
            continue
        yield source, relative_path


def _is_sensitive_asset(relative_path: PurePosixPath | Path) -> bool:
    lowered_parts = tuple(part.casefold() for part in relative_path.parts)
    if any(part in _SENSITIVE_COMPONENTS for part in lowered_parts):
        return True
    filename = lowered_parts[-1] if lowered_parts else ""
    return (
        filename.startswith(".env") or filename in _SENSITIVE_FILENAMES or Path(filename).suffix in _SENSITIVE_SUFFIXES
    )


def is_sensitive_project_asset_path(relative_path: PurePosixPath | Path) -> bool:
    """Public path-only policy shared by project export boundaries."""

    return _is_sensitive_asset(relative_path)


def _copy_if_allowed(
    source: Path,
    target: Path,
    target_root: Path,
    display_path: str,
    overwrite: bool,
    copied: list[str],
    skipped_existing: list[str],
) -> None:
    if not _is_safe_regular_file(source):
        return
    _reject_symlink_path(target_root, target)
    if target.exists() and not overwrite:
        skipped_existing.append(display_path)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_path(target_root, target)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.copying")
    try:
        shutil.copyfile(source, temporary, follow_symlinks=False)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    copied.append(display_path)


def _write_default_if_missing(root: Path, target: Path, content: str) -> None:
    _reject_symlink_path(root, target)
    if target.exists():
        return
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.creating")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _is_safe_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _reject_symlink_path(root: Path, target: Path) -> None:
    root = root.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError("Project Agent asset escaped its workspace") from exc
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise WorkspacePathError("Project Agent assets cannot traverse symbolic links")
