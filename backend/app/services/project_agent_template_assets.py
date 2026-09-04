"""Sanitized project Agent assets for project templates.

Project templates keep only the durable, human-authored portion of a project
Agent: display identity, role, soul, core memory, and safe text files from its
workspace. Database identity, ownership, conversations, runs, credentials, and
accounting never cross the template boundary.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.services.project_agent_workspace import (
    create_project_agent_workspace,
    is_sensitive_project_asset_path,
    project_agent_workspace,
    resolve_project_agent_path,
)
from app.services.workspace_paths import WorkspacePathError

MAX_TEMPLATE_AGENTS = 32
MAX_SOUL_BYTES = 128 * 1024
MAX_CORE_MEMORY_BYTES = 256 * 1024
MAX_WORKSPACE_FILES_PER_AGENT = 128
MAX_WORKSPACE_FILE_BYTES = 256 * 1024
MAX_TEMPLATE_ASSET_BYTES = 4 * 1024 * 1024
MAX_TEMPLATE_PATH_BYTES = 240

_AGENT_KEYS = {
    "name",
    "role_description",
    "is_leader",
    "is_enabled",
    "soul",
    "core_memory",
    "workspace_files",
    "runtime",
    "member_config",
}
_WORKSPACE_FILE_KEYS = {"path", "content"}
_RUNTIME_KEYS = {
    "primary_model_id",
    "fallback_model_id",
    "autonomy_policy",
    "context_window_size",
    "max_tool_rounds",
    "daily_memory_load_days",
    "max_tokens_per_day",
    "max_tokens_per_month",
    "reasoning_effort",
}
_MEMBER_CONFIG_KEYS = {
    "temperature",
    "reasoning_effort",
    "project_instruction",
    "enabled_project_tools",
    "disabled_project_tools",
}
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|credential)"
    r"[\"']?\s*[:=]\s*)([\"']?)([^\s,\"']+)([\"']?)"
)
_PEM_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----",
    re.DOTALL,
)
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")


class ProjectAgentTemplateAssetError(ValueError):
    """Raised when a project Agent template asset is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class ProjectAgentTemplateSource:
    """Project Agent metadata needed for template export."""

    agent_id: uuid.UUID
    name: str
    role_description: str = ""
    is_leader: bool = False
    is_enabled: bool = True
    include_assets: bool = True
    runtime: dict | None = None
    member_config: dict | None = None


@dataclass(frozen=True, slots=True)
class ProjectAgentTemplateInstance:
    """Fresh identity and metadata created while instantiating a template."""

    agent_id: uuid.UUID
    name: str
    role_description: str
    is_leader: bool
    is_enabled: bool
    agent_dir: str
    runtime: dict
    member_config: dict


@dataclass(frozen=True, slots=True)
class _ValidatedTemplateAgent:
    name: str
    role_description: str
    is_leader: bool
    is_enabled: bool
    soul: str
    core_memory: str
    workspace_files: tuple[tuple[str, str], ...]
    runtime: dict
    member_config: dict


def export_project_agent_template_assets(
    project_root: Path,
    sources: Sequence[ProjectAgentTemplateSource],
    *,
    redact_values: Iterable[str | uuid.UUID] = (),
) -> list[dict]:
    """Export project Agents into the sanitized ``definition.agents`` shape."""

    if len(sources) > MAX_TEMPLATE_AGENTS:
        raise ProjectAgentTemplateAssetError(f"A template can contain at most {MAX_TEMPLATE_AGENTS} Agents")

    common_redactions = tuple(str(value) for value in redact_values if str(value))
    total_bytes = 0
    exported: list[dict] = []
    for source in sources:
        agent_redactions = (*common_redactions, str(source.agent_id))
        if source.include_assets:
            layout = project_agent_workspace(project_root, source.agent_id)
            if not layout.root.is_dir():
                raise ProjectAgentTemplateAssetError(f"Project Agent assets are missing: {source.name}")
            soul = _read_sanitized_text(layout.soul, MAX_SOUL_BYTES, agent_redactions, required=True)
            core_memory = _read_sanitized_text(
                layout.memory,
                MAX_CORE_MEMORY_BYTES,
                agent_redactions,
                required=True,
            )
            workspace_files, workspace_bytes = _export_workspace_files(layout.workspace, agent_redactions)
        else:
            soul = ""
            core_memory = ""
            workspace_files = []
            workspace_bytes = 0
        total_bytes += len(soul.encode("utf-8")) + len(core_memory.encode("utf-8")) + workspace_bytes
        _enforce_template_total(total_bytes)
        item = {
            "name": _sanitize_text(source.name, agent_redactions).strip(),
            "role_description": _sanitize_text(source.role_description or "", agent_redactions),
            "is_leader": bool(source.is_leader),
            "is_enabled": bool(source.is_enabled),
            "soul": soul,
            "core_memory": core_memory,
            "workspace_files": workspace_files,
        }
        if source.runtime is not None:
            item["runtime"] = _validate_runtime(source.runtime)
        if source.member_config is not None:
            item["member_config"] = _validate_member_config(source.member_config, agent_redactions)
        exported.append(item)
    return exported


async def instantiate_project_agent_template_assets(
    project_root: Path,
    template_agents: Sequence[dict],
    *,
    agent_ids: Sequence[uuid.UUID] | None = None,
    id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> tuple[ProjectAgentTemplateInstance, ...]:
    """Create fresh ``.agents/<id>/`` assets from ``definition.agents``.

    All entries are validated before any files are written. Callers may supply
    preallocated Agent ids so filesystem creation and database insertion share
    one identity.
    """

    validated = _validate_template_agents(template_agents)
    if agent_ids is None:
        resolved_ids = tuple(id_factory() for _ in validated)
    else:
        if len(agent_ids) != len(validated):
            raise ProjectAgentTemplateAssetError("The number of Agent ids must match the template Agents")
        resolved_ids = tuple(uuid.UUID(str(agent_id)) for agent_id in agent_ids)
    if len(set(resolved_ids)) != len(resolved_ids):
        raise ProjectAgentTemplateAssetError("Template Agent ids must be unique")

    layouts = [project_agent_workspace(project_root, agent_id) for agent_id in resolved_ids]
    if any(layout.root.exists() for layout in layouts):
        raise ProjectAgentTemplateAssetError("A target project Agent directory already exists")

    created_roots: list[Path] = []
    instances: list[ProjectAgentTemplateInstance] = []
    try:
        for agent_id, agent, layout in zip(resolved_ids, validated, layouts, strict=True):
            await create_project_agent_workspace(project_root, agent_id)
            created_roots.append(layout.root)
            await asyncio.to_thread(_write_agent_assets, project_root, agent_id, agent)
            instances.append(
                ProjectAgentTemplateInstance(
                    agent_id=agent_id,
                    name=agent.name,
                    role_description=agent.role_description,
                    is_leader=agent.is_leader,
                    is_enabled=agent.is_enabled,
                    agent_dir=f".agents/{agent_id}",
                    runtime=dict(agent.runtime),
                    member_config=dict(agent.member_config),
                )
            )
    except Exception:
        await asyncio.to_thread(_remove_created_roots, project_root, created_roots)
        raise
    return tuple(instances)


def validate_project_agent_template_assets(template_agents: object) -> None:
    """Validate a template payload without creating project files."""

    sanitize_project_agent_template_assets(template_agents)


def sanitize_project_agent_template_assets(template_agents: object) -> list[dict]:
    """Return the validated, credential-safe JSON representation for storage."""

    if not isinstance(template_agents, list):
        raise ProjectAgentTemplateAssetError("Project template Agents must be a list")
    validated = _validate_template_agents(template_agents)
    result: list[dict] = []
    for raw_agent, agent in zip(template_agents, validated, strict=True):
        item = {
            "name": agent.name,
            "role_description": agent.role_description,
            "is_leader": agent.is_leader,
            "is_enabled": agent.is_enabled,
            "soul": agent.soul,
            "core_memory": agent.core_memory,
            "workspace_files": [{"path": path, "content": content} for path, content in agent.workspace_files],
        }
        if "runtime" in raw_agent:
            item["runtime"] = dict(agent.runtime)
        if "member_config" in raw_agent:
            item["member_config"] = dict(agent.member_config)
        result.append(item)
    return result


async def remove_project_agent_template_instances(
    project_root: Path,
    instances: Sequence[ProjectAgentTemplateInstance],
) -> None:
    """Compensate a failed database transaction after assets were created."""

    layouts = [project_agent_workspace(project_root, instance.agent_id) for instance in instances]
    await asyncio.to_thread(_remove_created_roots, project_root, [layout.root for layout in layouts])


def _export_workspace_files(workspace: Path, redact_values: Sequence[str]) -> tuple[list[dict], int]:
    if not workspace.exists():
        return [], 0
    if workspace.is_symlink() or not workspace.is_dir():
        raise ProjectAgentTemplateAssetError("Project Agent workspace must be a regular directory")

    exported: list[dict] = []
    total_bytes = 0
    for source in sorted(workspace.rglob("*")):
        relative = source.relative_to(workspace)
        if source.is_symlink():
            raise ProjectAgentTemplateAssetError(f"Symbolic links cannot be exported: {relative.as_posix()}")
        if source.is_dir():
            continue
        if not source.is_file():
            raise ProjectAgentTemplateAssetError(f"Unsupported workspace asset: {relative.as_posix()}")
        relative_path = _validate_workspace_path(relative.as_posix())
        if is_sensitive_project_asset_path(PurePosixPath(relative_path)):
            continue
        content = _read_sanitized_text(
            source,
            MAX_WORKSPACE_FILE_BYTES,
            redact_values,
            required=True,
        )
        total_bytes += len(content.encode("utf-8"))
        _enforce_template_total(total_bytes)
        exported.append({"path": relative_path, "content": content})
        if len(exported) > MAX_WORKSPACE_FILES_PER_AGENT:
            raise ProjectAgentTemplateAssetError(
                f"A project Agent template can contain at most {MAX_WORKSPACE_FILES_PER_AGENT} workspace files"
            )
    return exported, total_bytes


def _validate_template_agents(template_agents: Sequence[dict]) -> tuple[_ValidatedTemplateAgent, ...]:
    if len(template_agents) > MAX_TEMPLATE_AGENTS:
        raise ProjectAgentTemplateAssetError(f"A template can contain at most {MAX_TEMPLATE_AGENTS} Agents")

    validated: list[_ValidatedTemplateAgent] = []
    total_bytes = 0
    for raw_agent in template_agents:
        if not isinstance(raw_agent, dict):
            raise ProjectAgentTemplateAssetError("Each template Agent must be an object")
        unknown_keys = set(raw_agent) - _AGENT_KEYS
        if unknown_keys:
            raise ProjectAgentTemplateAssetError(
                f"Unsupported project Agent template fields: {', '.join(sorted(unknown_keys))}"
            )

        name = str(raw_agent.get("name") or "").strip()
        if not name or len(name) > 200:
            raise ProjectAgentTemplateAssetError("Each template Agent requires a valid display name")
        role_description = str(raw_agent.get("role_description") or "")
        soul = _validated_text_value(raw_agent.get("soul", ""), MAX_SOUL_BYTES, "soul")
        core_memory = _validated_text_value(
            raw_agent.get("core_memory", ""),
            MAX_CORE_MEMORY_BYTES,
            "core memory",
        )
        raw_files = raw_agent.get("workspace_files", [])
        if not isinstance(raw_files, list):
            raise ProjectAgentTemplateAssetError("workspace_files must be a list")
        if len(raw_files) > MAX_WORKSPACE_FILES_PER_AGENT:
            raise ProjectAgentTemplateAssetError(
                f"A project Agent template can contain at most {MAX_WORKSPACE_FILES_PER_AGENT} workspace files"
            )

        files: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                raise ProjectAgentTemplateAssetError("Each workspace file must be an object")
            unknown_file_keys = set(raw_file) - _WORKSPACE_FILE_KEYS
            if unknown_file_keys:
                raise ProjectAgentTemplateAssetError(
                    f"Unsupported workspace file fields: {', '.join(sorted(unknown_file_keys))}"
                )
            path = _validate_workspace_path(str(raw_file.get("path") or ""))
            if is_sensitive_project_asset_path(PurePosixPath(path)):
                continue
            if path in seen_paths:
                raise ProjectAgentTemplateAssetError(f"Duplicate workspace file path: {path}")
            seen_paths.add(path)
            content = _validated_text_value(
                raw_file.get("content", ""),
                MAX_WORKSPACE_FILE_BYTES,
                f"workspace file {path}",
            )
            files.append((path, content))
            total_bytes += len(content.encode("utf-8"))

        total_bytes += len(soul.encode("utf-8")) + len(core_memory.encode("utf-8"))
        _enforce_template_total(total_bytes)
        redactions: tuple[str, ...] = ()
        runtime = _validate_runtime(raw_agent.get("runtime", {}))
        member_config = _validate_member_config(raw_agent.get("member_config", {}), redactions)
        validated.append(
            _ValidatedTemplateAgent(
                name=_sanitize_text(name, redactions),
                role_description=_sanitize_text(role_description, redactions),
                is_leader=bool(raw_agent.get("is_leader", False)),
                is_enabled=bool(raw_agent.get("is_enabled", True)),
                soul=_sanitize_text(soul, redactions),
                core_memory=_sanitize_text(core_memory, redactions),
                workspace_files=tuple((path, _sanitize_text(content, redactions)) for path, content in files),
                runtime=runtime,
                member_config=member_config,
            )
        )
    return tuple(validated)


def _validate_runtime(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - _RUNTIME_KEYS:
        raise ProjectAgentTemplateAssetError("Project digital employee runtime configuration is invalid")
    result: dict = {}
    for key in ("primary_model_id", "fallback_model_id"):
        raw_id = value.get(key)
        if raw_id is not None:
            try:
                result[key] = str(uuid.UUID(str(raw_id)))
            except ValueError as exc:
                raise ProjectAgentTemplateAssetError(f"{key} must be a valid model identifier") from exc
        else:
            result[key] = None
    for key, default, minimum, maximum in (
        ("context_window_size", 100, 1, 10_000),
        ("max_tool_rounds", 50, 1, 500),
        ("daily_memory_load_days", 0, 0, 30),
    ):
        raw = value.get(key, default)
        if not isinstance(raw, int) or isinstance(raw, bool) or not minimum <= raw <= maximum:
            raise ProjectAgentTemplateAssetError(f"{key} is outside the supported range")
        result[key] = raw
    for key in ("max_tokens_per_day", "max_tokens_per_month"):
        raw = value.get(key)
        if raw is not None and (
            not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 1_000_000_000_000_000
        ):
            raise ProjectAgentTemplateAssetError(f"{key} is outside the supported range")
        result[key] = raw
    raw_policy = value.get("autonomy_policy", {})
    if not isinstance(raw_policy, dict):
        raise ProjectAgentTemplateAssetError("autonomy_policy must be an object")
    policy: dict[str, str] = {}
    for key, level in raw_policy.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", key):
            raise ProjectAgentTemplateAssetError("autonomy_policy contains an unsupported permission")
        if level not in {"L1", "L2", "L3"}:
            raise ProjectAgentTemplateAssetError("autonomy_policy levels must be L1, L2, or L3")
        policy[key] = level
    result["autonomy_policy"] = policy
    reasoning_effort = value.get("reasoning_effort")
    if reasoning_effort not in {
        None, "none", "minimal", "low", "medium", "high", "xhigh", "max"
    }:
        raise ProjectAgentTemplateAssetError("reasoning_effort is unsupported")
    result["reasoning_effort"] = reasoning_effort
    return result


def _validate_member_config(value: object, redactions: Sequence[str]) -> dict:
    if not isinstance(value, dict) or set(value) - _MEMBER_CONFIG_KEYS:
        raise ProjectAgentTemplateAssetError("Project digital employee member configuration is invalid")
    instruction = value.get("project_instruction", "")
    if not isinstance(instruction, str) or len(instruction.encode("utf-8")) > MAX_CORE_MEMORY_BYTES:
        raise ProjectAgentTemplateAssetError("Project instruction must be bounded UTF-8 text")
    temperature = value.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
    ):
        raise ProjectAgentTemplateAssetError("temperature is outside the supported range")
    result: dict = {
        "temperature": float(temperature) if temperature is not None else None,
        "reasoning_effort": value.get("reasoning_effort"),
        "project_instruction": _sanitize_text(instruction, redactions),
    }
    if result["reasoning_effort"] not in {
        None, "none", "minimal", "low", "medium", "high", "xhigh", "max"
    }:
        raise ProjectAgentTemplateAssetError("reasoning_effort is unsupported")
    for key in ("enabled_project_tools", "disabled_project_tools"):
        raw_names = value.get(key, [])
        if not isinstance(raw_names, list) or len(raw_names) > 256:
            raise ProjectAgentTemplateAssetError(f"{key} must be a bounded list")
        names: list[str] = []
        for raw_name in raw_names:
            if not isinstance(raw_name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", raw_name):
                raise ProjectAgentTemplateAssetError(f"{key} contains an unsupported tool name")
            names.append(raw_name)
        result[key] = names
    return result


def _read_sanitized_text(
    path: Path,
    max_bytes: int,
    redact_values: Sequence[str],
    *,
    required: bool,
) -> str:
    if path.is_symlink():
        raise ProjectAgentTemplateAssetError(f"Symbolic links cannot be exported: {path.name}")
    if not path.exists():
        if required:
            raise ProjectAgentTemplateAssetError(f"Required project Agent asset is missing: {path.name}")
        return ""
    if not path.is_file():
        raise ProjectAgentTemplateAssetError(f"Project Agent asset must be a regular file: {path.name}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ProjectAgentTemplateAssetError(f"Project Agent asset exceeds the {max_bytes}-byte limit: {path.name}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectAgentTemplateAssetError(f"Only UTF-8 text workspace assets can be exported: {path.name}") from exc
    return _sanitize_text(content, redact_values)


def _validated_text_value(value: object, max_bytes: int, label: str) -> str:
    if not isinstance(value, str):
        raise ProjectAgentTemplateAssetError(f"{label} must be UTF-8 text")
    if len(value.encode("utf-8")) > max_bytes:
        raise ProjectAgentTemplateAssetError(f"{label} exceeds the {max_bytes}-byte limit")
    return value


def _validate_workspace_path(raw_path: str) -> str:
    if not raw_path or "\\" in raw_path or len(raw_path.encode("utf-8")) > MAX_TEMPLATE_PATH_BYTES:
        raise ProjectAgentTemplateAssetError("Workspace file path is invalid")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectAgentTemplateAssetError(f"Workspace file path is unsafe: {raw_path}")
    return path.as_posix()


def _sanitize_text(content: str, redact_values: Sequence[str]) -> str:
    sanitized = content
    for value in sorted({value for value in redact_values if value}, key=len, reverse=True):
        sanitized = sanitized.replace(value, "[redacted-id]")
    sanitized = _UUID_PATTERN.sub("[redacted-id]", sanitized)
    sanitized = _EMAIL_PATTERN.sub("[redacted-email]", sanitized)
    sanitized = _BEARER_PATTERN.sub(r"\1[redacted]", sanitized)
    sanitized = _PEM_PATTERN.sub("[redacted-private-key]", sanitized)
    sanitized = _URL_CREDENTIAL_PATTERN.sub(r"\1[redacted]@", sanitized)
    sanitized = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(r"\1\2[redacted]\4", sanitized)
    return sanitized


def _write_agent_assets(project_root: Path, agent_id: uuid.UUID, agent: _ValidatedTemplateAgent) -> None:
    layout = project_agent_workspace(project_root, agent_id)
    _write_atomic(layout.soul, agent.soul)
    _write_atomic(layout.memory, agent.core_memory)
    for relative_path, content in agent.workspace_files:
        target = resolve_project_agent_path(
            project_root,
            agent_id,
            f"workspace/{relative_path}",
            allow_root=False,
        )
        _write_atomic(target, content)


def _write_atomic(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.template")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_created_roots(project_root: Path, created_roots: Sequence[Path]) -> None:
    expected_parent = (Path(project_root).resolve() / ".agents").resolve()
    for root in created_roots:
        try:
            resolved = root.resolve()
            if resolved.parent == expected_parent and resolved.name == str(uuid.UUID(resolved.name)):
                shutil.rmtree(resolved, ignore_errors=True)
        except (OSError, ValueError, WorkspacePathError):
            continue


def _enforce_template_total(total_bytes: int) -> None:
    if total_bytes > MAX_TEMPLATE_ASSET_BYTES:
        raise ProjectAgentTemplateAssetError(
            f"Project Agent template assets exceed the {MAX_TEMPLATE_ASSET_BYTES}-byte total limit"
        )
