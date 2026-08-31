"""Project validation and creation orchestration."""

from app.services.project_service_shared import *  # noqa: F403
from app.services.project_service_access import *  # noqa: F403
from app.services.project_service_membership import *  # noqa: F403

async def _validate_project_create_inputs(
    db: AsyncSession,
    user: User,
    data: ProjectCreate,
    tenant_id: uuid.UUID,
    *,
    allow_template_agents: bool = False,
) -> list[ProjectMemberCreate]:
    """Validate every external reference before creating managed storage."""

    git_config = dict((data.settings or {}).get("git") or {})
    git_mode = git_config.get("mode") or git_config.get("repository_mode", "managed")
    if git_mode != "managed":
        raise HTTPException(status_code=501, detail="External Git repositories require a connector")
    shared_user_ids = set(data.shared_with_user_ids)
    shared_user_ids.discard(user.id)
    if data.visibility == "shared" and not shared_user_ids:
        raise HTTPException(status_code=422, detail="shared visibility requires shared_with_user_ids")
    if shared_user_ids:
        valid_user_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(shared_user_ids),
                        User.tenant_id == tenant_id,
                        User.is_active.is_(True),
                    )
                )
            ).scalars()
        )
        if valid_user_ids != shared_user_ids:
            raise HTTPException(status_code=422, detail="Every shared user must be active in the project tenant")

    members = [member.model_copy(deep=True) for member in data.members]
    if not members and not allow_template_agents:
        raise HTTPException(
            status_code=422,
            detail="A project requires at least one digital employee",
        )
    member_ids = [member.agent_id for member in members]
    if len(member_ids) != len(set(member_ids)):
        raise HTTPException(status_code=422, detail="A project cannot include the same digital employee twice")
    leader_count = sum(member.is_leader for member in members)
    if leader_count > 1:
        raise HTTPException(status_code=422, detail="A project can have only one leader")
    if members and leader_count == 0:
        members[0] = members[0].model_copy(update={"is_leader": True})

    if member_ids:
        from app.core.permissions import build_visible_agents_query

        sources = (
            (
                await db.execute(
                    build_visible_agents_query(user, tenant_id=tenant_id).where(Agent.id.in_(set(member_ids)))
                )
            )
            .scalars()
            .all()
        )
        sources_by_id = {source.id: source for source in sources}
        if set(sources_by_id) != set(member_ids):
            raise HTTPException(status_code=422, detail="源数字员工不可用")
        if any(source.agent_type != "native" for source in sources):
            raise HTTPException(status_code=422, detail="仅原生数字员工可复制到项目")
    else:
        sources = []

    inherited_sources = {
        capability.inherited_from_agent_id
        for capability in data.capabilities
        if capability.source == "inherited"
    }
    if None in inherited_sources or not inherited_sources.issubset(set(member_ids)):
        raise HTTPException(status_code=422, detail="Inherited capability source must be a selected project member")

    capability_options = await load_project_capability_options(db, tenant_id, sources)

    for capability in data.capabilities:
        if capability.capability_type not in {"mcp", "skill"} or capability.capability_id is None:
            continue
        allowed = (
            capability_options.allows_shared(capability.capability_type, capability.capability_id)
            if capability.source == "shared"
            else capability_options.allows(
                capability.inherited_from_agent_id,
                capability.capability_type,
                capability.capability_id,
            )
        )
        if not allowed:
            raise HTTPException(status_code=422, detail="Selected project capability is unavailable")

    for capability in data.capabilities:
        await _resolve_capability(db, tenant_id, capability)

    selected_tool_ids = {
        capability_id
        for member in members
        for capability_id in member.enabled_inherited_capability_ids
    }
    selected_tool_ids.update(
        setting.tool_id
        for member in members
        if member.settings is not None
        for setting in member.settings.tools
    )
    explicit_tool_ids = {
        capability.capability_id
        for capability in data.capabilities
        if capability.capability_type == "tool" and capability.capability_id is not None
    }
    tool_ids = selected_tool_ids | explicit_tool_ids
    if tool_ids:
        available_tool_ids = set(
            (
                await db.execute(
                    select(Tool.id).where(
                        Tool.id.in_(tool_ids),
                        Tool.enabled.is_(True),
                        or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                    )
                )
            ).scalars()
        )
        if available_tool_ids != tool_ids:
            raise HTTPException(status_code=422, detail="One or more selected tools are unavailable")

    for member in members:
        if member.settings is None:
            continue
        if any(
            not capability_options.allows_tool(member.agent_id, setting.tool_id)
            for setting in member.settings.tools
        ):
            raise HTTPException(status_code=422, detail="One or more selected tools are unavailable for this digital employee")
        if any(
            not capability_options.allows(member.agent_id, "mcp", capability_id)
            for capability_id in member.settings.mcp_capability_ids
        ):
            raise HTTPException(status_code=422, detail="One or more selected MCP services are unavailable")
        override_server_ids = [
            setting.server_id for setting in member.settings.mcp_server_overrides
        ]
        if len(override_server_ids) != len(set(override_server_ids)):
            raise HTTPException(status_code=422, detail="MCP service configuration is duplicated")
        if any(
            not capability_options.allows(member.agent_id, "mcp", server_id)
            for server_id in override_server_ids
        ):
            raise HTTPException(status_code=422, detail="One or more configured MCP services are unavailable")
        private_server_ids = capability_options.agent_mcp_ids.get(member.agent_id, frozenset())
        if set(override_server_ids) & set(private_server_ids):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Private MCP credentials cannot be copied into a project. "
                    "They are referenced at runtime only when the execution user owns the source digital employee."
                ),
            )
        enabled_tool_ids = [
            setting.tool_id for setting in member.settings.tools if setting.enabled
        ]
        enabled_mcp_server_ids = set(
            (
                await db.execute(
                    select(Tool.mcp_server_id).where(
                        Tool.id.in_(enabled_tool_ids),
                        Tool.type == "mcp",
                        Tool.mcp_server_id.is_not(None),
                    )
                )
            ).scalars()
        ) if enabled_tool_ids else set()
        if not set(override_server_ids).issubset(enabled_mcp_server_ids):
            raise HTTPException(
                status_code=422,
                detail="MCP configuration requires at least one enabled tool from that service",
            )
        if any(
            not capability_options.allows(member.agent_id, "skill", capability_id)
            for capability_id in member.settings.skill_capability_ids
        ):
            raise HTTPException(status_code=422, detail="One or more selected Skills are unavailable")

    for capability_id in set(data.shared_capability_ids):
        if capability_id not in capability_options.market_skill_ids | capability_options.shared_mcp_ids:
            raise HTTPException(status_code=422, detail=f"Capability {capability_id} is unavailable")
    return members


async def create_project(
    db: AsyncSession,
    user: User,
    data: ProjectCreate,
    *,
    allow_template_agents: bool = False,
) -> Project:
    tenant_id = _tenant_id(user)
    members = await _validate_project_create_inputs(
        db,
        user,
        data,
        tenant_id,
        allow_template_agents=allow_template_agents,
    )
    project_id = uuid.uuid4()
    try:
        return await _create_project_uncompensated(
            db,
            user,
            data,
            tenant_id=tenant_id,
            project_id=project_id,
            members=members,
        )
    except Exception:
        from app.services.project_git_service import remove_project_repository

        cleanup_target = Project(id=project_id, tenant_id=tenant_id)
        try:
            await remove_project_repository(cleanup_target)
        except Exception:
            # Preserve the original create failure. Storage cleanup is
            # idempotent and is retried by the API transaction boundary.
            pass
        raise


async def _create_project_uncompensated(
    db: AsyncSession,
    user: User,
    data: ProjectCreate,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    members: list[ProjectMemberCreate],
) -> Project:
    settings = dict(data.settings or {})
    settings["planning"] = {
        **dict(settings.get("planning") or {}),
        "conversation_mode": "project_group",
    }
    project = Project(
        id=project_id,
        tenant_id=tenant_id,
        owner_user_id=user.id,
        template_id=data.template_id,
        name=data.name,
        description=data.description,
        goal=data.goal,
        success_criteria=data.success_criteria,
        visibility="private",
        status=data.status,
        settings=settings,
    )
    from app.services.project_git_service import (
        initialize_project_repo,
        project_user_git_email,
        reconcile_project_repository_operations,
    )

    db.add(project)
    await db.flush()
    git_config = dict((project.settings or {}).get("git") or {})
    git_mode = git_config.get("mode") or git_config.get("repository_mode", "managed")
    git_state = await initialize_project_repo(
        project,
        author_name=user.display_name,
        author_email=project_user_git_email(user.id),
    )
    project.settings = {
        **(project.settings or {}),
        "git": {**git_config, "mode": git_mode, **git_state},
    }
    await reconcile_project_repository_operations(project.id, db=db)

    # A newly created project owns independent digital employees. The selected
    # standard employees are sources for a one-time copy, never live members of
    # the project. This keeps professional identity, tools and Skill files inside
    # the project repository while source memory and workspace history stay in
    # global scope.
    from app.services.project_agent_service import create_project_agent

    project_agent_ids: list[uuid.UUID] = []
    source_to_project_agent: dict[uuid.UUID, uuid.UUID] = {}
    for member in members:
        project_agent, _project_member = await create_project_agent(
            db,
            project,
            user,
            ProjectAgentCreate(
                source_agent_id=member.agent_id,
                is_leader=member.is_leader,
            ),
        )
        project_agent_ids.append(project_agent.id)
        source_to_project_agent[member.agent_id] = project_agent.id

    capabilities: list[ProjectCapabilityCreate] = []
    for capability in data.capabilities:
        inherited_from_agent_id = capability.inherited_from_agent_id
        if capability.source == "inherited" and inherited_from_agent_id is not None:
            inherited_from_agent_id = source_to_project_agent.get(
                inherited_from_agent_id,
                inherited_from_agent_id,
            )
        capabilities.append(
            capability.model_copy(
                update={"inherited_from_agent_id": inherited_from_agent_id},
            )
        )

    explicit_capability_ids = {capability.capability_id for capability in capabilities}
    for capability_id in data.shared_capability_ids:
        if capability_id in explicit_capability_ids:
            continue
        skill = (
            await db.execute(
                select(Skill).where(
                    Skill.id == capability_id,
                    or_(Skill.tenant_id == tenant_id, Skill.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        capability_type = "skill"
        capability_name = skill.name if skill else None
        if skill is None:
            mcp = (
                await db.execute(
                    select(MCPServer).where(
                        MCPServer.id == capability_id,
                        or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None)),
                    )
                )
            ).scalar_one_or_none()
            if mcp is None:
                raise HTTPException(status_code=422, detail=f"Capability {capability_id} is unavailable")
            capability_type = "mcp"
            capability_name = mcp.display_name or mcp.name
        capabilities.append(
            ProjectCapabilityCreate(
                capability_type=capability_type,
                capability_id=capability_id,
                capability_name=capability_name,
                source="shared",
            )
        )

    # The capability picker starts from the normalized project starter set and
    # may add further Tool/MCP dependencies explicitly. Apply only those chosen
    # identifiers to the copied project employee; source Agent assignments and
    # configuration are never reused.
    for member in members:
        project_agent_id = source_to_project_agent[member.agent_id]
        for capability_id in member.enabled_inherited_capability_ids:
            tool = (
                await db.execute(
                    select(Tool).where(
                        Tool.id == capability_id,
                        Tool.enabled.is_(True),
                        or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                    )
                )
            ).scalar_one_or_none()
            if tool is None:
                continue
            normalized_type = "mcp" if tool.type == "mcp" else "tool"
            normalized_id = tool.mcp_server_id if normalized_type == "mcp" else tool.id
            if normalized_id is None:
                continue
            capabilities.append(
                ProjectCapabilityCreate(
                    capability_type=normalized_type,
                    capability_id=normalized_id,
                    capability_name=tool.mcp_server_name or tool.display_name or tool.name,
                    source="inherited",
                    inherited_from_agent_id=project_agent_id,
                )
            )

    from app.services.project_member_runtime import (
        apply_project_agent_tool_settings,
        merge_project_member_runtime_config,
        sync_project_capability_assignment,
    )
    from app.services.project_skill_assets import bind_library_skill_to_project_agent

    for capability in capabilities:
        if capability.capability_type == "skill":
            targets = (
                project_agent_ids
                if capability.source == "shared"
                else [capability.inherited_from_agent_id]
            )
            for target_agent_id in dict.fromkeys(targets):
                if target_agent_id is None:
                    continue
                existing_skill = (
                    await db.execute(
                        select(ProjectCapabilityBinding.id).where(
                            ProjectCapabilityBinding.project_id == project.id,
                            ProjectCapabilityBinding.tenant_id == project.tenant_id,
                            ProjectCapabilityBinding.capability_type == "skill",
                            ProjectCapabilityBinding.capability_id == capability.capability_id,
                            ProjectCapabilityBinding.inherited_from_agent_id == target_agent_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing_skill is not None:
                    continue
                await bind_library_skill_to_project_agent(
                    db,
                    project,
                    skill_id=capability.capability_id,
                    project_agent_id=target_agent_id,
                    is_enabled=capability.is_enabled,
                    scope=capability.scope,
                    actor_user_id=user.id,
                    actor_display_name=user.display_name,
                )
            continue

        existing_capability = (
            await db.execute(
                select(ProjectCapabilityBinding.id).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.capability_type == capability.capability_type,
                    ProjectCapabilityBinding.capability_id == capability.capability_id,
                    ProjectCapabilityBinding.source == capability.source,
                    ProjectCapabilityBinding.inherited_from_agent_id
                    == capability.inherited_from_agent_id,
                )
            )
        ).scalar_one_or_none()
        if existing_capability is not None:
            continue
        binding = await add_capability(db, project, capability, actor_user_id=user.id)
        await sync_project_capability_assignment(db, project, binding)

    for source_member in members:
        if source_member.settings is None:
            continue
        project_agent_id = source_to_project_agent[source_member.agent_id]
        project_member = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.agent_id == project_agent_id,
                )
            )
        ).scalar_one()
        project_member.config_snapshot = await merge_project_member_runtime_config(
            db,
            project,
            project_member,
            source_member.settings.config_snapshot,
        )
        await apply_project_agent_tool_settings(
            db,
            project,
            project_agent_id=project_agent_id,
            settings=[
                (setting.tool_id, setting.enabled, setting.config)
                for setting in source_member.settings.tools
            ],
        )
        # These are explicit project-local overrides submitted by the user,
        # not credentials copied from the source Agent. They affect only the
        # project Agent; source-Agent private credentials remain reference-only.
        for override in source_member.settings.mcp_server_overrides:
            db.add(
                MCPServerOverride(
                    mcp_server_id=override.server_id,
                    scope_type="agent",
                    scope_id=project_agent_id,
                    system_prompt_block=override.system_prompt_block,
                    url_template=override.url_template,
                    headers_template=override.headers_template,
                    credential_template=override.credential_template,
                    command_template=override.command_template,
                    args_template=override.args_template,
                    env_template=override.env_template,
                    last_modified_by_user_id=user.id,
                )
            )
        enabled_mcp_tool_ids = [
            setting.tool_id
            for setting in source_member.settings.tools
            if setting.enabled
        ]
        selected_mcp_server_ids = list(
            (
                await db.execute(
                    select(Tool.mcp_server_id).where(
                        Tool.id.in_(enabled_mcp_tool_ids),
                        Tool.type == "mcp",
                        Tool.mcp_server_id.is_not(None),
                    )
                )
            ).scalars()
        ) if enabled_mcp_tool_ids else []
        selected_mcp_server_id_set = set(selected_mcp_server_ids)
        for capability_type, capability_ids in (
            (
                "mcp",
                [
                    *source_member.settings.mcp_capability_ids,
                    *selected_mcp_server_ids,
                ],
            ),
            ("skill", source_member.settings.skill_capability_ids),
        ):
            for capability_id in dict.fromkeys(capability_ids):
                existing = (
                    await db.execute(
                        select(ProjectCapabilityBinding.id).where(
                            ProjectCapabilityBinding.project_id == project.id,
                            ProjectCapabilityBinding.capability_type == capability_type,
                            ProjectCapabilityBinding.capability_id == capability_id,
                            ProjectCapabilityBinding.inherited_from_agent_id == project_agent_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                capability = ProjectCapabilityCreate(
                    capability_type=capability_type,
                    capability_id=capability_id,
                    source="inherited",
                    inherited_from_agent_id=project_agent_id,
                    is_enabled=True,
                )
                if capability_type == "skill":
                    await bind_library_skill_to_project_agent(
                        db,
                        project,
                        skill_id=capability_id,
                        project_agent_id=project_agent_id,
                        is_enabled=True,
                        scope={},
                        actor_user_id=user.id,
                        actor_display_name=user.display_name,
                    )
                else:
                    binding = await add_capability(
                        db,
                        project,
                        capability,
                        actor_user_id=user.id,
                    )
                    if not (
                        capability_type == "mcp"
                        and capability_id in selected_mcp_server_id_set
                    ):
                        await sync_project_capability_assignment(db, project, binding)

    if data.visibility == "shared" and not data.shared_with_user_ids:
        raise HTTPException(status_code=422, detail="shared visibility requires shared_with_user_ids")
    await replace_access_grants(db, project, data.shared_with_user_ids, actor_user_id=user.id)
    # A project with Agents owns its canonical group root from creation time;
    # GET remains a safe fallback for projects created before this migration.
    if members:
        await ensure_project_group_session(db, project)
        await ensure_project_leader_session(db, project)
    add_event(db, project, "project.created", f"Created project {project.name}", actor_user_id=user.id)
    if project.status == "initializing":
        project.status = "planning"
    if project.status == "planning":
        add_event(
            db,
            project,
            "project.initialized",
            "Project snapshots and capability bindings are ready for project-owner planning",
            actor_user_id=user.id,
        )
    await db.flush()
    # PostgreSQL server-side timestamps are expired after the final UPDATE.
    # Refresh while we are still inside the async greenlet so response
    # serialization never triggers implicit synchronous IO (MissingGreenlet).
    await db.refresh(project)
    return project
