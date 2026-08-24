"""Immutable, credential-safe project trees embedded in project templates."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from app.models.project import Project
from app.services.project_agent_workspace import is_sensitive_project_asset_path
from app.services.project_git_service import commit_project_changes, project_repo_path

MAX_TEMPLATE_FILES = 5000
MAX_TEMPLATE_FILE_BYTES = 8 * 1024 * 1024
MAX_TEMPLATE_TREE_BYTES = 32 * 1024 * 1024
MAX_TEMPLATE_PATH_BYTES = 500
_SNAPSHOT_KEYS = {"schema_version", "source_head", "files", "excluded_file_count"}
_FILE_KEYS = {"path", "mode", "size", "sha256", "content_base64"}
_SECRET_KEY = re.compile(
    r"(?i)(?:secret|password|passwd|credential|token|api[_-]?key|private[_-]?key|"
    r"(?:^|[_-])(?:url|uri|endpoint|host|remote)(?:$|[_-]))"
)
_PLATFORM_ONLY_PATHS = {"PROJECT.json", ".gitmodules"}


class ProjectTemplateSnapshotError(ValueError):
    """Raised when a project tree cannot safely cross the template boundary."""


@dataclass(frozen=True, slots=True)
class ProjectHeadSnapshot:
    head: str
    project_files: dict
    agent_files: tuple[tuple[str, bytes], ...]


def sanitize_template_settings(value: object) -> dict:
    """Keep only portable project policy/runtime settings and reject secret-shaped keys."""

    if not isinstance(value, dict):
        return {}
    result = {
        key: _safe_json(value[key], f"settings.{key}") for key in ("runtime", "policies", "planning") if key in value
    }
    planning = result.get("planning")
    if isinstance(planning, dict):
        planning.pop("session_id", None)
        planning.pop("conversation_id", None)
        planning.pop("run_id", None)
    return result


def sanitize_template_scope(value: object) -> dict:
    if not isinstance(value, dict):
        raise ProjectTemplateSnapshotError("Project capability scope must be an object")
    return _safe_json(value, "capability.scope")  # type: ignore[return-value]


async def capture_project_head_snapshot(project: Project) -> ProjectHeadSnapshot:
    """Capture regular files from one immutable Git HEAD, never from the worktree."""

    return await asyncio.to_thread(_capture_project_head_snapshot, project)


def _capture_project_head_snapshot(project: Project) -> ProjectHeadSnapshot:
    repo = project_repo_path(project.tenant_id, project.id)
    if not (repo / ".git").is_dir():
        raise ProjectTemplateSnapshotError("The managed project repository is not initialized")
    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    tree = _git(repo, "ls-tree", "-r", "-l", "-z", head)
    files: list[dict] = []
    agent_files: list[tuple[str, bytes]] = []
    total_size = 0
    excluded_count = 0
    for record in tree.split(b"\x00"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 4:
            raise ProjectTemplateSnapshotError("Git returned an invalid project tree entry")
        mode, kind, object_id, raw_size = (part.decode("ascii") for part in parts)
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProjectTemplateSnapshotError("Project template paths must be UTF-8") from exc
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ProjectTemplateSnapshotError(f"Unsupported Git entry in project template: {path}")
        size = int(raw_size)
        _validate_path(path)
        if is_sensitive_project_asset_path(PurePosixPath(path)) or path in _PLATFORM_ONLY_PATHS:
            excluded_count += 1
            continue
        content = _git(repo, "cat-file", "blob", object_id)
        if len(content) != size:
            raise ProjectTemplateSnapshotError(f"Git blob size changed while publishing: {path}")
        if path.startswith(".agents/"):
            agent_files.append((path, content))
            continue
        if size > MAX_TEMPLATE_FILE_BYTES:
            raise ProjectTemplateSnapshotError(
                f"Project file exceeds the {MAX_TEMPLATE_FILE_BYTES}-byte template limit: {path}"
            )
        total_size += size
        if total_size > MAX_TEMPLATE_TREE_BYTES:
            raise ProjectTemplateSnapshotError(
                f"Project template files exceed the {MAX_TEMPLATE_TREE_BYTES}-byte total limit"
            )
        files.append(
            {
                "path": path,
                "mode": mode,
                "size": size,
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
        if len(files) > MAX_TEMPLATE_FILES:
            raise ProjectTemplateSnapshotError(f"A project template can contain at most {MAX_TEMPLATE_FILES} files")
    return ProjectHeadSnapshot(
        head=head,
        project_files={
            "schema_version": 1,
            "source_head": head,
            "files": files,
            "excluded_file_count": excluded_count,
        },
        agent_files=tuple(agent_files),
    )


@contextmanager
def materialize_snapshot_agent_files(snapshot: ProjectHeadSnapshot) -> Iterator[Path]:
    """Expose only committed Agent assets to the existing asset sanitizer."""

    with TemporaryDirectory(prefix="clawith-project-template-") as directory:
        root = Path(directory)
        for path, content in snapshot.agent_files:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        yield root


async def restore_project_template_files(
    project: Project,
    raw_snapshot: object,
    *,
    author_name: str,
    author_email: str,
) -> str | None:
    """Restore a validated tree into a fresh managed repository as a new commit."""

    files = _validate_snapshot(raw_snapshot)
    if files is None:
        return None
    changed_paths = await asyncio.to_thread(_write_snapshot_files, project, files)
    result = await commit_project_changes(
        project,
        "Restore final project assets from template",
        changed_paths,
        author_name=author_name,
        author_email=author_email,
    )
    return str(result["commit"])


def public_template_definition(definition: object) -> dict:
    """Return market-safe metadata without source content or internal references."""

    source = definition if isinstance(definition, dict) else {}
    agents = source.get("agents") if isinstance(source.get("agents"), list) else []
    tree = source.get("project_snapshot") if isinstance(source.get("project_snapshot"), dict) else {}
    result = {
        key: source[key]
        for key in ("featured", "goal", "objective", "success_criteria")
        if key in source
    }
    capabilities = source.get("capabilities") if isinstance(source.get("capabilities"), list) else []
    result["roles"] = [
        {
            "key": f"digital-employee-{index + 1}",
            "name": str(item.get("name") or "数字员工"),
            "description": str(item.get("role_description") or ""),
        }
        for index, item in enumerate(agents)
        if isinstance(item, dict)
    ]
    result["skills"] = [
        {"name": str(item.get("capability_name") or "Skill")}
        for item in capabilities
        if isinstance(item, dict) and item.get("capability_type") == "skill" and item.get("source") == "shared"
    ]
    result["mcp_servers"] = [
        {"name": str(item.get("capability_name") or "MCP")}
        for item in capabilities
        if isinstance(item, dict) and item.get("capability_type") == "mcp" and item.get("source") == "shared"
    ]
    project_files = tree.get("files") if isinstance(tree.get("files"), list) else []
    project_file_count = len(project_files)
    project_size_bytes = sum(
        item.get("size", 0)
        for item in project_files
        if isinstance(item, dict)
        and isinstance(item.get("size"), int)
        and not isinstance(item.get("size"), bool)
        and item.get("size", 0) >= 0
    )
    digital_employee_file_count = 0
    digital_employee_size_bytes = 0
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for key in ("soul", "core_memory"):
            content = agent.get(key)
            if isinstance(content, str):
                digital_employee_file_count += 1
                digital_employee_size_bytes += len(content.encode("utf-8"))
        workspace_files = agent.get("workspace_files")
        if not isinstance(workspace_files, list):
            continue
        for file in workspace_files:
            if not isinstance(file, dict) or not isinstance(file.get("content"), str):
                continue
            digital_employee_file_count += 1
            digital_employee_size_bytes += len(file["content"].encode("utf-8"))
    return result | {
        "asset_summary": {
            "file_count": project_file_count,
            "total_file_count": project_file_count + digital_employee_file_count,
            "digital_employee_file_count": digital_employee_file_count,
            "digital_employee_count": len(agents),
            "total_size_bytes": project_size_bytes + digital_employee_size_bytes,
            "excluded_file_count": tree.get("excluded_file_count", 0)
            if isinstance(tree.get("excluded_file_count"), int)
            else 0,
            "skill_count": len(result.get("skills", [])) if isinstance(result.get("skills"), list) else 0,
            "mcp_server_count": len(result.get("mcp_servers", []))
            if isinstance(result.get("mcp_servers"), list)
            else 0,
        }
    }


def _validate_snapshot(raw_snapshot: object) -> list[tuple[str, str, bytes]] | None:
    if raw_snapshot is None:
        return None
    if not isinstance(raw_snapshot, dict) or set(raw_snapshot) - _SNAPSHOT_KEYS:
        raise ProjectTemplateSnapshotError("Project template snapshot has unsupported fields")
    source_head = raw_snapshot.get("source_head")
    excluded_count = raw_snapshot.get("excluded_file_count", 0)
    if (
        raw_snapshot.get("schema_version") != 1
        or not isinstance(source_head, str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", source_head)
        or not isinstance(excluded_count, int)
        or isinstance(excluded_count, bool)
        or excluded_count < 0
    ):
        raise ProjectTemplateSnapshotError("Project template snapshot version is unsupported")
    raw_files = raw_snapshot.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > MAX_TEMPLATE_FILES:
        raise ProjectTemplateSnapshotError("Project template file manifest is invalid")
    result: list[tuple[str, str, bytes]] = []
    seen: set[str] = set()
    total_size = 0
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != _FILE_KEYS:
            raise ProjectTemplateSnapshotError("Project template file entry is invalid")
        path = str(raw_file.get("path") or "")
        _validate_path(path)
        if (
            path.startswith(".agents/")
            or path in _PLATFORM_ONLY_PATHS
            or is_sensitive_project_asset_path(PurePosixPath(path))
        ):
            raise ProjectTemplateSnapshotError(f"Project template contains a prohibited file: {path}")
        mode = str(raw_file.get("mode") or "")
        if mode not in {"100644", "100755"}:
            raise ProjectTemplateSnapshotError(f"Project template file mode is unsupported: {path}")
        if path in seen:
            raise ProjectTemplateSnapshotError(f"Duplicate project template file: {path}")
        seen.add(path)
        try:
            content = base64.b64decode(str(raw_file.get("content_base64") or ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProjectTemplateSnapshotError(f"Project template file content is invalid: {path}") from exc
        size = raw_file.get("size")
        digest = raw_file.get("sha256")
        if not isinstance(size, int) or size != len(content) or size > MAX_TEMPLATE_FILE_BYTES:
            raise ProjectTemplateSnapshotError(f"Project template file size is invalid: {path}")
        if not isinstance(digest, str) or hashlib.sha256(content).hexdigest() != digest:
            raise ProjectTemplateSnapshotError(f"Project template file checksum is invalid: {path}")
        total_size += size
        if total_size > MAX_TEMPLATE_TREE_BYTES:
            raise ProjectTemplateSnapshotError("Project template file manifest exceeds the total size limit")
        result.append((path, mode, content))
    return result


def _write_snapshot_files(project: Project, files: list[tuple[str, str, bytes]]) -> list[str]:
    repo = project_repo_path(project.tenant_id, project.id)
    if not (repo / ".git").is_dir():
        raise ProjectTemplateSnapshotError("The target managed project repository is not initialized")
    changed = {"README.md"}
    source_paths = {path for path, _mode, _content in files}
    if "README.md" not in source_paths:
        (repo / "README.md").unlink(missing_ok=True)
    for path, mode, content in files:
        target = (repo / path).resolve(strict=False)
        if repo.resolve() not in target.parents:
            raise ProjectTemplateSnapshotError(f"Project template path escaped the repository: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.template")
        try:
            temporary.write_bytes(content)
            os.chmod(temporary, 0o755 if mode == "100755" else 0o644)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        changed.add(path)
    return sorted(changed)


def _validate_path(path: str) -> None:
    pure = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or len(path.encode("utf-8")) > MAX_TEMPLATE_PATH_BYTES
        or pure.is_absolute()
        or pure.as_posix() != path
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(part.casefold() == ".git" for part in pure.parts)
    ):
        raise ProjectTemplateSnapshotError(f"Unsafe project template file path: {path}")


def _safe_json(value: object, path: str) -> object:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProjectTemplateSnapshotError(f"Project setting key is invalid: {path}")
            if _SECRET_KEY.search(key):
                continue
            result[key] = _safe_json(child, f"{path}.{key}")
        return result
    if isinstance(value, list):
        return [_safe_json(child, path) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProjectTemplateSnapshotError(f"Project setting is not JSON-safe: {path}")


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"},
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ProjectTemplateSnapshotError(detail or "Git could not create the project template snapshot")
    return result.stdout
