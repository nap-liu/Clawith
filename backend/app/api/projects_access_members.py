from app.api.projects_shared import *  # noqa: F401,F403

@router.get("/{project_id}/access-grants", response_model=list[ProjectAccessGrantOut])
async def list_access_grants(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    return (
        (
            await db.execute(
                select(ProjectAccessGrant).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )


@router.post("/{project_id}/access-grants", response_model=ProjectAccessGrantOut, status_code=201)
async def create_access_grant(
    project_id: uuid.UUID,
    data: ProjectAccessGrantCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    user = (
        await db.execute(
            select(User).where(User.id == data.user_id, User.tenant_id == project.tenant_id, User.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if user is None or user.id == project.owner_user_id:
        raise HTTPException(status_code=422, detail="Shared user must be another active user in the project tenant")
    grant = (
        await db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.user_id == user.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if grant:
        grant.role = data.role
    else:
        grant = ProjectAccessGrant(
            tenant_id=project.tenant_id,
            project_id=project.id,
            user_id=user.id,
            role=data.role,
            created_by_user_id=current_user.id,
        )
        db.add(grant)
    project.visibility = "shared"
    add_event(db, project, "project.shared", f"Shared project with {user.display_name}", actor_user_id=current_user.id)
    await db.flush()
    return grant


@router.delete("/{project_id}/access-grants/{grant_id}")
async def delete_access_grant(
    project_id: uuid.UUID,
    grant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id, lock=True)
    grant = (
        await db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.id == grant_id,
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=404, detail="Access grant not found")
    previous_execution_user_id = project_execution_user_id(project)
    remaining = (
        await db.execute(
            select(func.count(ProjectAccessGrant.id)).where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.id != grant.id,
            )
        )
    ).scalar_one()
    if grant.user_id == previous_execution_user_id and remaining:
        raise HTTPException(
            status_code=409,
            detail="The current project execution user must be changed before removing their access",
        )
    await db.execute(
        delete(ProjectAccessGrant).where(
            ProjectAccessGrant.id == grant_id,
            ProjectAccessGrant.project_id == project.id,
            ProjectAccessGrant.tenant_id == project.tenant_id,
        )
    )
    if remaining == 0:
        project.visibility = "private"
        project.execution_user_id = None
        if previous_execution_user_id != project.owner_user_id:
            add_event(
                db,
                project,
                "project.execution_user.changed",
                "Changed the project execution user",
                actor_user_id=current_user.id,
                metadata={
                    "previous_execution_user_id": str(previous_execution_user_id),
                    "execution_user_id": str(project.owner_user_id),
                    "automatic_fallback": True,
                },
            )
    add_event(db, project, "project.unshared", "Removed a project access grant", actor_user_id=current_user.id)
    return {"ok": True}


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_project_members(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    return (
        (
            await db.execute(
                select(ProjectMemberSnapshot)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectMemberSnapshot.is_leader.desc(), ProjectMemberSnapshot.created_at)
            )
        )
        .scalars()
        .all()
    )


@router.get("/{project_id}/agents", response_model=list[ProjectAgentOut])
async def list_project_agents(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the project-owned Agents visible to every project participant."""

    project = await require_project(db, current_user, project_id)
    records = await list_project_agent_records(db, project)
    return [await serialize_project_agent(project, agent, member) for agent, member in records]


@router.post("/{project_id}/agents", response_model=ProjectAgentOut, status_code=201)
async def create_project_owned_agent(
    project_id: uuid.UUID,
    data: ProjectAgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create or copy an Agent as a project-owned asset."""

    project = await require_owner(db, current_user, project_id)
    agent, member = await create_project_agent(db, project, current_user, data)
    await db.commit()
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.get("/{project_id}/agents/{agent_id}", response_model=ProjectAgentOut)
async def get_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    return await serialize_project_agent(project, agent, member)


@router.patch("/{project_id}/agents/{agent_id}", response_model=ProjectAgentOut)
async def patch_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    await update_project_agent(db, project, current_user, agent, member, data)
    await db.commit()
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.post("/{project_id}/agents/{agent_id}/deactivate", response_model=ProjectAgentOut)
async def deactivate_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.subagent_runtime import finalize_cancelled_project_member_turns

    project = await require_owner(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    child_ids = await deactivate_project_agent(
        db,
        project,
        current_user,
        agent,
        member,
        reason=data.reason if data else None,
    )
    await db.commit()
    await finalize_cancelled_project_member_turns(project.id, child_ids)
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.post("/{project_id}/agents/{agent_id}/restore", response_model=ProjectAgentOut)
async def restore_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    await restore_project_agent(
        db,
        project,
        current_user,
        agent,
        member,
        reason=data.reason if data else None,
    )
    await db.commit()
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.post("/{project_id}/agents/{agent_id}/promote", response_model=ProjectAgentPromotionOut, status_code=201)
async def promote_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentPromoteRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    agent, _member = await get_project_agent_record(db, project, agent_id)
    promoted = await promote_project_agent(
        db,
        project,
        current_user,
        agent,
        name=data.name if data else None,
    )
    await db.commit()
    await db.refresh(promoted)
    return {
        "id": promoted.id,
        "name": promoted.name,
        "role_description": promoted.role_description or "",
        "source_project_id": project.id,
        "source_project_agent_id": agent.id,
    }


@router.post("/{project_id}/members", response_model=ProjectMemberOut, status_code=201)
async def create_project_member(
    project_id: uuid.UUID,
    data: ProjectMemberCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    member = await add_member(db, project, data, actor_user_id=current_user.id)
    await reconcile_project_repository_operations(project.id, db=db)
    return member


async def _load_project_member(
    db: AsyncSession,
    project: Project,
    member_id: uuid.UUID,
) -> ProjectMemberSnapshot:
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.id == member_id,
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Project member not found")
    return member


@router.post("/{project_id}/members/{member_id}/remove", response_model=ProjectMemberOut)
async def remove_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    data: ProjectMemberLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-remove a member while retaining every historical project record."""

    from app.services.subagent_runtime import finalize_cancelled_project_member_turns

    project = await require_project(db, current_user, project_id, edit=True)
    member = await _load_project_member(db, project, member_id)
    await _guard_project_agent_member_mutation(db, current_user, project, member)
    child_ids = await deactivate_project_member(
        db,
        project,
        member,
        actor_user_id=current_user.id,
        reason=data.reason if data else None,
    )
    await db.commit()
    await finalize_cancelled_project_member_turns(project.id, child_ids)
    await db.refresh(member)
    return member


@router.post("/{project_id}/members/{member_id}/restore", response_model=ProjectMemberOut)
async def restore_removed_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    data: ProjectMemberLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Explicitly restore the same project-local member snapshot."""

    project = await require_project(db, current_user, project_id, edit=True)
    member = await _load_project_member(db, project, member_id)
    await _guard_project_agent_member_mutation(db, current_user, project, member)
    await restore_project_member(
        db,
        project,
        member,
        actor_user_id=current_user.id,
        reason=data.reason if data else None,
    )
    await db.commit()
    await db.refresh(member)
    return member


@router.patch("/{project_id}/members/{member_id}", response_model=ProjectMemberOut)
async def patch_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    data: ProjectMemberUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    member = await _load_project_member(db, project, member_id)
    updates = data.model_dump(exclude_unset=True)
    requested_enabled = updates.pop("is_enabled", None)
    requested_leader = updates.pop("is_leader", None)
    if requested_enabled is False and requested_leader is True:
        raise HTTPException(status_code=422, detail="A departed member cannot become project owner")
    if "config_snapshot" in updates:
        from app.services.project_member_runtime import merge_project_member_runtime_config

        member.config_snapshot = await merge_project_member_runtime_config(
            db,
            project,
            member,
            updates.pop("config_snapshot"),
        )
    if requested_enabled is False:
        from app.services.subagent_runtime import finalize_cancelled_project_member_turns

        child_ids = await deactivate_project_member(
            db,
            project,
            member,
            actor_user_id=current_user.id,
            reason="member_patch_disable",
        )
        await db.commit()
        await finalize_cancelled_project_member_turns(project.id, child_ids)
    elif requested_enabled is True:
        await restore_project_member(
            db,
            project,
            member,
            actor_user_id=current_user.id,
            reason="member_patch_restore",
        )
    if requested_leader is False and member.is_leader:
        raise HTTPException(
            status_code=422,
            detail="Assign another enabled project owner instead of clearing responsibility",
        )
    if requested_leader is True:
        if not member.is_enabled:
            raise HTTPException(status_code=422, detail="Project owner must be an active project member")
        await db.execute(
            ProjectMemberSnapshot.__table__.update()
            .where(ProjectMemberSnapshot.project_id == project.id)
            .values(is_leader=False)
        )
        member.is_leader = True
    add_event(
        db,
        project,
        "member.snapshot.updated",
        f"Updated snapshot for {member.name_snapshot}",
        actor_user_id=current_user.id,
        actor_agent_id=member.agent_id,
    )
    await db.flush()
    await db.refresh(member)
    return member


async def _project_member_tools_payload(
    db: AsyncSession,
    project: Project,
    member: ProjectMemberSnapshot,
) -> list[dict]:
    from app.api.tools import (
        _agent_visible_tool_clause,
        _globally_visible_tool_clause,
        _load_agent_tool_assignments,
        _tool_availability,
        _tool_record_visible_to_agent,
    )
    from app.services.agent_tools import _agent_has_feishu
    from app.services.tool_enablement import resolved_agent_tool_enabled, tool_is_required

    agent = await db.get(Agent, member.agent_id)
    if agent is None or agent.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member Agent was not found")
    assignments = await _load_agent_tool_assignments(db, agent.id)
    tools = (
        await db.execute(
            select(Tool)
            .where(
                _globally_visible_tool_clause(),
                _agent_visible_tool_clause(agent.tenant_id, assignments),
            )
            .order_by(Tool.category, Tool.name)
        )
    ).scalars().all()
    has_feishu = await _agent_has_feishu(agent.id)
    config = dict(member.config_snapshot or {})
    enabled_overrides = {str(name) for name in config.get("enabled_platform_tools", [])}
    disabled_overrides = {str(name) for name in config.get("disabled_platform_tools", [])}
    is_project_agent = agent.scope == "project" and agent.project_id == project.id
    result: list[dict] = []
    for tool in tools:
        if tool.category == "feishu" and not has_feishu:
            continue
        if (tool.config or {}).get("okr_agent_only") and not agent.is_system:
            continue
        assignment = assignments.get(str(tool.id))
        if not _tool_record_visible_to_agent(tool, agent.tenant_id, assignments):
            continue
        base_enabled = resolved_agent_tool_enabled(tool.name, assignment)
        enabled = (
            base_enabled
            if is_project_agent
            else tool_is_required(tool.name)
            or tool.name in enabled_overrides
            or (base_enabled and tool.name not in disabled_overrides)
        )
        result.append(
            {
                "id": str(tool.id),
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "type": tool.type,
                "category": tool.category,
                "icon": tool.icon,
                "enabled": enabled,
                "is_default": tool.is_default,
                "mcp_server_name": tool.mcp_server_name,
                "mcp_server_url": tool.mcp_server_url,
                "mcp_server_id": str(tool.mcp_server_id) if tool.mcp_server_id else None,
                "source": tool.source,
                **_tool_availability(tool.name),
            }
        )
    return result


@router.get("/{project_id}/members/{member_id}/tools")
async def get_project_member_tools(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    member = await _load_project_member(db, project, member_id)
    return await _project_member_tools_payload(db, project, member)


@router.put("/{project_id}/members/{member_id}/tools")
async def put_project_member_tools(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    updates: list[ProjectMemberToolUpdate],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.tools import (
        _agent_visible_tool_clause,
        _load_agent_tool_assignments,
        _tool_record_visible_to_agent,
    )
    from app.services.project_member_runtime import (
        merge_project_member_runtime_config,
        sync_project_agent_mcp_bindings,
        sync_project_agent_tool_bindings,
    )
    from app.services.tool_enablement import tool_is_required

    project = await require_owner(db, current_user, project_id)
    member = await _load_project_member(db, project, member_id)
    if not member.is_enabled:
        raise HTTPException(status_code=409, detail="Departed project members cannot change tools")
    agent = await db.get(Agent, member.agent_id)
    if agent is None or agent.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member Agent was not found")
    assignments = await _load_agent_tool_assignments(db, agent.id)
    requested_ids = {update.tool_id for update in updates}
    tools = (
        await db.execute(
            select(Tool).where(
                Tool.id.in_(requested_ids),
                _agent_visible_tool_clause(agent.tenant_id, assignments),
            )
        )
    ).scalars().all()
    tool_by_id = {tool.id: tool for tool in tools}
    if set(tool_by_id) != requested_ids or any(
        not _tool_record_visible_to_agent(tool, agent.tenant_id, assignments)
        for tool in tools
    ):
        raise HTTPException(status_code=404, detail="Tool was not found for this project member")
    for update in updates:
        tool = tool_by_id[update.tool_id]
        if not update.enabled and tool_is_required(tool.name):
            raise HTTPException(status_code=409, detail="Required tools cannot be disabled")

    if agent.scope == "project" and agent.project_id == project.id:
        affected_mcp_server_ids: set[uuid.UUID] = set()
        affected_tool_ids: set[uuid.UUID] = set()
        for update in updates:
            tool = tool_by_id[update.tool_id]
            assignment = assignments.get(str(update.tool_id))
            if assignment is None:
                assignment = AgentTool(
                    agent_id=agent.id,
                    tool_id=update.tool_id,
                    enabled=update.enabled,
                    source="user_installed",
                )
                db.add(assignment)
            else:
                assignment.enabled = update.enabled
            if tool.type == "mcp" and tool.mcp_server_id is not None:
                affected_mcp_server_ids.add(tool.mcp_server_id)
            elif tool.type != "mcp":
                affected_tool_ids.add(tool.id)
        await db.flush()
        await sync_project_agent_tool_bindings(
            db,
            project,
            project_agent_id=agent.id,
            tool_ids=affected_tool_ids,
        )
        await sync_project_agent_mcp_bindings(
            db,
            project,
            project_agent_id=agent.id,
            server_ids=affected_mcp_server_ids,
        )
    else:
        config = dict(member.config_snapshot or {})
        enabled = {str(name) for name in config.get("enabled_platform_tools", [])}
        disabled = {str(name) for name in config.get("disabled_platform_tools", [])}
        for update in updates:
            name = tool_by_id[update.tool_id].name
            if update.enabled:
                enabled.add(name)
                disabled.discard(name)
            else:
                disabled.add(name)
                enabled.discard(name)
        member.config_snapshot = await merge_project_member_runtime_config(
            db,
            project,
            member,
            {
                **config,
                "enabled_platform_tools": sorted(enabled),
                "disabled_platform_tools": sorted(disabled),
            },
        )
    add_event(
        db,
        project,
        "member.tools.updated",
        f"Updated tools for {member.name_snapshot}",
        actor_user_id=current_user.id,
        actor_agent_id=member.agent_id,
    )
    await db.commit()
    return await _project_member_tools_payload(db, project, member)


@router.put("/{project_id}/leader", response_model=ProjectMemberOut)
async def put_project_leader(
    project_id: uuid.UUID,
    data: LeaderUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == data.agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=422, detail="Project owner must be an enabled project member")
    await _guard_project_agent_member_mutation(db, current_user, project, member)
    await db.execute(
        ProjectMemberSnapshot.__table__.update()
        .where(ProjectMemberSnapshot.project_id == project.id)
        .values(is_leader=False)
    )
    member.is_leader = True
    add_event(
        db,
        project,
        "leader.changed",
        f"Assigned {member.name_snapshot} as project leader",
        actor_user_id=current_user.id,
        actor_agent_id=member.agent_id,
    )
    await db.flush()
    await db.refresh(member)
    return member
