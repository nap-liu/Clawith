from app.api.projects_shared import *  # noqa: F401,F403

@router.get("/{project_id}/capabilities", response_model=list[CapabilityOut])
async def list_project_capabilities(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    bindings = list(
        (
            await db.execute(
                select(ProjectCapabilityBinding)
                .where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                )
                .order_by(ProjectCapabilityBinding.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [await serialize_project_capability(db, project, binding) for binding in bindings]


@router.post("/{project_id}/capabilities/skill-backfill")
async def project_skill_backfill(
    project_id: uuid.UUID,
    data: ProjectSkillBackfillRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview, apply, or roll back the safe normalization of legacy Skill bindings."""

    project = await require_owner(db, current_user, project_id)
    if data.action == "dry_run":
        summary, _plans = await plan_project_skill_backfill(db, project)
        return {"action": "dry_run", "operation_id": None, **summary}
    if data.action == "apply":
        result, rollback_entries = await apply_project_skill_backfill(
            db,
            project,
            actor_user_id=current_user.id,
            actor_display_name=current_user.display_name,
        )
        if not rollback_entries:
            return {"action": "apply", "operation_id": None, **result}
        event = add_event(
            db,
            project,
            "capability.skill_backfill.applied",
            f"Normalized {len(rollback_entries)} legacy project Skill bindings",
            actor_user_id=current_user.id,
            metadata={
                "applied_count": len(rollback_entries),
                "rollback_entries": rollback_entries,
            },
        )
        await db.flush()
        return {"action": "apply", "operation_id": str(event.id), **result}

    operation = (
        await db.execute(
            select(ProjectEvent).where(
                ProjectEvent.id == data.operation_id,
                ProjectEvent.project_id == project.id,
                ProjectEvent.tenant_id == project.tenant_id,
                ProjectEvent.event_type == "capability.skill_backfill.applied",
            )
        )
    ).scalar_one_or_none()
    if operation is None:
        raise HTTPException(status_code=404, detail="Project Skill backfill operation was not found")
    rollback_events = list(
        (
            await db.execute(
                select(ProjectEvent).where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_type == "capability.skill_backfill.rolled_back",
                )
            )
        ).scalars()
    )
    if any(
        str(event.event_metadata.get("operation_id")) == str(operation.id)
        for event in rollback_events
        if isinstance(event.event_metadata, dict)
    ):
        raise HTTPException(status_code=409, detail="Project Skill backfill was already rolled back")
    result = await rollback_project_skill_backfill(
        db,
        project,
        dict(operation.event_metadata or {}).get("rollback_entries"),
        actor_user_id=current_user.id,
        actor_display_name=current_user.display_name,
    )
    rollback_event = add_event(
        db,
        project,
        "capability.skill_backfill.rolled_back",
        f"Rolled back {result['rolled_back_count']} project Skill bindings",
        actor_user_id=current_user.id,
        metadata={
            "operation_id": str(operation.id),
            "rolled_back_count": result["rolled_back_count"],
            "binding_ids": result["binding_ids"],
        },
    )
    await db.flush()
    return {
        "action": "rollback",
        "operation_id": str(operation.id),
        "rollback_event_id": str(rollback_event.id),
        **result,
    }


@router.post("/{project_id}/capabilities", response_model=CapabilityOut, status_code=201)
async def create_project_capability(
    project_id: uuid.UUID,
    data: ProjectCapabilityCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    if data.capability_type in {"mcp", "skill"}:
        if data.capability_id is None:
            raise HTTPException(status_code=422, detail="A registered capability is required")
        source_agent = None
        if data.source == "inherited" and data.inherited_from_agent_id is not None:
            project_agent = await db.get(Agent, data.inherited_from_agent_id)
            if (
                project_agent is not None
                and project_agent.tenant_id == project.tenant_id
                and project_agent.project_id == project.id
                and project_agent.scope == "project"
                and not project_agent.is_deleted
                and project_agent.source_agent_id is not None
            ):
                source_agent = await db.get(Agent, project_agent.source_agent_id)
                if (
                    source_agent is None
                    or source_agent.tenant_id != project.tenant_id
                    or source_agent.scope != "standard"
                    or source_agent.is_deleted
                ):
                    source_agent = None
        options = await load_project_capability_options(
            db,
            project.tenant_id,
            [source_agent] if source_agent is not None else [],
        )
        allowed = (
            options.allows(source_agent.id, data.capability_type, data.capability_id)
            if data.source == "inherited" and source_agent is not None
            else options.allows_shared(data.capability_type, data.capability_id)
        )
        if not allowed:
            raise HTTPException(status_code=422, detail="Selected project capability is unavailable")
    if data.capability_type == "skill":
        if data.source != "inherited":
            raise HTTPException(status_code=422, detail="Project Skills must belong to a project digital employee")
        binding = await bind_library_skill_to_project_agent(
            db,
            project,
            skill_id=data.capability_id,
            project_agent_id=data.inherited_from_agent_id,
            is_enabled=data.is_enabled,
            scope=data.scope,
            actor_user_id=current_user.id,
            actor_display_name=current_user.display_name,
        )
        add_event(
            db,
            project,
            "capability.bound",
            f"Bound skill capability {binding.capability_name}",
            actor_user_id=current_user.id,
            actor_agent_id=binding.inherited_from_agent_id,
            metadata={"binding_id": str(binding.id), "source": binding.source},
        )
    else:
        binding = await add_capability(db, project, data, actor_user_id=current_user.id)
        from app.services.project_member_runtime import sync_project_capability_assignment

        await sync_project_capability_assignment(db, project, binding)
    await db.refresh(binding)
    return await serialize_project_capability(db, project, binding)


@router.get("/{project_id}/capabilities/{binding_id}/delete-impact")
async def get_project_skill_delete_impact(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    return await project_skill_deletion_impact(db, project, binding)


@router.delete("/{project_id}/capabilities/{binding_id}")
async def delete_project_skill_capability(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    confirm: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    impact = await project_skill_deletion_impact(db, project, binding)
    if not confirm:
        raise HTTPException(
            status_code=409,
            detail={"code": "project_skill_delete_confirmation_required", **impact},
        )
    deleted = await delete_project_skill_asset(
        db,
        project,
        binding,
        actor_user_id=current_user.id,
        actor_display_name=current_user.display_name,
    )
    add_event(
        db,
        project,
        "capability.deleted",
        f"Deleted project Skill {deleted['skill_name']}",
        actor_user_id=current_user.id,
        metadata={
            "asset_id": deleted["asset_id"],
            "affected_member_count": deleted["affected_member_count"],
        },
    )
    return {"ok": True, **deleted}


@router.post("/{project_id}/capabilities/{binding_id}/refresh")
async def refresh_project_skill_capability(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    result = await refresh_project_skill_asset(
        db,
        project,
        binding,
        actor_user_id=current_user.id,
        actor_display_name=current_user.display_name,
    )
    add_event(
        db,
        project,
        "capability.refreshed",
        f"Refreshed project Skill {binding.capability_name}",
        actor_user_id=current_user.id,
        metadata={
            "asset_id": result["asset_id"],
            "affected_member_count": result["affected_member_count"],
            "changed": result["changed"],
        },
    )
    await db.flush()
    await db.refresh(binding)
    return {**result, "capability": await serialize_project_capability(db, project, binding)}


@router.patch("/{project_id}/capabilities/{binding_id}", response_model=CapabilityOut)
async def patch_project_capability(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    data: CapabilityUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    updates = data.model_dump(exclude_unset=True)
    if binding.capability_type == "skill":
        if "config" in updates:
            raise HTTPException(status_code=422, detail="Project Skill asset metadata cannot be edited directly")
        if "is_enabled" in updates and updates["is_enabled"] is not None:
            await set_project_skill_enabled(
                db,
                project,
                binding,
                enabled=updates.pop("is_enabled"),
                actor_user_id=current_user.id,
                actor_display_name=current_user.display_name,
            )
        for key, value in updates.items():
            setattr(binding, key, value)
    else:
        for key, value in updates.items():
            setattr(binding, key, value)
        from app.services.project_member_runtime import sync_project_capability_assignment

        await sync_project_capability_assignment(db, project, binding)
    add_event(
        db,
        project,
        "capability.updated",
        f"Updated capability {binding.capability_name}",
        actor_user_id=current_user.id,
        metadata={"binding_id": str(binding.id), "enabled": binding.is_enabled},
    )
    await db.flush()
    await db.refresh(binding)
    return await serialize_project_capability(db, project, binding)


async def _project_capability_binding(
    db: AsyncSession,
    project: Project,
    binding_id: uuid.UUID,
) -> ProjectCapabilityBinding:
    binding = (
        await db.execute(
            select(ProjectCapabilityBinding).where(
                ProjectCapabilityBinding.id == binding_id,
                ProjectCapabilityBinding.project_id == project.id,
                ProjectCapabilityBinding.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise HTTPException(status_code=404, detail="Capability binding not found")
    return binding
