"""Safe managed-Git operations for project artifacts.

History is never rewritten: recovery restores a tree and creates a new commit.
The service never executes through a shell and validates every repository path
under the configured ``_projects`` root.
"""

import asyncio
import codecs
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import selectors
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.project import Project, ProjectMemberSnapshot, ProjectRepositoryOperation
from app.services.chat_attachments import sniff_image_mime_bytes, sniff_media_mime_bytes

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SCP_REMOTE_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?"
    r"(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r":(?P<path>[A-Za-z0-9._~@%+/-]+)$"
)
_REPO_LOCKS: dict[Path, threading.RLock] = {}
_REPO_LOCKS_GUARD = threading.Lock()
_REPO_LOCK_STATE = threading.local()
_DEFAULT_PROJECT_AUTHOR_NAME = "项目负责人"
_DEFAULT_PROJECT_AUTHOR_EMAIL = "project@project.local"
_LEGACY_PROJECT_AUTHOR_NAMES = frozenset({"clawith", "clawith project"})
PROJECT_FILE_EDIT_LIMIT_BYTES = 1024 * 1024
PROJECT_FILE_CONTENT_MAX_CHARS = 1024 * 1024
PROJECT_GIT_DIFF_PATCH_MAX_BYTES = 256 * 1024
PROJECT_GIT_DIFF_PATCH_HARD_LIMIT_BYTES = 1024 * 1024
PROJECT_GIT_DIFF_CONTENT_MAX_BYTES = 512 * 1024
PROJECT_GIT_DIFF_FILE_LIST_MAX_BYTES = 1024 * 1024
PROJECT_GIT_DIFF_MAX_FILES = 500
PROJECT_DIRECTORY_ARCHIVE_MAX_FILES = 5000
PROJECT_DIRECTORY_ARCHIVE_MAX_BYTES = 100 * 1024 * 1024
PROJECT_FILE_LIST_PREVIEW_BLOB_MAX_BYTES = 64 * 1024
PROJECT_FILE_LIST_PREVIEW_TOTAL_BYTES = 2 * 1024 * 1024
_TEXT_MIME_BY_SUFFIX = {
    ".c": "text/x-c",
    ".cc": "text/x-c++src",
    ".conf": "text/plain",
    ".cpp": "text/x-c++src",
    ".css": "text/css",
    ".csv": "text/csv",
    ".env": "text/plain",
    ".go": "text/x-go",
    ".h": "text/x-c",
    ".hpp": "text/x-c++hdr",
    ".html": "text/html",
    ".ini": "text/plain",
    ".java": "text/x-java-source",
    ".js": "text/javascript",
    ".json": "application/json",
    ".jsx": "text/javascript",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".mjs": "text/javascript",
    ".py": "text/x-python",
    ".rb": "text/x-ruby",
    ".rs": "text/x-rust",
    ".sh": "text/x-shellscript",
    ".sql": "application/sql",
    ".toml": "application/toml",
    ".ts": "text/typescript",
    ".tsx": "text/typescript-jsx",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


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


@dataclass(slots=True)
class ProjectSandboxWorkspace:
    """Isolated public project tree mounted into one foreground sandbox call."""

    temp_dir: tempfile.TemporaryDirectory
    root: Path
    baseline_hashes: dict[str, str]
    max_file_bytes: int
    max_total_bytes: int

    @property
    def venv_root(self) -> Path:
        return Path(self.temp_dir.name) / ".venv"

    @property
    def runtime_temp_root(self) -> Path:
        return Path(self.temp_dir.name) / ".runtime-tmp"

    def cleanup(self) -> None:
        self.temp_dir.cleanup()


@dataclass(slots=True)
class ProjectReadWorkspace:
    """Exact committed project files materialized for structured readers."""

    temp_dir: tempfile.TemporaryDirectory
    root: Path

    def cleanup(self) -> None:
        self.temp_dir.cleanup()


def _managed_root() -> Path:
    return (Path(get_settings().STORAGE_LOCAL_ROOT).expanduser().resolve() / "_projects").resolve()


def project_repo_path(tenant_id: uuid.UUID, project_id: uuid.UUID) -> Path:
    root = _managed_root()
    repo = (root / str(tenant_id) / str(project_id) / "repo").resolve()
    if root != repo and root not in repo.parents:
        raise RuntimeError("Resolved project repository escaped the managed root")
    return repo


async def remove_project_repository(project: Project) -> None:
    """Remove only this project's managed storage after a failed create flow."""

    project_root = project_repo_path(project.tenant_id, project.id).parent
    managed_root = _managed_root()
    if project_root.name != str(project.id) or managed_root not in project_root.parents:
        raise RuntimeError("Resolved project storage escaped the managed root")
    await asyncio.to_thread(shutil.rmtree, project_root, True)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
    }
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result


def project_agent_git_email(agent_id: uuid.UUID) -> str:
    """Return a stable, repository-local address for one project Agent."""

    return f"agent-{agent_id}@project.local"


def project_user_git_email(user_id: uuid.UUID) -> str:
    """Return a stable, repository-local address for one project user."""

    return f"user-{user_id}@project.local"


def _commit_with_author(
    repo: Path,
    *args: str,
    author_name: str | None,
    author_email: str | None,
) -> subprocess.CompletedProcess[str]:
    """Create a commit with an explicit per-operation identity.

    Repository config remains a neutral safety net. Supplying the identity on
    each commit prevents one actor's configuration from leaking into another
    Agent's later work.
    """

    name = " ".join((author_name or _DEFAULT_PROJECT_AUTHOR_NAME).split())
    email = (author_email or _DEFAULT_PROJECT_AUTHOR_EMAIL).strip()
    if not name:
        name = _DEFAULT_PROJECT_AUTHOR_NAME
    if not email or any(char.isspace() for char in email):
        email = _DEFAULT_PROJECT_AUTHOR_EMAIL
    return _git(
        repo,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        *args,
    )


def _git_stdout_prefix(repo: Path, *args: str, limit: int) -> tuple[bytes, bool]:
    """Read bounded Git stdout and stop the process as soon as the cap is hit.

    Diff output is controlled by repository contents and can be arbitrarily
    large.  Unlike ``subprocess.run(capture_output=True)``, this helper never
    buffers the complete output before applying the API limit.
    """

    safe_limit = max(1, min(int(limit), PROJECT_GIT_DIFF_PATCH_HARD_LIMIT_BYTES))
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
    }
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            env=env,
        )
        output = bytearray()
        truncated = False
        deadline = time.monotonic() + 30
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.terminate()
                    raise HTTPException(status_code=504, detail="Git diff timed out")
                events = selector.select(timeout=min(remaining, 0.25))
                if not events:
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), min(64 * 1024, safe_limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > safe_limit:
                    truncated = True
                    process.terminate()
                    break
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=5)
        if not truncated and return_code != 0:
            stderr_file.seek(0)
            detail = stderr_file.read(8192).decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or "git command failed")
        return bytes(output[:safe_limit]), truncated


class _RepositoryWorkspaceLock:
    """Re-entrant process and cross-process lock for one managed repository."""

    def __init__(self, repo: Path):
        self.repo = repo
        with _REPO_LOCKS_GUARD:
            self.thread_lock = _REPO_LOCKS.setdefault(repo, threading.RLock())

    def __enter__(self):
        self.thread_lock.acquire()
        key = str(self.repo)
        depths = getattr(_REPO_LOCK_STATE, "depths", {})
        handles = getattr(_REPO_LOCK_STATE, "handles", {})
        try:
            if depths.get(key, 0) == 0:
                self.repo.parent.mkdir(parents=True, exist_ok=True)
                handle = (self.repo.parent / f".{self.repo.name}.workspace.lock").open("a+b")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except Exception:
                    handle.close()
                    raise
                handles[key] = handle
            depths[key] = depths.get(key, 0) + 1
            _REPO_LOCK_STATE.depths = depths
            _REPO_LOCK_STATE.handles = handles
            return self
        except Exception:
            self.thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback):
        key = str(self.repo)
        depths = _REPO_LOCK_STATE.depths
        handles = _REPO_LOCK_STATE.handles
        depths[key] -= 1
        if depths[key] == 0:
            depths.pop(key, None)
            handle = handles.pop(key)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self.thread_lock.release()


def _repo_lock(repo: Path) -> _RepositoryWorkspaceLock:
    return _RepositoryWorkspaceLock(repo)


def _reset_project_repository_head(
    project: Project,
    revision: str,
    expected_head: str,
) -> bool:
    repo = _repo_for(project)
    with _repo_lock(repo):
        current_head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        if current_head != expected_head:
            return False
        _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
        _git(repo, "reset", "--hard", revision)
        return True


async def reset_project_repository_head(
    project: Project,
    revision: str,
    *,
    expected_head: str,
) -> bool:
    """Compensate only when the failed commit is still the exact repository head."""

    return await asyncio.to_thread(
        _reset_project_repository_head,
        project,
        revision,
        expected_head,
    )


def _project_repository_commit_is_ancestor(
    project: Project,
    ancestor: str,
    descendant: str,
) -> bool:
    repo = _repo_for(project)
    with _repo_lock(repo):
        result = _git(
            repo,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        return result.returncode == 0


async def project_repository_commit_is_ancestor(
    project: Project,
    ancestor: str,
    descendant: str,
) -> bool:
    return await asyncio.to_thread(
        _project_repository_commit_is_ancestor,
        project,
        ancestor,
        descendant,
    )


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

    if (
        not raw_path
        or "\x00" in raw_path
        or "\\" in raw_path
        or any(ord(char) < 32 or ord(char) == 127 for char in raw_path)
    ):
        raise HTTPException(status_code=422, detail="Invalid project file path")
    pure = PurePosixPath(raw_path)
    normalized = pure.as_posix()
    if pure.is_absolute() or normalized != raw_path or any(part in {"", ".", ".."} for part in pure.parts):
        raise HTTPException(status_code=422, detail="Project file path must be a normalized relative path")
    if any(part.lower() == ".git" for part in pure.parts):
        raise HTTPException(status_code=422, detail="The Git metadata directory cannot be modified")
    target = (repo / normalized).resolve(strict=False)
    if target == repo or repo not in target.parents:
        raise HTTPException(status_code=422, detail="Project file path escaped the managed repository")
    return normalized, target


def _safe_project_delivery_path(repo: Path, raw_path: str) -> tuple[str, Path]:
    """Resolve a public project file without exposing internal Agent assets."""

    normalized, target = _safe_relative_path(repo, raw_path)
    lexical_parts = PurePosixPath(normalized).parts
    resolved_parts = target.relative_to(repo).parts
    if lexical_parts[0].casefold() == ".agents" or resolved_parts[0].casefold() == ".agents":
        raise HTTPException(status_code=422, detail="Project-internal Agent files are not project deliverables")
    candidate = repo
    for part in lexical_parts:
        candidate /= part
        if candidate.is_symlink():
            raise HTTPException(status_code=422, detail="Symbolic links are unavailable to project file tools")
    return normalized, target


def _safe_project_delivery_root(repo: Path, raw_path: str) -> tuple[str, Path]:
    value = str(raw_path or "").strip()
    if value in {"", "."}:
        return "", repo
    return _safe_project_delivery_path(repo, value.rstrip("/"))


def _stream_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_sandbox_parent(repo: Path) -> Path:
    # Keep transient copies outside AGENT_DATA_DIR. Remote AIO sandboxes mount
    # that whole tree for standard Agent workspaces, so placing a project copy
    # beside the repository would expose it to unrelated sandbox sessions.
    parent = Path(tempfile.gettempdir()) / "clawith-project-sandboxes"
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    return parent


def _materialize_git_blob(
    repo: Path,
    object_id: str,
    target: Path,
    *,
    max_bytes: int | None = None,
) -> None:
    size_result = _git(repo, "cat-file", "-s", object_id)
    try:
        size = int(size_result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Git returned an invalid project blob size") from exc
    if max_bytes is not None and size > max_bytes:
        raise HTTPException(status_code=413, detail="Project file exceeds the tool's existing size limit")

    target.parent.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen | None = None
    try:
        with target.open("wb") as output:
            process = subprocess.Popen(
                ["git", "-c", f"safe.directory={repo}", "-C", str(repo), "cat-file", "blob", object_id],
                stdout=output,
                stderr=subprocess.PIPE,
            )
            try:
                _stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired as exc:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise RuntimeError("Project file materialization timed out") from exc
        if process.returncode != 0:
            raise RuntimeError(
                (stderr or b"project file materialization failed")
                .decode("utf-8", errors="replace")[:500]
            )
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _committed_project_delivery_blobs(repo: Path, revision: str) -> Iterator[tuple[str, str]]:
    listing = _git(repo, "ls-tree", "-r", "-z", revision)
    for record in listing.stdout.split("\x00"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RuntimeError("Git returned an invalid project tree entry")
        mode, object_type, object_id = fields
        if object_type != "blob" or mode not in {"100644", "100755"}:
            continue
        try:
            normalized, _target = _safe_project_delivery_path(repo, raw_path)
        except HTTPException:
            continue
        yield normalized, object_id


def _create_project_sandbox_workspace(
    project: Project,
    max_file_bytes: int,
    max_total_bytes: int,
) -> ProjectSandboxWorkspace:
    repo = _repo_for(project)
    with _repo_lock(repo):
        temp_dir = tempfile.TemporaryDirectory(
            prefix="project-code-",
            dir=_project_sandbox_parent(repo),
        )
        root = Path(temp_dir.name) / "repository"
        root.mkdir(mode=0o700)
        baseline_hashes: dict[str, str] = {}
        total_bytes = 0
        try:
            revision = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
            for relative, object_id in _committed_project_delivery_blobs(repo, revision):
                object_size = int(_git(repo, "cat-file", "-s", object_id).stdout.strip())
                if object_size > max_file_bytes or total_bytes + object_size > max_total_bytes:
                    continue
                target = root / relative
                _materialize_git_blob(repo, object_id, target, max_bytes=max_file_bytes)
                baseline_hashes[relative] = _stream_file_hash(target)
                total_bytes += object_size
        except Exception:
            temp_dir.cleanup()
            raise
        return ProjectSandboxWorkspace(
            temp_dir,
            root,
            baseline_hashes,
            max_file_bytes,
            max_total_bytes,
        )


async def create_project_sandbox_workspace(
    project: Project,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> ProjectSandboxWorkspace:
    return await asyncio.to_thread(
        _create_project_sandbox_workspace,
        project,
        max_file_bytes,
        max_total_bytes,
    )


def _materialize_project_read_workspace(
    project: Project,
    paths: list[str],
    max_bytes: int | None,
) -> ProjectReadWorkspace:
    repo = _repo_for(project)
    with _repo_lock(repo):
        temp_dir = tempfile.TemporaryDirectory(
            prefix="project-read-",
            dir=_project_sandbox_parent(repo),
        )
        root = Path(temp_dir.name)
        try:
            revision = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
            for raw_path in paths:
                normalized, _source = _safe_project_delivery_path(repo, raw_path)
                entry = _git(repo, "ls-tree", "-z", revision, "--", normalized, check=False)
                if entry.returncode != 0 or not entry.stdout:
                    raise HTTPException(status_code=404, detail=f"Project file does not exist: {normalized}")
                metadata, separator, recorded_path = entry.stdout.rstrip("\x00").partition("\t")
                fields = metadata.split()
                if (
                    not separator
                    or recorded_path != normalized
                    or len(fields) != 3
                    or fields[1] != "blob"
                    or fields[0] not in {"100644", "100755"}
                ):
                    raise HTTPException(status_code=422, detail=f"Project path is not a regular file: {normalized}")
                target = root / normalized
                _materialize_git_blob(repo, fields[2], target, max_bytes=max_bytes)
        except Exception:
            temp_dir.cleanup()
            raise
        return ProjectReadWorkspace(temp_dir, root)


async def materialize_project_read_workspace(
    project: Project,
    paths: list[str],
    *,
    max_bytes: int | None = None,
) -> ProjectReadWorkspace:
    return await asyncio.to_thread(_materialize_project_read_workspace, project, paths, max_bytes)




__all__ = [name for name in globals() if not name.startswith("__")]
