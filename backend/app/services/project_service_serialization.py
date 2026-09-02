"""Project run, event, and summary serialization."""

from app.services.project_service_shared import *  # noqa: F403
from app.services.project_service_access import *  # noqa: F403
from app.services.project_service_membership import *  # noqa: F403

async def freeze_run_members(
    db: AsyncSession,
    project: Project,
    run: ProjectRun,
    *,
    source_run_id: uuid.UUID | None = None,
) -> list[ProjectRunMemberSnapshot]:
    if source_run_id is not None:
        source_snapshots = (
            (
                await db.execute(
                    select(ProjectRunMemberSnapshot).where(
                        ProjectRunMemberSnapshot.run_id == source_run_id,
                        ProjectRunMemberSnapshot.project_id == project.id,
                        ProjectRunMemberSnapshot.tenant_id == project.tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not source_snapshots:
            raise HTTPException(status_code=422, detail="Parent project run has no member snapshots")
        inherited = [
            ProjectRunMemberSnapshot(
                tenant_id=project.tenant_id,
                project_id=project.id,
                run_id=run.id,
                project_member_id=snapshot.project_member_id,
                agent_id=snapshot.agent_id,
                is_leader=snapshot.is_leader,
                member_config_snapshot=dict(snapshot.member_config_snapshot or {}),
                capability_snapshot=list(snapshot.capability_snapshot or []),
            )
            for snapshot in source_snapshots
        ]
        db.add_all(inherited)
        await db.flush()
        return inherited

    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    capabilities = (
        (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    agent_tools = (
        (
            await db.execute(
                select(AgentTool, Tool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id.in_([member.agent_id for member in members]),
                    Tool.enabled.is_(True),
                    or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
                )
            )
        ).all()
        if members
        else []
    )
    from app.services.tool_enablement import resolved_agent_tool_enabled, tool_is_required

    tool_rows_by_agent: dict[uuid.UUID, dict[str, tuple[AgentTool, Tool]]] = {}
    for assignment, tool in agent_tools:
        tool_rows_by_agent.setdefault(assignment.agent_id, {})[tool.name] = (
            assignment,
            tool,
        )
    override_names = {
        str(name)
        for member in members
        for key in ("enabled_platform_tools", "disabled_platform_tools")
        for name in dict(member.config_snapshot or {}).get(key, [])
    }
    override_tools = (
        (
            await db.execute(
                select(Tool).where(
                    Tool.name.in_(override_names),
                    Tool.enabled.is_(True),
                    Tool.source.in_(("builtin", "admin")),
                    or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
                )
            )
        ).scalars().all()
        if override_names
        else []
    )
    override_tool_by_name = {tool.name: tool for tool in override_tools}
    member_agents = {
        agent.id: agent
        for agent in (
            (
                await db.execute(
                    select(Agent).where(Agent.id.in_([member.agent_id for member in members]))
                )
            )
            .scalars()
            .all()
        )
    }
    frozen: list[ProjectRunMemberSnapshot] = []
    for member in members:
        effective = [
            {
                "binding_id": str(binding.id),
                "type": binding.capability_type,
                "capability_id": str(binding.capability_id) if binding.capability_id else None,
                "name": binding.capability_name,
                "source": binding.source,
                "scope": binding.scope,
                "config": binding.config,
            }
            for binding in capabilities
            if binding.source == "shared" or binding.inherited_from_agent_id == member.agent_id
        ]
        member_config = dict(member.config_snapshot or {})
        member_agent = member_agents.get(member.agent_id)
        is_project_agent = bool(
            member_agent
            and member_agent.scope == "project"
            and member_agent.project_id == project.id
        )
        enabled_overrides = (
            set()
            if is_project_agent
            else {str(name) for name in member_config.get("enabled_platform_tools", [])}
        )
        disabled_overrides = (
            set()
            if is_project_agent
            else {str(name) for name in member_config.get("disabled_platform_tools", [])}
        )
        frozen_tool_names: set[str] = set()
        for tool_name, (assignment, tool) in tool_rows_by_agent.get(member.agent_id, {}).items():
            enabled = (
                resolved_agent_tool_enabled(tool_name, assignment)
                if is_project_agent
                else tool_is_required(tool_name)
                or tool_name in enabled_overrides
                or (
                    resolved_agent_tool_enabled(tool_name, assignment)
                    and tool_name not in disabled_overrides
                )
            )
            if not enabled:
                continue
            effective.append(
                {
                    "type": "agent_tool",
                    "tool_id": str(tool.id),
                    "name": tool.name,
                    "config": dict(assignment.config or {}),
                }
            )
            frozen_tool_names.add(tool_name)
        for tool_name in sorted(enabled_overrides - frozen_tool_names):
            tool = override_tool_by_name.get(tool_name)
            if tool is None:
                continue
            effective.append(
                {
                    "type": "agent_tool",
                    "tool_id": str(tool.id),
                    "name": tool.name,
                    "config": {},
                }
            )
        snapshot = ProjectRunMemberSnapshot(
            tenant_id=project.tenant_id,
            project_id=project.id,
            run_id=run.id,
            project_member_id=member.id,
            agent_id=member.agent_id,
            is_leader=member.is_leader,
            member_config_snapshot=dict(member.config_snapshot or {}),
            capability_snapshot=effective,
        )
        db.add(snapshot)
        frozen.append(snapshot)
    await db.flush()
    return frozen


def _trace_uuid(*values: Any) -> uuid.UUID | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            continue
    return None


async def serialize_project_run_member_snapshots(
    db: AsyncSession,
    project: Project,
    snapshots: list[ProjectRunMemberSnapshot],
) -> list[dict[str, Any]]:
    """Expose frozen execution facts through one stable product summary.

    The durable snapshots retain IDs, configuration payloads, and assignment
    details for execution. Public APIs intentionally return model names,
    product settings, and neutral capability entries instead of that internal
    persistence structure.
    """

    if not snapshots:
        return []
    member_ids = {snapshot.project_member_id for snapshot in snapshots}
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.id.in_(member_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    member_by_id = {member.id: member for member in members}
    model_ids = {
        model_id
        for snapshot in snapshots
        for field in ("primary_model_id", "fallback_model_id")
        if (model_id := _trace_uuid(dict(snapshot.member_config_snapshot or {}).get(field))) is not None
    }
    models = (
        (
            await db.execute(
                select(LLMModel).where(
                    LLMModel.id.in_(model_ids),
                    or_(LLMModel.tenant_id == project.tenant_id, LLMModel.tenant_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
        if model_ids
        else []
    )
    model_names = {model.id: model.label or model.model for model in models}

    def model_summary(config: dict[str, Any], field: str) -> dict[str, Any] | None:
        raw_value = config.get(field)
        if raw_value in (None, ""):
            return None
        model_id = _trace_uuid(raw_value)
        name = model_names.get(model_id) if model_id else None
        return {"name": name, "availability": "available" if name else "missing"}

    payloads: list[dict[str, Any]] = []
    for snapshot in snapshots:
        member = member_by_id.get(snapshot.project_member_id)
        config = dict(snapshot.member_config_snapshot or {})
        capability_items: dict[tuple[str, str], dict[str, str]] = {}
        for raw_item in list(snapshot.capability_snapshot or []):
            item = dict(raw_item or {})
            raw_type = str(item.get("type") or "").strip()
            capability_type = "tool" if raw_type == "agent_tool" else raw_type
            if capability_type not in {"tool", "mcp", "skill"}:
                capability_type = "other"
            key = str(item.get("key") or item.get("name") or "").strip()
            if not key:
                continue
            name = str(item.get("display_name") or item.get("name") or key).strip()
            source = "project" if item.get("source") == "shared" else "member"
            capability_items[(capability_type, key)] = {
                "type": capability_type,
                "key": key,
                "name": name,
                "source": source,
            }
        items = sorted(capability_items.values(), key=lambda item: (item["type"], item["name"], item["key"]))
        by_type: dict[str, int] = {"tool": 0, "mcp": 0, "skill": 0}
        for item in items:
            by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        member_summary = {
            "project_member_id": snapshot.project_member_id,
            "agent_id": snapshot.agent_id,
            "name": member.name_snapshot if member else None,
            "responsibility": member.role_snapshot if member else "",
            "is_leader": snapshot.is_leader,
            "configuration": {
                "primary_model": model_summary(config, "primary_model_id"),
                "fallback_model": model_summary(config, "fallback_model_id"),
                "temperature": config.get("temperature"),
                "max_tool_rounds": config.get("max_tool_rounds"),
                "has_project_instruction": bool(str(config.get("project_instruction") or "").strip()),
            },
            "capabilities": {
                "total": len(items),
                "by_type": by_type,
                "items": items,
            },
        }
        payloads.append(
            {
                "id": snapshot.id,
                "project_id": snapshot.project_id,
                "run_id": snapshot.run_id,
                "project_member_id": snapshot.project_member_id,
                "agent_id": snapshot.agent_id,
                "is_leader": snapshot.is_leader,
                "member_snapshot": member_summary,
                "created_at": snapshot.created_at,
            }
        )
    return payloads


def _project_a2a_receipt(result: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the structured receipt emitted by the native project transport.

    Legacy transports return a human-readable confirmation and are resolved by
    the scoped pair lookup. A value that claims to be JSON must be valid: a
    malformed native receipt must never silently attach the run to whichever
    same-pair conversation happened to be updated most recently.
    """

    stripped = str(result or "").strip()
    if not stripped.startswith("{"):
        return None, None
    try:
        receipt = json.loads(stripped)
    except json.JSONDecodeError:
        return None, "Project A2A transport returned a malformed JSON receipt"
    if not isinstance(receipt, dict):
        return None, "Project A2A transport receipt must be a JSON object"
    return receipt, None


async def _resolve_project_a2a_session_info(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    project_run_id: uuid.UUID,
    source_agent_id: uuid.UUID,
    target_agent_id: uuid.UUID,
    result: str,
) -> tuple[dict[str, Any], str | None]:
    """Resolve and validate the exact native A2A/session receipt.

    Native delivery returns all durable identities. They are treated as one
    integrity boundary: any mismatch fails delivery instead of falling back to
    a latest-session guess. Only legacy plain-text transports use the scoped
    pair lookup retained at the end of this function.
    """

    receipt, receipt_error = _project_a2a_receipt(result)
    if receipt_error:
        return {}, receipt_error

    if receipt is not None:
        raw_session_id = receipt.get("a2a_session_id") or receipt.get("session_id")
        raw_run_id = receipt.get("project_run_id")
        raw_subagent_run_id = receipt.get("subagent_run_id")
        raw_subagent_session_id = receipt.get("subagent_session_id")
        if not all(
            (
                raw_session_id,
                raw_run_id,
                raw_subagent_run_id,
                raw_subagent_session_id,
            )
        ):
            return {}, "Project A2A transport receipt is missing durable identity fields"

        session_id = _trace_uuid(raw_session_id)
        receipt_run_id = _trace_uuid(raw_run_id)
        subagent_run_id = _trace_uuid(raw_subagent_run_id)
        subagent_session_id = _trace_uuid(raw_subagent_session_id)
        if None in {
            session_id,
            receipt_run_id,
            subagent_run_id,
            subagent_session_id,
        }:
            return {}, "Project A2A transport receipt contains an invalid UUID"
        if receipt_run_id != project_run_id:
            return {}, "Project A2A transport receipt references another project run"
        if subagent_run_id != subagent_session_id:
            return {}, "Project A2A child run and child session identities do not match"

        session = await db.get(ChatSession, session_id)
        expected_pair = {source_agent_id, target_agent_id}
        if (
            session is None
            or session.project_id != project_id
            or session.source_channel != "agent"
            or {session.agent_id, session.peer_agent_id} != expected_pair
        ):
            return {}, "Project A2A transport receipt references an invalid collaboration session"

        child_run = await db.get(SubagentRun, subagent_run_id)
        child_session = await db.get(ChatSession, subagent_session_id)
        if (
            child_run is None
            or child_session is None
            or child_run.project_id != project_id
            or child_run.parent_session_id != session.id
            or child_session.project_id != project_id
            or child_session.source_channel != "subagent"
            or child_session.agent_id != target_agent_id
        ):
            return {}, "Project A2A transport receipt references an invalid execution session"

        return (
            {
                "session_id": str(session.id),
                "a2a_session_id": str(session.id),
                "session_agent_id": str(session.agent_id),
                "session_access_agent_id": str(session.agent_id),
                "session_title": session.title,
                "project_run_id": str(project_run_id),
                "subagent_run_id": str(child_run.id),
                "subagent_session_id": str(child_session.id),
            },
            None,
        )

    session_agent_id = min(source_agent_id, target_agent_id, key=str)
    session_peer_id = max(source_agent_id, target_agent_id, key=str)
    session = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.project_id == project_id,
                ChatSession.source_channel == "agent",
                ChatSession.agent_id == session_agent_id,
                ChatSession.peer_agent_id == session_peer_id,
            )
            .order_by(
                ChatSession.last_message_at.desc().nulls_last(),
                ChatSession.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if session is None:
        return {}, None
    return (
        {
            "session_id": str(session.id),
            "a2a_session_id": str(session.id),
            "session_agent_id": str(session.agent_id),
            "session_access_agent_id": str(session.agent_id),
            "session_title": session.title,
        },
        None,
    )


async def serialize_project_runs(
    db: AsyncSession,
    project: Project,
    runs: list[ProjectRun],
) -> list[dict[str, Any]]:
    """Return ProjectRuns with immutable member and exact session identity.

    Consumers must never infer a conversation from a group root or from the
    currently active member list.  The execution-time snapshot survives member
    removal, while the explicit session fields distinguish the visible A2A
    conversation from the durable worker child.
    """
    if not runs:
        return []
    run_ids = [run.id for run in runs]
    snapshots = (
        (
            await db.execute(
                select(ProjectRunMemberSnapshot).where(
                    ProjectRunMemberSnapshot.project_id == project.id,
                    ProjectRunMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectRunMemberSnapshot.run_id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    public_snapshots = await serialize_project_run_member_snapshots(db, project, list(snapshots))
    public_by_snapshot_id = {payload["id"]: payload["member_snapshot"] for payload in public_snapshots}
    snapshots_by_run: dict[uuid.UUID, list[ProjectRunMemberSnapshot]] = {}
    for snapshot in snapshots:
        snapshots_by_run.setdefault(snapshot.run_id, []).append(snapshot)

    payloads: list[dict[str, Any]] = []
    for run in runs:
        output = dict(run.output or {})
        input_data = dict(run.input or {})
        dispatch = dict(input_data.get("dispatch") or {})
        responsible = next(
            (snapshot for snapshot in snapshots_by_run.get(run.id, []) if snapshot.agent_id == run.agent_id),
            None,
        )
        subagent_session_id = _trace_uuid(
            output.get("subagent_session_id"),
            output.get("subagent_run_id"),
        )
        visible_session_id = _trace_uuid(
            output.get("session_id"),
            output.get("a2a_session_id"),
            subagent_session_id,
        )
        group_session_id = _trace_uuid(
            output.get("group_session_id"),
            input_data.get("group_session_id"),
            dispatch.get("group_session_id"),
        )
        member_snapshot = public_by_snapshot_id.get(responsible.id) if responsible else None
        payloads.append(
            {
                "id": run.id,
                "project_id": run.project_id,
                "work_item_id": run.work_item_id,
                "agent_id": run.agent_id,
                "initiated_by_user_id": run.initiated_by_user_id,
                "status": run.status,
                "trigger_type": run.trigger_type,
                "title": str(input_data.get("title") or "").strip() or None,
                "input": input_data,
                "output": output,
                "error": run.error,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "project_member_id": responsible.project_member_id if responsible else None,
                "agent_name": member_snapshot.get("name") if member_snapshot else None,
                "member_snapshot": member_snapshot,
                "session_id": visible_session_id,
                "subagent_session_id": subagent_session_id,
                "group_session_id": group_session_id,
            }
        )
    return payloads


async def serialize_project_events(
    db: AsyncSession,
    project: Project,
    events: list[ProjectEvent],
) -> list[dict[str, Any]]:
    """Attach the same frozen product summary to run-linked audit events."""

    if not events:
        return []
    run_ids = {event.run_id for event in events if event.run_id is not None}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.id.in_(run_ids),
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    run_payloads = await serialize_project_runs(db, project, list(runs))
    member_summary_by_run = {payload["id"]: payload.get("member_snapshot") for payload in run_payloads}
    return [
        {
            "id": event.id,
            "project_id": event.project_id,
            "work_item_id": event.work_item_id,
            "run_id": event.run_id,
            "actor_user_id": event.actor_user_id,
            "actor_agent_id": event.actor_agent_id,
            "from_agent_id": event.from_agent_id,
            "to_agent_id": event.to_agent_id,
            "event_type": event.event_type,
            "summary": event.summary,
            "event_metadata": dict(event.event_metadata or {}),
            "member_snapshot": member_summary_by_run.get(event.run_id),
            "created_at": event.created_at,
        }
        for event in events
    ]


async def project_summary(
    db: AsyncSession,
    project: Project,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> dict:
    members = (
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
    grants = (
        await db.execute(
            select(ProjectAccessGrant, User.display_name)
            .join(User, User.id == ProjectAccessGrant.user_id)
            .where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
            )
        )
    ).all()
    work_counts = dict(
        (
            await db.execute(
                select(ProjectWorkItem.status, func.count(ProjectWorkItem.id))
                .where(ProjectWorkItem.project_id == project.id, ProjectWorkItem.tenant_id == project.tenant_id)
                .group_by(ProjectWorkItem.status)
            )
        ).all()
    )
    total = sum(work_counts.values())
    done = work_counts.get("done", 0)
    leader = next((member for member in members if member.is_leader), None)
    owner_name = (
        await db.execute(
            select(User.display_name).where(User.id == project.owner_user_id, User.tenant_id == project.tenant_id)
        )
    ).scalar_one_or_none()
    execution_user_id = project_execution_user_id(project)
    execution_user_name = (
        await db.execute(
            select(User.display_name).where(User.id == execution_user_id, User.tenant_id == project.tenant_id)
        )
    ).scalar_one_or_none()
    actor = await db.get(User, actor_user_id) if actor_user_id is not None else None
    is_project_owner = actor_user_id == project.owner_user_id
    can_manage_as_owner = actor is not None and can_manage_project_as_owner(actor, project)
    can_manage_execution_user = actor is not None and can_manage_project_execution_user(actor, project)
    access_role = "owner" if can_manage_as_owner else None
    if access_role is None and actor_user_id is not None:
        access_role = next(
            (grant.role for grant, _display_name in grants if grant.user_id == actor_user_id),
            None,
        )
    return {
        "id": str(project.id),
        "tenant_id": str(project.tenant_id),
        "owner_user_id": str(project.owner_user_id),
        "execution_user_id": str(execution_user_id),
        "execution_user_name": execution_user_name,
        "template_id": str(project.template_id) if project.template_id else None,
        "name": project.name,
        "description": project.description,
        "goal": project.goal,
        "objective": project.goal,
        "success_criteria": project.success_criteria,
        "visibility": project.visibility,
        "status": project.status,
        "settings": project.settings,
        "progress": round(done * 100 / total) if total else 0,
        "work_item_counts": work_counts,
        "members": [
            {
                "id": str(member.id),
                "agent_id": str(member.agent_id),
                "agent_name": member.name_snapshot,
                "name_snapshot": member.name_snapshot,
                "role_snapshot": member.role_snapshot,
                "is_leader": member.is_leader,
                "is_enabled": member.is_enabled,
                "status": "active" if member.is_enabled else "departed",
                "config_snapshot": member.config_snapshot,
            }
            for member in members
        ],
        "leader_name": leader.name_snapshot if leader else None,
        "active_agent_count": sum(member.is_enabled for member in members),
        "current_signal": project.settings.get("current_signal"),
        "next_action": project.settings.get("next_action"),
        "owner_name": owner_name,
        "access_role": access_role,
        "is_project_owner": is_project_owner,
        "can_delete": can_manage_as_owner,
        "can_manage_sharing": can_manage_as_owner,
        "can_manage_execution_user": can_manage_execution_user,
        "shared_with_user_ids": [str(grant.user_id) for grant, _ in grants],
        "shared_with_names": [display_name for _, display_name in grants],
        "shared_with": [
            {"user_id": str(grant.user_id), "display_name": display_name, "role": grant.role}
            for grant, display_name in grants
        ],
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }
