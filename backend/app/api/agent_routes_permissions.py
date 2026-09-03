"""Mechanically separated Agent API route group."""

from app.api.agent_api_shared import *  # noqa: F401,F403
from app.services.provider_identity_policy import mask_identity_claim


@router.get("/{agent_id}/permissions")
async def get_agent_permissions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent permission scope."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    perms = result.scalars().all()
    can_manage = access_level == "manage"
    is_owner = is_agent_creator(current_user, agent)
    access_mode = getattr(agent, "access_mode", None) or "company"

    if not perms:
        return {
            "scope_type": access_mode,
            "scope_ids": [],
            "user_access": [],
            "department_access": [],
            "access_level": "manage" if is_owner else "use",
            "effective_access_level": access_level,
            "can_manage": can_manage,
            "is_owner": is_owner,
            "creator_id": str(agent.creator_id) if agent.creator_id else None,
        }

    scope_type = access_mode
    scope_ids = [str(p.scope_id) for p in perms if p.scope_type == "user" and p.scope_id]
    department_perms = [
        p for p in perms if p.scope_type == "department" and p.scope_id
    ]
    perm_access_level = getattr(agent, "company_access_level", None) or next(
        (p.access_level for p in perms if p.scope_type == "company"),
        "use",
    )

    # Resolve names for display
    scope_names = []
    user_access = []
    department_access = []
    if department_perms:
        departments_result = await db.execute(
            select(OrgDepartment, IdentityProvider)
            .outerjoin(IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id)
            .where(
                OrgDepartment.id.in_([p.scope_id for p in department_perms]),
                OrgDepartment.tenant_id == agent.tenant_id,
                OrgDepartment.status == "active",
            )
            .order_by(OrgDepartment.path.asc())
        )
        departments_by_id = {
            department.id: (department, provider)
            for department, provider in departments_result.all()
        }
        for perm in department_perms:
            department_row = departments_by_id.get(perm.scope_id)
            if department_row:
                department, provider = department_row
                department_access.append(
                    {
                        "id": str(department.id),
                        "name": department.name,
                        "path": department.path,
                        "provider_id": str(department.provider_id) if department.provider_id else None,
                        "provider_name": provider.name if provider else None,
                        "provider_type": provider.provider_type if provider else None,
                        "access_level": perm.access_level or "use",
                        "include_descendants": True,
                    }
                )
    display_user_ids = {uuid.UUID(sid) for sid in scope_ids}
    if access_mode == "custom":
        if agent.creator_id:
            display_user_ids.add(agent.creator_id)
        display_user_ids.update(admin.id for admin in await _get_active_admin_users(db, agent.tenant_id))

    if display_user_ids:
        users_result = await db.execute(
            select(User).where(
                User.id.in_(display_user_ids),
                User.tenant_id == agent.tenant_id,
            )
        )
        users_by_id = {str(u.id): u for u in users_result.scalars().all()}
        canonical_members = canonical_org_member_id_subquery(
            tenant_id=agent.tenant_id,
            prefer_directory_profile=True,
        )
        members_result = await db.execute(
            select(OrgMember)
            .join(
                canonical_members,
                and_(OrgMember.id == canonical_members.c.om_id, canonical_members.c.rn == 1),
            )
            .where(OrgMember.user_id.in_(display_user_ids))
        )
        members_by_user_id = {
            str(member.user_id): member
            for member in members_result.scalars().all()
            if member.user_id
        }
        access_by_user_id = {
            str(perm.scope_id): (perm.access_level or "use")
            for perm in perms
            if perm.scope_type == "user" and perm.scope_id
        }
        ordered_user_ids = [str(uid) for uid in display_user_ids]
        source_map, binding_map = await load_directory_identity_summaries(
            db, tenant_id=agent.tenant_id, user_ids=display_user_ids
        )
        ordered_user_ids.sort(key=lambda sid: (users_by_id.get(sid).display_name or users_by_id.get(sid).username or "") if users_by_id.get(sid) else "")
        for perm in perms:
            if perm.scope_type != "user" or not perm.scope_id:
                continue
            sid = str(perm.scope_id)
            if sid not in ordered_user_ids:
                ordered_user_ids.append(sid)

        for sid in ordered_user_ids:
            u = users_by_id.get(sid)
            if not u:
                continue
            member = members_by_user_id.get(sid)
            is_creator = agent.creator_id == u.id
            is_admin = u.role in ("platform_admin", "org_admin")
            is_required = access_mode == "custom" and (is_creator or is_admin)
            item = {
                "id": sid,
                "name": u.display_name or u.username,
                "username": u.username,
                "email": u.email,
                "phone_masked": mask_identity_claim(
                    "phone",
                    (u.identity.phone if u.identity else None)
                    or (member.phone if member else None),
                ),
                "title": member.title if member else u.title,
                "avatar_url": member.avatar_url if member else u.avatar_url,
                "department_path": member.department_path if member else "",
                "role": u.role,
                "access_level": "manage" if is_required else access_by_user_id.get(sid, "use"),
                "is_required": is_required,
                "required_reason": "creator" if is_creator else "company_admin" if is_admin else None,
                "directory_sources": source_map.get(u.id, []),
                "channel_bindings": binding_map.get(u.id, []),
            }
            scope_names.append({"id": sid, "name": item["name"]})
            user_access.append(item)

    return {
        "scope_type": scope_type,
        "scope_ids": scope_ids,
        "scope_names": scope_names,
        "user_access": user_access,
        "department_access": department_access,
        "access_level": perm_access_level,
        "effective_access_level": access_level,
        "can_manage": can_manage,
        "is_owner": is_owner,
        "creator_id": str(agent.creator_id) if agent.creator_id else None,
    }


@router.put("/{agent_id}/permissions")
async def update_agent_permissions(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent permission scope (owner or platform_admin only)."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    scope_type = data.get("scope_type", "company")
    scope_ids = data.get("scope_ids", [])
    user_access = data.get("user_access", [])
    department_access = data.get("department_access", [])
    access_level = data.get("access_level", "use")
    if access_level not in ("use", "manage"):
        access_level = "use"
    if scope_type not in ("company", "user", "private", "custom"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported scope_type")
    if scope_type == "user":
        scope_type = "private"

    valid_user_ids: set[uuid.UUID] = set()
    if scope_type == "custom":
        try:
            requested_user_ids = {
                uuid.UUID(str(item.get("id") or item.get("user_id")))
                for item in user_access
                if item.get("id") or item.get("user_id")
            }
            requested_user_ids.update(uuid.UUID(str(scope_id)) for scope_id in scope_ids)
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user id",
            ) from exc
        if requested_user_ids:
            users_result = await db.execute(
                select(User.id).where(
                    User.id.in_(requested_user_ids),
                    User.tenant_id == agent.tenant_id,
                    User.is_active == True,  # noqa: E712
                )
            )
            valid_user_ids = set(users_result.scalars().all())
            if valid_user_ids != requested_user_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User not found in this organization",
                )

    valid_departments: dict[uuid.UUID, OrgDepartment] = {}
    if scope_type == "custom" and department_access:
        try:
            requested_department_ids = {
                uuid.UUID(str(item.get("id") or item.get("department_id")))
                for item in department_access
                if item.get("id") or item.get("department_id")
            }
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid department id",
            ) from exc
        departments_result = await db.execute(
            select(OrgDepartment).where(
                OrgDepartment.id.in_(requested_department_ids),
                OrgDepartment.tenant_id == agent.tenant_id,
                OrgDepartment.status == "active",
            )
        )
        valid_departments = {
            department.id: department
            for department in departments_result.scalars().all()
        }
        if set(valid_departments) != requested_department_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Department not found in this organization",
            )

    # Delete existing permissions
    from sqlalchemy import delete as sql_delete
    await db.execute(sql_delete(AgentPermission).where(AgentPermission.agent_id == agent_id))

    # Insert new permissions
    if scope_type == "company":
        agent.access_mode = "company"
        agent.company_access_level = access_level
        db.add(AgentPermission(agent_id=agent_id, scope_type="company", access_level=access_level))
    elif scope_type == "private":
        agent.access_mode = "private"
        agent.company_access_level = access_level
        # "Only me" means private to the agent creator, even when an org admin
        # is managing a company-visible agent created by someone else.
        db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=agent.creator_id or current_user.id, access_level="manage"))
    elif scope_type == "custom":
        agent.access_mode = "custom"
        agent.company_access_level = access_level
        seen_user_ids: set[uuid.UUID] = set()
        creator_id = agent.creator_id or current_user.id
        required_manager_ids = {creator_id}
        required_manager_ids.update(admin.id for admin in await _get_active_admin_users(db, agent.tenant_id))
        for item in user_access:
            sid = item.get("id") or item.get("user_id")
            if not sid:
                continue
            uid = uuid.UUID(str(sid))
            if uid in seen_user_ids:
                continue
            lvl = item.get("access_level", "use")
            if lvl not in ("use", "manage"):
                lvl = "use"
            if uid in required_manager_ids:
                lvl = "manage"
            seen_user_ids.add(uid)
            db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=uid, access_level=lvl))
        for sid in scope_ids:
            uid = uuid.UUID(str(sid))
            if uid not in seen_user_ids:
                seen_user_ids.add(uid)
                db.add(AgentPermission(
                    agent_id=agent_id,
                    scope_type="user",
                    scope_id=uid,
                    access_level="manage" if uid in required_manager_ids else access_level,
                ))
        seen_department_ids: set[uuid.UUID] = set()
        for item in department_access:
            sid = item.get("id") or item.get("department_id")
            if not sid:
                continue
            department_id = uuid.UUID(str(sid))
            if department_id in seen_department_ids or department_id not in valid_departments:
                continue
            level = item.get("access_level", "use")
            if level not in ("use", "manage"):
                level = "use"
            seen_department_ids.add(department_id)
            db.add(
                AgentPermission(
                    agent_id=agent_id,
                    scope_type="department",
                    scope_id=department_id,
                    access_level=level,
                )
            )
        for uid in required_manager_ids:
            if uid not in seen_user_ids:
                db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=uid, access_level="manage"))

    await db.flush()
    relationships_changed = await ensure_access_granted_platform_relationships(
        db,
        agent,
        created_by_user_id=current_user.id,
    )
    if relationships_changed:
        from app.api.relationships import _regenerate_relationships_file
        await _regenerate_relationships_file(db, agent_id)

    await db.commit()
    return {"status": "ok"}


@router.get("/{agent_id}/permissions/directory/departments")
async def get_agent_permission_departments(
    agent_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a lazy-loadable, tenant-scoped department directory."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")
    return await permission_directory_departments(
        db,
        tenant_id=agent.tenant_id,
        current_user_id=current_user.id,
        parent_id=parent_id,
        search=search,
        limit=limit,
    )


@router.get("/{agent_id}/permissions/directory/members")
async def get_agent_permission_members(
    agent_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    execution_assignable: bool = False,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return canonical platform users for the permission picker."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")
    if not agent.tenant_id:
        return {"items": [], "page": page, "page_size": page_size, "total": 0, "has_more": False}

    if execution_assignable:
        accessible_user_ids = build_agent_accessible_user_ids_query(agent).subquery()
        candidate_filters = [
            User.tenant_id == agent.tenant_id,
            User.is_active == True,  # noqa: E712
            or_(Identity.id.is_(None), Identity.is_active == True),  # noqa: E712
            or_(
                User.id.in_(select(accessible_user_ids.c.id)),
                User.role == "platform_admin",
                Identity.is_platform_admin == True,  # noqa: E712
            ),
        ]
        normalized_search = (search or "").strip()
        search_rank = None
        if normalized_search:
            pattern = f"%{normalized_search}%"
            source_columns = (
                OrgMember.name,
                OrgMember.nickname,
                OrgMember.email,
                OrgMember.phone,
            )
            source_base = select(OrgMember.user_id).outerjoin(
                IdentityProvider, IdentityProvider.id == OrgMember.provider_id
            ).where(
                OrgMember.tenant_id == agent.tenant_id,
                OrgMember.status == "active",
                OrgMember.user_id.is_not(None),
                IdentityProvider.is_active.is_(True),
            )
            matching_source_users = source_base.where(
                or_(*(column.ilike(pattern) for column in source_columns))
            )
            candidate_filters.append(
                or_(
                    User.display_name.ilike(pattern),
                    Identity.email.ilike(pattern),
                    Identity.phone.ilike(pattern),
                    User.id.in_(matching_source_users),
                )
            )
            exact_value = normalized_search.casefold()
            exact_source_users = source_base.where(
                or_(*(func.lower(column) == exact_value for column in source_columns))
            )
            search_rank = case(
                (
                    or_(
                        func.lower(User.display_name) == exact_value,
                        func.lower(Identity.email) == exact_value,
                        func.lower(Identity.phone) == exact_value,
                        User.id.in_(exact_source_users),
                    ),
                    0,
                ),
                else_=1,
            )
        elif department_id:
            department_result = await db.execute(
                select(OrgDepartment).where(
                    OrgDepartment.id == department_id,
                    OrgDepartment.tenant_id == agent.tenant_id,
                    OrgDepartment.status == "active",
                )
            )
            department = department_result.scalar_one_or_none()
            if not department:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
            department_ids = [department.id]
            if include_descendants:
                subtree = department_subtree_cte(
                    tenant_id=agent.tenant_id,
                    department_id=department.id,
                    name="execution_picker_department_subtree",
                )
                department_ids = select(subtree.c.department_id)
            memberships = directory_memberships_subquery(
                tenant_id=agent.tenant_id,
                name="execution_picker_memberships",
            )
            department_user_ids = (
                select(OrgMember.user_id)
                .join(memberships, memberships.c.account_id == OrgMember.id)
                .where(
                    OrgMember.tenant_id == agent.tenant_id,
                    OrgMember.status == "active",
                    memberships.c.group_id.in_(department_ids),
                    same_directory_provider(
                        memberships.c.provider_id,
                        OrgMember.provider_id,
                    ),
                    OrgMember.user_id.is_not(None),
                )
            )
            candidate_filters.append(User.id.in_(department_user_ids))

        candidate_query = select(User).outerjoin(Identity, Identity.id == User.identity_id).where(*candidate_filters)
        count_result = await db.execute(
            select(func.count()).select_from(candidate_query.subquery())
        )
        total = int(count_result.scalar_one() or 0)
        ordering = [User.display_name.asc(), User.id.asc()]
        if search_rank is not None:
            ordering.insert(0, search_rank.asc())
        candidates_result = await db.execute(
            candidate_query
            .options(selectinload(User.identity))
            .order_by(*ordering)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        candidates = candidates_result.scalars().all()

        profile_map: dict[uuid.UUID, OrgMember] = {}
        if candidates:
            canonical_profiles = canonical_org_member_id_subquery(
                tenant_id=agent.tenant_id,
                prefer_directory_profile=True,
            )
            profile_result = await db.execute(
                select(OrgMember)
                .join(
                    canonical_profiles,
                    and_(
                        OrgMember.id == canonical_profiles.c.om_id,
                        canonical_profiles.c.rn == 1,
                    ),
                )
                .where(OrgMember.user_id.in_([candidate.id for candidate in candidates]))
                .order_by(OrgMember.user_id.asc(), OrgMember.id.asc())
            )
            for profile in profile_result.scalars().all():
                if profile.user_id and profile.user_id not in profile_map:
                    profile_map[profile.user_id] = profile

        source_map, binding_map = await load_directory_identity_summaries(
            db,
            tenant_id=agent.tenant_id,
            user_ids=[candidate.id for candidate in candidates],
        )
        return {
            "items": [
                {
                    "id": str(candidate.id),
                    "member_id": str(profile.id) if (profile := profile_map.get(candidate.id)) else None,
                    "name": candidate.display_name,
                    "nickname": profile.nickname if profile else None,
                    "department_id": str(profile.department_id) if profile and profile.department_id else None,
                    "department_path": (profile.department_path or "") if profile else "",
                    "title": (profile.title or candidate.title or "") if profile else (candidate.title or ""),
                    "avatar_url": (profile.avatar_url if profile else None) or candidate.avatar_url,
                    "email": candidate.email,
                    "phone_masked": mask_identity_claim(
                        "phone",
                        (candidate.identity.phone if candidate.identity else None)
                        or (profile.phone if profile else None),
                    ),
                    "directory_sources": source_map.get(candidate.id, []),
                    "channel_bindings": binding_map.get(candidate.id, []),
                }
                for candidate in candidates
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": page * page_size < total,
        }

    return await permission_directory_members(
        db,
        tenant_id=agent.tenant_id,
        department_id=department_id,
        include_descendants=include_descendants,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/{agent_id}/permissions/candidates")
async def get_agent_permission_candidates(
    agent_id: uuid.UUID,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return org members that can be granted custom access.

    For members without a linked platform account (user_id is None), we call
    get_platform_user_by_org_member which will find-or-create a User using the
    member's email/phone, then link it back to the OrgMember row.
    """
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    member_query = select(OrgMember).where(
        OrgMember.tenant_id == agent.tenant_id,
        OrgMember.status == "active",
    )
    if search:
        pattern = f"%{search}%"
        member_query = member_query.where(
            OrgMember.name.ilike(pattern) |
            OrgMember.nickname.ilike(pattern) |
            OrgMember.email.ilike(pattern) |
            OrgMember.name_translit_full.ilike(pattern) |
            OrgMember.name_translit_initial.ilike(pattern)
        )

    members_result = await db.execute(member_query.order_by(OrgMember.name.asc()).limit(50))
    members = members_result.scalars().all()

    # For members already linked, batch-load User rows for display info.
    linked_user_ids = [m.user_id for m in members if m.user_id]
    users_by_id: dict[uuid.UUID, User] = {}
    if linked_user_ids:
        users_result = await db.execute(
            select(User)
            .where(User.id.in_(linked_user_ids), User.tenant_id == agent.tenant_id)
            .options(selectinload(User.identity))
        )
        users_by_id = {u.id: u for u in users_result.scalars().all()}

    from app.services.channel_user_service import get_platform_user_by_org_member

    candidates = []
    for m in members:
        if m.user_id:
            u = users_by_id.get(m.user_id)
        else:
            # No platform account yet — find-or-create one from OrgMember info
            # and link it back so future lookups hit Case 1.
            try:
                u = await get_platform_user_by_org_member(
                    db, m, agent_tenant_id=agent.tenant_id
                )
            except Exception:
                # If user creation fails for any reason, skip this member
                continue

        if u is None:
            continue

        candidates.append({
            "id": str(u.id),  # always a valid User.id
            "name": m.name,
            "nickname": m.nickname,
            "username": u.username if u else None,
            "email": m.email or (u.email if u else None),
            "title": m.title or None,
            "avatar_url": m.avatar_url or None,
        })

    await db.commit()

    return {
        "users": candidates,
        "agents": [],
    }


__all__ = [name for name in globals() if not name.startswith("__")]
