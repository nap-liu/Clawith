from __future__ import annotations

from pathlib import Path
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User as UserModel
from app.services.agent_tools_file_support import _get_agent_tenant_id
from app.services.agent_tools_sql_support import (
    DEFAULT_SQL_MAX_ROWS,
    _clamp_sql_max_rows,
    _resolve_sql_max_bytes,
    _sql_execute_mysql,
    _sql_execute_postgres,
    _sql_execute_sqlite,
)

SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret", "atlassian_api_key"}


def _decrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    """Decrypt sensitive fields in config dict.

    When config_schema is provided, also decrypts fields with type='password'
    (e.g. smithery_api_key) that are not in the hardcoded SENSITIVE_FIELD_KEYS.
    """
    if not config:
        return config

    from app.core.security import decrypt_data
    from app.config import get_settings

    settings = get_settings()
    result = dict(config)

    # Build the set of sensitive keys: hardcoded + schema-derived
    sensitive_keys = set(SENSITIVE_FIELD_KEYS)
    if config_schema:
        for field in config_schema.get("fields", []):
            if field.get("type") == "password":
                key = field.get("key", "")
                if key:
                    sensitive_keys.add(key)

    for key in sensitive_keys:
        if key in result and result[key]:
            value = result[key]
            if isinstance(value, str) and value:
                try:
                    result[key] = decrypt_data(value, settings.SECRET_KEY)
                except Exception:
                    # If decryption fails, assume it's plaintext
                    pass

    return result


async def _get_email_config(agent_id: uuid.UUID) -> dict:
    """Retrieve per-agent email config from the send_email tool's AgentTool config."""
    from app.models.tool import Tool, AgentTool

    async with async_session() as db:
        # Find the send_email tool
        r = await db.execute(select(Tool).where(Tool.name == "send_email"))
        tool = r.scalar_one_or_none()
        if not tool:
            return {}

        # Get per-agent config
        at_r = await db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == agent_id,
                AgentTool.tool_id == tool.id,
            )
        )
        at = at_r.scalar_one_or_none()
        agent_config = (at.config or {}) if at else {}
        merged = {**(tool.config or {}), **agent_config}
        return _decrypt_sensitive_fields(merged, tool.config_schema)


async def _handle_email_tool(tool_name: str, agent_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Dispatch email tool calls to the email_service module."""
    from app.services.email_service import send_email, read_emails, reply_email

    config = await _get_email_config(agent_id)
    if not config.get("email_address") or not config.get("auth_code"):
        return (
            "❌ Email not configured for this agent.\n\n"
            "Please go to Agent → Tools → Send Email → Config to set up your email:\n"
            "1. Select your email provider\n"
            "2. Enter your email address\n"
            "3. Enter your authorization code (not your login password)"
        )

    try:
        if tool_name == "send_email":
            return await send_email(
                config=config,
                to=arguments.get("to", ""),
                subject=arguments.get("subject", ""),
                body=arguments.get("body", ""),
                cc=arguments.get("cc"),
                attachments=arguments.get("attachments"),
                workspace_path=ws,
            )
        elif tool_name == "read_emails":
            return await read_emails(
                config=config,
                limit=arguments.get("limit", 10),
                search=arguments.get("search"),
                folder=arguments.get("folder", "INBOX"),
            )
        elif tool_name == "reply_email":
            return await reply_email(
                config=config,
                message_id=arguments.get("message_id", ""),
                body=arguments.get("body", ""),
            )
        else:
            return f"❌ Unknown email tool: {tool_name}"
    except Exception as e:
        return f"❌ Email tool error: {str(e)[:200]}"


async def _search_clawhub(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search the ClawHub skill registry."""
    query = arguments.get("query", "").strip()
    if not query:
        return "Missing required argument 'query'"

    # Resolve tenant ClawHub API key
    from app.api.skills import _clawhub_search_endpoint, _fetch_clawhub_json, _get_clawhub_key

    tenant_id = await _get_agent_tenant_id(agent_id)
    api_key = await _get_clawhub_key(tenant_id)

    try:
        data, _ = await _fetch_clawhub_json(
            _clawhub_search_endpoint,
            api_key=api_key,
            params={"q": query},
        )
    except Exception as e:
        return f"❌ ClawHub search error: {str(e)[:200]}"

    results = data.get("results", [])
    if not results:
        return f"No skills found matching '{query}'."

    lines = [f"Found {len(results)} skill(s) matching '{query}':\n"]
    for r in results:
        name = r.get("displayName") or r.get("slug", "?")
        slug = r.get("slug", "")
        summary = (r.get("summary") or "")[:120]
        updated = ""
        if r.get("updatedAt"):
            from datetime import datetime

            try:
                dt = datetime.fromtimestamp(r["updatedAt"] / 1000)
                updated = f" | Updated: {dt.strftime('%Y-%m-%d')}"
            except Exception:
                pass
        lines.append(f"• **{name}** (`{slug}`){updated}")
        if summary:
            lines.append(f"  {summary}")
    lines.append('\nTo install a skill, use: install_skill(source="<slug>")')
    return "\n".join(lines)


async def _install_skill(agent_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Install a skill from ClawHub slug or GitHub URL into the agent's workspace."""
    source = arguments.get("source", "").strip()
    if not source:
        return "❌ Missing required argument 'source'. Provide a ClawHub slug (e.g. 'market-research') or a GitHub URL."

    is_url = source.startswith("http://") or source.startswith("https://")
    base = ws  # agent workspace dir (skills/ lives under workspace/)

    try:
        if is_url:
            # ── GitHub URL path ──
            from app.api.skills import _parse_github_url, _fetch_github_directory, _get_github_token

            parsed = _parse_github_url(source)
            if not parsed:
                return "❌ Invalid GitHub URL. Expected format: https://github.com/{owner}/{repo} or https://github.com/{owner}/{repo}/tree/{branch}/{path}"

            owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]
            tenant_id = await _get_agent_tenant_id(agent_id)
            token = await _get_github_token(tenant_id)
            files = await _fetch_github_directory(owner, repo, path, branch, token)
            if not files:
                return "❌ No files found at the specified URL."

            folder_name = path.rstrip("/").split("/")[-1] if path else repo
        else:
            # ── ClawHub slug path ──
            slug = source
            from app.api.skills import _fetch_clawhub_skill_archive, _fetch_clawhub_skill_meta, _get_clawhub_key

            # 1. Fetch metadata from ClawHub (with tenant API key)
            tenant_id = await _get_agent_tenant_id(agent_id)
            api_key = await _get_clawhub_key(tenant_id)
            try:
                _meta, meta_base = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
            except Exception as e:
                return f"Failed to connect to ClawHub: {str(e)[:200]}"

            # 2. Fetch files from the ClawHub archive
            files, _ = await _fetch_clawhub_skill_archive(slug, api_key=api_key, preferred_base=meta_base)
            if not files:
                return f"❌ No files found for skill '{slug}' in the ClawHub archive."

            folder_name = slug

        # 3. Write files to agent workspace
        skill_dir = base / "skills" / folder_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        written = []
        for f in files:
            file_path = (skill_dir / f["path"]).resolve()
            if not str(file_path).startswith(str(base.resolve())):
                continue  # safety: skip path traversal
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(f["content"], encoding="utf-8")
            written.append(f["path"])

        return f"✅ Skill '{folder_name}' installed successfully ({len(written)} files written to skills/{folder_name}/).\n\nFiles: {', '.join(written)}"

    except Exception as e:
        return f"❌ Install failed: {str(e)[:300]}"


async def _search_skill_market(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search the first-party Skill market within the Agent's tenant scope."""
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "Missing required argument 'query'"

    from app.services.skill_market import list_market_skills

    try:
        async with async_session() as db:
            agent = await db.get(AgentModel, agent_id)
            if not agent:
                return "❌ Agent not found"
            results = await list_market_skills(db, tenant_id=agent.tenant_id, query=query, limit=5)
    except Exception as exc:
        return f"❌ Skill market search failed: {str(exc)[:240]}"

    if not results:
        return f"No market Skills found matching '{query}'."

    lines = [f"Found {len(results)} market Skill(s) matching '{query}':\n"]
    for item in results:
        scope = "public" if item["visibility"] == "public" else "company"
        lines.append(
            f"• **{item['name']}** (`{item['id']}`) — v{item['version']} · "
            f"{item['downloads']} Agent installs · {scope} · by {item['publisher_name']}"
        )
        if item.get("description"):
            lines.append(f"  {item['description'][:180]}")
    lines.append('\nTo install one, obtain user confirmation and call install_skill_from_market(skill_id="<id>").')
    return "\n".join(lines)


async def _market_tool_actor(db, agent_id: uuid.UUID, user_id: uuid.UUID | None):
    """Resolve and enforce the human manage authority behind a mutating market tool."""
    if not user_id:
        return None, None, "Permission denied: a confirmed human actor is required"

    from app.core.permissions import check_agent_access

    actor = await db.get(UserModel, user_id)
    if not actor:
        return None, None, "Permission denied: user not found"
    try:
        agent, access_level = await check_agent_access(db, actor, agent_id)
    except Exception as exc:
        return None, None, f"Permission denied: {str(exc)[:160]}"
    if access_level != "manage":
        return None, None, "Permission denied: Agent manage access required"
    return actor, agent, None


async def _market_install_actor(
    db,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str | None,
    turn_anchor_id: uuid.UUID | None,
):
    """Resolve the real human speaking in the current conversation turn."""
    if not session_id or not turn_anchor_id:
        return None, None, "Permission denied: a current human conversation is required"

    try:
        conversation_id = uuid.UUID(str(session_id))
        anchor_id = uuid.UUID(str(turn_anchor_id))
    except (TypeError, ValueError, AttributeError):
        return None, None, "Permission denied: invalid conversation context"

    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == conversation_id,
            ChatSession.agent_id == agent_id,
        )
    )
    if not session or session.source_channel in {"agent", "trigger", "subagent", "project"}:
        return None, None, "Permission denied: a current human conversation is required"

    sender_user_id = await db.scalar(
        select(ChatMessage.sender_user_id).where(
            ChatMessage.id == anchor_id,
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == str(conversation_id),
            ChatMessage.role == "user",
        )
    )
    if sender_user_id is None or sender_user_id != user_id:
        return None, None, "Permission denied: current conversation participant mismatch"

    return await _market_tool_actor(db, agent_id, sender_user_id)


async def _install_skill_from_market(
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    arguments: dict,
    *,
    session_id: str | None = None,
    turn_anchor_id: uuid.UUID | None = None,
) -> str:
    raw_skill_id = str(arguments.get("skill_id") or "").strip()
    try:
        skill_id = uuid.UUID(raw_skill_id)
    except ValueError:
        return "❌ skill_id must be a valid UUID returned by search_skill_market"

    from app.services.skill_market import install_market_skill

    try:
        async with async_session() as db:
            actor, agent, error = await _market_install_actor(
                db,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
            )
            if error:
                return error
            result = await install_market_skill(
                db,
                agent=agent,
                skill_id=skill_id,
                actor_user_id=actor.id,
                actor_agent_id=agent_id,
            )
        return (
            f"✅ Installed market Skill '{result['skill_name']}' v{result['installed_version']} "
            f"to skills/{result['folder_name']} ({result['files_written']} files)."
        )
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        return f"❌ Market Skill installation failed: {str(detail)[:260]}"


async def _publish_skill_to_market(
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    arguments: dict,
) -> str:
    path = str(arguments.get("path") or "").strip()
    name = str(arguments.get("name") or "").strip()
    description = str(arguments.get("description") or "").strip()
    category = str(arguments.get("category") or "general").strip()
    visibility = str(arguments.get("visibility") or "tenant").strip()
    if not path or not name:
        return "❌ path and name are required"

    from app.services.skill_market import publish_agent_skill

    try:
        async with async_session() as db:
            actor, agent, error = await _market_tool_actor(db, agent_id, user_id)
            if error:
                return error
            skill = await publish_agent_skill(
                db,
                agent=agent,
                actor=actor,
                path=path,
                name=name,
                description=description,
                category=category,
                visibility=visibility,
            )
            await db.commit()
        scope = "public market" if skill.visibility == "public" else "company market"
        return f"✅ Published '{skill.name}' v{skill.version} to the {scope} (Skill ID: {skill.id})."
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        return f"❌ Skill publication failed: {str(detail)[:260]}"


async def _withdraw_skill_from_market(
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    arguments: dict,
) -> str:
    raw_skill_id = str(arguments.get("skill_id") or "").strip()
    try:
        skill_id = uuid.UUID(raw_skill_id)
    except ValueError:
        return "❌ skill_id must be a valid UUID returned after publication or search"

    from app.services.skill_market import withdraw_agent_skill

    try:
        async with async_session() as db:
            _actor, agent, error = await _market_tool_actor(db, agent_id, user_id)
            if error:
                return error
            skill = await withdraw_agent_skill(db, skill_id=skill_id, agent=agent)
            await db.commit()
        return (
            f"✅ Took '{skill.name}' offline from the Skill market. "
            "The source Skill and existing installs are unaffected, and it may be published again."
        )
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        return f"❌ Taking the Skill offline failed: {str(detail)[:260]}"


async def _sql_execute(arguments: dict) -> str:
    """Execute SQL on any database via connection URI.

    Parses and clamps max_rows (default DEFAULT_SQL_MAX_ROWS, hard ceiling HARD_SQL_MAX_ROWS)
    and resolves max_bytes (env-overridable, hard ceiling HARD_SQL_MAX_BYTES), then passes both
    through to the DB-specific backend so that streaming and dual-limit enforcement are active
    for every engine.
    """
    import asyncio

    connection_string = arguments.get("connection_string", "").strip()
    sql = arguments.get("sql", "").strip()
    timeout = min(int(arguments.get("timeout", 30)), 120)
    max_rows = _clamp_sql_max_rows(arguments.get("max_rows", DEFAULT_SQL_MAX_ROWS))
    max_bytes = _resolve_sql_max_bytes()

    if not connection_string:
        return "❌ Missing required argument 'connection_string'"
    if not sql:
        return "❌ Missing required argument 'sql'"

    uri_lower = connection_string.lower()
    try:
        if uri_lower.startswith("sqlite"):
            return await asyncio.wait_for(
                _sql_execute_sqlite(connection_string, sql, max_rows, max_bytes), timeout=timeout
            )
        elif uri_lower.startswith("mysql"):
            return await asyncio.wait_for(
                _sql_execute_mysql(connection_string, sql, max_rows, max_bytes), timeout=timeout
            )
        elif uri_lower.startswith("postgresql") or uri_lower.startswith("postgres"):
            return await asyncio.wait_for(
                _sql_execute_postgres(connection_string, sql, max_rows, max_bytes), timeout=timeout
            )
        else:
            return "❌ Unsupported database type. Supported: mysql://, postgresql://, sqlite:///"
    except asyncio.TimeoutError:
        return f"❌ Query timed out after {timeout}s"
    except Exception as e:
        return f"❌ Database error: {type(e).__name__}: {str(e)[:500]}"


__all__ = [
    "SENSITIVE_FIELD_KEYS",
    "_decrypt_sensitive_fields",
    "_get_email_config",
    "_handle_email_tool",
    "_search_clawhub",
    "_install_skill",
    "_search_skill_market",
    "_market_tool_actor",
    "_market_install_actor",
    "_install_skill_from_market",
    "_publish_skill_to_market",
    "_withdraw_skill_from_market",
    "_sql_execute",
]
