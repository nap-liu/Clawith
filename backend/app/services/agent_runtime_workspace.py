"""Runtime workspace routing for standard and project-scoped Agents.

The active route is bound once at the conversation runtime boundary. Storage,
context, and tool services can then resolve the same Agent-visible path without
querying project state or duplicating scope conditionals.
"""

from __future__ import annotations

import uuid
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.services.project_git_service import project_repo_path
from app.services.storage_runtime.facade import normalize_storage_key

PROJECT_AGENT_SCOPE = "project"
PROJECT_AGENT_RUNTIME_CONFIG_KEY = "agent_runtime_workspace"
_LEGACY_AGENT_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _normalize_standard_agent_id(agent_id: uuid.UUID | str) -> uuid.UUID | str:
    """Preserve legacy opaque test/adapter IDs without weakening path safety."""

    raw = str(agent_id).strip()
    try:
        return uuid.UUID(raw)
    except ValueError:
        if not _LEGACY_AGENT_KEY_RE.fullmatch(raw):
            raise ValueError("Invalid standard Agent identifier") from None
        return raw


def _normalize_relative_path(path: str) -> str:
    """Normalize an Agent-visible path without permitting root traversal."""
    clean = str(path or "").replace("\\", "/").strip().lstrip("/")
    parts: list[str] = []
    for part in clean.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class AgentRuntimeWorkspace:
    """Canonical local and storage roots for one Agent execution."""

    agent_id: uuid.UUID | str
    local_root: Path
    storage_prefix: str
    project_id: uuid.UUID | None = None
    project_repo_root: Path | None = None

    @property
    def is_project(self) -> bool:
        return self.project_id is not None

    @property
    def supports_daily_memory(self) -> bool:
        return not self.is_project

    def storage_key(self, relative_path: str = "") -> str:
        normalized = _normalize_relative_path(relative_path)
        actual_path = self._actual_relative_path(normalized)
        if not actual_path:
            return self.storage_prefix
        return normalize_storage_key(f"{self.storage_prefix}/{actual_path}")

    def local_path(self, relative_path: str = "") -> Path:
        normalized = _normalize_relative_path(relative_path)
        actual_path = self._actual_relative_path(normalized)
        candidate = (self.local_root / actual_path).resolve() if actual_path else self.local_root.resolve()
        root = self.local_root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("Agent workspace path escaped its runtime root")
        return candidate

    def is_agent_write_protected(self, relative_path: str) -> bool:
        if not self.is_project:
            return False
        normalized = _normalize_relative_path(relative_path)
        return normalized in {"soul.md", "memory.md", "memory/memory.md"}

    def as_session_config(self) -> dict[str, str]:
        """Create a validated routing hint for durable project sessions."""
        if not self.is_project or self.project_repo_root is None:
            return {}
        return {
            "project_id": str(self.project_id),
            "project_repo_root": str(self.project_repo_root),
            "agent_dir": f".agents/{self.agent_id}",
        }

    def _actual_relative_path(self, normalized: str) -> str:
        # Project Agent memory is intentionally a single project-owned file.
        # Keep the standard virtual path so the existing context/tool contract
        # does not need a second public memory filename.
        if self.is_project and normalized == "memory/memory.md":
            return "memory.md"
        return normalized


_active_workspace: ContextVar[AgentRuntimeWorkspace | None] = ContextVar(
    "agent_runtime_workspace",
    default=None,
)


def standard_agent_runtime_workspace(agent_id: uuid.UUID | str) -> AgentRuntimeWorkspace:
    normalized_agent_id = _normalize_standard_agent_id(agent_id)
    settings = get_settings()
    local_root = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR).expanduser().resolve()
    return AgentRuntimeWorkspace(
        agent_id=normalized_agent_id,
        local_root=local_root / str(normalized_agent_id),
        storage_prefix=normalize_storage_key(str(normalized_agent_id)),
    )


def project_agent_runtime_workspace(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str,
    project_id: uuid.UUID | str,
) -> AgentRuntimeWorkspace:
    normalized_agent_id = uuid.UUID(str(agent_id))
    normalized_tenant_id = uuid.UUID(str(tenant_id))
    normalized_project_id = uuid.UUID(str(project_id))
    repo_root = project_repo_path(normalized_tenant_id, normalized_project_id)
    local_root = (repo_root / ".agents" / str(normalized_agent_id)).resolve()
    if repo_root != local_root and repo_root not in local_root.parents:
        raise ValueError("Project Agent workspace escaped the project repository")

    storage_root = Path(get_settings().STORAGE_LOCAL_ROOT).expanduser().resolve()
    try:
        storage_prefix = local_root.relative_to(storage_root).as_posix()
    except ValueError as exc:
        raise ValueError("Project repository is outside the configured storage root") from exc

    return AgentRuntimeWorkspace(
        agent_id=normalized_agent_id,
        local_root=local_root,
        storage_prefix=normalize_storage_key(storage_prefix),
        project_id=normalized_project_id,
        project_repo_root=repo_root,
    )


def resolve_agent_runtime_workspace(
    *,
    agent_id: uuid.UUID | str,
    agent_scope: str | None,
    agent_project_id: uuid.UUID | str | None,
    tenant_id: uuid.UUID | str | None,
    session_project_id: uuid.UUID | str | None,
    session_config: Mapping[str, object] | None = None,
) -> AgentRuntimeWorkspace:
    """Resolve a runtime route from already-loaded Agent/session identity."""
    if str(agent_scope or "").strip().lower() != PROJECT_AGENT_SCOPE:
        return standard_agent_runtime_workspace(agent_id)
    normalized_agent_id = uuid.UUID(str(agent_id))
    if tenant_id is None or agent_project_id is None or session_project_id is None:
        raise ValueError("Project Agent execution requires tenant and project scope")

    normalized_agent_project_id = uuid.UUID(str(agent_project_id))
    normalized_session_project_id = uuid.UUID(str(session_project_id))
    if normalized_agent_project_id != normalized_session_project_id:
        raise ValueError("Project Agent cannot execute outside its owning project")

    resolved = project_agent_runtime_workspace(
        agent_id=normalized_agent_id,
        tenant_id=tenant_id,
        project_id=normalized_session_project_id,
    )
    hint = dict(session_config or {}).get(PROJECT_AGENT_RUNTIME_CONFIG_KEY)
    if isinstance(hint, Mapping) and hint:
        expected = resolved.as_session_config()
        for field in ("project_id", "project_repo_root", "agent_dir"):
            supplied = str(hint.get(field) or "")
            if supplied and supplied != expected[field]:
                raise ValueError(f"Project Agent runtime hint has an invalid {field}")
    return resolved


def current_agent_runtime_workspace(agent_id: uuid.UUID | str) -> AgentRuntimeWorkspace:
    normalized_agent_id = _normalize_standard_agent_id(agent_id)
    active = _active_workspace.get()
    if active is None:
        return standard_agent_runtime_workspace(normalized_agent_id)
    if active.agent_id != normalized_agent_id:
        # A runtime route is an execution principal, not ambient access to a
        # different Agent's private directory.
        return standard_agent_runtime_workspace(normalized_agent_id)
    return active


@contextmanager
def bind_agent_runtime_workspace(workspace: AgentRuntimeWorkspace) -> Iterator[None]:
    token: Token[AgentRuntimeWorkspace | None] = _active_workspace.set(workspace)
    try:
        yield
    finally:
        _active_workspace.reset(token)
