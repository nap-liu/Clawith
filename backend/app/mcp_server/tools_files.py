"""MCP file tools — generic read/write for agent workspace files."""
from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import Context

from app.api.files import CREATOR_ONLY_FILES, _agent_base_dir, _visible_path
from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, needs_confirm, resolve_manageable_agent
from app.mcp_server.auth import resolve_pat_context
from app.mcp_server.tools import _resolve_visible_agent
from app.services.focus_service import is_focus_file_path
from app.services.workspace_collaboration import write_workspace_file

_UNAUTH = "❌ 未鉴权：请在 MCP 客户端配置 Authorization: Bearer <clw_...> 令牌。"
_MAX_READ_BYTES = 100 * 1024  # 100 KB cap for inline display


# ── F1: read_agent_file ──


async def read_agent_file_impl(ctx, agent: str, path: str) -> str:
    """Read a file from an agent's workspace. Read scope is sufficient."""
    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return _UNAUTH

        ag = await _resolve_visible_agent(db, pc.user, agent)
        if ag is None:
            return "❌ 找不到该 agent，或你无权访问。"

        filename = Path(path).name
        is_creator = ag.creator_id == pc.user.id or pc.user.role == "platform_admin"

        # secrets.md is creator-only
        if filename in CREATOR_ONLY_FILES and not is_creator:
            return f"❌ 拒绝访问：{filename} 仅 agent 创建者或平台管理员可读。"

        # Focus files live in the system database, not in workspace files
        if is_focus_file_path(path):
            return "❌ Focus 不在文件里：Focus 存储在系统数据库中，请使用 Focus API 查询。"

        try:
            target, _rel_root, _is_enterprise = _visible_path(ag.id, path, pc.tenant_id)
        except Exception:
            return f"❌ 路径无效：{path}"

        if not target.exists() or not target.is_file():
            return f"❌ 文件不存在：{path}"

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return f"❌ 读取失败：{exc}"

        truncated = False
        if len(raw) > _MAX_READ_BYTES:
            raw = raw[:_MAX_READ_BYTES]
            truncated = True

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"[二进制文件：{target.name}，{target.stat().st_size} bytes，无法以文本显示]"

        header = f"# {path}\n"
        suffix = f"\n\n[⚠ 文件过大，仅显示前 {_MAX_READ_BYTES // 1024} KB，完整内容请分段读取]" if truncated else ""
        return header + content + suffix


@mcp.tool()
async def read_agent_file(ctx: Context, agent: str, path: str) -> str:  # noqa: D401
    """Read any workspace file for an agent (read scope sufficient).

    agent: id or name. path: workspace-relative path (e.g. soul.md,
    memory/notes.md, HEARTBEAT.md, skills/foo/bar.md, etc.).
    Returns the file content prefixed with a path header. Large files are
    capped at ~100 KB with a note. Respects creator-only (secrets.md) and
    focus-file gating."""
    return await read_agent_file_impl(ctx, agent=agent, path=path)


# ── F2: write_agent_file ──


async def write_agent_file_impl(
    ctx, agent: str, path: str, content: str, confirm: bool = False
) -> str:
    """Write (create or overwrite) a workspace file. Overwrite requires confirm=True."""
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err

        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        filename = Path(path).name
        is_creator = ag.creator_id == pc.user.id or pc.user.role == "platform_admin"

        # secrets.md is creator-only
        if filename in CREATOR_ONLY_FILES and not is_creator:
            return f"❌ 拒绝访问：{filename} 仅 agent 创建者或平台管理员可写。"

        # enterprise_info is admin-only
        if path.startswith("enterprise_info"):
            if pc.user.role not in ("platform_admin", "org_admin"):
                return "❌ 企业知识库仅管理员可编辑（需要 platform_admin 或 org_admin 角色）。"
            if path.strip("/") == "enterprise_info":
                return "❌ 无法覆盖 enterprise_info 根目录。"

        # Focus files are managed via Focus API, not workspace writes
        if is_focus_file_path(path):
            return "❌ Focus 不在文件里：Focus 存储在系统数据库中，请使用 Focus API 管理。"

        try:
            target, _rel_root, _is_enterprise = _visible_path(ag.id, path, pc.tenant_id)
        except Exception:
            return f"❌ 路径无效：{path}"

        # Capture BEFORE content if the file already exists (overwrite scenario)
        before_content: str | None = None
        if target.exists() and target.is_file():
            try:
                before_content = target.read_text(encoding="utf-8")
            except OSError:
                before_content = None

        # Overwrite requires confirmation; new files do not
        if before_content is not None:
            guidance = needs_confirm(
                confirm,
                f"将覆盖「{ag.name}」的文件 {path}（{len(before_content)} 字符将被替换）",
            )
            if guidance is not None:
                return guidance

        result = await write_workspace_file(
            db,
            agent_id=ag.id,
            base_dir=_agent_base_dir(ag.id),
            path=path,
            content=content,
            actor_type="user",
            actor_id=pc.user.id,
            operation="write",
            session_id=None,
            enforce_human_lock=False,
            merge_user_autosave=False,
        )
        if not result.ok:
            return f"❌ {result.message}"

        await db.commit()

    # Build rollback hint with embedded prior content
    if before_content is None:
        rollback_hint = "↩ 回滚：此为新建文件，回滚 = 用 write_agent_file 删除（或写入空内容）。"
        before_display = "(原本不存在)"
    else:
        before_display = before_content
        rollback_hint = (
            f"↩ 回滚：用 write_agent_file 把以下旧内容写回（加 confirm=True）："
            f"\n----- BEFORE -----\n{before_display}\n-----------------"
        )

    return (
        f"✅ 已写入「{ag.name}」{path}（revision={result.revision_id}）。\n"
        f"{rollback_hint}"
    )


@mcp.tool()
async def write_agent_file(  # noqa: D401
    ctx: Context, agent: str, path: str, content: str, confirm: bool = False
) -> str:
    """Create or overwrite a workspace file for an agent (write scope + manage).

    agent: id or name. path: workspace-relative path (e.g. soul.md,
    memory/notes.md, skills/foo/bar.md, etc.). content: full file content.
    confirm: MUST be True when overwriting an existing file — first call without
    confirm shows a preview and does NOT write anything. New files need no confirm.

    Returns prior content embedded in the response for easy rollback.
    Respects: creator-only (secrets.md), admin-only (enterprise_info/*),
    and focus-file gating."""
    return await write_agent_file_impl(ctx, agent=agent, path=path, content=content, confirm=confirm)
