from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import re
import unicodedata
import uuid
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.workspace_collaboration import normalize_workspace_path

_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.STORAGE_LOCAL_ROOT or _settings.AGENT_DATA_DIR)


def _root_agent_tools():
    from app.services import agent_tools as root_agent_tools

    return root_agent_tools


def _is_enterprise_info_path(path: str | None) -> bool:
    normalized = str(path or "").replace("\\", "/").strip().strip("/")
    return normalized == "enterprise_info" or normalized.startswith("enterprise_info/")


def _is_webhook_inbox_path(path: str | None) -> bool:
    normalized = str(path or "").replace("\\", "/").strip().strip("/")
    return normalized == "webhook" or normalized.startswith("webhook/")


async def _get_agent_tenant_id(agent_id: uuid.UUID) -> str | None:
    """Get the agent tenant ID for tenant-scoped shared paths."""
    try:
        async with async_session() as db:
            r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
            tenant_id = r.scalar_one_or_none()
            if tenant_id:
                return str(tenant_id)
    except Exception:
        pass
    return None


def _agent_workspace_root(agent_id: uuid.UUID) -> Path:
    """Return the per-agent local path without creating or hydrating it."""
    return current_agent_runtime_workspace(agent_id).local_root


def _non_empty_paths(*paths: str | None) -> list[str] | None:
    selected = [path for path in paths if path]
    return selected or None


def _normalize_tool_rel_path(rel_path: str) -> str:
    """Normalize a relative path for tool I/O.

    `.lstrip("./")` is a charset strip (any leading '.' or '/' chars), not a
    string strip. That accidentally erased the leading dot of hidden
    directories like `.tool_results/` — the very directory persisted-output
    writes into. Result: every read_file targeting a persisted-output file
    looked up the wrong path and returned "File not found", trapping the
    agent in a retry loop. Fix: strip only the literal "./" prefix (one or
    more), then strip leading slashes. `..` traversal is still neutralized
    by the startswith(root) check in `_resolve_tool_source_path`.
    """
    normalized = unicodedata.normalize("NFC", (rel_path or "").strip()).replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    normalized = re.sub(r"^(\./)+", "", normalized)
    return normalized.lstrip("/")


def _collapse_filename_for_match(name: str) -> str:
    """Return a comparison-only filename key for tolerant source lookup.

    Exact storage paths always win. This key is used only when an exact source
    does not exist, and a match is accepted only when it is unique. NFKC folds
    full-width variants while ``isspace`` covers Unicode whitespace that a model
    may insert when retyping a displayed filename (for example ``6 月``).
    """
    normalized = unicodedata.normalize("NFKC", name or "").casefold()
    return "".join(char for char in normalized if not char.isspace())


def _allowed_root_for_tool_path(ws: Path, rel_path: str, tenant_id: str | None = None) -> tuple[Path, str]:
    normalized = _normalize_tool_rel_path(rel_path)
    if normalized.startswith("enterprise_info"):
        workspace_root = _root_agent_tools().WORKSPACE_ROOT
        enterprise_root = (
            (workspace_root / f"enterprise_info_{tenant_id}").resolve()
            if tenant_id
            else (workspace_root / "enterprise_info").resolve()
        )
        sub = normalized[len("enterprise_info"):].lstrip("/")
        return enterprise_root, sub
    return ws.resolve(), normalized


def _resolve_tool_source_path(ws: Path, rel_path: str, tenant_id: str | None = None) -> Path:
    root, normalized = _allowed_root_for_tool_path(ws, rel_path, tenant_id=tenant_id)
    candidate = (root / normalized).resolve() if normalized else root
    if not str(candidate).startswith(str(root)):
        raise ValueError("Access denied for this path")
    if candidate.exists():
        return candidate

    parent = candidate.parent
    if parent.exists():
        wanted = _collapse_filename_for_match(candidate.name)
        matches = [sibling for sibling in parent.iterdir() if _collapse_filename_for_match(sibling.name) == wanted]
        if len(matches) == 1:
            return matches[0]
    return candidate


_QUOTED_UPLOAD_PATH_PATTERNS = (
    re.compile(r"'(?P<path>[^'\r\n]*workspace/uploads/[^'\r\n]+)'"),
    re.compile(r'"(?P<path>[^"\r\n]*workspace/uploads/[^"\r\n]+)"'),
)


def _canonicalize_execute_code_upload_paths(ws: Path, code: str) -> tuple[str, list[tuple[str, str]]]:
    """Canonicalize unique existing upload paths embedded in code literals.

    ``execute_code`` is intentionally generic, so target paths must not be
    fuzzy-rewritten. Uploaded attachments are the narrow safe exception:
    when a quoted ``workspace/uploads/...`` literal does not exist but folds
    to exactly one existing file, replace only that literal's upload suffix.
    This covers model-generated ``6 月.xlsx`` variants while preserving new
    output paths elsewhere in the workspace exactly as requested.
    """
    rewritten = code
    replacements: list[tuple[str, str]] = []
    root = ws.resolve()

    for pattern in _QUOTED_UPLOAD_PATH_PATTERNS:

        def _replace(match: re.Match[str]) -> str:
            raw_path = match.group("path")
            marker_index = raw_path.find("workspace/uploads/")
            if marker_index < 0:
                return match.group(0)
            prefix = raw_path[:marker_index]
            virtual_path = raw_path[marker_index:]
            try:
                resolved = _resolve_tool_source_path(root, virtual_path)
                if not resolved.is_file():
                    return match.group(0)
                canonical = resolved.relative_to(root).as_posix()
            except (OSError, ValueError):
                return match.group(0)
            if canonical == virtual_path:
                return match.group(0)
            canonical_raw = f"{prefix}{canonical}"
            replacements.append((raw_path, canonical_raw))
            quote = match.group(0)[0]
            return f"{quote}{canonical_raw}{quote}"

        rewritten = pattern.sub(_replace, rewritten)

    return rewritten, replacements


def _resolve_tool_target_path(ws: Path, rel_path: str, tenant_id: str | None = None) -> Path:
    root, normalized = _allowed_root_for_tool_path(ws, rel_path, tenant_id=tenant_id)
    candidate = (root / normalized).resolve() if normalized else root
    if not str(candidate).startswith(str(root)):
        raise ValueError("❌ Access denied.")
    return candidate


def _tool_storage_key(agent_id: uuid.UUID, rel_path: str, tenant_id: str | None = None) -> tuple[str, str, bool]:
    normalized = normalize_workspace_path(_normalize_tool_rel_path(rel_path))
    if _is_enterprise_info_path(normalized):
        if not tenant_id:
            return (
                normalize_storage_key("enterprise_info/" + normalized.removeprefix("enterprise_info").lstrip("/")),
                normalized,
                True,
            )
        sub = normalized[len("enterprise_info"):].lstrip("/")
        key = f"enterprise_info_{tenant_id}/{sub}" if sub else f"enterprise_info_{tenant_id}"
        return normalize_storage_key(key), normalized, True
    key = current_agent_runtime_workspace(agent_id).storage_key(normalized)
    return key, normalized, False


@dataclass(frozen=True)
class _ResolvedStorageSource:
    """Canonical storage identity for one agent-visible source path."""

    storage_key: str
    virtual_path: str
    is_enterprise: bool
    exists: bool
    ambiguous_candidates: tuple[str, ...] = ()


async def _resolve_exact_storage_source_path(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
) -> _ResolvedStorageSource:
    """Resolve a file-tool source by its exact canonical storage key."""
    storage = _root_agent_tools().get_storage_backend()
    storage_key, normalized, is_enterprise = _tool_storage_key(agent_id, rel_path, tenant_id)
    exists = bool(normalized) and await storage.is_file(storage_key)
    return _ResolvedStorageSource(storage_key, normalized, is_enterprise, exists)


async def _resolve_storage_source_path(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
) -> _ResolvedStorageSource:
    """Resolve a source path before selective workspace materialization."""
    storage = _root_agent_tools().get_storage_backend()
    storage_key, normalized, is_enterprise = _tool_storage_key(agent_id, rel_path, tenant_id)
    if normalized and await storage.is_file(storage_key):
        return _ResolvedStorageSource(storage_key, normalized, is_enterprise, True)

    requested = PurePosixPath(normalized)
    if not normalized or not requested.name:
        return _ResolvedStorageSource(storage_key, normalized, is_enterprise, False)

    parent_virtual = requested.parent.as_posix()
    if parent_virtual == ".":
        parent_virtual = ""
    parent_key, _, parent_is_enterprise = _tool_storage_key(agent_id, parent_virtual, tenant_id)
    if not await storage.is_dir(parent_key):
        return _ResolvedStorageSource(storage_key, normalized, is_enterprise, False)

    wanted = _collapse_filename_for_match(requested.name)
    matches = [
        entry
        for entry in await storage.list_dir(parent_key)
        if not entry.is_dir and _collapse_filename_for_match(entry.name) == wanted
    ]
    candidate_paths = tuple(f"{parent_virtual}/{entry.name}" if parent_virtual else entry.name for entry in matches)
    if len(matches) == 1:
        return _ResolvedStorageSource(matches[0].key, candidate_paths[0], parent_is_enterprise, True)
    return _ResolvedStorageSource(
        storage_key,
        normalized,
        is_enterprise,
        False,
        ambiguous_candidates=candidate_paths,
    )


def _storage_source_error(rel_path: str, resolved: _ResolvedStorageSource) -> str:
    if resolved.ambiguous_candidates:
        candidates = "\n".join(f"- {path}" for path in resolved.ambiguous_candidates)
        return (
            f"File path is ambiguous after normalization: {rel_path}\n"
            f"Use one exact path from these candidates:\n{candidates}"
        )
    return f"File not found: {rel_path}"


def _exact_storage_source_error(resolved: _ResolvedStorageSource) -> str:
    requested = resolved.virtual_path or "root"
    return (
        "File not found.\n"
        f"Requested path: {requested}\n"
        "The requested path was not modified. "
        "Use list_files and copy an exact returned path."
    )


def _display_size(size_bytes: int) -> str:
    return f"{size_bytes}B" if size_bytes < 1024 else f"{size_bytes / 1024:.1f}KB"


async def _storage_list_dir(agent_id: uuid.UUID, rel_path: str, tenant_id: str | None = None) -> str:
    storage = _root_agent_tools().get_storage_backend()
    storage_key, normalized, _ = _tool_storage_key(agent_id, rel_path, tenant_id)

    try:
        exists = await storage.exists(storage_key)
        is_dir = await storage.is_dir(storage_key)
    except Exception as exc:
        return (
            "Directory listing failed.\n"
            "Stage: storage_lookup\n"
            f"Requested path: {normalized or 'root'}\n"
            f"Reason: {type(exc).__name__}: {str(exc)[:200]}"
        )
    if exists and not is_dir:
        return f"Path is not a directory: {rel_path}"
    if not exists and not is_dir and normalized:
        return f"Directory not found: {rel_path or '/'}"

    items: list[str] = []
    dir_count = 0
    file_count = 0
    if not normalized and tenant_id:
        items.append("  📁 enterprise_info/ (shared company info)")
        dir_count += 1

    try:
        entries = await storage.list_dir(storage_key) if exists or is_dir else []
    except Exception as exc:
        return (
            "Directory listing failed.\n"
            "Stage: storage_list\n"
            f"Requested path: {normalized or 'root'}\n"
            f"Reason: {type(exc).__name__}: {str(exc)[:200]}"
        )
    for entry in entries:
        if entry.name.startswith("."):
            continue
        display_path = f"{normalized.rstrip('/')}/{entry.name}" if normalized else entry.name
        if entry.is_dir:
            dir_count += 1
            try:
                child_count = len([c for c in await storage.list_dir(entry.key) if not c.name.startswith(".")])
                child_summary = f"{child_count} items"
            except Exception as exc:
                child_summary = f"item count unavailable: {type(exc).__name__}: {str(exc)[:120]}"
            items.append(f"  📁 {display_path}/ ({child_summary})")
        else:
            file_count += 1
            items.append(f"  📄 {display_path} ({_display_size(entry.size)})")

    if not items:
        return f"📂 {rel_path or 'root'}: Empty directory (0 files, 0 folders)"
    header = f"📂 {rel_path or 'root'}: {dir_count} folder(s), {file_count} file(s)\n"
    return header + "\n".join(items)


async def _storage_read_file(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: str | None = None,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    storage = _root_agent_tools().get_storage_backend()
    try:
        resolved = await _resolve_exact_storage_source_path(agent_id, rel_path, tenant_id)
    except Exception as exc:
        return (
            "File read failed.\n"
            "Stage: storage_lookup\n"
            f"Requested path: {rel_path}\n"
            f"Reason: {type(exc).__name__}: {str(exc)[:200]}"
        )
    if not resolved.virtual_path:
        return _exact_storage_source_error(resolved)
    if not resolved.exists:
        return _exact_storage_source_error(resolved)
    try:
        start = max(0, offset)
        line_range = await storage.read_text_lines(
            resolved.storage_key,
            offset=start,
            limit=max(0, limit),
            encoding="utf-8",
            errors="replace",
        )
        total_lines = line_range.total_lines
        end = min(total_lines, start + max(0, limit))
        if start >= total_lines and total_lines > 0:
            return f"Offset {offset} exceeds file length ({total_lines} lines total)"
        output = "\n".join(f"{i + 1:6}\t{line}" for i, line in enumerate(line_range.lines, start=start))
        if total_lines > end:
            output += f"\n\n... [{total_lines - end} more lines not shown, lines {end + 1}-{total_lines}]"
        header = f"📄 {resolved.virtual_path} (lines {start + 1 if total_lines else 0}-{end} of {total_lines})\n"
        return header + output
    except Exception as exc:
        return (
            "File read failed.\n"
            "Stage: storage_read\n"
            f"Requested path: {resolved.virtual_path}\n"
            f"Reason: {type(exc).__name__}: {str(exc)[:200]}"
        )


async def _storage_walk_files(storage, root_key: str) -> list:
    out = []
    for entry in await storage.list_dir(root_key):
        if entry.name.startswith("."):
            continue
        out.append(entry)
        if entry.is_dir:
            out.extend(await _storage_walk_files(storage, entry.key))
    return out


def _relative_storage_display(entry_key: str, base_key: str, display_base: str) -> str:
    rel = entry_key.removeprefix(base_key.rstrip("/") + "/")
    return f"{display_base.rstrip('/')}/{rel}".strip("/") if display_base else rel


async def _storage_search_files(
    agent_id: uuid.UUID,
    pattern: str,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
    tenant_id: str | None = None,
) -> str:
    storage = _root_agent_tools().get_storage_backend()
    rel_path = "" if path in ("", ".") else path
    base_key, normalized, _ = _tool_storage_key(agent_id, rel_path, tenant_id)
    if not await storage.is_dir(base_key) and normalized:
        return f"Directory not found: {path}"
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    results: list[str] = []
    total_matches = 0
    files_searched = 0
    entries = await _storage_walk_files(storage, base_key) if await storage.is_dir(base_key) else []
    for entry in entries:
        if entry.is_dir:
            continue
        rel_display = _relative_storage_display(entry.key, base_key, normalized)
        if not fnmatch.fnmatch(Path(rel_display).name, file_pattern) and not fnmatch.fnmatch(rel_display, file_pattern):
            continue
        if Path(rel_display).suffix.lower() in {
            ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz",
        }:
            continue
        files_searched += 1
        try:
            content = await storage.read_text(entry.key, encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                results.append(f"{rel_display}:{i}: {line.strip()[:100]}")
                total_matches += 1
                if len(results) >= 50:
                    break
        if len(results) >= 50:
            break
    if not results:
        return f"No matches found for pattern '{pattern}' in {files_searched} file(s)"
    truncated = total_matches > len(results)
    truncation_note = f" (showing first {len(results)} of {total_matches}+ — refine pattern or path for more)" if truncated else ""
    return (
        f"🔍 Found {total_matches}+ match(es) in {files_searched} file(s) for pattern '{pattern}'{truncation_note}:\n"
        + "\n".join(results)
    )


async def _storage_find_files(
    agent_id: uuid.UUID,
    pattern: str,
    path: str = ".",
    tenant_id: str | None = None,
) -> str:
    storage = _root_agent_tools().get_storage_backend()
    rel_path = "" if path in ("", ".") else path
    base_key, normalized, _ = _tool_storage_key(agent_id, rel_path, tenant_id)
    if not await storage.is_dir(base_key) and normalized:
        return f"Directory not found: {path}"
    entries = await _storage_walk_files(storage, base_key) if await storage.is_dir(base_key) else []
    matches = []
    for entry in entries:
        rel_display = _relative_storage_display(entry.key, base_key, normalized)
        if fnmatch.fnmatch(rel_display, pattern) or fnmatch.fnmatch(Path(rel_display).name, pattern):
            matches.append((entry, rel_display))
    if not matches:
        return f"No files matching pattern: {pattern}"
    results = []
    dir_count = 0
    file_count = 0
    for entry, rel_display in matches[:100]:
        if entry.is_dir:
            dir_count += 1
            results.append(f"📁 {rel_display}/")
        else:
            file_count += 1
            results.append(f"📄 {rel_display} ({_display_size(entry.size)})")
    return (
        f"📂 Found {len(matches)} item(s) ({dir_count} dirs, {file_count} files) matching '{pattern}':\n"
        + "\n".join(results)
    )
