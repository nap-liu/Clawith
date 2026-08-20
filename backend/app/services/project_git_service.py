"""Safe managed-Git operations for project artifacts.

History is never rewritten: recovery restores a tree and creates a new commit.
The service never executes through a shell and validates every repository path
under the configured ``_projects`` root.
"""

import asyncio
import fcntl
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.project import Project, ProjectRepositoryOperation

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SCP_REMOTE_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?"
    r"(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r":(?P<path>[A-Za-z0-9._~@%+/-]+)$"
)
_REPO_LOCKS: dict[Path, threading.RLock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


@dataclass(slots=True)
class ProjectRepositoryCloneOperation:
    """A repository swap awaiting its surrounding database transaction.

    ``backup`` intentionally survives the filesystem swap. The API finalizes
    it only after project settings and the audit event are durably committed;
    otherwise rollback atomically restores the managed initialization
    baseline so the clone can be retried.
    """

    id: uuid.UUID
    repo: Path
    backup: Path
    staging_root: Path
    candidate: Path
    old_head: str
    new_head: str
    result: dict[str, Any]
    lock_handle: BinaryIO | None = None
    active: bool = True


def _managed_root() -> Path:
    return (Path(get_settings().STORAGE_LOCAL_ROOT).expanduser().resolve() / "_projects").resolve()


def project_repo_path(tenant_id: uuid.UUID, project_id: uuid.UUID) -> Path:
    root = _managed_root()
    repo = (root / str(tenant_id) / str(project_id) / "repo").resolve()
    if root != repo and root not in repo.parents:
        raise RuntimeError("Resolved project repository escaped the managed root")
    return repo


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result


def _repo_lock(repo: Path) -> threading.RLock:
    with _REPO_LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(repo, threading.RLock())


def _acquire_repository_operation_lock(repo: Path, *, blocking: bool) -> BinaryIO | None:
    """Acquire the cross-process lock that spans clone DB/FS state changes."""

    repo.parent.mkdir(parents=True, exist_ok=True)
    handle = (repo.parent / ".repository-operation.lock").open("a+b")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_repository_operation_lock(operation: ProjectRepositoryCloneOperation) -> None:
    handle = operation.lock_handle
    operation.lock_handle = None
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _safe_remote_name(name: str) -> str:
    value = name.strip()
    if not _REMOTE_NAME_RE.fullmatch(value) or value in {".", ".."}:
        raise HTTPException(status_code=422, detail="Invalid Git remote name")
    return value


def _safe_remote_url(raw_url: str) -> str:
    """Accept provider-neutral network Git URLs without embedded credentials."""

    value = raw_url.strip()
    if (
        not value
        or value.startswith(("-", "/", "./", "../", "~/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise HTTPException(status_code=422, detail="Invalid network Git remote URL")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid network Git remote URL") from exc
    if "://" in value:
        if parsed.scheme.lower() not in {"http", "https", "ssh", "git"}:
            raise HTTPException(status_code=422, detail="Git remote URL must use http(s), ssh, or git")
        safe_ssh_username = (
            parsed.scheme.lower() == "ssh"
            and parsed.username is not None
            and re.fullmatch(r"[A-Za-z0-9._-]+", parsed.username) is not None
        )
        forbidden_userinfo = parsed.username is not None and not safe_ssh_username
        if not parsed.hostname or forbidden_userinfo or parsed.password is not None:
            raise HTTPException(status_code=422, detail="Git remote URL cannot contain userinfo or credentials")
        if parsed.query or parsed.fragment:
            raise HTTPException(status_code=422, detail="Git remote URL cannot contain query credentials")
        if not parsed.path or parsed.path == "/":
            raise HTTPException(status_code=422, detail="Git remote URL must identify a repository")
        if parsed.scheme.lower() == "ssh":
            ssh_path = parsed.path
            normalized_ssh_path = ssh_path.lstrip("/")
            if (
                re.fullmatch(r"[A-Za-z0-9._~@+/-]+", ssh_path) is None
                or normalized_ssh_path.startswith("-")
                or ".." in PurePosixPath(ssh_path).parts
            ):
                raise HTTPException(status_code=422, detail="Unsafe SSH Git repository path")
        return value
    scp_match = _SCP_REMOTE_RE.fullmatch(value)
    if scp_match is None:
        raise HTTPException(status_code=422, detail="Git remote must be a network URL or SCP-style address")
    scp_path = scp_match.group("path")
    if scp_path.startswith("-") or ".." in PurePosixPath(scp_path).parts:
        raise HTTPException(status_code=422, detail="Unsafe SCP-style Git repository path")
    return value


def _remote_network_target(safe_url: str) -> tuple[str, int]:
    """Return the provider-neutral host/port for one structurally safe URL."""

    if "://" in safe_url:
        parsed = urlsplit(safe_url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid Git remote port") from exc
        defaults = {"http": 80, "https": 443, "ssh": 22, "git": 9418}
        return hostname, parsed_port or defaults[parsed.scheme.lower()]
    matched = _SCP_REMOTE_RE.fullmatch(safe_url)
    if matched is None:  # Kept defensive; _safe_remote_url owns the syntax gate.
        raise HTTPException(status_code=422, detail="Invalid Git remote URL")
    return matched.group("host").strip("[]").rstrip(".").lower(), 22


async def validate_project_remote_url(raw_url: str) -> str:
    """Reject Git targets that resolve to local, private, metadata, or reserved IPs.

    The check applies equally to HTTP(S), SSH, git://, and SCP-style addresses.
    HTTP redirects are disabled in the clone command, preventing a validated
    public origin from redirecting Git to an unvalidated internal destination.
    """

    safe_url = _safe_remote_url(raw_url)
    hostname, port = _remote_network_target(safe_url)
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise HTTPException(status_code=422, detail="Git remote cannot target a local or private network")
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except (OSError, socket.gaierror) as exc:
        raise HTTPException(status_code=422, detail="Git remote hostname could not be resolved") from exc
    resolved = {item[4][0] for item in addresses if item[4]}
    if not resolved:
        raise HTTPException(status_code=422, detail="Git remote hostname could not be resolved")
    for address in resolved:
        try:
            public = ipaddress.ip_address(address).is_global
        except ValueError:
            public = False
        if not public:
            raise HTTPException(status_code=422, detail="Git remote cannot target a local or private network")
    return safe_url


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


def _list_remotes(project: Project) -> list[dict[str, str]]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        names = [line.strip() for line in _git(repo, "remote").stdout.splitlines() if line.strip()]
        return [
            {
                "name": name,
                "url": _git(repo, "remote", "get-url", "--", name).stdout.strip(),
            }
            for name in names
        ]


async def list_git_remotes(project: Project) -> list[dict[str, str]]:
    return await asyncio.to_thread(_list_remotes, project)


def _put_remote(project: Project, name: str, safe_url: str) -> dict[str, str | bool]:
    repo = _repo_for(project)
    safe_name = _safe_remote_name(name)
    with _repo_lock(repo):
        exists = _git(repo, "remote", "get-url", "--", safe_name, check=False).returncode == 0
        command = ("remote", "set-url", "--", safe_name, safe_url) if exists else (
            "remote",
            "add",
            "--",
            safe_name,
            safe_url,
        )
        _git(repo, *command)
        return {"name": safe_name, "url": safe_url, "created": not exists}


async def put_git_remote(project: Project, name: str, url: str) -> dict[str, str | bool]:
    safe_url = await validate_project_remote_url(url)
    return await asyncio.to_thread(_put_remote, project, name, safe_url)


def _delete_remote(project: Project, name: str) -> dict[str, str]:
    repo = _repo_for(project)
    safe_name = _safe_remote_name(name)
    with _repo_lock(repo):
        current = _git(repo, "remote", "get-url", "--", safe_name, check=False)
        if current.returncode != 0:
            raise HTTPException(status_code=404, detail="Git remote not found")
        _git(repo, "remote", "remove", "--", safe_name)
        return {"name": safe_name, "url": current.stdout.strip(), "status": "deleted"}


async def delete_git_remote(project: Project, name: str) -> dict[str, str]:
    return await asyncio.to_thread(_delete_remote, project, name)


def _assert_replaceable_baseline(repo: Path) -> None:
    if _git(repo, "status", "--porcelain").stdout.strip():
        raise HTTPException(status_code=409, detail="Project repository has uncommitted changes")
    count = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    files = {
        line
        for line in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        if line
    }
    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    if count != "1" or files != {"PROJECT.json", "README.md"} or subject != "Initialize AI-native project":
        raise HTTPException(
            status_code=409,
            detail="Clone is only allowed before the project repository contains user output",
        )


def _restore_clone_backup(operation: ProjectRepositoryCloneOperation) -> None:
    repo = operation.repo
    displaced = repo.parent / f".repo-rollback-displaced-{operation.id}"
    try:
        with _repo_lock(repo):
            if operation.backup.exists():
                if repo.exists():
                    if displaced.exists():
                        shutil.rmtree(displaced)
                    os.replace(repo, displaced)
                try:
                    os.replace(operation.backup, repo)
                except BaseException:
                    if displaced.exists() and not repo.exists():
                        os.replace(displaced, repo)
                    raise
            elif not repo.exists() or _git(repo, "rev-parse", "HEAD", check=False).stdout.strip() != operation.old_head:
                raise RuntimeError("Clone rollback lost its durable initialization backup")
            if displaced.exists():
                shutil.rmtree(displaced)
            if operation.staging_root.exists():
                shutil.rmtree(operation.staging_root)
            operation.active = False
    finally:
        _release_repository_operation_lock(operation)


def _stage_clone_repository(
    project: Project,
    safe_url: str,
    branch: str | None,
    operation_id: uuid.UUID,
) -> ProjectRepositoryCloneOperation:
    if project.status != "planning":
        raise HTTPException(status_code=409, detail="A remote can only be cloned while the project is planning")
    repo = _repo_for(project)
    lock_handle = _acquire_repository_operation_lock(repo, blocking=True)
    if lock_handle is None:  # Blocking acquisition only returns None defensively.
        raise HTTPException(status_code=409, detail="A repository operation is already in progress")
    staging_root = repo.parent / f".clawith-clone-{operation_id}"
    candidate = staging_root / "repo"
    backup = repo.parent / f".repo-backup-{operation_id}"
    operation: ProjectRepositoryCloneOperation | None = None
    try:
        if staging_root.exists() or backup.exists():
            raise RuntimeError("Clone operation paths already exist")
        _assert_replaceable_baseline(repo)
        if branch and _git(repo, "check-ref-format", "--branch", branch, check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Invalid Git branch name")
        old_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        staging_root.mkdir(mode=0o700)
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
        }
        args = ["git", "-c", "http.followRedirects=false", "clone", "--no-hardlinks"]
        if branch:
            args.extend(["--branch", branch])
        args.extend(["--", safe_url, str(candidate)])
        cloned = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env=env,
        )
        if cloned.returncode != 0:
            detail = (cloned.stderr or cloned.stdout or "git clone failed").strip()
            raise HTTPException(status_code=422, detail=f"Git clone failed: {detail[:500]}")
        if not (candidate / ".git").is_dir():
            raise HTTPException(status_code=422, detail="Cloned source is not a Git repository")
        if _git(candidate, "fsck", "--no-dangling", check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Cloned Git repository failed validation")
        verified_head = _git(candidate, "rev-parse", "--verify", "HEAD", check=False)
        if verified_head.returncode != 0:
            raise HTTPException(status_code=422, detail="Cloned Git repository has no valid HEAD")
        head = verified_head.stdout.strip()
        default_branch = _git(candidate, "branch", "--show-current").stdout.strip()
        _git(candidate, "config", "user.name", "Clawith Project")
        _git(candidate, "config", "user.email", "projects@clawith.local")
        remotes = [
            {
                "name": name,
                "url": _git(candidate, "remote", "get-url", "--", name).stdout.strip(),
            }
            for name in _git(candidate, "remote").stdout.splitlines()
            if name.strip()
        ]
        operation = ProjectRepositoryCloneOperation(
            id=operation_id,
            repo=repo,
            backup=backup,
            staging_root=staging_root,
            candidate=candidate,
            old_head=old_head,
            new_head=head,
            lock_handle=lock_handle,
            result={
                "status": "completed",
                "operation": "clone",
                "url": safe_url,
                "head": head,
                "default_branch": default_branch,
                "remotes": remotes,
            },
        )
        return operation
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Git clone timed out without prompting") from exc
    finally:
        if operation is None:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            placeholder = ProjectRepositoryCloneOperation(
                id=operation_id,
                repo=repo,
                backup=backup,
                staging_root=staging_root,
                candidate=candidate,
                old_head="",
                new_head="",
                result={},
                lock_handle=lock_handle,
            )
            _release_repository_operation_lock(placeholder)


async def begin_project_repository_clone(
    project: Project,
    url: str,
    branch: str | None = None,
) -> ProjectRepositoryCloneOperation:
    safe_url = await validate_project_remote_url(url)
    operation_id = uuid.uuid4()
    return await asyncio.to_thread(_stage_clone_repository, project, safe_url, branch, operation_id)


def _apply_clone_repository(operation: ProjectRepositoryCloneOperation) -> None:
    with _repo_lock(operation.repo):
        current_head = _git(operation.repo, "rev-parse", "HEAD", check=False).stdout.strip()
        candidate_head = _git(operation.candidate, "rev-parse", "HEAD", check=False).stdout.strip()
        if current_head != operation.old_head or candidate_head != operation.new_head:
            raise HTTPException(status_code=409, detail="Project repository changed while clone was staged")
        os.replace(operation.repo, operation.backup)
        try:
            os.replace(operation.candidate, operation.repo)
        except BaseException:
            os.replace(operation.backup, operation.repo)
            raise


async def apply_project_repository_clone(operation: ProjectRepositoryCloneOperation) -> None:
    await asyncio.to_thread(_apply_clone_repository, operation)


def _finalize_clone_repository(operation: ProjectRepositoryCloneOperation) -> None:
    try:
        with _repo_lock(operation.repo):
            current_head = _git(operation.repo, "rev-parse", "HEAD", check=False).stdout.strip()
            if current_head != operation.new_head:
                raise RuntimeError("Committed clone repository no longer matches its journal")
            if operation.backup.exists():
                shutil.rmtree(operation.backup)
            if operation.staging_root.exists():
                shutil.rmtree(operation.staging_root)
            operation.active = False
    finally:
        _release_repository_operation_lock(operation)


async def finalize_project_repository_clone(operation: ProjectRepositoryCloneOperation) -> None:
    """Discard the initialization backup after the database commit succeeds."""

    await asyncio.to_thread(_finalize_clone_repository, operation)


async def rollback_project_repository_clone(operation: ProjectRepositoryCloneOperation) -> None:
    """Restore the pre-clone repository after a database transaction failure."""

    await asyncio.to_thread(_restore_clone_backup, operation)


async def release_project_repository_clone_lock(operation: ProjectRepositoryCloneOperation) -> None:
    """Release the process lock when the durable DB state cannot be read.

    The journal and filesystem are intentionally left untouched. The next
    repository access can then decide between rollback and finalize from the
    durable state instead of guessing after an ambiguous commit result.
    """

    await asyncio.to_thread(_release_repository_operation_lock, operation)


def _operation_path(repo: Path, name: str, *, prefix: str) -> Path:
    if Path(name).name != name or not name.startswith(prefix):
        raise RuntimeError("Invalid repository operation journal path")
    path = (repo.parent / name).resolve(strict=False)
    if path.parent != repo.parent.resolve():
        raise RuntimeError("Repository operation journal escaped the managed root")
    return path


def _operation_from_journal(row: ProjectRepositoryOperation) -> ProjectRepositoryCloneOperation:
    repo = project_repo_path(row.tenant_id, row.project_id)
    staging_root = _operation_path(repo, row.staging_name, prefix=".clawith-clone-")
    backup = _operation_path(repo, row.backup_name, prefix=".repo-backup-")
    return ProjectRepositoryCloneOperation(
        id=row.id,
        repo=repo,
        backup=backup,
        staging_root=staging_root,
        candidate=staging_root / "repo",
        old_head=row.old_head,
        new_head=row.new_head,
        result={},
    )


def _reconcile_clone_journal(row: ProjectRepositoryOperation) -> bool:
    operation = _operation_from_journal(row)
    operation.lock_handle = _acquire_repository_operation_lock(operation.repo, blocking=False)
    if operation.lock_handle is None:
        return False
    if row.operation_type != "clone":
        _release_repository_operation_lock(operation)
        raise RuntimeError(f"Unsupported repository operation: {row.operation_type}")
    if row.state == "prepared":
        _restore_clone_backup(operation)
    elif row.state == "committed":
        _finalize_clone_repository(operation)
    else:
        _release_repository_operation_lock(operation)
        raise RuntimeError(f"Unsupported repository operation state: {row.state}")
    return True


async def _reconcile_project_repository_operations(
    db: AsyncSession,
    project_id: uuid.UUID | None,
) -> int:
    """Repair abandoned clone journals before the next repository access."""

    from sqlalchemy import select

    statement = select(ProjectRepositoryOperation).order_by(ProjectRepositoryOperation.created_at)
    if project_id is not None:
        statement = statement.where(ProjectRepositoryOperation.project_id == project_id)
    rows = (await db.execute(statement)).scalars().all()
    reconciled = 0
    for row in rows:
        completed = await asyncio.to_thread(_reconcile_clone_journal, row)
        if not completed:
            if project_id is not None:
                raise HTTPException(status_code=409, detail="A repository operation is already in progress")
            continue
        await db.delete(row)
        reconciled += 1
    await db.flush()
    return reconciled


async def reconcile_project_repository_operations(
    project_id: uuid.UUID | None = None,
    *,
    db: AsyncSession | None = None,
) -> int:
    if db is not None:
        return await _reconcile_project_repository_operations(db, project_id)
    async with async_session() as owned_db:
        reconciled = await _reconcile_project_repository_operations(owned_db, project_id)
        await owned_db.commit()
        return reconciled


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


def _read_file(project: Project, path: str, max_chars: int) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, _target = _safe_relative_path(repo, path)
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"HEAD:{normalized}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=404, detail="Project file is not committed at HEAD")
        if b"\x00" in result.stdout:
            raise HTTPException(status_code=422, detail="Binary project files cannot be read as text")
        content = result.stdout.decode("utf-8", errors="replace")
        return {
            "path": normalized,
            "commit": _git(repo, "rev-parse", "HEAD").stdout.strip(),
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }


async def read_project_file(project: Project, path: str, max_chars: int = 20_000) -> dict:
    bounded = min(100_000, max(1, int(max_chars)))
    return await asyncio.to_thread(_read_file, project, path, bounded)


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
