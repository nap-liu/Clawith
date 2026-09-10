"""Mechanically separated Agent API route group."""

from app.api.agent_api_shared import *  # noqa: F401,F403


@router.get("/templates")
async def list_templates(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all available agent templates."""
    from app.models.agent import AgentTemplate
    result = await db.execute(
        select(AgentTemplate).order_by(AgentTemplate.is_builtin.desc(), AgentTemplate.created_at.asc())
    )
    templates = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "category": t.category,
            "is_builtin": t.is_builtin,
            "soul_template": t.soul_template,
            "default_skills": t.default_skills,
            "default_autonomy_policy": t.default_autonomy_policy,
            "capability_bullets": t.capability_bullets or [],
            "has_bootstrap": bool(t.bootstrap_content),
        }
        for t in templates
    ]


async def _agent_to_out(
    db: AsyncSession,
    agent: Agent,
    viewer_id: uuid.UUID,
) -> AgentOut:
    """Serialize one agent with ``onboarded_for_me`` for the given viewer."""
    from app.services.onboarding import is_onboarded
    model = AgentOut.model_validate(agent)
    model.onboarded_for_me = await is_onboarded(db, agent.id, viewer_id)
    return model


async def _agents_to_out(
    db: AsyncSession,
    agents: list[Agent],
    viewer_id: uuid.UUID,
) -> list[AgentOut]:
    """List variant that fetches all junction rows in one query."""
    from app.services.onboarding import onboarded_agent_ids
    onboarded = await onboarded_agent_ids(db, viewer_id, [a.id for a in agents])
    out: list[AgentOut] = []
    for a in agents:
        model = AgentOut.model_validate(a)
        model.onboarded_for_me = a.id in onboarded
        out.append(model)
    return out


@router.get("/", response_model=list[AgentOut])
async def list_agents(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all agents the current user has access to."""
    if tenant_id and tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only list agents in your own company",
        )

    requested_tenant_id = current_user.tenant_id

    stmt = build_visible_agents_query(
        current_user,
        tenant_id=requested_tenant_id,
    ).order_by(Agent.created_at.desc())

    result = await db.execute(stmt)
    agents = result.scalars().all()
    # Lazy reset token counters
    needs_flush = False
    for a in agents:
        if await _lazy_reset_token_counters(a, db):
            needs_flush = True
    if needs_flush:
        await db.commit()
    unread_by_agent = await _build_unread_count_by_agent(db, agents, current_user)
    creator_ids = {a.creator_id for a in agents if a.creator_id}
    creators_by_id: dict[uuid.UUID, User] = {}
    if creator_ids:
        from sqlalchemy.orm import selectinload

        creator_rows = await db.execute(
            select(User)
            .where(User.id.in_(creator_ids))
            .options(selectinload(User.identity))
        )
        creators_by_id = {creator.id: creator for creator in creator_rows.scalars().all()}
    from app.services.onboarding import onboarded_agent_ids
    onboarded = await onboarded_agent_ids(db, current_user.id, [a.id for a in agents])
    out: list[AgentOut] = []
    for a in agents:
        creator = creators_by_id.get(a.creator_id)
        model = _serialize_agent_out(
            a,
            unread_by_agent.get(str(a.id), 0),
            creator_username=creator.username if creator else None,
            creator_display_name=creator.display_name if creator else None,
        )
        model.onboarded_for_me = a.id in onboarded
        out.append(model)
    return out


@router.get("/explore", response_model=AgentExplorePageOut)
async def explore_agents(
    tenant_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=120),
    agent_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the lightweight, paginated Agent directory used by Explore."""
    if tenant_id and tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only list agents in your own company",
        )
    if agent_status not in (None, "running", "idle", "stopped", "creating", "error"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid agent status",
        )

    visible = build_visible_agents_query(
        current_user,
        tenant_id=current_user.tenant_id,
    ).with_only_columns(
        Agent.id,
        Agent.name,
        Agent.avatar_url,
        Agent.role_description,
        Agent.bio,
        Agent.status,
        Agent.creator_id,
        Agent.agent_type,
        Agent.openclaw_last_seen,
        Agent.created_at,
        Agent.last_active_at,
    ).subquery("explore_visible_agents")

    counts_result = await db.execute(
        select(
            func.count(visible.c.id),
            func.coalesce(func.sum(case((visible.c.status == "running", 1), else_=0)), 0),
            func.coalesce(func.sum(case((visible.c.status == "idle", 1), else_=0)), 0),
            func.coalesce(func.sum(case((visible.c.status == "stopped", 1), else_=0)), 0),
        ).select_from(visible)
    )
    all_count, running_count, idle_count, stopped_count = counts_result.one()

    filters = []
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                visible.c.name.ilike(pattern),
                visible.c.role_description.ilike(pattern),
                visible.c.bio.ilike(pattern),
            )
        )
    if agent_status:
        filters.append(visible.c.status == agent_status)

    filtered = select(visible).where(*filters).subquery("explore_filtered_agents")
    total_result = await db.execute(select(func.count()).select_from(filtered))
    total = int(total_result.scalar_one() or 0)

    creator = aliased(User)
    creator_identity = aliased(Identity)
    status_order = case(
        (filtered.c.status == "running", 0),
        (filtered.c.status == "idle", 1),
        (filtered.c.status == "creating", 2),
        (filtered.c.status == "stopped", 3),
        (filtered.c.status == "error", 4),
        else_=5,
    )
    rows_result = await db.execute(
        select(
            filtered.c.id,
            filtered.c.name,
            filtered.c.avatar_url,
            filtered.c.role_description,
            filtered.c.bio,
            filtered.c.status,
            filtered.c.creator_id,
            creator.display_name.label("creator_display_name"),
            creator_identity.username.label("creator_username"),
            filtered.c.agent_type,
            filtered.c.openclaw_last_seen,
            filtered.c.created_at,
            filtered.c.last_active_at,
        )
        .select_from(filtered)
        .outerjoin(creator, creator.id == filtered.c.creator_id)
        .outerjoin(creator_identity, creator_identity.id == creator.identity_id)
        .order_by(status_order.asc(), filtered.c.last_active_at.desc().nullslast(), filtered.c.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    rows = [dict(row) for row in rows_result.mappings().all()]
    unread_by_agent = await _build_unread_count_by_agent_ids(
        db,
        [row["id"] for row in rows],
        current_user,
    )
    for row in rows:
        row["unread_count"] = unread_by_agent.get(str(row["id"]), 0)

    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
        "counts": {
            "all": int(all_count or 0),
            "running": int(running_count or 0),
            "idle": int(idle_count or 0),
            "stopped": int(stopped_count or 0),
        },
    }


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new digital employee (any authenticated user)."""
    from app.services.agent_provisioning import AgentProvisionInput, provision_agent
    from app.services.quota_guard import QuotaExceeded

    # Admins may target another tenant; everyone else uses their own.
    target_tenant_id = current_user.tenant_id
    if current_user.role in ("platform_admin", "org_admin") and data.tenant_id:
        target_tenant_id = data.tenant_id

    inp = AgentProvisionInput(
        name=data.name,
        agent_type=data.agent_type or "native",
        role_description=data.role_description,
        bio=data.bio,
        avatar_url=data.avatar_url,
        personality=data.personality,
        boundaries=data.boundaries,
        primary_model_id=data.primary_model_id,
        fallback_model_id=data.fallback_model_id,
        temperature=data.temperature,
        reasoning_effort=data.reasoning_effort,
        permission_scope_type=data.permission_scope_type,
        permission_scope_ids=data.permission_scope_ids,
        permission_access_level=data.permission_access_level,
        permission_grants=data.permission_grants,
        autonomy_policy=data.autonomy_policy,
        max_tokens_per_day=data.max_tokens_per_day,
        max_tokens_per_month=data.max_tokens_per_month,
        template_id=data.template_id,
        skill_ids=data.skill_ids,
    )
    try:
        agent, raw_key = await provision_agent(
            db, creator=current_user, tenant_id=target_tenant_id, data=inp
        )
    except QuotaExceeded as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if agent.agent_type == "openclaw":
        out = (await _agent_to_out(db, agent, current_user.id)).model_dump()
        out["api_key"] = raw_key
        return out
    return await _agent_to_out(db, agent, current_user.id)


@router.get("/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent details."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    # Lazy reset token counters
    if await _lazy_reset_token_counters(agent, db):
        await db.commit()
    out_model = await _agent_to_out(db, agent, current_user.id)
    out = out_model.model_dump()
    out["access_level"] = access_level
    from app.services.scene_service import scene_tool_enabled

    out["scene_config_enabled"] = await scene_tool_enabled(db, agent_id)

    # Resolve creator username (one extra query, only on detail page).
    # IMPORTANT: User.username is an association_proxy to User.identity.username.
    # We must eagerly load the identity relationship (selectinload) to avoid
    # async lazy-loading errors (SQLAlchemy raises MissingGreenlet in async context).
    if agent.creator_id:
        from sqlalchemy.orm import selectinload

        from app.models.user import Identity  # noqa: F401
        creator_result = await db.execute(
            select(User)
            .where(User.id == agent.creator_id)
            .options(selectinload(User.identity))
        )
        creator = creator_result.scalar_one_or_none()
        out["creator_username"] = creator.username if creator else None
        out["creator_display_name"] = creator.display_name if creator else None

    # Resolve effective timezone (agent → tenant → UTC)
    effective_tz = agent.timezone
    if not effective_tz and agent.tenant_id:
        from app.models.tenant import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == agent.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant:
            effective_tz = tenant.timezone or "UTC"
    out["effective_timezone"] = effective_tz or "UTC"

    return out


__all__ = [name for name in globals() if not name.startswith("__")]
