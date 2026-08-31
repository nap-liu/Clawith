"""Project workspace mutations and commit operations."""

from app.services.project_git_inspection import *  # noqa: F401,F403

def _read_file(project: Project, path: str, max_chars: int) -> dict:
    result = _read_file_content(project, path, max_chars)
    if not result["is_text"]:
        raise HTTPException(status_code=422, detail="Binary project files cannot be read as text")
    return {
        "path": result["path"],
        "commit": result["head"],
        "content": result["content"],
        "truncated": result["truncated"],
    }


async def read_project_file(project: Project, path: str, max_chars: int = 20_000) -> dict:
    bounded = min(100_000, max(1, int(max_chars)))
    return await asyncio.to_thread(_read_file, project, path, bounded)


def _write_file(
    project: Project,
    path: str,
    content: str,
    author_name: str | None,
    author_email: str | None,
) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_relative_path(repo, path)
        if target.exists() and target.is_dir():
            raise HTTPException(status_code=422, detail="Project file path points to a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".clawith-write-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _git(repo, "add", "--", normalized)
        if _git(repo, "diff", "--cached", "--quiet", "--", normalized, check=False).returncode == 0:
            raise HTTPException(status_code=409, detail="File content is unchanged; no commit was created")
        _commit_with_author(
            repo,
            "-m",
            f"更新项目文件：{normalized}",
            "--",
            normalized,
            author_name=author_name,
            author_email=author_email,
        )
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {
            "status": "completed",
            "operation": "write_file",
            "path": normalized,
            "commit": head,
            "file": _file_record(repo, normalized),
        }


async def write_project_file(
    project: Project,
    path: str,
    content: str,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        _write_file,
        project,
        path,
        content,
        author_name,
        author_email,
    )


def _path_exists_at_head(repo: Path, path: str) -> bool:
    return _git(repo, "cat-file", "-e", f"HEAD:{path}", check=False).returncode == 0


def _rollback_project_delivery_paths(repo: Path, paths: list[str], existed_at_head: dict[str, bool]) -> None:
    for path in paths:
        _git(repo, "reset", "-q", "HEAD", "--", path, check=False)
        _git(repo, "restore", "--source=HEAD", "--worktree", "--", path, check=False)
    for path in paths:
        if existed_at_head.get(path, False):
            continue
        try:
            _normalized, target = _safe_project_delivery_path(repo, path)
        except HTTPException:
            continue
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def _commit_project_delivery_paths(
    repo: Path,
    *,
    paths: list[str],
    message: str,
    author_name: str | None,
    author_email: str | None,
) -> str:
    # Include deleted paths too; filtering to files that still exist leaves a
    # mixed update+delete partially staged and the repository dirty.
    # Stage independently. ``git mv`` already removes the source from the
    # index, so a combined add can reject that now-absent source path before it
    # reaches the destination. Independent adds preserve updates, deletions,
    # and renames without staging unrelated repository files.
    for path in paths:
        _git(repo, "add", "-A", "--", path, check=False)
    if _git(repo, "diff", "--cached", "--quiet", "--", *paths, check=False).returncode == 0:
        raise HTTPException(status_code=409, detail="Project files are unchanged; no commit was created")
    _commit_with_author(
        repo,
        "-m",
        message,
        "--",
        *paths,
        author_name=author_name,
        author_email=author_email,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_project_workspace_file(
    project: Project,
    path: str,
    content: str,
    author_name: str | None,
    author_email: str | None,
) -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_project_delivery_path(repo, path)
        if target.exists() and target.is_dir():
            raise HTTPException(status_code=422, detail="Project file path points to a directory")
        existed_at_head = {normalized: _path_exists_at_head(repo, normalized)}
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".project-tool-write-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(temporary, target)
            commit = _commit_project_delivery_paths(
                repo,
                paths=[normalized],
                message=f"更新项目文件：{normalized}",
                author_name=author_name,
                author_email=author_email,
            )
        except Exception:
            _rollback_project_delivery_paths(repo, [normalized], existed_at_head)
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {
            "status": "completed",
            "operation": "write_file",
            "path": normalized,
            "commit": commit,
            "file": _file_record(repo, normalized),
        }


async def write_project_workspace_file(
    project: Project,
    path: str,
    content: str,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _write_project_workspace_file,
        project,
        path,
        content,
        author_name,
        author_email,
    )


def _edit_project_workspace_file(
    project: Project,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    author_name: str | None,
    author_email: str | None,
) -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_project_delivery_path(repo, path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"File not found: {normalized}")
        content = target.read_text(encoding="utf-8", errors="replace")
        if old_string not in content:
            raise HTTPException(status_code=422, detail="old_string was not found in the project file")
        count = content.count(old_string)
        if count > 1 and not replace_all:
            raise HTTPException(
                status_code=409,
                detail=f"old_string appears {count} times; provide more context or set replace_all",
            )
        updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        existed_at_head = {normalized: True}
        fd, temporary = tempfile.mkstemp(prefix=".project-tool-edit-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
            os.replace(temporary, target)
            commit = _commit_project_delivery_paths(
                repo,
                paths=[normalized],
                message=f"编辑项目文件：{normalized}",
                author_name=author_name,
                author_email=author_email,
            )
        except Exception:
            _rollback_project_delivery_paths(repo, [normalized], existed_at_head)
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {
            "status": "completed",
            "operation": "edit_file",
            "path": normalized,
            "replacements": count if replace_all else 1,
            "commit": commit,
            "file": _file_record(repo, normalized),
        }


async def edit_project_workspace_file(
    project: Project,
    path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _edit_project_workspace_file,
        project,
        path,
        old_string,
        new_string,
        replace_all,
        author_name,
        author_email,
    )


def _move_project_workspace_path(
    project: Project,
    source_path: str,
    destination_path: str,
    overwrite: bool,
    author_name: str | None,
    author_email: str | None,
) -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        source, source_target = _safe_project_delivery_path(repo, source_path)
        if not source_target.exists() or not _path_exists_at_head(repo, source):
            raise HTTPException(status_code=404, detail="Project source path was not found")
        raw_destination = str(destination_path or "")
        if raw_destination.endswith("/"):
            raw_destination = f"{raw_destination}{PurePosixPath(source).name}"
        destination, destination_target = _safe_project_delivery_path(repo, raw_destination)
        if destination_target.exists() and destination_target.is_dir():
            destination = f"{destination}/{PurePosixPath(source).name}"
            destination, destination_target = _safe_project_delivery_path(repo, destination)
        if source == destination:
            raise HTTPException(status_code=409, detail="Source and destination are the same project path")
        if source_target.is_dir() and destination.startswith(f"{source}/"):
            raise HTTPException(status_code=409, detail="Cannot move a folder into itself")
        if destination_target.exists() and not overwrite:
            raise HTTPException(status_code=409, detail="Project destination already exists")
        paths = [source, destination]
        existed_at_head = {path: _path_exists_at_head(repo, path) for path in paths}
        destination_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            args = ["mv"]
            if overwrite:
                args.append("-f")
            _git(repo, *args, "--", source, destination)
            commit = _commit_project_delivery_paths(
                repo,
                paths=paths,
                message=f"移动项目文件：{source} → {destination}",
                author_name=author_name,
                author_email=author_email,
            )
        except Exception:
            _rollback_project_delivery_paths(repo, paths, existed_at_head)
            raise
        return {
            "status": "completed",
            "operation": "move_file",
            "source_path": source,
            "destination_path": destination,
            "commit": commit,
        }


async def move_project_workspace_path(
    project: Project,
    source_path: str,
    destination_path: str,
    *,
    overwrite: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _move_project_workspace_path,
        project,
        source_path,
        destination_path,
        overwrite,
        author_name,
        author_email,
    )


def _delete_project_workspace_file(
    project: Project,
    path: str,
    author_name: str | None,
    author_email: str | None,
) -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_project_delivery_path(repo, path)
        if not target.exists() or not _path_exists_at_head(repo, normalized):
            raise HTTPException(status_code=404, detail=f"File not found: {normalized}")
        existed_at_head = {normalized: True}
        try:
            _git(repo, "rm", "-r", "--", normalized)
            commit = _commit_project_delivery_paths(
                repo,
                paths=[normalized],
                message=f"删除项目文件：{normalized}",
                author_name=author_name,
                author_email=author_email,
            )
        except Exception:
            _rollback_project_delivery_paths(repo, [normalized], existed_at_head)
            raise
        return {
            "status": "completed",
            "operation": "delete_file",
            "path": normalized,
            "commit": commit,
        }


async def delete_project_workspace_file(
    project: Project,
    path: str,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _delete_project_workspace_file,
        project,
        path,
        author_name,
        author_email,
    )


def _commit_project_workspace_sandbox_changes(
    project: Project,
    workspace: ProjectSandboxWorkspace,
    *,
    author_name: str | None,
    author_email: str | None,
) -> dict[str, Any] | None:
    """Conflict-check and commit changes from an isolated public sandbox tree."""

    repo = _repo_for(project)
    with _repo_lock(repo):
        sandbox_hashes: dict[str, str] = {}
        sandbox_files: dict[str, Path] = {}
        total_bytes = 0
        for candidate in workspace.root.rglob("*"):
            if candidate.is_symlink():
                raise HTTPException(status_code=422, detail="Symbolic links are unavailable to project sandboxes")
            if not candidate.is_file():
                continue
            file_size = candidate.stat().st_size
            if file_size > workspace.max_file_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Project sandbox file exceeds the standard size limit: {candidate.name}",
                )
            total_bytes += file_size
            if total_bytes > workspace.max_total_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="Project sandbox files exceed the standard total size limit",
                )
            relative = candidate.relative_to(workspace.root).as_posix()
            normalized, _target = _safe_project_delivery_path(repo, relative)
            sandbox_files[normalized] = candidate
            sandbox_hashes[normalized] = _stream_file_hash(candidate)

        changed_paths = {
            path
            for path in set(workspace.baseline_hashes) | set(sandbox_hashes)
            if workspace.baseline_hashes.get(path) != sandbox_hashes.get(path)
        }
        if not changed_paths:
            return None

        conflicts: list[str] = []
        for path in sorted(changed_paths):
            _normalized, target = _safe_project_delivery_path(repo, path)
            current_hash = _stream_file_hash(target) if target.is_file() else None
            if current_hash != workspace.baseline_hashes.get(path):
                conflicts.append(path)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail="Project files changed during sandbox execution: " + ", ".join(conflicts[:10]),
            )

        existed_at_head = {path: _path_exists_at_head(repo, path) for path in changed_paths}
        temporary_paths: list[str] = []
        try:
            for path in sorted(changed_paths):
                _normalized, target = _safe_project_delivery_path(repo, path)
                source = sandbox_files.get(path)
                if source is None:
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".project-sandbox-", dir=target.parent)
                os.close(fd)
                temporary_paths.append(temporary)
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
                temporary_paths.remove(temporary)
            delivery_paths = sorted(changed_paths)
            commit = _commit_project_delivery_paths(
                repo,
                paths=delivery_paths,
                message="更新项目文件：沙箱执行结果",
                author_name=author_name,
                author_email=author_email,
            )
        except Exception:
            _rollback_project_delivery_paths(repo, sorted(changed_paths), existed_at_head)
            raise
        finally:
            for temporary in temporary_paths:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {
            "status": "completed",
            "operation": "sandbox_commit",
            "commit": commit,
            "paths": delivery_paths,
        }


async def commit_project_workspace_sandbox_changes(
    project: Project,
    workspace: ProjectSandboxWorkspace,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict[str, Any] | None:
    return await asyncio.to_thread(
        _commit_project_workspace_sandbox_changes,
        project,
        workspace,
        author_name=author_name,
        author_email=author_email,
    )


_MILESTONE_OPERATION_TRAILER = "Project-Milestone-Operation"
_LEGACY_MILESTONE_OPERATION_TRAILER = "Clawith-Milestone-Operation"


def _existing_milestone_operation_commit(repo: Path, operation_key: str) -> str | None:
    for trailer_name in (_MILESTONE_OPERATION_TRAILER, _LEGACY_MILESTONE_OPERATION_TRAILER):
        trailer = f"{trailer_name}: {operation_key}"
        result = _git(
            repo,
            "log",
            "--all",
            "-1",
            "--format=%H",
            "--fixed-strings",
            f"--grep={trailer}",
            check=False,
        )
        if result.stdout.strip():
            return result.stdout.strip()
    return None


def _milestone_commit_result(
    repo: Path,
    commit: str,
    message: str,
    normalized_paths: list[str] | None,
    *,
    recovered: bool,
) -> dict:
    changed_paths = [
        line
        for line in _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).stdout.splitlines()
        if line
    ]
    return {
        "status": "completed",
        "operation": "milestone_commit",
        "commit": commit,
        "message": message,
        "paths": normalized_paths,
        "changed": bool(changed_paths),
        "milestone": True,
        "idempotent_replay": recovered,
    }


def _commit(
    project: Project,
    message: str,
    paths: list[str] | None,
    *,
    milestone: bool,
    operation_key: str | None = None,
    force_add: bool = False,
    author_name: str | None,
    author_email: str | None,
) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized_paths = [_safe_relative_path(repo, path)[0] for path in paths] if paths else None
        if operation_key:
            if not milestone:
                raise ValueError("A milestone operation key can only be used for milestone commits")
            existing_commit = _existing_milestone_operation_commit(repo, operation_key)
            if existing_commit:
                return _milestone_commit_result(
                    repo,
                    existing_commit,
                    message,
                    normalized_paths,
                    recovered=True,
                )
        add_args = [
            "add",
            "-A",
            *(["-f"] if force_add else []),
            "--",
            *(normalized_paths or ["."]),
        ]
        _git(repo, *add_args)
        diff_args = ["diff", "--cached", "--quiet"]
        if normalized_paths:
            diff_args.extend(["--", *normalized_paths])
        changed = _git(repo, *diff_args, check=False).returncode != 0
        if not changed and not milestone:
            raise HTTPException(status_code=409, detail="There are no selected project changes to commit")
        commit_args: list[str] = []
        if not changed:
            commit_args.append("--allow-empty")
        if normalized_paths:
            commit_args.append("--only")
        commit_args.extend(["-m", message])
        if operation_key:
            commit_args.extend(["-m", f"{_MILESTONE_OPERATION_TRAILER}: {operation_key}"])
        if normalized_paths:
            commit_args.extend(["--", *normalized_paths])
        _commit_with_author(
            repo,
            *commit_args,
            author_name=author_name,
            author_email=author_email,
        )
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if milestone:
            return _milestone_commit_result(
                repo,
                head,
                message,
                normalized_paths,
                recovered=False,
            )
        return {
            "status": "completed",
            "operation": "milestone_commit" if milestone else "commit",
            "commit": head,
            "message": message,
            "paths": normalized_paths,
            "changed": changed,
            "milestone": milestone,
        }


async def commit_project_changes(
    project: Project,
    message: str,
    paths: list[str] | None = None,
    *,
    milestone: bool = False,
    operation_key: str | None = None,
    force_add: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        _commit,
        project,
        message,
        paths,
        milestone=milestone,
        operation_key=operation_key,
        force_add=force_add,
        author_name=author_name,
        author_email=author_email,
    )




__all__ = [name for name in globals() if not name.startswith("__")]
