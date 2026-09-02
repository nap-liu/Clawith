from app.api.projects_shared import *  # noqa: F401,F403


@router.get("/templates")
async def list_project_templates(
    category: str | None = None,
    q: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ProjectTemplate).where(_visible_template_clause(current_user))
    if category:
        stmt = stmt.where(ProjectTemplate.category == category)
    if q:
        stmt = stmt.where(ProjectTemplate.name.ilike(f"%{q}%"))
    templates = (await db.execute(stmt.order_by(ProjectTemplate.created_at.desc()))).scalars().all()
    return [await _template_payload(db, template, current_user) for template in templates]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_project_template(
    data: ProjectTemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    definition = dict(data.definition)
    if "project_snapshot" in definition:
        raise HTTPException(
            status_code=422,
            detail="Final project assets can only be published from an owned project",
        )
    if "roles" in definition and not isinstance(definition["roles"], list):
        raise HTTPException(status_code=422, detail="项目模板角色配置必须为列表。")
    template_agents = definition.get("agents", [])
    try:
        sanitized_agents = sanitize_project_agent_template_assets(template_agents)
        if "agents" in definition:
            definition["agents"] = sanitized_agents
    except ProjectAgentTemplateAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    template = ProjectTemplate(
        tenant_id=_tenant_id(current_user),
        created_by_user_id=current_user.id,
        **data.model_dump(exclude={"definition"}),
        definition=definition,
    )
    db.add(template)
    await db.flush()
    return await _template_payload(db, template, current_user)


@router.get("/templates/{template_id}")
async def get_project_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _require_template(db, current_user, template_id)
    return await _template_payload(db, template, current_user)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _require_template(db, current_user, template_id, manage=True, lock=True)
    editor_projects = (
        (
            await db.execute(
                select(Project).where(
                    Project.settings["template_editor"]["template_id"].as_string() == str(template.id)
                )
            )
        )
        .scalars()
        .all()
    )
    for project in editor_projects:
        settings = dict(project.settings or {})
        settings.pop("template_editor", None)
        project.settings = settings
    await db.delete(template)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/bootstrap-options")
async def get_project_bootstrap_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.project_agent_service import (
        build_selectable_project_source_agents_query,
    )

    tenant_id = _tenant_id(current_user)
    cache_key = f"projects:bootstrap:v1:{tenant_id}:{current_user.id}"
    try:
        cached = await (await get_redis()).get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        logger.debug("Project bootstrap cache read failed; loading from the database")
    agents = (
        (
            await db.execute(
                build_selectable_project_source_agents_query(
                    current_user,
                    tenant_id=tenant_id,
                ).order_by(Agent.name)
            )
        )
        .scalars()
        .all()
    )
    platform_tools = (
        (
            await db.execute(
                select(Tool)
                .where(
                    Tool.enabled.is_(True),
                    Tool.source.in_(("builtin", "admin")),
                    or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                )
                .order_by(Tool.category, Tool.display_name, Tool.name)
            )
        )
        .scalars()
        .all()
    )
    agent_installed_mcp_tools = (
        (
            await db.execute(
                select(AgentTool, Tool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id.in_([agent.id for agent in agents]),
                    AgentTool.enabled.is_(True),
                    Tool.enabled.is_(True),
                    Tool.type == "mcp",
                    Tool.source == "agent",
                    or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                )
                .order_by(AgentTool.agent_id, Tool.mcp_server_name, Tool.display_name, Tool.name)
            )
        )
        .all()
    ) if agents else []
    users = (
        (
            await db.execute(
                select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True)).order_by(User.display_name)
            )
        )
        .scalars()
        .all()
    )
    capability_options = await load_project_capability_options(db, tenant_id, agents)
    selectable_mcp_names = {
        uuid.UUID(str(capability["capability_id"])): str(capability["name"])
        for capability in capability_options.capabilities
        if capability.get("type") == "mcp"
        and capability.get("capability_id")
        and capability.get("name")
    }
    payload = {
        "agents": [
            {
                "id": str(agent.id),
                "name": agent.name,
                "role_description": agent.role_description,
                "avatar_url": agent.avatar_url,
                "status": agent.status,
                "agent_type": agent.agent_type,
                "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
                "fallback_model_id": str(agent.fallback_model_id) if agent.fallback_model_id else None,
                "temperature": agent.temperature,
                "max_tool_rounds": agent.max_tool_rounds,
            }
            for agent in agents
        ],
        "tools": [
            {
                "id": str(tool.id),
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "category": tool.category,
                "type": tool.type,
                "icon": tool.icon,
                "source": tool.source,
                "agent_tool_source": None,
                "installed_by_agent_id": None,
                "config_schema": tool.config_schema or {},
                "agent_config": {},
                "mcp_server_id": str(tool.mcp_server_id) if tool.mcp_server_id else None,
                "mcp_server_name": selectable_mcp_names.get(
                    tool.mcp_server_id,
                    tool.mcp_server_name,
                ),
                "enabled": tool.type != "mcp" and (
                    tool_is_required(tool.name) or tool.name in PROJECT_AGENT_DEFAULT_TOOL_NAMES
                ),
                "can_disable": not tool_is_required(tool.name),
            }
            for tool in platform_tools
            if tool.type != "mcp"
            or (
                tool.mcp_server_id is not None
                and tool.mcp_server_id in capability_options.shared_mcp_ids
            )
        ] + [
            {
                "id": str(tool.id),
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "category": tool.category,
                "type": tool.type,
                "icon": tool.icon,
                "source": tool.source,
                "agent_tool_source": "user_installed",
                "installed_by_agent_id": str(assignment.agent_id),
                "config_schema": tool.config_schema or {},
                "agent_config": {},
                "mcp_server_id": str(tool.mcp_server_id) if tool.mcp_server_id else None,
                "mcp_server_name": selectable_mcp_names.get(
                    tool.mcp_server_id,
                    tool.mcp_server_name,
                ),
                "enabled": False,
                "can_disable": True,
            }
            for assignment, tool in agent_installed_mcp_tools
            if tool.mcp_server_id is not None
            and tool.mcp_server_id
            in capability_options.agent_mcp_ids.get(assignment.agent_id, frozenset())
        ],
        "users": [
            {
                "id": str(user.id),
                "name": user.display_name,
                "email": getattr(user, "email", None),
                "avatar_url": user.avatar_url,
            }
            for user in users
            if user.id != current_user.id
        ],
        "capabilities": [
            capability
            for capability in capability_options.capabilities
            if capability.get("type") == "skill"
        ],
    }
    try:
        await (await get_redis()).set(cache_key, json.dumps(payload, ensure_ascii=False), ex=30)
    except Exception:
        logger.debug("Project bootstrap cache write failed; returning database result")
    return payload


@router.post("/from-template", status_code=status.HTTP_201_CREATED)
async def create_project_from_template(
    data: ProjectFromTemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _create_project_from_template(data, current_user, db)


async def _create_project_from_template(
    data: ProjectFromTemplateCreate,
    current_user: User,
    db: AsyncSession,
    *,
    editor_template_id: uuid.UUID | None = None,
):
    template = await _require_template(db, current_user, data.template_id)
    allowed_override_keys = {"members", "capabilities", "shared_with_user_ids"}
    unknown_override_keys = set(data.overrides) - allowed_override_keys
    if unknown_override_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported project template overrides: {', '.join(sorted(unknown_override_keys))}",
        )
    stored_definition = dict(template.definition or {})
    definition = {**stored_definition, **data.overrides}
    template_agents = list(definition.get("agents") or [])
    if not template_agents:
        if "roles" in definition and not isinstance(definition["roles"], list):
            raise HTTPException(status_code=422, detail="项目模板角色配置必须为列表。")
        normalized_roles: list[tuple[str, str]] = []
        for role in list(definition.get("roles") or []):
            if isinstance(role, dict):
                role_name = str(role.get("name") or role.get("key") or "").strip()
                role_description = str(
                    role.get("description") or role.get("role_description") or ""
                ).strip()
            else:
                role_name = str(role).strip()
                role_description = ""
            if not role_name:
                continue
            normalized_roles.append(
                (
                    role_name[:200],
                    (role_description or f"负责{role_name}相关工作。")[:500],
                )
            )
        if not normalized_roles:
            normalized_roles = [("项目负责人", "负责项目整体协调与推进。")]
        template_agents = [
            {
                "name": role_name,
                "role_description": role_description,
                "is_leader": index == 0,
                "is_enabled": True,
            }
            for index, (role_name, role_description) in enumerate(normalized_roles)
        ]
    packaged_snapshot = stored_definition.get("project_snapshot")
    packaged_skill_assets = stored_definition.get("skill_assets", []) if packaged_snapshot is not None else []
    packaged_capabilities = stored_definition.get("capabilities", []) if packaged_snapshot is not None else []
    capabilities_overridden = "capabilities" in data.overrides
    template_capabilities_to_restore = (
        [
            item
            for item in packaged_capabilities
            if not capabilities_overridden or (isinstance(item, dict) and item.get("source") == "inherited")
        ]
        if packaged_snapshot is not None
        else list(definition.get("capabilities", []))
    )
    project_settings = dict(definition.get("settings", {}))
    if editor_template_id is not None:
        project_settings["template_editor"] = {"template_id": str(editor_template_id)}
    payload = ProjectCreate(
        name=data.name or template.name,
        description=data.description if data.description is not None else template.description,
        goal=definition.get("goal", definition.get("objective", "")),
        success_criteria=definition.get("success_criteria", []),
        visibility=data.visibility,
        template_id=template.id,
        settings=project_settings,
        members=definition.get("members", []),
        capabilities=definition.get("capabilities", []) if capabilities_overridden or packaged_snapshot is None else [],
        shared_with_user_ids=definition.get("shared_with_user_ids", []),
    )
    project = await create_project(
        db,
        current_user,
        payload,
        allow_template_agents=True,
    )
    try:
        await restore_project_template_files(
            project,
            packaged_snapshot,
            author_name=current_user.display_name,
            author_email=project_user_git_email(current_user.id),
        )
        created_agents = await instantiate_project_agents_from_template(
            db,
            project,
            current_user,
            template_agents,
        )
        if packaged_snapshot is not None:
            await instantiate_project_skills_from_template(
                db,
                project,
                current_user.display_name,
                current_user.id,
                packaged_skill_assets,
                [agent.id for agent, _member in created_agents],
            )
            await instantiate_project_capabilities_from_template(
                db,
                project,
                current_user,
                template_capabilities_to_restore,
                created_agents,
            )
    except Exception as exc:
        try:
            await remove_project_repository(project)
        except Exception:
            logger.exception("Failed to compensate project template storage for project {}", project.id)
        if isinstance(exc, (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise

    try:
        await ensure_project_group_session(db, project)
        await ensure_project_leader_session(db, project)
        explicit_leader_id = next(
            (member.agent_id for member in payload.members if member.is_leader),
            None,
        )
        if explicit_leader_id is not None:
            project_leader_id = (
                await db.execute(
                    select(Agent.id)
                    .where(
                        Agent.project_id == project.id,
                        Agent.tenant_id == project.tenant_id,
                        Agent.is_deleted.is_(False),
                        or_(
                            Agent.id == explicit_leader_id,
                            Agent.source_agent_id == explicit_leader_id,
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if project_leader_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Selected project owner is unavailable",
                )
            await db.execute(
                ProjectMemberSnapshot.__table__.update()
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .values(is_leader=False)
            )
            await db.execute(
                ProjectMemberSnapshot.__table__.update()
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id == project_leader_id,
                )
                .values(is_leader=True)
            )
            add_event(
                db,
                project,
                "leader.changed",
                "Applied the responsible person selected during template creation",
                actor_user_id=current_user.id,
                actor_agent_id=project_leader_id,
                metadata={"source": "template_override"},
            )
            await db.flush()
        git_state = await repository_state(project, limit=1)
        project.settings = {
            **dict(project.settings or {}),
            "git": {
                **dict(dict(project.settings or {}).get("git") or {}),
                "head": git_state.get("head"),
            },
        }
        if packaged_snapshot is not None:
            add_event(
                db,
                project,
                "project.template.restored",
                "Restored final project assets and digital employee configuration from a template",
                actor_user_id=current_user.id,
                metadata={
                    "template_id": str(template.id),
                    "file_count": len(packaged_snapshot.get("files", []))
                    if isinstance(packaged_snapshot, dict) and isinstance(packaged_snapshot.get("files"), list)
                    else 0,
                    "digital_employee_count": len(created_agents),
                    "skill_count": len(packaged_skill_assets)
                    if isinstance(packaged_skill_assets, list)
                    else 0,
                },
            )
        await db.flush()
        await db.refresh(project)
        summary = await project_summary(db, project, actor_user_id=current_user.id)
        restored_files = (
            packaged_snapshot.get("files", [])
            if isinstance(packaged_snapshot, dict) and isinstance(packaged_snapshot.get("files"), list)
            else []
        )
        portable_capabilities = [item for item in template_capabilities_to_restore if isinstance(item, dict)]
        restored_tool_ids = {
            str(item.get("capability_id"))
            for item in portable_capabilities
            if item.get("capability_type") == "tool" and item.get("capability_id")
        }
        restored_connection_ids = {
            str(item.get("capability_id"))
            for item in portable_capabilities
            if item.get("capability_type") == "mcp" and item.get("capability_id")
        }
        summary["template_setup_summary"] = {
            "restored_file_count": len(restored_files),
            "restored_digital_employee_count": len(created_agents),
            "restored_skill_count": len(packaged_skill_assets) if isinstance(packaged_skill_assets, list) else 0,
            "restored_connection_count": len(restored_connection_ids),
            "restored_tool_count": len(restored_tool_ids),
        }
        # Project rows and their managed repository form one product-level
        # creation boundary. Commit here so a database commit failure can still
        # compensate the repository before the request dependency exits.
        await db.commit()
        return summary
    except Exception:
        await db.rollback()
        try:
            await remove_project_repository(project)
        except Exception:
            logger.exception("Failed to compensate project template storage for project {}", project.id)
        raise
