from __future__ import annotations

import re
from pathlib import Path

from app.services.agent_tools_file_support import (
    WORKSPACE_ROOT,
    _is_enterprise_info_path,
    _resolve_tool_source_path,
)


def _list_files(ws: Path, rel_path: str, tenant_id: str | None = None) -> str:
    # Handle enterprise_info/ as shared directory (tenant-scoped)
    if rel_path and rel_path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        # Remap: enterprise_info/... → enterprise_info_{tenant_id}/...
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        target = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not str(target).startswith(str(enterprise_root)):
            return "Access denied for this path"
    else:
        target = (ws / rel_path) if rel_path else ws
        target = target.resolve()
        if not str(target).startswith(str(ws.resolve())):
            return "Access denied for this path"

    if not target.exists():
        return f"Directory not found: {rel_path or '/'}"

    items = []
    # If listing root, also show enterprise_info entry
    if not rel_path:
        if tenant_id:
            enterprise_dir = WORKSPACE_ROOT / f"enterprise_info_{tenant_id}"
        else:
            enterprise_dir = WORKSPACE_ROOT / "enterprise_info"
        if enterprise_dir.exists():
            items.append("  📁 enterprise_info/ (shared company info)")

    dir_count = 0
    file_count = 0
    for p in sorted(target.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            dir_count += 1
            child_count = len([c for c in p.iterdir() if not c.name.startswith(".")])
            items.append(f"  📁 {p.name}/ ({child_count} items)")
        elif p.is_file():
            file_count += 1
            size_bytes = p.stat().st_size
            if size_bytes < 1024:
                size_str = f"{size_bytes}B"
            else:
                size_str = f"{size_bytes / 1024:.1f}KB"
            items.append(f"  📄 {p.name} ({size_str})")

    if not items:
        return f"📂 {rel_path or 'root'}: Empty directory (0 files, 0 folders)"

    header = f"📂 {rel_path or 'root'}: {dir_count} folder(s), {file_count} file(s)\n"
    return header + "\n".join(items)


def _read_file(ws: Path, rel_path: str, tenant_id: str | None = None, offset: int = 0, limit: int = 2000) -> str:
    """Read file contents with optional line range support.

    Args:
        ws: Workspace root path
        rel_path: Relative file path
        tenant_id: Optional tenant ID for enterprise_info
        offset: Starting line number (0-indexed)
        limit: Maximum number of lines to read

    Returns:
        File content with line numbers, or error message
    """
    try:
        file_path = _resolve_tool_source_path(ws, rel_path, tenant_id=tenant_id)
    except ValueError as exc:
        return str(exc)

    if not file_path.exists():
        return f"File not found: {rel_path}"

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total_lines = len(lines)

        # Apply offset and limit
        start = max(0, offset)
        end = min(total_lines, start + limit)

        if start >= total_lines:
            return f"Offset {offset} exceeds file length ({total_lines} lines total)"

        selected_lines = lines[start:end]

        # Format with line numbers (like cat -n)
        result = []
        for i, line in enumerate(selected_lines, start=start):
            result.append(f"{i + 1:6}\t{line}")

        output = "\n".join(result)

        # Add pagination info if file is larger than what we show
        if total_lines > end:
            output += f"\n\n... [{total_lines - end} more lines not shown, lines {end + 1}-{total_lines}]"

        # Add header with file info
        header = f"📄 {rel_path} (lines {start + 1}-{end} of {total_lines})\n"
        return header + output
    except Exception as e:
        return f"Read failed: {e}"


def _write_file(ws: Path, rel_path: str, content: str, tenant_id: str | None = None) -> str:
    if rel_path.strip("/") == "tasks.json":
        return "tasks.json is a legacy read-only snapshot. Use the task APIs/UI to manage tasks."
    if _is_enterprise_info_path(rel_path):
        return "enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."

    if rel_path and rel_path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        sub = rel_path[len("enterprise_info"):].lstrip("/")
        if not sub:
            return "Write failed: please provide a file path under enterprise_info/, e.g. enterprise_info/knowledge_base/report.md"
        file_path = (enterprise_root / sub).resolve()
        if not str(file_path).startswith(str(enterprise_root)):
            return "Access denied for this path"
    else:
        file_path = (ws / rel_path).resolve()
        if not str(file_path).startswith(str(ws.resolve())):
            return "Access denied for this path"

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"✅ Written to {rel_path} ({len(content)} chars)"
    except Exception as e:
        return f"Write failed: {e}"


def _delete_file(ws: Path, rel_path: str) -> str:
    protected = {"tasks.json", "soul.md"}
    if rel_path.strip("/") in protected:
        return f"{rel_path} cannot be deleted (protected)"
    if _is_enterprise_info_path(rel_path):
        return "enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."

    file_path = (ws / rel_path).resolve()
    if not str(file_path).startswith(str(ws.resolve())):
        return "Access denied for this path"
    if not file_path.exists():
        return f"File not found: {rel_path}"

    try:
        if file_path.is_dir():
            import shutil

            shutil.rmtree(file_path)
            return f"✅ Deleted directory {rel_path}"
        else:
            file_path.unlink()
            return f"✅ Deleted {rel_path}"
    except Exception as e:
        return f"Delete failed: {e}"


def _edit_file(
    ws: Path, rel_path: str, old_string: str, new_string: str, replace_all: bool = False, tenant_id: str | None = None
) -> str:
    """Perform surgical string replacement in a file.

    Args:
        ws: Workspace root path
        rel_path: Relative file path
        old_string: Exact text to find and replace
        new_string: Replacement text
        replace_all: Replace all occurrences if True
        tenant_id: Optional tenant ID for enterprise_info

    Returns:
        Success message or error
    """
    if _is_enterprise_info_path(rel_path):
        return "enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."

    # Handle enterprise_info/ as shared directory (tenant-scoped)
    if rel_path and rel_path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        file_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not str(file_path).startswith(str(enterprise_root)):
            return "Access denied for this path"
    else:
        file_path = (ws / rel_path).resolve()
        if not str(file_path).startswith(str(ws.resolve())):
            return "Access denied for this path"

    if not file_path.exists():
        return f"File not found: {rel_path}"

    if not file_path.is_file():
        return f"Not a file: {rel_path}"

    try:
        content = file_path.read_text(encoding="utf-8")

        if old_string not in content:
            return f"❌ 'old_string' not found in {rel_path}. Please check the exact text including whitespace and newlines."

        if replace_all:
            new_content = content.replace(old_string, new_string)
            count = content.count(old_string)
        else:
            # Ensure uniqueness for single replacement
            count = content.count(old_string)
            if count > 1:
                return f"❌ 'old_string' appears {count} times in {rel_path}. Use replace_all=true or provide more context to make the match unique."
            new_content = content.replace(old_string, new_string, 1)
            count = 1

        file_path.write_text(new_content, encoding="utf-8")
        return f"✅ Replaced {count} occurrence(s) in {rel_path}"
    except Exception as e:
        return f"Edit failed: {e}"


def _search_files(
    ws: Path,
    pattern: str,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
    tenant_id: str | None = None,
) -> str:
    """Search for content patterns across files using regex.

    Args:
        ws: Workspace root path
        pattern: Regex pattern to search for
        path: Directory to search in (relative to workspace root)
        file_pattern: File pattern to match (glob)
        ignore_case: Case-insensitive search
        tenant_id: Optional tenant ID for enterprise_info

    Returns:
        Matching lines with file paths and line numbers
    """
    # Handle enterprise_info/ as shared directory (tenant-scoped)
    if path and path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        sub = path[len("enterprise_info") :].lstrip("/")
        search_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not str(search_path).startswith(str(enterprise_root)):
            return "Access denied for this path"
        ws_for_relative = enterprise_root
    else:
        search_path = (ws / path).resolve() if path and path != "." else ws
        if not str(search_path).startswith(str(ws.resolve())):
            return "Access denied for this path"
        ws_for_relative = ws

    if not search_path.exists():
        return f"Directory not found: {path}"

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    results = []
    total_matches = 0
    files_searched = 0

    # Use rglob for recursive search
    for file_path in search_path.rglob(file_pattern):
        if not file_path.is_file():
            continue
        # Skip hidden files and common binary/extensions
        if file_path.name.startswith("."):
            continue
        suffix = file_path.suffix.lower()
        if suffix in {
            ".pyc",
            ".pyo",
            ".so",
            ".dll",
            ".exe",
            ".bin",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".zip",
            ".tar",
            ".gz",
        }:
            continue

        files_searched += 1
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    rel_path = file_path.relative_to(ws_for_relative)
                    # Truncate long lines
                    display_line = line.strip()[:100]
                    results.append(f"{rel_path}:{i}: {display_line}")
                    total_matches += 1
                    if len(results) >= 50:  # Limit results per query
                        break
        except Exception:
            continue

        if len(results) >= 50:
            break

    if not results:
        return f"No matches found for pattern '{pattern}' in {files_searched} file(s)"

    # Warn the LLM if results were capped so it knows to refine the search.
    truncated = total_matches > len(results)
    truncation_note = (
        f" (showing first {len(results)} of {total_matches}+ — refine pattern or path for more)" if truncated else ""
    )
    header = (
        f"🔍 Found {total_matches}+ match(es) in {files_searched} file(s) for pattern '{pattern}'{truncation_note}:\n"
    )
    return header + "\n".join(results)


def _find_files(ws: Path, pattern: str, path: str = ".", tenant_id: str | None = None) -> str:
    """Find files matching glob patterns.

    Args:
        ws: Workspace root path
        pattern: Glob pattern to match files
        path: Base directory for search (relative to workspace root)
        tenant_id: Optional tenant ID for enterprise_info

    Returns:
        List of matching files with sizes
    """
    # Handle enterprise_info/ as shared directory (tenant-scoped)
    if path and path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        sub = path[len("enterprise_info") :].lstrip("/")
        search_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not str(search_path).startswith(str(enterprise_root)):
            return "Access denied for this path"
        ws_for_relative = enterprise_root
    else:
        search_path = (ws / path).resolve() if path and path != "." else ws
        if not str(search_path).startswith(str(ws.resolve())):
            return "Access denied for this path"
        ws_for_relative = ws

    if not search_path.exists():
        return f"Directory not found: {path}"

    try:
        matches = list(search_path.glob(pattern))
    except Exception as e:
        return f"Invalid glob pattern: {e}"

    if not matches:
        return f"No files matching pattern: {pattern}"

    # Sort by modification time (most recent first)
    matches.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
    results = []
    dir_count = 0
    file_count = 0

    for m in matches[:100]:  # Limit to 100 results
        rel_path = m.relative_to(ws_for_relative)
        if m.is_dir():
            dir_count += 1
            results.append(f"📁 {rel_path}/")
        else:
            file_count += 1
            try:
                size = m.stat().st_size
                size_str = f"{size // 1024}KB" if size > 1024 else f"{size}B"
                results.append(f"📄 {rel_path} ({size_str})")
            except Exception:
                results.append(f"📄 {rel_path}")

    header = f"📂 Found {len(matches)} item(s) ({dir_count} dirs, {file_count} files) matching '{pattern}':\n"
    return header + "\n".join(results)
