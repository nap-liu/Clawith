"""Skill discovery and marketplace tools extracted from agent_tools."""

import uuid
from pathlib import Path

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User as UserModel
from app.services.agent_tools_file_support import _get_agent_tenant_id
from app.services.timezone_utils import format_datetime_for_agent, get_agent_timezone


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
    timezone_name = await get_agent_timezone(agent_id)
    for r in results:
        name = r.get("displayName") or r.get("slug", "?")
        slug = r.get("slug", "")
        summary = (r.get("summary") or "")[:120]
        updated = ""
        if r.get("updatedAt"):
            from datetime import datetime, timezone

            try:
                dt = datetime.fromtimestamp(r["updatedAt"] / 1000, tz=timezone.utc)
                updated = f" | Updated: {format_datetime_for_agent(dt, timezone_name)}"
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

__all__ = [name for name in globals() if not name.startswith("__")]
