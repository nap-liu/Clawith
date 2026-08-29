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


def _initialize(
    project: Project,
    author_name: str | None,
    author_email: str | None,
) -> dict:
    repo = project_repo_path(project.tenant_id, project.id)
    with _repo_lock(repo):
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            _git(repo, "init")
            _git(repo, "config", "user.name", _DEFAULT_PROJECT_AUTHOR_NAME)
            _git(repo, "config", "user.email", _DEFAULT_PROJECT_AUTHOR_EMAIL)
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
            _commit_with_author(
                repo,
                "-m",
                "创建项目初始版本",
                author_name=author_name,
                author_email=author_email,
            )
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {
            "mode": "managed",
            "head": head,
            "default_branch": _git(repo, "branch", "--show-current").stdout.strip(),
        }


async def initialize_project_repo(
    project: Project,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict:
    return await asyncio.to_thread(_initialize, project, author_name, author_email)


def _repo_for(project: Project) -> Path:
    git_settings = dict((project.settings or {}).get("git") or {})
    git_mode = git_settings.get("mode") or git_settings.get("repository_mode", "managed")
    if git_mode != "managed":
        raise HTTPException(status_code=501, detail="External Git repositories require a connector")
    repo = project_repo_path(project.tenant_id, project.id)
    if not (repo / ".git").is_dir():
        raise HTTPException(status_code=409, detail="Managed project repository is not initialized")
    return repo


def _restore(
    project: Project,
    commit: str,
    message: str | None,
    author_name: str | None,
    author_email: str | None,
) -> dict:
    if not _COMMIT_RE.fullmatch(commit):
        raise HTTPException(status_code=422, detail="Invalid commit identifier")
    repo = _repo_for(project)
    with _repo_lock(repo):
        if _git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Commit does not exist in this project repository")
        _git(repo, "restore", "--source", commit, "--", ".")
        _git(repo, "add", "-A")
        _commit_with_author(
            repo,
            "--allow-empty",
            "-m",
            message or "恢复项目版本",
            author_name=author_name,
            author_email=author_email,
        )
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {"status": "completed", "operation": "restore_commit", "source_commit": commit, "commit": head}


async def restore_as_new_commit(
    project: Project,
    commit: str,
    message: str | None = None,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        _restore,
        project,
        commit,
        message,
        author_name,
        author_email,
    )


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
        command = (
            ("remote", "set-url", "--", safe_name, safe_url)
            if exists
            else (
                "remote",
                "add",
                "--",
                safe_name,
                safe_url,
            )
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


def _assert_replaceable_baseline(project: Project, repo: Path) -> None:
    """Allow clone only over the generated pre-delivery project baseline.

    Project creation records the initialization commit in settings, then may
    add one system-owned member-directory commit so every selected member has
    a project-local identity.  Those generated assets are still part of the
    replaceable planning baseline; any other commit or path is user output and
    must make clone fail closed.
    """

    if _git(repo, "status", "--porcelain").stdout.strip():
        raise HTTPException(status_code=409, detail="Project repository has uncommitted changes")
    configured_head = str(dict((project.settings or {}).get("git") or {}).get("head") or "")
    baseline_exists = bool(
        _COMMIT_RE.fullmatch(configured_head)
        and _git(repo, "cat-file", "-e", f"{configured_head}^{{commit}}", check=False).returncode == 0
    )
    baseline_files = (
        {line for line in _git(repo, "ls-tree", "-r", "--name-only", configured_head).stdout.splitlines() if line}
        if baseline_exists
        else set()
    )
    baseline_subject = _git(repo, "log", "-1", "--format=%s", configured_head).stdout.strip() if baseline_exists else ""
    baseline_is_ancestor = bool(
        baseline_exists
        and _git(repo, "merge-base", "--is-ancestor", configured_head, "HEAD", check=False).returncode == 0
    )
    generated_subjects = (
        _git(repo, "log", "--format=%s", f"{configured_head}..HEAD").stdout.splitlines() if baseline_is_ancestor else []
    )
    generated_authors = (
        _git(repo, "log", "--format=%ae", f"{configured_head}..HEAD").stdout.splitlines()
        if baseline_is_ancestor
        else []
    )
    generated_paths = (
        {line for line in _git(repo, "diff", "--name-only", configured_head, "HEAD").stdout.splitlines() if line}
        if baseline_is_ancestor
        else set()
    )
    generated_subjects_only = all(
        subject in {"Ensure project member directories", "创建项目成员目录"}
        or subject.startswith(("Create project Agent: ", "创建项目数字员工："))
        for subject in generated_subjects
    )
    generated_authors_only = all(
        author in {_DEFAULT_PROJECT_AUTHOR_EMAIL, project_user_git_email(project.owner_user_id)}
        for author in generated_authors
    )
    generated_only = (
        generated_subjects_only
        and generated_authors_only
        and all(path.startswith(".agents/") for path in generated_paths)
    )
    if (
        baseline_files != {"PROJECT.json", "README.md"}
        or baseline_subject
        not in {"Initialize project", "Initialize AI-native project", "创建项目初始版本"}
        or not baseline_is_ancestor
        or not generated_only
    ):
        raise HTTPException(
            status_code=409,
            detail="Clone is only allowed before the project repository contains user output",
        )


def _copy_generated_member_baseline(repo: Path, candidate: Path) -> None:
    """Carry the current project's generated member identities into a clone."""

    source = repo / ".agents"
    if not source.is_dir():
        return
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise HTTPException(status_code=409, detail="Generated project member assets contain a symbolic link")
    shutil.copytree(source, candidate / ".agents", dirs_exist_ok=True)
    _git(candidate, "add", "--", ".agents")
    if _git(candidate, "diff", "--cached", "--quiet", "--", ".agents", check=False).returncode == 0:
        return
    _commit_with_author(
        candidate,
        "-m",
        "创建项目成员目录",
        author_name=None,
        author_email=None,
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
        _assert_replaceable_baseline(project, repo)
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
        _copy_generated_member_baseline(repo, candidate)
        if _git(candidate, "fsck", "--no-dangling", check=False).returncode != 0:
            raise HTTPException(status_code=422, detail="Cloned Git repository failed validation")
        verified_head = _git(candidate, "rev-parse", "--verify", "HEAD", check=False)
        if verified_head.returncode != 0:
            raise HTTPException(status_code=422, detail="Cloned Git repository has no valid HEAD")
        head = verified_head.stdout.strip()
        default_branch = _git(candidate, "branch", "--show-current").stdout.strip()
        _git(candidate, "config", "user.name", _DEFAULT_PROJECT_AUTHOR_NAME)
        _git(candidate, "config", "user.email", _DEFAULT_PROJECT_AUTHOR_EMAIL)
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
    if project_id is not None:
        await _ensure_project_member_directories(db, project_id)
    await db.flush()
    return reconciled


def _missing_project_member_identity_paths(repo: Path, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    missing: dict[uuid.UUID, list[str]] = {}
    with _repo_lock(repo):
        tracked_paths = set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", ".agents").stdout.splitlines())
        for agent_id in agent_ids:
            agent_dir = f".agents/{agent_id}"
            paths = [f"{agent_dir}/soul.md", f"{agent_dir}/memory.md"]
            absent = [path for path in paths if path not in tracked_paths]
            if absent:
                missing[agent_id] = absent
    return missing


def _commit_project_member_workspace_paths(project: Project, paths: list[str]) -> None:
    """Commit only files copied into member snapshots, never unrelated changes."""

    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized_paths = [_safe_relative_path(repo, path)[0] for path in paths]
        _git(repo, "add", "--", *normalized_paths)
        diff_args = ["diff", "--cached", "--quiet", "--", *normalized_paths]
        if _git(repo, *diff_args, check=False).returncode == 0:
            return
        _commit_with_author(
            repo,
            "--only",
            "-m",
            "创建项目成员目录",
            "--",
            *normalized_paths,
            author_name=None,
            author_email=None,
        )


async def _ensure_project_member_directories(db: AsyncSession, project_id: uuid.UUID) -> None:
    """Keep every member snapshot visible through the canonical project Git HEAD."""

    project = await db.get(Project, project_id)
    if project is None:
        return
    repo = project_repo_path(project.tenant_id, project.id)
    if not (repo / ".git").is_dir():
        return
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectMemberSnapshot.created_at, ProjectMemberSnapshot.id)
            )
        )
        .scalars()
        .all()
    )
    missing = await asyncio.to_thread(
        _missing_project_member_identity_paths,
        repo,
        [member.agent_id for member in members],
    )
    if not missing:
        return

    agents = (
        (
            await db.execute(
                select(Agent).where(
                    Agent.tenant_id == project.tenant_id,
                    Agent.id.in_(missing),
                )
            )
        )
        .scalars()
        .all()
    )
    agent_by_id = {agent.id: agent for agent in agents}

    from app.services.project_agent_workspace import (
        build_project_agent_identity_defaults,
        create_project_agent_workspace,
    )

    changed_paths: set[str] = set()
    for member in members:
        if member.agent_id not in missing:
            continue
        agent = agent_by_id.get(member.agent_id)
        source_agent_id = None
        if agent is not None:
            source_agent_id = agent.source_agent_id if agent.scope == "project" else agent.id
        default_soul, default_memory = build_project_agent_identity_defaults(
            project_name=project.name,
            project_goal=project.goal or "",
            success_criteria=project.success_criteria or [],
            agent_name=member.name_snapshot,
            role_description=member.role_snapshot,
        )
        copy_result = await create_project_agent_workspace(
            repo,
            member.agent_id,
            source_agent_id=source_agent_id,
            default_soul=default_soul,
            default_memory=default_memory,
            copy_source_memory=False,
            copy_source_workspace=False,
        )
        changed_paths.update(missing[member.agent_id])
        changed_paths.update(f".agents/{member.agent_id}/{path}" for path in copy_result.copied)
    await asyncio.to_thread(
        _commit_project_member_workspace_paths,
        project,
        sorted(changed_paths),
    )


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


def _blob_prefix(repo: Path, object_id: str, limit: int) -> bytes:
    """Read only a bounded prefix from an immutable Git object."""

    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "blob", object_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        return process.stdout.read(max(0, limit))
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _mime_and_kind(path: str, sample: bytes) -> tuple[str, str, bool]:
    suffix = PurePosixPath(path).suffix.lower()
    sniffed_image = sniff_image_mime_bytes(sample)
    if sniffed_image:
        return sniffed_image, "image", False
    sniffed_media = sniff_media_mime_bytes(sample, path)
    if sniffed_media:
        return sniffed_media, sniffed_media.split("/", 1)[0], False
    mime_type = _TEXT_MIME_BY_SUFFIX.get(suffix)
    if mime_type is None:
        mime_type = mimetypes.guess_type(path, strict=False)[0]

    media_kind = (mime_type or "").split("/", 1)[0]
    if media_kind in {"image", "video", "audio"}:
        return mime_type or "application/octet-stream", media_kind, False

    known_text = bool(
        mime_type
        and (
            mime_type.startswith("text/")
            or mime_type
            in {
                "application/json",
                "application/ld+json",
                "application/sql",
                "application/toml",
                "application/xml",
                "application/yaml",
                "application/javascript",
            }
            or mime_type.endswith(("+json", "+xml"))
        )
    )
    if b"\x00" in sample:
        return mime_type or "application/octet-stream", "binary", False
    try:
        # ``sample`` is a bounded blob prefix and may end in the middle of a
        # valid multi-byte code point. An incremental decoder validates every
        # complete sequence while retaining an incomplete trailing sequence
        # for the unread next chunk.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        decoder.decode(sample, final=False)
    except UnicodeDecodeError:
        return mime_type or "application/octet-stream", "binary", False
    if known_text or mime_type is None:
        return mime_type or "text/plain", "text", True
    return mime_type, "binary", False


def _tree_entry_at(repo: Path, revision: str, path: str) -> tuple[str, int] | None:
    result = _git(repo, "ls-tree", "-l", "-z", revision, "--", path, check=False)
    if result.returncode != 0 or not result.stdout:
        return None
    record = result.stdout.rstrip("\x00")
    metadata, separator, recorded_path = record.partition("\t")
    parts = metadata.split()
    if not separator or recorded_path != path or len(parts) != 4 or parts[1] != "blob":
        return None
    object_id = parts[2]
    try:
        size = int(parts[3])
    except ValueError as exc:
        raise RuntimeError("Git returned an invalid project blob size") from exc
    return object_id, size


def _tree_entry(repo: Path, path: str) -> tuple[str, int]:
    entry = _tree_entry_at(repo, "HEAD", path)
    if entry is None:
        raise HTTPException(status_code=404, detail="Project file is not committed at HEAD")
    return entry


def _file_record_at(repo: Path, revision: str, path: str, *, preview_limit: int = 4000) -> dict:
    entry = _tree_entry_at(repo, revision, path)
    if entry is None:
        raise HTTPException(status_code=404, detail="Project file is not committed at this revision")
    object_id, size = entry
    sample = _blob_prefix(repo, object_id, max(preview_limit + 1, 8192))
    mime_type, kind, is_text = _mime_and_kind(path, sample)
    preview = sample[:preview_limit].decode("utf-8", errors="replace") if is_text else ""
    commit = _git(repo, "log", "-1", "--format=%H", revision, "--", path).stdout.strip()
    head = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.strip()
    return {
        "id": path,
        "path": path,
        "name": PurePosixPath(path).name,
        "size": size,
        "commit": commit,
        "commit_hash": commit,
        "head": head,
        "object_id": object_id,
        "mime_type": mime_type,
        "kind": kind,
        "is_text": is_text,
        "is_editable": is_text and size <= PROJECT_FILE_EDIT_LIMIT_BYTES,
        "preview": preview,
        "content_preview": preview,
    }


def _file_record(repo: Path, path: str, *, preview_limit: int = 4000) -> dict:
    return _file_record_at(repo, "HEAD", path, preview_limit=preview_limit)


def _batch_blob_prefixes(
    repo: Path,
    entries: list[tuple[str, int]],
    *,
    prefix_limit: int,
) -> dict[str, bytes]:
    """Read bounded previews for many Git blobs through one Git process.

    The file-list endpoint only needs a small preview for editor routing and
    empty-state hints.  Large blobs are deliberately excluded, and the total
    batch size is capped so a repository containing generated assets cannot
    turn a metadata request into an unbounded content download.
    """

    selected: dict[str, int] = {}
    total_size = 0
    for object_id, size in entries:
        if object_id in selected or size > PROJECT_FILE_LIST_PREVIEW_BLOB_MAX_BYTES:
            continue
        if total_size + size > PROJECT_FILE_LIST_PREVIEW_TOTAL_BYTES:
            continue
        selected[object_id] = size
        total_size += size
    if not selected:
        return {}

    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
    }
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo}",
            "-C",
            str(repo),
            "cat-file",
            "--batch",
        ],
        input="".join(f"{object_id}\n" for object_id in selected).encode(),
        capture_output=True,
        timeout=30,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "git cat-file batch failed")

    payload = result.stdout
    position = 0
    previews: dict[str, bytes] = {}
    for expected_object_id, expected_size in selected.items():
        header_end = payload.find(b"\n", position)
        if header_end < 0:
            raise RuntimeError("Git returned a truncated batch header")
        header = payload[position:header_end].decode("ascii", errors="strict")
        parts = header.split()
        if len(parts) != 3 or parts[1] != "blob":
            raise RuntimeError("Git returned an invalid batch object")
        object_id, _kind, raw_size = parts
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise RuntimeError("Git returned an invalid batch object size") from exc
        if object_id != expected_object_id or size != expected_size:
            raise RuntimeError("Git batch output did not match the requested object")
        content_start = header_end + 1
        content_end = content_start + size
        if content_end >= len(payload) or payload[content_end : content_end + 1] != b"\n":
            raise RuntimeError("Git returned a truncated batch object")
        previews[object_id] = payload[content_start:content_end][:prefix_limit]
        position = content_end + 1
    return previews


def _last_commit_by_path(repo: Path, paths: set[str]) -> dict[str, str]:
    """Resolve last-changing commits for all visible paths in one traversal."""

    if not paths:
        return {}
    history = _git(
        repo,
        "log",
        "--pretty=format:%x1e%H%x1f",
        "--name-only",
        "-z",
        "HEAD",
    ).stdout
    commits: dict[str, str] = {}
    for record in history.split("\x1e")[1:]:
        commit, separator, names = record.partition("\x1f")
        if not separator:
            continue
        for path in names.strip("\x00\n").split("\x00"):
            normalized = path.strip("\n")
            if normalized in paths and normalized not in commits:
                commits[normalized] = commit
        if len(commits) == len(paths):
            break
    return commits


def _list_files(project: Project) -> list[dict]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        tree = _git(repo, "ls-tree", "-r", "-l", "-z", "HEAD").stdout
        entries: list[tuple[str, str, int]] = []
        for record in tree.split("\x00"):
            if not record:
                continue
            metadata, separator, path = record.partition("\t")
            parts = metadata.split()
            if not separator or len(parts) != 4 or parts[1] != "blob":
                continue
            try:
                size = int(parts[3])
            except ValueError as exc:
                raise RuntimeError("Git returned an invalid project blob size") from exc
            entries.append((path, parts[2], size))

        head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        commits = _last_commit_by_path(repo, {path for path, _object_id, _size in entries})
        samples = _batch_blob_prefixes(
            repo,
            [(object_id, size) for _path, object_id, size in entries],
            prefix_limit=8192,
        )
        files: list[dict] = []
        for path, object_id, size in entries:
            sample = samples.get(object_id, b"")
            mime_type, kind, is_text = _mime_and_kind(path, sample)
            preview = sample[:4000].decode("utf-8", errors="replace") if is_text else ""
            commit = commits.get(path, head)
            files.append(
                {
                    "id": path,
                    "path": path,
                    "name": PurePosixPath(path).name,
                    "size": size,
                    "commit": commit,
                    "commit_hash": commit,
                    "head": head,
                    "object_id": object_id,
                    "mime_type": mime_type,
                    "kind": kind,
                    "is_text": is_text,
                    "is_editable": is_text and size <= PROJECT_FILE_EDIT_LIMIT_BYTES,
                    "preview": preview,
                    "content_preview": preview,
                }
            )
        return files


async def list_project_files(project: Project) -> list[dict]:
    return await asyncio.to_thread(_list_files, project)


def _display_project_tool_size(size: int) -> str:
    return f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB"


def _list_project_workspace(project: Project, path: str) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_project_delivery_root(repo, path)
        if not target.exists():
            return f"Directory not found: {path or '/'}"
        if not target.is_dir():
            return f"Path is not a directory: {path}"
        items: list[str] = []
        folder_count = 0
        file_count = 0
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or child.is_symlink():
                continue
            child_path = f"{normalized}/{child.name}" if normalized else child.name
            if child.is_dir():
                folder_count += 1
                child_count = len(
                    [
                        entry
                        for entry in child.iterdir()
                        if not entry.name.startswith(".") and not entry.is_symlink()
                    ]
                )
                items.append(f"  📁 {child_path}/ ({child_count} items)")
            elif child.is_file():
                file_count += 1
                items.append(f"  📄 {child_path} ({_display_project_tool_size(child.stat().st_size)})")
        label = normalized or "root"
        if not items:
            return f"📂 {label}: Empty directory (0 files, 0 folders)"
        return f"📂 {label}: {folder_count} folder(s), {file_count} file(s)\n" + "\n".join(items)


async def list_project_workspace(project: Project, path: str = "") -> str:
    return await asyncio.to_thread(_list_project_workspace, project, path)


def _read_project_workspace_file(project: Project, path: str, offset: int, limit: int) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_project_delivery_path(repo, path)
        if not target.exists() or not target.is_file():
            return (
                "File not found.\n"
                f"Requested path: {normalized}\n"
                "The requested path was not modified. Use list_files and copy an exact returned path."
            )
        start = max(0, int(offset))
        bounded_limit = max(0, int(limit))
        selected: list[str] = []
        total_lines = 0
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                total_lines = index + 1
                if start <= index < start + bounded_limit:
                    selected.append(line.rstrip("\r\n"))
        if start >= total_lines and total_lines:
            return f"Offset {offset} exceeds file length ({total_lines} lines total)"
        end = min(total_lines, start + bounded_limit)
        body = "\n".join(
            f"{index + 1:6}\t{line}"
            for index, line in enumerate(selected, start=start)
        )
        if end < total_lines:
            body += f"\n\n... [{total_lines - end} more lines not shown, lines {end + 1}-{total_lines}]"
        header = f"📄 {normalized} (lines {start + 1 if total_lines else 0}-{end} of {total_lines})\n"
        return header + body


async def read_project_workspace_file(
    project: Project,
    path: str,
    *,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    return await asyncio.to_thread(_read_project_workspace_file, project, path, offset, limit)


def _project_workspace_search(
    project: Project,
    pattern: str,
    path: str,
    file_pattern: str,
    ignore_case: bool,
) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        _normalized, target = _safe_project_delivery_root(repo, path)
        if not target.exists() or not target.is_dir():
            return f"Directory not found: {path}"
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return f"Invalid regex pattern: {exc}"

        results: list[str] = []
        total_matches = 0
        files_searched = 0
        binary_suffixes = {
            ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png",
            ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz",
        }
        for candidate in target.rglob("*"):
            if len(results) >= 50:
                break
            try:
                matched_path, candidate = _safe_project_delivery_path(
                    repo,
                    candidate.relative_to(repo).as_posix(),
                )
            except (HTTPException, ValueError):
                continue
            if (
                not candidate.is_file()
                or any(part.startswith(".") for part in PurePosixPath(matched_path).parts)
                or candidate.suffix.lower() in binary_suffixes
            ):
                continue
            relative_match = candidate.relative_to(target).as_posix()
            if not (
                fnmatch.fnmatch(candidate.name, file_pattern)
                or fnmatch.fnmatch(relative_match, file_pattern)
                or fnmatch.fnmatch(matched_path, file_pattern)
            ):
                continue
            files_searched += 1
            try:
                content = candidate.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    results.append(f"{matched_path}:{line_number}: {line.strip()[:100]}")
                    total_matches += 1
                    if len(results) >= 50:
                        break
        if not results:
            return f"No matches found for pattern '{pattern}' in {files_searched} file(s)"
        truncated = total_matches > len(results)
        note = (
            f" (showing first {len(results)} of {total_matches}+ — refine pattern or path for more)"
            if truncated
            else ""
        )
        return (
            f"🔍 Found {total_matches}+ match(es) in {files_searched} file(s) "
            f"for pattern '{pattern}'{note}:\n" + "\n".join(results)
        )


async def search_project_workspace(
    project: Project,
    pattern: str,
    *,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
) -> str:
    return await asyncio.to_thread(
        _project_workspace_search,
        project,
        pattern,
        path,
        file_pattern,
        ignore_case,
    )


def _find_project_workspace_files(project: Project, pattern: str, path: str) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        _normalized, target = _safe_project_delivery_root(repo, path)
        if not target.exists() or not target.is_dir():
            return f"Directory not found: {path}"
        try:
            found: list[tuple[str, Path]] = []
            for candidate in target.glob(pattern):
                try:
                    matched_path, safe_candidate = _safe_project_delivery_path(
                        repo,
                        candidate.relative_to(repo).as_posix(),
                    )
                except (HTTPException, ValueError):
                    continue
                if any(part.startswith(".") for part in PurePosixPath(matched_path).parts):
                    continue
                found.append((matched_path, safe_candidate))
        except Exception as exc:
            return f"Invalid glob pattern: {exc}"
        found.sort(key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0, reverse=True)
        matches: list[str] = []
        folder_count = 0
        file_count = 0
        for matched_path, candidate in found[:100]:
            if candidate.is_dir():
                folder_count += 1
                matches.append(f"📁 {matched_path}/")
            elif candidate.is_file():
                file_count += 1
                matches.append(f"📄 {matched_path} ({_display_project_tool_size(candidate.stat().st_size)})")
        if not matches:
            return f"No files matching pattern: {pattern}"
        return (
            f"📂 Found {len(found)} item(s) ({folder_count} dirs, {file_count} files) "
            f"matching '{pattern}':\n" + "\n".join(matches)
        )


async def find_project_workspace_files(project: Project, pattern: str, *, path: str = ".") -> str:
    return await asyncio.to_thread(_find_project_workspace_files, project, pattern, path)


def _inspect_file(project: Project, path: str) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, _target = _safe_relative_path(repo, path)
        return _file_record(repo, normalized, preview_limit=0)


async def inspect_project_file(project: Project, path: str) -> dict:
    return await asyncio.to_thread(_inspect_file, project, path)


def _inspect_file_at(project: Project, path: str, revision: str) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, _target = _safe_relative_path(repo, path)
        commit = _resolve_reachable_commit(repo, revision)
        return _file_record_at(repo, commit, normalized, preview_limit=0)


async def inspect_project_file_at(project: Project, path: str, revision: str) -> dict:
    return await asyncio.to_thread(_inspect_file_at, project, path, revision)


def _read_file_content(project: Project, path: str, max_chars: int) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, _target = _safe_relative_path(repo, path)
        metadata = _file_record(repo, normalized, preview_limit=0)
        if not metadata["is_text"]:
            return {**metadata, "content": None, "truncated": False, "is_editable": False}
        # A UTF-8 code point uses at most four bytes. The extra byte lets us
        # distinguish an exact boundary from a truncated preview.
        sample = _blob_prefix(repo, metadata["object_id"], max_chars * 4 + 1)
        try:
            decoded = sample.decode("utf-8")
        except UnicodeDecodeError:
            # The sample may end in the middle of one UTF-8 code point.
            decoded = sample.decode("utf-8", errors="ignore")
        content = decoded[:max_chars]
        truncated = metadata["size"] > len(sample) or len(decoded) > max_chars
        return {
            **metadata,
            "content": content,
            "truncated": truncated,
            # Never let a caller overwrite a file from a partial editor buffer.
            "is_editable": bool(metadata["is_editable"] and not truncated),
        }


async def read_project_file_content(project: Project, path: str, max_chars: int = 200_000) -> dict:
    bounded = min(PROJECT_FILE_CONTENT_MAX_CHARS, max(1, int(max_chars)))
    return await asyncio.to_thread(_read_file_content, project, path, bounded)


def iter_project_file_blob(
    project: Project,
    object_id: str,
    *,
    start: int,
    end: int,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Yield one immutable HEAD blob range without materializing it in memory."""

    repo = _repo_for(project)
    remaining = max(0, end - start + 1)
    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "blob", object_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        skipped = 0
        while skipped < start:
            chunk = process.stdout.read(min(chunk_size, start - skipped))
            if not chunk:
                raise RuntimeError("Git project blob ended before the requested range")
            skipped += len(chunk)
        while remaining:
            chunk = process.stdout.read(min(chunk_size, remaining))
            if not chunk:
                raise RuntimeError("Git project blob ended before the requested range")
            remaining -= len(chunk)
            yield chunk
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _directory_snapshot(project: Project, path: str, revision: str = "HEAD") -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        commit = (
            _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
            if revision == "HEAD"
            else _resolve_reachable_commit(repo, revision)
        )
        normalized = ""
        if path:
            normalized, _target = _safe_relative_path(repo, path)
            directory = _git(repo, "ls-tree", "-d", "-z", commit, "--", normalized, check=False)
            if directory.returncode != 0 or not directory.stdout:
                raise HTTPException(status_code=404, detail="Project directory is not committed at this revision")

        listing = _git(repo, "ls-tree", "-r", "-l", "-z", commit, "--", *([normalized] if normalized else []))
        file_count = 0
        total_size = 0
        for raw_record in listing.stdout.split("\x00"):
            if not raw_record:
                continue
            metadata, separator, recorded_path = raw_record.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 4 or fields[1] != "blob":
                raise HTTPException(status_code=422, detail="Directory contains an unsupported Git entry")
            # Symlinks can become traversal primitives after ZIP extraction.
            if fields[0] not in {"100644", "100755"}:
                raise HTTPException(status_code=422, detail="Directory archives cannot contain symbolic links")
            safe_path, _target = _safe_relative_path(repo, recorded_path)
            if normalized and not safe_path.startswith(f"{normalized}/"):
                raise HTTPException(status_code=422, detail="Directory archive path escaped its prefix")
            try:
                size = int(fields[3])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Directory contains an unsupported Git entry") from exc
            file_count += 1
            total_size += size
            if file_count > PROJECT_DIRECTORY_ARCHIVE_MAX_FILES:
                raise HTTPException(status_code=413, detail="Directory contains too many files to download")
            if total_size > PROJECT_DIRECTORY_ARCHIVE_MAX_BYTES:
                raise HTTPException(status_code=413, detail="Directory is too large to download")
        if file_count == 0:
            raise HTTPException(status_code=404, detail="Project directory has no committed files")
        archive_stem = PurePosixPath(normalized).name if normalized else f"project-{str(project.id)[:8]}"
        return {
            "path": normalized,
            "head": commit,
            "name": f"{archive_stem}.zip",
            "file_count": file_count,
            "total_size": total_size,
        }


async def inspect_project_directory(
    project: Project,
    path: str = "",
    revision: str = "HEAD",
) -> dict[str, Any]:
    return await asyncio.to_thread(_directory_snapshot, project, path, revision)


def iter_project_directory_archive(
    project: Project,
    revision: str,
    path: str = "",
    *,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Stream a validated immutable Git tree as ZIP without staging files."""

    repo = _repo_for(project)
    # Store mode keeps CPU and output expansion predictable after the
    # preflight uncompressed-size limit.
    args = ["git", "-c", f"safe.directory={repo}", "-C", str(repo), "archive", "--format=zip", "-0", revision]
    if path:
        args.extend(["--", path])
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdout is not None
        while chunk := process.stdout.read(chunk_size):
            yield chunk
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)
        if return_code != 0:
            raise RuntimeError("Git directory archive failed")


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


def _resolve_reachable_commit(repo: Path, raw_commit: str) -> str:
    value = raw_commit.strip()
    if not _COMMIT_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail="Invalid commit identifier")
    resolved = _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}", check=False)
    if resolved.returncode != 0:
        raise HTTPException(status_code=422, detail="Commit does not exist in this project repository")
    commit = resolved.stdout.strip()
    # Git can retain unreachable objects after a branch is deleted. Knowing an
    # object id must not be enough to read repository content through this API.
    if _git(repo, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode != 0:
        raise HTTPException(status_code=422, detail="Commit is not reachable from the project history")
    return commit


def _utf8_patch_prefix(data: bytes, *, truncated: bool) -> str:
    text = data.decode("utf-8", errors="ignore")
    if truncated and "\n" in text:
        # Keep the response on a complete diff line. The response is explicitly
        # marked truncated and is a viewer payload, never an applicable patch.
        text = text[: text.rfind("\n") + 1]
    return text


def _text_blob_at(repo: Path, revision: str | None, path: str) -> dict[str, Any]:
    if revision is None:
        return {"content": "", "truncated": False, "binary": False, "size": 0}
    entry = _tree_entry_at(repo, revision, path)
    if entry is None:
        return {"content": "", "truncated": False, "binary": False, "size": 0}
    object_id, size = entry
    sample = _blob_prefix(repo, object_id, PROJECT_GIT_DIFF_CONTENT_MAX_BYTES + 1)
    _mime_type, _kind, is_text = _mime_and_kind(path, sample[:8192])
    if not is_text:
        return {"content": None, "truncated": False, "binary": True, "size": size}
    prefix = sample[:PROJECT_GIT_DIFF_CONTENT_MAX_BYTES]
    content = prefix.decode("utf-8", errors="ignore")
    return {
        "content": content,
        "truncated": size > PROJECT_GIT_DIFF_CONTENT_MAX_BYTES,
        "binary": False,
        "size": size,
    }


def _parse_numstat(payload: bytes) -> tuple[list[tuple[str, int | None, int | None]], bool]:
    records = payload.split(b"\x00")
    complete = payload.endswith(b"\x00")
    if complete:
        records.pop()
    elif records:
        records.pop()  # Never expose a partial path/stat record.
    parsed: list[tuple[str, int | None, int | None]] = []
    for raw_record in records:
        fields = raw_record.split(b"\t", 2)
        if len(fields) != 3:
            continue
        additions_raw, deletions_raw, raw_path = fields
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            additions = None if additions_raw == b"-" else int(additions_raw)
            deletions = None if deletions_raw == b"-" else int(deletions_raw)
        except ValueError:
            continue
        parsed.append((path, additions, deletions))
        if len(parsed) >= PROJECT_GIT_DIFF_MAX_FILES:
            return parsed, True
    return parsed, not complete


def _project_commit_diff(
    project: Project,
    commit: str,
    parent: str | None,
    path: str | None,
    max_patch_bytes: int,
) -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        target_commit = _resolve_reachable_commit(repo, commit)
        revision_line = _git(repo, "rev-list", "--parents", "-n", "1", target_commit).stdout.strip().split()
        available_parents = revision_line[1:]
        if parent:
            parent_commit = _resolve_reachable_commit(repo, parent)
            if parent_commit not in available_parents:
                raise HTTPException(status_code=422, detail="Parent must be a direct parent of the target commit")
        else:
            parent_commit = available_parents[0] if available_parents else None

        normalized_path = _safe_relative_path(repo, path)[0] if path else None
        common_options = [
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ]
        if parent_commit is None:
            base_args = ["diff-tree", "--root", "--no-commit-id", "-r"]
            revisions = [target_commit]
        else:
            base_args = ["diff"]
            revisions = [parent_commit, target_commit]

        path_args = ["--", normalized_path] if normalized_path else ["--"]
        patch_data, patch_truncated = _git_stdout_prefix(
            repo,
            *base_args,
            "-p",
            "--unified=3",
            *common_options,
            *revisions,
            *path_args,
            limit=max_patch_bytes,
        )
        stats_data, stats_truncated = _git_stdout_prefix(
            repo,
            *base_args,
            "--numstat",
            "-z",
            *common_options,
            *revisions,
            *path_args,
            limit=PROJECT_GIT_DIFF_FILE_LIST_MAX_BYTES,
        )
        stats, files_truncated = _parse_numstat(stats_data)
        files: list[dict[str, Any]] = []
        for changed_path, additions, deletions in stats:
            try:
                safe_path = _safe_relative_path(repo, changed_path)[0]
            except HTTPException:
                files_truncated = True
                continue
            original_entry = _tree_entry_at(repo, parent_commit, safe_path) if parent_commit else None
            modified_entry = _tree_entry_at(repo, target_commit, safe_path)
            if normalized_path:
                original = _text_blob_at(repo, parent_commit, safe_path)
                modified = _text_blob_at(repo, target_commit, safe_path)
            else:
                # Commit-wide requests remain metadata/patch bounded. Full
                # before/after blobs are returned only for an explicitly
                # validated path, which is what the Monaco viewer requests.
                is_binary = additions is None or deletions is None
                original = {
                    "content": None,
                    "truncated": False,
                    "binary": is_binary,
                    "size": original_entry[1] if original_entry else 0,
                }
                modified = {
                    "content": None,
                    "truncated": False,
                    "binary": is_binary,
                    "size": modified_entry[1] if modified_entry else 0,
                }
            if parent_commit is None or original_entry is None:
                status = "added"
            elif modified_entry is None:
                status = "deleted"
            else:
                status = "modified"
            files.append(
                {
                    "path": safe_path,
                    "status": status,
                    "additions": additions,
                    "deletions": deletions,
                    "binary": bool(original["binary"] or modified["binary"]),
                    "original_content": original["content"],
                    "modified_content": modified["content"],
                    "content_included": normalized_path is not None,
                    "content_truncated": bool(original["truncated"] or modified["truncated"]),
                    "original_size": original["size"],
                    "modified_size": modified["size"],
                }
            )

        safe_patch_limit = max(1, min(int(max_patch_bytes), PROJECT_GIT_DIFF_PATCH_HARD_LIMIT_BYTES))
        return {
            "commit": target_commit,
            "target_commit": target_commit,
            "parent": parent_commit,
            "parent_commit": parent_commit,
            "available_parent_commits": available_parents,
            "is_root": parent_commit is None,
            "path": normalized_path,
            "patch": _utf8_patch_prefix(patch_data, truncated=patch_truncated),
            "patch_truncated": patch_truncated,
            "patch_bytes": min(len(patch_data), safe_patch_limit),
            "max_patch_bytes": safe_patch_limit,
            "files": files,
            "files_truncated": bool(files_truncated or stats_truncated),
        }


async def project_commit_diff(
    project: Project,
    commit: str,
    parent: str | None = None,
    path: str | None = None,
    max_patch_bytes: int = PROJECT_GIT_DIFF_PATCH_MAX_BYTES,
) -> dict[str, Any]:
    return await asyncio.to_thread(_project_commit_diff, project, commit, parent, path, max_patch_bytes)


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
            if author.strip().casefold() in _LEGACY_PROJECT_AUTHOR_NAMES:
                author = "项目成员"
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
            line.lstrip("* ") for line in _git(repo, "branch", "--format=%(refname:short)").stdout.splitlines() if line
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
