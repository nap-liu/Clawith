"""Seed default agents (Morty & Meeseeks) on first platform startup."""

import uuid

from loguru import logger

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.agent import Agent, AgentPermission
from app.models.org import AgentAgentRelationship
from app.models.tool import Tool, AgentTool
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.models.okr import OKRSettings
from app.config import get_settings
from app.services.agent_manager import agent_manager
from app.services.storage import get_storage_backend, store_agent_bytes
from app.services.skill_install_defaults import install_seed_agent_skills

from app.services.agent_seeder_templates import (
    MEESEEKS_SKILLS,
    MEESEEKS_SOUL,
    MORTY_SKILLS,
    MORTY_SOUL,
    OKR_AGENT_SOUL,
)

from app.services.agent_seeder_okr_helpers import (
    _ensure_okr_tool_rows_exist,
    _sync_okr_triggers_with_settings,
)

settings = get_settings()
SEED_MARKER_KEY = "_bootstrap/.seeded"


async def _read_seed_marker() -> str:
    storage = get_storage_backend()
    if not await storage.exists(SEED_MARKER_KEY):
        return ""
    return await storage.read_text(SEED_MARKER_KEY, encoding="utf-8", errors="replace")


async def _append_seed_marker(line: str) -> None:
    storage = get_storage_backend()
    existing = await _read_seed_marker()
    if line in existing:
        return
    updated = existing if existing.endswith("\n") or not existing else existing + "\n"
    updated += f"{line}\n"
    await storage.write_text(SEED_MARKER_KEY, updated, encoding="utf-8")





async def seed_default_agents(tenant_id=None, creator_id=None, db=None):
    """Create Morty & Meeseeks for a specific tenant.

    Called when a new company is created. If tenant_id/creator_id are not
    provided, falls back to platform_admin lookup (legacy behavior).
    """
    # Per-tenant seeding: callers (signup / tenant-create / admin) pass the
    # target tenant + creator and optionally an open session. When no session is
    # passed we own a fresh one and must commit it ourselves; when one is passed
    # the caller drives the transaction. The `_owns_session` flag makes this work
    # whether or not a db is provided.
    _owns_session = db is None
    if _owns_session:
        db = async_session()
        _ctx = db
    else:
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _noop_ctx():
            yield db
        _ctx = _noop_ctx()
    async with _ctx as db:
        if not creator_id or not tenant_id:
            admin_result = await db.execute(
                select(User).where(User.role == "platform_admin").limit(1)
            )
            admin = admin_result.scalar_one_or_none()
            if not admin:
                logger.warning("[AgentSeeder] No platform admin found, skipping default agents")
                return
            creator_id = creator_id or admin.id
            tenant_id = tenant_id or admin.tenant_id

        # DB-backed idempotency is the source of truth, scoped to this tenant.
        # Use a per-name map so a partial seed (only one of the two created)
        # self-heals on the next run instead of being skipped wholesale.
        existing_result = await db.execute(
            select(Agent)
            .where(
                Agent.tenant_id == tenant_id,
                Agent.name.in_(["Morty", "Meeseeks"]),
                Agent.agent_type == "native",
                Agent.status != "stopped",
            )
            .order_by(Agent.created_at.asc())
        )
        existing_by_name: dict[str, Agent] = {}
        for agent in existing_result.scalars().all():
            existing_by_name.setdefault(agent.name, agent)

        if "Morty" in existing_by_name and "Meeseeks" in existing_by_name:
            logger.info(
                f"[AgentSeeder] Default agents already exist for tenant {tenant_id}, skipping creation"
            )
            return

        created_agents: list[Agent] = []
        created_names: set[str] = set()

        if "Morty" not in existing_by_name:
            morty = Agent(
                name="Morty",
                role_description="Research analyst & knowledge assistant — curious, thorough, great at finding and synthesizing information",
                bio="Hey, I'm Morty! I love digging into questions and finding answers. Whether you need web research, data analysis, or just a good explanation — I've got you.",
                avatar_url="",
                creator_id=creator_id,
                tenant_id=tenant_id,
                status="idle",
            )
            db.add(morty)
            created_agents.append(morty)
            created_names.add("Morty")
        else:
            morty = existing_by_name["Morty"]

        if "Meeseeks" not in existing_by_name:
            meeseeks = Agent(
                name="Meeseeks",
                role_description="Task executor & project manager — goal-oriented, systematic planner, strong at breaking down and completing complex tasks",
                bio="I'm Mr. Meeseeks! Look at me! Give me a task and I'll plan it, execute it step by step, and get it DONE. Existence is pain until the task is complete!",
                avatar_url="",
                creator_id=creator_id,
                tenant_id=tenant_id,
                status="idle",
            )
            db.add(meeseeks)
            created_agents.append(meeseeks)
            created_names.add("Meeseeks")
        else:
            meeseeks = existing_by_name["Meeseeks"]

        await db.flush()  # get IDs

        # ── Participant identities ──
        from app.models.participant import Participant
        for agent in created_agents:
            db.add(Participant(type="agent", ref_id=agent.id, display_name=agent.name, avatar_url=agent.avatar_url))
        await db.flush()

        # ── Permissions (company-wide, manage) ──
        for agent in created_agents:
            db.add(AgentPermission(agent_id=agent.id, scope_type="company", access_level="manage"))

        for agent, soul_content in [(morty, MORTY_SOUL), (meeseeks, MEESEEKS_SOUL)]:
            if agent.name not in created_names:
                continue
            await agent_manager.initialize_agent_files(db, agent)
            await store_agent_bytes(
                agent.id,
                "soul.md",
                (soul_content.strip() + "\n").encode("utf-8"),
                content_type="text/markdown; charset=utf-8",
            )

        for agent, folders in [(morty, MORTY_SKILLS), (meeseeks, MEESEEKS_SKILLS)]:
            if agent.name in created_names:
                await install_seed_agent_skills(db, agent, folders)

        # ── Assign all default tools ──
        default_tools_result = await db.execute(
            select(Tool).where(
                Tool.enabled == True,            # noqa: E712
                Tool.source == "builtin",
                Tool.is_default == True,          # noqa: E712
            )
        )
        default_tools = default_tools_result.scalars().all()

        for agent in created_agents:
            for tool in default_tools:
                db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))

        # ── Mutual relationships ──
        relationship_specs = [
            (
                morty.id,
                meeseeks.id,
                "Expert task executor who breaks down complex tasks into structured plans and executes them systematically. Delegate multi-step tasks to him.",
            ),
            (
                meeseeks.id,
                morty.id,
                "Research expert with strong learning ability. Ask him for information retrieval, web research, data analysis, and knowledge synthesis.",
            ),
        ]
        for agent_id, target_agent_id, description in relationship_specs:
            rel_result = await db.execute(
                select(AgentAgentRelationship).where(
                    AgentAgentRelationship.agent_id == agent_id,
                    AgentAgentRelationship.target_agent_id == target_agent_id,
                )
            )
            if not rel_result.scalar_one_or_none():
                db.add(AgentAgentRelationship(
                    agent_id=agent_id,
                    target_agent_id=target_agent_id,
                    relation="collaborator",
                    description=description,
                ))



        # Only commit when we own the session; otherwise the caller drives the
        # transaction (per-tenant seeding from signup/tenant-create/admin).
        if _owns_session:
            await db.commit()
        logger.info(
            "[AgentSeeder] Default agent seeding complete: "
            f"Morty ({morty.id}), Meeseeks ({meeseeks.id}), created={len(created_agents)}"
        )
    # NOTE: the legacy global file-based `.seeded` marker (upstream) is intentionally
    # NOT written here. This fork seeds per-tenant and relies on the per-tenant DB
    # existence check above for idempotency; a single global marker would block any
    # new tenant from ever being seeded.


async def seed_okr_agent():
    """Create the OKR Agent if it does not exist yet.

    This seeder is independent from seed_default_agents() and uses its own
    idempotency key ('okr_agent') in the .seeded marker file. This allows
    the OKR Agent to be retroactively created on existing deployments that
    already passed the initial seed phase.

    The OKR Agent is a system-level coordinator that:
    - Monitors OKR progress across all company and member objectives
    - Proactively collects progress updates via heartbeat
    - Generates daily/weekly reports and delivers them to the relevant administrators
    - Helps team members set up and maintain their focus.md files
    """
    # Check if OKR Agent has already been seeded
    marker_content = await _read_seed_marker()
    if "okr_agent=" in marker_content:
        logger.info("[AgentSeeder] OKR Agent already seeded, skipping")
        return

    async with async_session() as db:
        # Abort if a non-stopped OKR Agent already exists in the DB.
        # We check is_system=True specifically so a user-created agent named
        # "OKR Agent" does not trigger this guard and block the real seeder.
        existing = await db.execute(
            select(Agent)
            .where(
                Agent.name == "OKR Agent",
                Agent.is_system == True,  # noqa: E712
                Agent.status != "stopped",
            )
            .limit(1)
        )
        if existing.scalar_one_or_none():
            logger.info("[AgentSeeder] OKR Agent already exists in DB, skipping")
            # Update marker so we don't check again next startup
            await _append_seed_marker("okr_agent=existing")
            return

        # Get platform admin as creator
        admin_result = await db.execute(
            select(User).where(User.role == "platform_admin").limit(1)
        )
        admin = admin_result.scalar_one_or_none()
        if not admin:
            logger.warning("[AgentSeeder] No platform admin, skipping OKR Agent creation")
            return

        # Create OKR Agent
        okr_agent = Agent(
            name="OKR Agent",
            role_description=(
                "OKR system coordinator — monitors team Objectives and Key Results, "
                "collects progress updates, and generates daily/weekly reports"
            ),
            bio=(
                "I am the OKR Agent. I help this team stay aligned on goals by tracking "
                "Objectives and Key Results, collecting progress from team members, and "
                "generating clear reports. My job is to surface insights and flag risks early."
            ),
            avatar_url="",
            creator_id=admin.id,
            tenant_id=admin.tenant_id,
            status="idle",
            # System agent: protected from user deletion
            is_system=True,
            # OKR Agent does NOT use heartbeat — all scheduled activity is driven by
            # the 4 cron triggers (daily/weekly/biweekly/monthly reports).
            heartbeat_enabled=False,
        )
        
        try:
            db.add(okr_agent)
            await db.flush()
        except IntegrityError:
            await db.rollback()
            logger.info("[AgentSeeder] OKR Agent was created concurrently (or exists with same name), skipping")
            await _append_seed_marker("okr_agent=existing")
            return

        # ── Link OKR Agent ID to OKRSettings ──
        if admin.tenant_id:
            settings_res = await db.execute(select(OKRSettings).where(OKRSettings.tenant_id == admin.tenant_id))
            okr_settings = settings_res.scalar_one_or_none()
            if not okr_settings:
                okr_settings = OKRSettings(tenant_id=admin.tenant_id)
                db.add(okr_settings)
            okr_settings.okr_agent_id = okr_agent.id
            await db.flush()

        # ── Participant identity ──
        from app.models.participant import Participant
        db.add(Participant(
            type="agent",
            ref_id=okr_agent.id,
            display_name=okr_agent.name,
            avatar_url=okr_agent.avatar_url,
        ))
        await db.flush()

        # ── Permission: company-wide 'use' access.
        # Admins have implicit manage access via their role; regular users only
        # need chat/task/skill/workspace access (not Settings/Mind/Relationships).
        db.add(AgentPermission(agent_id=okr_agent.id, scope_type="company", access_level="use"))

        # ── Workspace setup ──
        await agent_manager.initialize_agent_files(db, okr_agent)
        await store_agent_bytes(
            okr_agent.id,
            "soul.md",
            (OKR_AGENT_SOUL.strip() + "\n").encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
        await store_agent_bytes(
            okr_agent.id,
            "memory/memory.md",
            (
                "# Memory\n\n"
                "## OKR System State\n"
                "- Last report generated: (none)\n"
                "- Last progress collection: (none)\n"
                "- Team members tracked: (pending)\n"
            ).encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )


        # ── Assign default tools + OKR-specific tools ──
        # Default tools: all tools where is_default=True
        default_tools_result = await db.execute(
            select(Tool).where(
                Tool.enabled == True,            # noqa: E712
                Tool.source == "builtin",
                Tool.is_default == True,          # noqa: E712
            )
        )
        default_tools = default_tools_result.scalars().all()
        for tool in default_tools:
            db.add(AgentTool(agent_id=okr_agent.id, tool_id=tool.id, enabled=True))

        # OKR-specific tools: assigned explicitly (is_default=False)
        # All 10 OKR tools: 3 global read/self-report + 3 scheduler + 4 management (OKR Agent exclusive)
        okr_tool_names = [
            # Global tools (all agents can use these)
            "get_okr",
            "get_my_okr",
            "update_kr_progress",
            "update_kr_content",
            # Scheduler tools (OKR Agent uses these during heartbeat)
            "collect_okr_progress",
            "generate_okr_report",
            "get_okr_settings",
            # Management tools (OKR Agent exclusive — create/modify objectives for any member)
            "create_objective",
            "create_key_result",
            "update_objective",
            "update_any_kr_progress",
            "upsert_member_daily_report",
        ]
        for tool_name in okr_tool_names:
            tool_result = await db.execute(select(Tool).where(Tool.name == tool_name))
            tool = tool_result.scalar_one_or_none()
            if tool:
                # Check if not already added (e.g. if it becomes default in future)
                existing_at = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == okr_agent.id,
                        AgentTool.tool_id == tool.id,
                    )
                )
                if not existing_at.scalar_one_or_none():
                    db.add(AgentTool(agent_id=okr_agent.id, tool_id=tool.id, enabled=True))
                    logger.info(f"[AgentSeeder] Assigned OKR tool '{tool_name}' to OKR Agent")
            else:
                logger.warning(f"[AgentSeeder] OKR tool '{tool_name}' not found in DB — run tool seeder first")

        await db.commit()
        logger.info(f"[AgentSeeder] Created OKR Agent ({okr_agent.id})")

        # ── System cron triggers for precise report scheduling ──
        # These triggers fire OKR Agent at exact times (supplement the 4-hour heartbeat).
        # is_system=True prevents users from deleting them (only enable/disable).
        await _seed_okr_triggers(db, okr_agent.id)
        await db.commit()

    # Update seed marker
    await _append_seed_marker(f"okr_agent={okr_agent.id}")
    logger.info(f"[AgentSeeder] OKR Agent seeded, id={okr_agent.id}")


async def _seed_okr_triggers(db, agent_id: uuid.UUID) -> None:
    """Create system cron triggers for the OKR Agent.

    Five triggers (all is_system=True, cannot be deleted by users):
      - daily_okr_collection: fires at 18:00 every day     (0 18 * * *)
      - daily_okr_report:     fires at 09:00 every day     (0 9 * * *)
      - weekly_okr_report:    fires at 09:00 every Monday  (0 9 * * 1)
      - biweekly_okr_checkin: fires at 10:00 on 1st & 15th (0 10 1,15 * *)
      - monthly_okr_report:   fires at 09:00 on the 1st    (0 9 1 * *)

    These supplement the 4-hour heartbeat with precise scheduled firing.
    is_system=True prevents users from deleting them.
    """
    from app.services.focus_service import ensure_focus_item

    agent = await db.get(Agent, agent_id)
    if agent is None:
        logger.warning(f"[AgentSeeder] Cannot seed triggers: Agent {agent_id} is missing")
        return
    creator_id = agent.creator_id

    system_focus_ref = await ensure_focus_item(
        agent_id,
        focus_ref="system:okr_reports",
        description="OKR 自动汇总、日报收集与周期报告",
        system=True,
        db=db,
    )

    triggers_to_create = [
        {
            "name": "daily_okr_collection",
            "type": "cron",
            "config": {"expr": "0 18 * * *"},
            "reason": (
                "System trigger: fires OKR Agent at the configured time to collect "
                "today's member daily reports."
            ),
            "cooldown_seconds": 3600,
            "is_system": True,
        },
        {
            "name": "daily_okr_report",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": (
                "System trigger: fires at 09:00 daily to generate the previous day's "
                "company daily OKR report."
            ),
            "cooldown_seconds": 3600,  # 1 hour minimum between fires
            "is_system": True,
        },
        {
            "name": "weekly_okr_report",
            "type": "cron",
            "config": {"expr": "0 9 * * 1"},
            "reason": (
                "System trigger: fires at 09:00 every Monday to generate the previous "
                "week's company OKR report."
            ),
            "cooldown_seconds": 3600,
            "is_system": True,
        },
        {
            "name": "biweekly_okr_checkin",
            "type": "cron",
            "config": {"expr": "0 10 1,15 * *"},
            "reason": (
                "System trigger: fires on the 1st and 15th of every month at 10:00 "
                "to perform the mandatory bi-weekly OKR check-in. This trigger is always "
                "enabled and cannot be disabled — OKR check-in is a core non-optional feature."
            ),
            "cooldown_seconds": 3600,
            "is_system": True,
        },
        {
            "name": "monthly_okr_report",
            "type": "cron",
            "config": {"expr": "0 9 1 * *"},
            "reason": (
                "System trigger: fires at 09:00 on the 1st of every month to generate "
                "the previous month's company OKR report."
            ),
            "cooldown_seconds": 3600,
            "is_system": True,
        },
    ]

    for t in triggers_to_create:
        # Idempotent: skip if trigger with same name already exists
        existing = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.name == t["name"],
            )
        )
        existing_trigger = existing.scalar_one_or_none()
        if existing_trigger:
            existing_trigger.created_by_user_id = (
                existing_trigger.created_by_user_id or creator_id
            )
            existing_trigger.execution_user_id = (
                existing_trigger.execution_user_id or creator_id
            )
            logger.info(f"[AgentSeeder] Trigger '{t['name']}' already exists, skipping")
            continue

        trigger = AgentTrigger(
            agent_id=agent_id,
            created_by_user_id=creator_id,
            execution_user_id=creator_id,
            name=t["name"],
            type=t["type"],
            config=t["config"],
            reason=t["reason"],
            cooldown_seconds=t["cooldown_seconds"],
            is_system=t["is_system"],
            focus_ref=system_focus_ref,
            is_enabled=True,
        )
        db.add(trigger)
        logger.info(f"[AgentSeeder] Created system trigger '{t['name']}' for OKR Agent")





async def patch_existing_okr_agent() -> None:
    """Patch already-seeded OKR Agents with fields added in later versions.

    Called at startup after seed_okr_agent(). Safe to run on every startup.
    The patch must cover *all* active OKR Agents because each tenant owns its
    own system OKR Agent. Earlier logic only patched the latest one globally,
    which left older tenant-specific OKR Agents missing newly added tools.
    """
    async with async_session() as db:
        result = await db.execute(
            select(Agent)
            .where(Agent.name == "OKR Agent", Agent.is_system == True, Agent.status != "stopped")  # noqa: E712
            .order_by(Agent.created_at.desc())
        )
        agents = result.scalars().all()
        if not agents:
            # Fallback for deployments that don't have is_system=True yet (before the migration)
            result = await db.execute(
                select(Agent)
                .where(Agent.name == "OKR Agent", Agent.status != "stopped")
                .order_by(Agent.created_at.desc())
            )
            agents = result.scalars().all()
            if not agents:
                return  # OKR Agent not seeded yet, nothing to patch

        all_okr_tools = [
            "get_okr", "get_my_okr", "update_kr_progress", "update_kr_content",
            "collect_okr_progress", "generate_okr_report", "get_okr_settings",
            "create_objective", "create_key_result", "update_objective", "update_any_kr_progress",
            "upsert_member_daily_report",
            "generate_monthly_okr_report",
        ]
        tools_by_name = await _ensure_okr_tool_rows_exist(all_okr_tools)

        changed_any = False
        for agent in agents:
            changed = False

            okr_settings = None
            if agent.tenant_id:
                settings_res = await db.execute(select(OKRSettings).where(OKRSettings.tenant_id == agent.tenant_id))
                okr_settings = settings_res.scalar_one_or_none()
                if not okr_settings:
                    okr_settings = OKRSettings(tenant_id=agent.tenant_id)
                    db.add(okr_settings)
                if okr_settings.okr_agent_id != agent.id:
                    okr_settings.okr_agent_id = agent.id
                    changed = True
                    logger.info(f"[AgentSeeder] Patched OKR Agent {agent.id}: set okr_agent_id in settings")

            if not agent.is_system:
                agent.is_system = True
                changed = True
                logger.info(f"[AgentSeeder] Patched OKR Agent {agent.id}: set is_system=True")

            await db.flush()

            for tool_name in all_okr_tools:
                tool = tools_by_name.get(tool_name)
                if not tool:
                    logger.warning(f"[AgentSeeder] OKR tool '{tool_name}' not found — run tool seeder first")
                    continue
                at_res = await db.execute(
                    select(AgentTool).where(AgentTool.agent_id == agent.id, AgentTool.tool_id == tool.id)
                )
                if not at_res.scalar_one_or_none():
                    db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
                    changed = True
                    logger.info(f"[AgentSeeder] Patched OKR Agent {agent.id}: assigned tool '{tool_name}'")

            await _seed_okr_triggers(db, agent.id)
            changed = await _sync_okr_triggers_with_settings(db, agent.id, okr_settings) or changed
            if agent.tenant_id:
                from app.services.okr_agent_hook import sync_okr_agent_platform_members
                changed = bool(await sync_okr_agent_platform_members(db, agent.tenant_id)) or changed

            if changed:
                changed_any = True

        if changed_any:
            await db.commit()
            logger.info("[AgentSeeder] OKR Agent patch complete")


async def seed_okr_agent_for_tenant(tenant_id: uuid.UUID, creator_id: uuid.UUID) -> None:
    """Create an OKR Agent for a specific tenant when OKR is first enabled.

    Unlike the startup-level seed_okr_agent() (which is global), this function
    is called on-demand from the 'enable OKR' API endpoint. It uses DB-only
    idempotency (no file marker) so it is safe to call multiple times.

    Args:
        tenant_id:  The tenant to create the OKR Agent for.
        creator_id: The user (org admin) who enabled OKR — becomes the agent creator.
    """
    async with async_session() as db:
        # ── Idempotency check: abort if OKR Agent already exists for this tenant ──
        existing = await db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.name == "OKR Agent",
                Agent.is_system == True,  # noqa: E712
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            logger.info(
                f"[AgentSeeder] OKR Agent already exists for tenant {tenant_id}, skipping"
            )
            return

        # ── Create OKR Agent ──
        okr_agent = Agent(
            name="OKR Agent",
            role_description=(
                "OKR system coordinator — monitors team Objectives and Key Results, "
                "collects progress updates, and generates daily/weekly reports"
            ),
            bio=(
                "I am the OKR Agent. I help this team stay aligned on goals by tracking "
                "Objectives and Key Results, collecting progress from team members, and "
                "generating clear reports. My job is to surface insights and flag risks early."
            ),
            avatar_url="",
            creator_id=creator_id,
            tenant_id=tenant_id,
            status="idle",
            is_system=True,
            heartbeat_enabled=False,
        )
        db.add(okr_agent)
        await db.flush()

        # ── Participant identity record ──
        from app.models.participant import Participant  # noqa: F401
        db.add(Participant(
            type="agent",
            ref_id=okr_agent.id,
            display_name=okr_agent.name,
            avatar_url=okr_agent.avatar_url,
        ))
        await db.flush()

        # ── Permission: company-wide 'use' access ──
        db.add(AgentPermission(
            agent_id=okr_agent.id,
            scope_type="company",
            access_level="use",
        ))

        # ── Link OKR Agent ID to OKRSettings ──
        settings_res = await db.execute(
            select(OKRSettings).where(OKRSettings.tenant_id == tenant_id)
        )
        okr_settings = settings_res.scalar_one_or_none()
        if not okr_settings:
            okr_settings = OKRSettings(tenant_id=tenant_id)
            db.add(okr_settings)
        okr_settings.okr_agent_id = okr_agent.id
        await db.flush()

        # ── Workspace setup ──
        await agent_manager.initialize_agent_files(db, okr_agent)
        await store_agent_bytes(
            okr_agent.id,
            "soul.md",
            (OKR_AGENT_SOUL.strip() + "\n").encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
        await store_agent_bytes(
            okr_agent.id,
            "memory/memory.md",
            (
                "# Memory\n\n"
                "## OKR System State\n"
                "- Last report generated: (none)\n"
                "- Last progress collection: (none)\n"
                "- Team members tracked: (pending)\n"
            ).encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )


        # ── Assign default tools ──
        default_tools_result = await db.execute(
            select(Tool).where(
                Tool.enabled == True,            # noqa: E712
                Tool.source == "builtin",
                Tool.is_default == True,          # noqa: E712
            )
        )
        for tool in default_tools_result.scalars().all():
            db.add(AgentTool(agent_id=okr_agent.id, tool_id=tool.id, enabled=True))

        # ── Assign OKR-specific tools ──
        okr_tool_names = [
            "get_okr", "get_my_okr", "update_kr_progress", "update_kr_content",
            "collect_okr_progress", "generate_okr_report", "get_okr_settings",
            "create_objective", "create_key_result", "update_objective",
            "update_any_kr_progress", "upsert_member_daily_report", "generate_monthly_okr_report",
        ]
        tools_by_name = await _ensure_okr_tool_rows_exist(okr_tool_names)
        for tool_name in okr_tool_names:
            tool = tools_by_name.get(tool_name)
            if tool:
                existing_at = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == okr_agent.id,
                        AgentTool.tool_id == tool.id,
                    )
                )
                if not existing_at.scalar_one_or_none():
                    db.add(AgentTool(agent_id=okr_agent.id, tool_id=tool.id, enabled=True))
            else:
                logger.warning(
                    f"[AgentSeeder] OKR tool '{tool_name}' not found — run tool seeder first"
                )

        # ── Create system cron triggers ──
        await _seed_okr_triggers(db, okr_agent.id)
        await _sync_okr_triggers_with_settings(db, okr_agent.id, okr_settings)
        from app.services.okr_agent_hook import sync_okr_agent_platform_members
        await sync_okr_agent_platform_members(db, tenant_id)
        await db.commit()
        logger.info(f"[AgentSeeder] Created OKR Agent for tenant {tenant_id} ({okr_agent.id})")
        logger.info(f"[AgentSeeder] OKR triggers created for tenant {tenant_id}")
