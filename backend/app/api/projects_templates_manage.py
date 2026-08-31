from app.api.projects_shared import *  # noqa: F401,F403
from app.api.projects_bootstrap import _create_project_from_template

@router.post("/templates/{template_id}/editor", status_code=status.HTTP_201_CREATED)
async def create_project_template_editor(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an isolated, hidden project for editing one manageable template."""

    template = await _require_template(db, current_user, template_id, manage=True)
    return await _create_project_from_template(
        ProjectFromTemplateCreate(template_id=template.id),
        current_user,
        db,
        editor_template_id=template.id,
    )


@router.get("")
async def list_projects(
    scope: str = Query("mine", pattern="^(mine|shared|running|archived|all)$"),
    q: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Project).where(
        accessible_projects_clause(current_user),
        func.coalesce(Project.settings["template_editor"]["template_id"].as_string(), "") == "",
    )
    if scope == "mine":
        stmt = stmt.where(Project.owner_user_id == current_user.id)
    elif scope == "shared":
        stmt = stmt.where(Project.owner_user_id != current_user.id)
    elif scope == "running":
        stmt = stmt.where(Project.status == "running")
    elif scope == "archived":
        stmt = stmt.where(Project.status == "archived")
    if status_filter:
        stmt = stmt.where(Project.status == status_filter)
    elif scope != "archived":
        stmt = stmt.where(Project.status != "archived")
    if q:
        stmt = stmt.where(or_(Project.name.ilike(f"%{q}%"), Project.goal.ilike(f"%{q}%")))
    projects = (await db.execute(stmt.order_by(Project.updated_at.desc()))).scalars().all()
    return [await project_summary(db, project, actor_user_id=current_user.id) for project in projects]


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await create_project(db, current_user, data)
    try:
        summary = await project_summary(db, project, actor_user_id=current_user.id)
        await db.commit()
        return summary
    except Exception:
        await db.rollback()
        try:
            await remove_project_repository(project)
        except Exception:
            logger.exception("Failed to compensate project storage for project {}", project.id)
        raise


@router.post("/{project_id}/templates", status_code=status.HTTP_201_CREATED)
async def create_template_from_project(
    project_id: uuid.UUID,
    data: ProjectTemplateFromProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publish a sanitized project template, including project-owned Agents."""

    project = await require_owner(db, current_user, project_id)
    # Serialize publication for one source project. The immutable package and
    # its definition live in the same database row, so request rollback keeps
    # both invisible and a retry can safely return the completed publication.
    await db.refresh(project, with_for_update=True)
    try:
        definition, snapshot = await _build_project_template_definition(
            db,
            project,
            included_skill_binding_ids=data.included_skill_binding_ids,
        )
    except (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    template_agents = definition["agents"]
    publication_key = hashlib.sha256(
        "\x1f".join(
            (
                str(project.tenant_id),
                str(project.id),
                snapshot.head,
                str(data.name or project.name),
                str(data.description if data.description is not None else project.description or ""),
                data.category,
                data.version,
                "published" if data.is_published else "draft",
                ",".join(sorted(str(value) for value in data.included_skill_binding_ids)),
            )
        ).encode("utf-8")
    ).hexdigest()
    existing_templates = (
        (
            await db.execute(
                select(ProjectTemplate).where(
                    ProjectTemplate.tenant_id == project.tenant_id,
                    ProjectTemplate.created_by_user_id == current_user.id,
                    ProjectTemplate.name == (data.name or project.name),
                    ProjectTemplate.category == data.category,
                    ProjectTemplate.version == data.version,
                    ProjectTemplate.is_published == data.is_published,
                )
            )
        )
        .scalars()
        .all()
    )
    existing = next(
        (
            item
            for item in existing_templates
            if isinstance(item.definition, dict) and item.definition.get("_publication_key") == publication_key
        ),
        None,
    )
    if existing is not None:
        return await _template_payload(db, existing, current_user)
    definition["_publication_key"] = publication_key
    template = ProjectTemplate(
        tenant_id=project.tenant_id,
        created_by_user_id=current_user.id,
        name=data.name or project.name,
        description=data.description if data.description is not None else project.description,
        category=data.category,
        version=data.version,
        is_published=data.is_published,
        definition=definition,
    )
    db.add(template)
    await db.flush()
    add_event(
        db,
        project,
        "project.template.published",
        "Published final project assets and digital employee configuration as a template",
        actor_user_id=current_user.id,
        metadata={
            "template_id": str(template.id),
            "source_head": snapshot.head,
            "file_count": len(snapshot.project_files["files"]),
            "digital_employee_count": len(template_agents),
            "skill_count": len(definition["skill_assets"]),
            "excluded_file_count": snapshot.project_files["excluded_file_count"],
        },
    )
    await db.flush()
    await db.refresh(template)
    return await _template_payload(db, template, current_user)


@router.put("/templates/{template_id}/from-project/{project_id}")
async def update_template_from_project(
    template_id: uuid.UUID,
    project_id: uuid.UUID,
    data: ProjectTemplateFromProjectUpdate | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace a manageable template definition with one project snapshot."""

    template = await _require_template(db, current_user, template_id, manage=True, lock=True)
    project = await require_owner(db, current_user, project_id)
    await db.refresh(project, with_for_update=True)
    if (
        not is_platform_admin_user(current_user)
        and template.tenant_id is not None
        and template.tenant_id != project.tenant_id
    ):
        raise HTTPException(status_code=404, detail="Project template not found")
    update = data or ProjectTemplateFromProjectUpdate()
    try:
        definition, snapshot = await _build_project_template_definition(
            db,
            project,
            included_skill_binding_ids=update.included_skill_binding_ids,
        )
    except (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    next_name = update.name if update.name is not None else template.name
    next_description = update.description if update.description is not None else template.description
    next_category = update.category if update.category is not None else template.category
    next_version = update.version if update.version is not None else template.version
    next_published = update.is_published if update.is_published is not None else template.is_published
    definition["_publication_key"] = hashlib.sha256(
        "\x1f".join(
            (
                str(template.id),
                str(project.id),
                snapshot.head,
                next_name,
                next_description or "",
                next_category,
                next_version,
                "published" if next_published else "draft",
                ",".join(sorted(str(value) for value in update.included_skill_binding_ids)),
            )
        ).encode("utf-8")
    ).hexdigest()
    template.name = next_name
    template.description = next_description
    template.category = next_category
    template.version = next_version
    template.is_published = next_published
    template.definition = definition
    add_event(
        db,
        project,
        "project.template.updated",
        "Updated a project template from the current project snapshot",
        actor_user_id=current_user.id,
        metadata={
            "template_id": str(template.id),
            "source_head": snapshot.head,
            "file_count": len(snapshot.project_files["files"]),
            "digital_employee_count": len(definition["agents"]),
            "skill_count": len(definition["skill_assets"]),
            "excluded_file_count": snapshot.project_files["excluded_file_count"],
        },
    )
    await db.flush()
    await db.refresh(template)
    return await _template_payload(db, template, current_user)


@router.get("/{project_id}/template-manifest")
async def get_project_template_manifest(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview the exact safe assets that project-template publication will include."""

    project = await require_owner(db, current_user, project_id)
    try:
        definition, _snapshot = await _build_project_template_definition(db, project)
    except (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = public_template_definition(definition)
    skill_rows = await project_skill_manifest(db, project)
    skill_bindings = {
        binding.id: binding
        for binding in (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.capability_type == "skill",
                )
            )
        ).scalars()
    }
    result["skills"] = []
    for item in skill_rows:
        binding = skill_bindings[uuid.UUID(item["binding_id"])]
        impact = await project_skill_deletion_impact(db, project, binding)
        result["skills"].append(
            {
                "binding_id": item["binding_id"],
                "member_id": item["member_id"],
                "member_agent_id": item["member_agent_id"],
                "member_name": item["member_name"],
                "member_role": item["member_role"],
                "name": item["name"],
                "version": item["version"],
                "selected": False,
                "selection_state": "unselected",
                "is_enabled": item["is_enabled"],
                "file_count": item["file_count"],
                "size_bytes": item["size_bytes"],
                "affected_members": impact["affected_members"],
                "affected_member_count": impact["affected_member_count"],
            }
        )
    member_rows = list(
        (
            await db.execute(
                select(ProjectMemberSnapshot, Agent)
                .join(Agent, Agent.id == ProjectMemberSnapshot.agent_id)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    Agent.tenant_id == project.tenant_id,
                    Agent.is_deleted.is_(False),
                )
                .order_by(
                    ProjectMemberSnapshot.is_leader.desc(),
                    ProjectMemberSnapshot.created_at,
                    ProjectMemberSnapshot.id,
                )
            )
        ).all()
    )
    members = [member for member, _agent in member_rows]
    capability_bindings = list(
        (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.capability_type.in_(["tool", "mcp"]),
                )
            )
        ).scalars()
    )
    binding_ids: dict[tuple[str, uuid.UUID], list[str]] = {}
    for binding in capability_bindings:
        if binding.capability_id is not None:
            binding_ids.setdefault((binding.capability_type, binding.capability_id), []).append(str(binding.id))

    grouped_capabilities: dict[tuple[str, uuid.UUID], dict] = {}
    for item in definition.get("capabilities", []):
        if not isinstance(item, dict) or item.get("capability_type") not in {"tool", "mcp"}:
            continue
        try:
            capability_id = uuid.UUID(str(item.get("capability_id")))
        except ValueError:
            continue
        key = (str(item["capability_type"]), capability_id)
        grouped = grouped_capabilities.setdefault(
            key,
            {
                "name": str(item.get("capability_name") or ""),
                "is_enabled": False,
                "member_indexes": set(),
            },
        )
        item_enabled = bool(item.get("is_enabled", True))
        grouped["is_enabled"] = grouped["is_enabled"] or item_enabled
        if not item_enabled:
            continue
        if item.get("source") == "shared":
            grouped["member_indexes"].update(range(len(members)))
        else:
            index = item.get("digital_employee_index")
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(members):
                grouped["member_indexes"].add(index)

    result["capabilities"] = []
    for (capability_type, capability_id), grouped in sorted(
        grouped_capabilities.items(), key=lambda item: (item[0][0], item[1]["name"], str(item[0][1]))
    ):
        synthetic = ProjectCapabilityBinding(
            tenant_id=project.tenant_id,
            project_id=project.id,
            capability_type=capability_type,
            capability_id=capability_id,
            capability_name=grouped["name"],
            source="shared",
            is_enabled=grouped["is_enabled"],
            scope={},
            config={},
        )
        observed = await serialize_project_capability(db, project, synthetic)
        affected = [members[index] for index in sorted(grouped["member_indexes"])]
        persisted_binding_ids = binding_ids.get((capability_type, capability_id), [])
        result["capabilities"].append(
            {
                "binding_id": persisted_binding_ids[0] if persisted_binding_ids else None,
                "binding_ids": persisted_binding_ids,
                "capability_id": str(capability_id),
                "type": capability_type,
                "key": observed.get("key"),
                "name": grouped["name"],
                "description": observed["description"],
                "selected": grouped["is_enabled"],
                "selection_state": "selected" if grouped["is_enabled"] else "unselected",
                "is_enabled": grouped["is_enabled"],
                "availability": observed["availability"],
                "affected_members": [
                    {
                        "member_id": str(member.id),
                        "agent_id": str(member.agent_id),
                        "name": member.name_snapshot,
                        "role": member.role_snapshot,
                        "is_active": member.is_enabled,
                    }
                    for member in affected
                ],
                "affected_member_count": len(affected),
            }
        )
    result["asset_summary"] = {
        **dict(result.get("asset_summary") or {}),
        "skill_count": len(result["skills"]),
        "skill_file_count": sum(item["file_count"] for item in result["skills"]),
        "skill_size_bytes": sum(item["size_bytes"] for item in result["skills"]),
        "capability_count": len(result["capabilities"]),
    }
    return result
