"""Safe managed-Git operations for project artifacts.

History is never rewritten: recovery restores a tree and creates a new commit.
The service never executes through a shell and validates every repository path
under the configured ``_projects`` root.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from app.config import get_settings
from app.models.project import Project

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REPO_LOCKS: dict[Path, threading.RLock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def _managed_root() -> Path:
    return (Path(get_settings().STORAGE_LOCAL_ROOT).expanduser().resolve() / "_projects").resolve()


def project_repo_path(tenant_id: uuid.UUID, project_id: uuid.UUID) -> Path:
    root = _managed_root()
    repo = (root / str(tenant_id) / str(project_id) / "repo").resolve()
    if root != repo and root not in repo.parents:
        raise RuntimeError("Resolved project repository escaped the managed root")
    return repo


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result


def _repo_lock(repo: Path) -> threading.RLock:
    with _REPO_LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(repo, threading.RLock())


def _safe_relative_path(repo: Path, raw_path: str) -> tuple[str, Path]:
    """Resolve one user path under a managed repo without following it outside."""

    if not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise HTTPException(status_code=422, detail="Invalid project file path")
    pure = PurePosixPath(raw_path)
    normalized = pure.as_posix()
    if (
        pure.is_absolute()
        or normalized != raw_path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise HTTPException(status_code=422, detail="Project file path must be a normalized relative path")
    if pure.parts[0].lower() == ".git":
        raise HTTPException(status_code=422, detail="The Git metadata directory cannot be modified")
    target = (repo / normalized).resolve(strict=False)
    if target == repo or repo not in target.parents:
        raise HTTPException(status_code=422, detail="Project file path escaped the managed repository")
    return normalized, target


def _initialize(project: Project) -> dict:
    repo = project_repo_path(project.tenant_id, project.id)
    with _repo_lock(repo):
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            _git(repo, "init")
            _git(repo, "config", "user.name", "Clawith Project")
            _git(repo, "config", "user.email", "projects@clawith.local")
            (repo / "README.md").write_text(
                f"# {project.name}\n\n{project.description}\n\n## Goal\n\n{project.goal}\n",
                encoding="utf-8",
            )
            (repo / "PROJECT.json").write_text(
                json.dumps(
                    {
                        "project_id": str(project.id),
                        "name": project.name,
                        "goal": project.goal,
                        "success_criteria": project.success_criteria,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _git(repo, "add", "--", "README.md", "PROJECT.json")
            _git(repo, "commit", "-m", "Initialize AI-native project")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {
            "mode": "managed",
            "head": head,
            "default_branch": _git(repo, "branch", "--show-current").stdout.strip(),
        }


async def initialize_project_repo(project: Project) -> dict:
    return await asyncio.to_thread(_initialize, project)


def _repo_for(project: Project) -> Path:
    git_settings = dict((project.settings or {}).get("git") or {})
    git_mode = git_settings.get("mode") or git_settings.get("repository_mode", "managed")
    if git_mode != "managed":
        raise HTTPException(status_code=501, detail="External Git repositories require a connector")
    repo = project_repo_path(project.tenant_id, project.id)
    if not (repo / ".git").is_dir():
        raise HTTPException(status_code=409, detail="Managed project repository is not initialized")
    return repo


def _restore(project: Project, commit: str, message: str | None) -> dict:
    if not _COMMIT_RE.fullmatch(commit):
        raise HTTPException(status_code=422, detail="Invalid commit identifier")
    repo = _repo_for(project)
    with _repo_lock(repo):
        if _git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Commit does not exist in this project repository")
        _git(repo, "restore", "--source", commit, "--", ".")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--allow-empty", "-m", message or f"Restore project tree from {commit[:12]}")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {"status": "completed", "operation": "restore_commit", "source_commit": commit, "commit": head}


async def restore_as_new_commit(project: Project, commit: str, message: str | None = None) -> dict:
    return await asyncio.to_thread(_restore, project, commit, message)


def _create_branch(project: Project, name: str, from_commit: str | None) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        if _git(repo, "check-ref-format", "--branch", name, check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Invalid Git branch name")
        start = from_commit or "HEAD"
        if from_commit and (
            not _COMMIT_RE.fullmatch(from_commit)
            or _git(repo, "cat-file", "-e", f"{from_commit}^{{commit}}", check=False).returncode != 0
        ):
            raise HTTPException(status_code=422, detail="Branch source commit does not exist")
        result = _git(repo, "branch", name, start, check=False)
        if result.returncode != 0:
            raise HTTPException(status_code=409, detail=(result.stderr or "Branch already exists").strip())
        return {
            "status": "completed",
            "operation": "create_branch",
            "branch": name,
            "from_commit": _git(repo, "rev-parse", start).stdout.strip(),
        }


async def create_branch(project: Project, name: str, from_commit: str | None = None) -> dict:
    return await asyncio.to_thread(_create_branch, project, name, from_commit)


def _file_record(repo: Path, path: str, *, preview_limit: int = 4000) -> dict:
    size_text = _git(repo, "cat-file", "-s", f"HEAD:{path}", check=False).stdout.strip()
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{path}"],
        capture_output=True,
        timeout=30,
        check=False,
    ).stdout
    preview = "" if b"\x00" in blob else blob[:preview_limit].decode("utf-8", errors="replace")
    commit = _git(repo, "log", "-1", "--format=%H", "--", path).stdout.strip()
    return {
        "id": path,
        "path": path,
        "name": PurePosixPath(path).name,
        "size": int(size_text) if size_text.isdigit() else 0,
        "commit": commit,
        "commit_hash": commit,
        "preview": preview,
        "content_preview": preview,
    }


def _list_files(project: Project) -> list[dict]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        paths = [line for line in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines() if line]
        return [_file_record(repo, path) for path in paths]


async def list_project_files(project: Project) -> list[dict]:
    return await asyncio.to_thread(_list_files, project)


def _write_file(project: Project, path: str, content: str) -> dict:
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
        _git(repo, "commit", "-m", f"Update project file: {normalized}", "--", normalized)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {
            "status": "completed",
            "operation": "write_file",
            "path": normalized,
            "commit": head,
            "file": _file_record(repo, normalized),
        }


async def write_project_file(project: Project, path: str, content: str) -> dict:
    return await asyncio.to_thread(_write_file, project, path, content)


def _commit(project: Project, message: str, paths: list[str] | None, *, milestone: bool) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized_paths = [_safe_relative_path(repo, path)[0] for path in paths] if paths else None
        add_args = ["add", "-A", "--", *(normalized_paths or ["."])]
        _git(repo, *add_args)
        diff_args = ["diff", "--cached", "--quiet"]
        if normalized_paths:
            diff_args.extend(["--", *normalized_paths])
        changed = _git(repo, *diff_args, check=False).returncode != 0
        if not changed and not milestone:
            raise HTTPException(status_code=409, detail="There are no selected project changes to commit")
        commit_args = ["commit"]
        if not changed:
            commit_args.append("--allow-empty")
        if normalized_paths:
            commit_args.append("--only")
        commit_args.extend(["-m", message])
        if normalized_paths:
            commit_args.extend(["--", *normalized_paths])
        _git(repo, *commit_args)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
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
) -> dict:
    return await asyncio.to_thread(_commit, project, message, paths, milestone=milestone)


def _repository_state(project: Project, limit: int) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        log = _git(
            repo,
            "log",
            f"-{limit}",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s",
        ).stdout
        commits = []
        for line in log.splitlines():
            full, short, author, created_at, subject = line.split("\x1f", 4)
            commits.append(
                {
                    "commit": full,
                    "short_commit": short,
                    "author": author,
                    "created_at": created_at,
                    "message": subject,
                }
            )
        files = [line for line in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines() if line]
        branches = [
            line.lstrip("* ")
            for line in _git(repo, "branch", "--format=%(refname:short)").stdout.splitlines()
            if line
        ]
        return {
            "mode": "managed",
            "head": _git(repo, "rev-parse", "HEAD").stdout.strip(),
            "commits": commits,
            "files": files,
            "branches": branches,
        }


async def repository_state(project: Project, limit: int = 50) -> dict:
    return await asyncio.to_thread(_repository_state, project, limit)
