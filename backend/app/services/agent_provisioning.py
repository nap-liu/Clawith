"""Shared agent-provisioning service.

Extracted from the REST route ``create_agent`` (``app/api/agents.py``) so the
same creation flow can be reused by other entry points (e.g. the MCP
``create_agent`` tool) without duplicating ~250 lines of side-effect-laden
logic.

This is a behaviour-preserving extraction: same operations, same ordering,
same flush/commit points. The only intentional differences from the route are:

* HTTP concerns are removed — the tenant is passed in by the caller, quota
  failures (:class:`QuotaExceeded`) propagate instead of becoming HTTP 403s,
  and an invalid ``permission_scope_type`` raises :class:`ValueError` instead
  of HTTP 400.
* Serialization is left to the caller; this returns ``(agent, raw_api_key)``
  where ``raw_api_key`` is non-``None`` only for openclaw agents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from app.models.agent import Agent, AgentPermission
from app.services.access_relationships import ensure_access_granted_platform_relationships


@dataclass
class AgentProvisionInput:
    name: str
    agent_type: str = "native"
    role_description: str = ""
    bio: str | None = None
    avatar_url: str | None = None
    personality: str = ""
    boundaries: str = ""
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    permission_scope_type: str = "company"     # company | user | custom
    permission_scope_ids: list = field(default_factory=list)
    permission_access_level: str = "use"
    autonomy_policy: dict | None = None
    max_tokens_per_day: int | None = None
    max_tokens_per_month: int | None = None
    template_id: uuid.UUID | None = None
    skill_ids: list = field(default_factory=list)


async def provision_agent(db, *, creator, tenant_id, data: AgentProvisionInput) -> tuple:
    """Create an agent + all side-effects. Returns (agent, raw_api_key|None).

    raw_api_key is non-None only for openclaw agents. Raises QuotaExceeded and
    ValueError.
    """
    # Check agent creation quota
    from app.services.quota_guard import check_agent_creation_quota
    await check_agent_creation_quota(creator.id)

    # A TTL of 0 or less means the agent never expires.
    from datetime import datetime, timedelta, timezone as tz
    ttl_hours = creator.quota_agent_ttl_hours

    # Get default limits from target tenant
    max_llm_calls = 1000
    default_max_triggers = 20
    default_min_poll = 5
    default_webhook_rate = 5
    default_heartbeat_interval = 240  # model default
    tenant_default_model_id = None
    if tenant_id:
        from app.models.tenant import Tenant
        tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = tenant_result.scalar_one_or_none()
        if tenant:
            ttl_hours = tenant.default_agent_ttl_hours
            max_llm_calls = tenant.default_max_llm_calls_per_day or 1000
            default_max_triggers = tenant.default_max_triggers or 20
            default_min_poll = tenant.min_poll_interval_floor or 5
            default_webhook_rate = tenant.max_webhook_rate_ceiling or 5
            tenant_default_model_id = tenant.default_model_id
            # Enforce heartbeat floor: new agents must respect company minimum
            if tenant.min_heartbeat_interval_minutes and tenant.min_heartbeat_interval_minutes > default_heartbeat_interval:
                default_heartbeat_interval = tenant.min_heartbeat_interval_minutes

    # If the caller didn't pick a model, fall back to the tenant's default.
    effective_primary_model_id = data.primary_model_id or tenant_default_model_id
    expires_at = datetime.now(tz.utc) + timedelta(hours=ttl_hours) if ttl_hours and ttl_hours > 0 else None

    agent = Agent(
        name=data.name,
        role_description=data.role_description,
        bio=data.bio,
        avatar_url=data.avatar_url,
        creator_id=creator.id,
        tenant_id=tenant_id,
        agent_type=data.agent_type or "native",
        primary_model_id=effective_primary_model_id,
        fallback_model_id=data.fallback_model_id,
        max_tokens_per_day=data.max_tokens_per_day,
        max_tokens_per_month=data.max_tokens_per_month,
        template_id=data.template_id,
        status="creating" if data.agent_type != "openclaw" else "idle",
        expires_at=expires_at,
        max_llm_calls_per_day=max_llm_calls,
        max_triggers=default_max_triggers,
        min_poll_interval_min=default_min_poll,
        webhook_rate_limit=default_webhook_rate,
        heartbeat_interval_minutes=default_heartbeat_interval,
    )
    if data.autonomy_policy:
        agent.autonomy_policy = data.autonomy_policy

    db.add(agent)
    await db.flush()

    # Auto-create Participant identity for the new agent
    from app.models.participant import Participant
    db.add(Participant(
        type="agent", ref_id=agent.id,
        display_name=agent.name, avatar_url=agent.avatar_url,
    ))
    await db.flush()

    # Set permissions
    access_level = data.permission_access_level if data.permission_access_level in ("use", "manage") else "use"
    if data.permission_scope_type not in ("company", "user", "custom"):
        raise ValueError("Unsupported permission_scope_type")
    if data.permission_scope_type == "company":
        agent.access_mode = "company"
        agent.company_access_level = access_level
        db.add(AgentPermission(agent_id=agent.id, scope_type="company", access_level=access_level))
    elif data.permission_scope_type == "user":
        agent.access_mode = "private"
        agent.company_access_level = access_level
        if data.permission_scope_ids:
            for scope_id in data.permission_scope_ids:
                db.add(AgentPermission(agent_id=agent.id, scope_type="user", scope_id=scope_id, access_level=access_level))
        else:
            # "仅自己" — insert creator as the only permitted user
            db.add(AgentPermission(agent_id=agent.id, scope_type="user", scope_id=creator.id, access_level="manage"))
    elif data.permission_scope_type == "custom":
        agent.access_mode = "custom"
        agent.company_access_level = access_level
        db.add(AgentPermission(agent_id=agent.id, scope_type="user", scope_id=creator.id, access_level="manage"))

    await db.flush()
    await ensure_access_granted_platform_relationships(db, agent, created_by_user_id=creator.id)

    # Seed explicit AgentTool rows from the platform default set so this agent's
    # toolset is materialized data, not a query-time is_default fallback.
    from app.models.tool import Tool, AgentTool
    from app.services.tool_enablement import default_tool_ids_to_seed

    _default_tools = (
        await db.execute(
            select(Tool).where(
                Tool.enabled == True,            # noqa: E712
                Tool.source == "builtin",
                Tool.is_default == True,          # noqa: E712
            )
        )
    ).scalars().all()
    for _tool_id in default_tool_ids_to_seed(_default_tools, existing_tool_ids=set()):
        db.add(AgentTool(agent_id=agent.id, tool_id=_tool_id, enabled=True))
    await db.flush()

    # For OpenClaw agents: skip file system and container setup, generate API key
    if agent.agent_type == "openclaw":
        import hashlib
        import secrets
        raw_key = f"oc-{secrets.token_urlsafe(32)}"
        agent.api_key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        agent.status = "idle"
        await db.commit()

        from app.services.okr_agent_hook import hook_new_agent
        if agent.tenant_id:
            await hook_new_agent(db, agent.id, agent.tenant_id)
            await db.commit()

        return agent, raw_key

    # Initialize agent file system from template
    from app.services.agent_manager import agent_manager
    await agent_manager.initialize_agent_files(
        db, agent,
        personality=data.personality,
        boundaries=data.boundaries,
    )
    from app.api.relationships import _regenerate_relationships_file
    await _regenerate_relationships_file(db, agent.id)

    # Copy selected skills + mandatory default skills into agent workspace
    from app.models.skill import Skill
    from sqlalchemy.orm import selectinload

    # Always include global default skills (mcp-installer, skill-creator,
    # complex-task-executor)
    default_result = await db.execute(
        select(Skill).where(Skill.is_default)
    )
    default_ids = {s.id for s in default_result.scalars().all()}

    # Include the template's declared default skills (e.g. trading templates
    # ship with `market-data` / `financial-calendar` in their meta.yaml).
    # Without this, the SKILL.md never reaches `<agent_dir>/skills/<folder>/`,
    # so the agent has no idea those MCP-backed skills exist and silently
    # falls back to web search.
    template_skill_ids: set = set()
    if data.template_id:
        from app.models.agent import AgentTemplate
        tpl_r = await db.execute(
            select(AgentTemplate).where(AgentTemplate.id == data.template_id)
        )
        tpl = tpl_r.scalar_one_or_none()
        folder_names = list((tpl.default_skills if tpl else None) or [])
        if folder_names:
            tpl_skills_r = await db.execute(
                select(Skill).where(Skill.folder_name.in_(folder_names))
            )
            template_skill_ids = {s.id for s in tpl_skills_r.scalars().all()}

    # Merge user-selected + global default + template-default skill IDs
    all_skill_ids = set(data.skill_ids or []) | default_ids | template_skill_ids

    if all_skill_ids:
        agent_dir = agent_manager._agent_dir(agent.id)
        skills_dir = agent_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        for sid in all_skill_ids:
            result = await db.execute(
                select(Skill).where(Skill.id == sid).options(selectinload(Skill.files))
            )
            skill = result.scalar_one_or_none()
            if not skill:
                continue
            # Create folder: skills/<folder_name>/
            skill_folder = skills_dir / skill.folder_name
            skill_folder.mkdir(parents=True, exist_ok=True)
            # Write each file
            for sf in skill.files:
                file_path = skill_folder / sf.path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(sf.content, encoding="utf-8")

    # Auto-install template-declared MCP servers using the system Smithery key.
    # For trading agents, this means shibui/finance lands in the agent's tool
    # list at creation time rather than relying on the agent to install it on
    # first use via the MCP_INSTALLER skill (which depends on LLM compliance).
    # Failures are logged and swallowed — agent creation must not fail because
    # an external Smithery call did.
    template_mcp_servers = list((tpl.default_mcp_servers if data.template_id and tpl else None) or [])
    if template_mcp_servers:
        # Commit the in-flight transaction first so the agent row exists in
        # the database when import_mcp_from_smithery opens its own session
        # to insert AgentTool rows. Without this commit the FK to agents.id
        # is invisible to the parallel session and we get a FK violation.
        await db.commit()
        await db.refresh(agent)

        from loguru import logger
        from app.services.resource_discovery import import_mcp_from_smithery
        for server_id in template_mcp_servers:
            try:
                result_msg = await import_mcp_from_smithery(
                    server_id=server_id,
                    agent_id=agent.id,
                    config={},  # falls back to system Smithery key
                )
                if result_msg.startswith("❌"):
                    logger.warning(
                        f"[create_agent] MCP pre-install for '{server_id}' "
                        f"on agent {agent.id} reported error: {result_msg[:200]}"
                    )
                else:
                    logger.info(
                        f"[create_agent] MCP pre-install '{server_id}' "
                        f"succeeded for agent {agent.id}"
                    )
            except Exception as e:
                logger.warning(
                    f"[create_agent] MCP pre-install for '{server_id}' "
                    f"on agent {agent.id} raised: {e}"
                )

    # Start container
    await agent_manager.start_container(db, agent)
    await db.flush()

    from app.services.okr_agent_hook import hook_new_agent
    if agent.tenant_id:
        await hook_new_agent(db, agent.id, agent.tenant_id)
        await db.commit()

    return agent, None
