from project_actions_support import *  # noqa: F401,F403

async def test_project_capability_options_are_the_create_authorization_boundary(
    project_api: ProjectApiEnv,
):
    env = project_api
    shared_mcp = MCPServer(
        tenant_id=env.tenant_id,
        name=f"shared-project-mcp-{uuid.uuid4().hex[:8]}",
        display_name="Shared Project MCP",
        base_url_template="https://shared.project.test/mcp",
        headers_template={},
    )
    worker_mcp = MCPServer(
        tenant_id=env.tenant_id,
        name=f"worker-project-mcp-{uuid.uuid4().hex[:8]}",
        display_name="Worker Project MCP",
        base_url_template="https://worker.project.test/mcp",
        headers_template={},
    )
    market_skill = Skill(
        tenant_id=env.tenant_id,
        name="Market Project Skill",
        description="A published project Skill.",
        category="general",
        folder_name=f"market-project-skill-{uuid.uuid4().hex[:8]}",
        visibility="public",
        status="published",
    )
    worker_skill = Skill(
        tenant_id=env.tenant_id,
        name="Worker Project Skill",
        description="A Skill installed for one digital employee.",
        category="general",
        folder_name=f"worker-project-skill-{uuid.uuid4().hex[:8]}",
        visibility="tenant",
        status="published",
        publisher_agent_id=env.source_reviewer_id,
    )
    draft_market_skill = Skill(
        tenant_id=env.tenant_id,
        name="Draft Market Project Skill",
        description="Not published.",
        category="general",
        folder_name=f"draft-market-project-skill-{uuid.uuid4().hex[:8]}",
        visibility="public",
        status="draft",
    )
    offline_worker_skill = Skill(
        tenant_id=env.tenant_id,
        name="Offline Worker Project Skill",
        description="No longer available.",
        category="general",
        folder_name=f"offline-worker-project-skill-{uuid.uuid4().hex[:8]}",
        visibility="tenant",
        status="offline",
        publisher_agent_id=env.source_worker_id,
    )
    draft_worker_skill = Skill(
        tenant_id=env.tenant_id,
        name="Draft Worker Project Skill",
        description="Not ready for project use.",
        category="general",
        folder_name=f"draft-worker-project-skill-{uuid.uuid4().hex[:8]}",
        visibility="tenant",
        status="draft",
        publisher_agent_id=env.source_worker_id,
    )
    env.db.add_all(
        [
            shared_mcp,
            worker_mcp,
            market_skill,
            worker_skill,
            draft_market_skill,
            offline_worker_skill,
            draft_worker_skill,
        ]
    )
    await env.db.flush()
    shared_tool = Tool(
        name=f"shared_project_mcp_tool_{uuid.uuid4().hex[:8]}",
        display_name="Shared project action",
        description="Perform an approved shared action.",
        type="mcp",
        category="general",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        source="admin",
        tenant_id=env.tenant_id,
        mcp_server_id=shared_mcp.id,
    )
    worker_tool = Tool(
        name=f"worker_project_mcp_tool_{uuid.uuid4().hex[:8]}",
        display_name="Worker project action",
        description="Perform an action installed by one digital employee.",
        type="mcp",
        category="general",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        source="agent",
        tenant_id=env.tenant_id,
        mcp_server_id=worker_mcp.id,
    )
    env.db.add_all([shared_tool, worker_tool])
    await env.db.flush()
    env.db.add_all(
        [
            AgentTool(
                agent_id=env.source_worker_id,
                tool_id=worker_tool.id,
                enabled=True,
                source="user_installed",
            ),
            SkillInstall(
                tenant_id=env.tenant_id,
                skill_id=worker_skill.id,
                agent_id=env.source_worker_id,
                installed_by_user_id=env.owner_id,
                is_active=True,
            ),
            SkillFile(
                skill_id=market_skill.id,
                path="SKILL.md",
                content="---\nname: Market Project Skill\ndescription: Published\n---\n",
            ),
            SkillFile(
                skill_id=worker_skill.id,
                path="SKILL.md",
                content="---\nname: Worker Project Skill\ndescription: Installed\n---\n",
            ),
        ]
    )
    await env.db.commit()
    shared_mcp_id = shared_mcp.id
    worker_mcp_id = worker_mcp.id
    worker_tool_id = worker_tool.id
    market_skill_id = market_skill.id
    worker_skill_id = worker_skill.id
    draft_market_skill_id = draft_market_skill.id
    offline_worker_skill_id = offline_worker_skill.id
    draft_worker_skill_id = draft_worker_skill.id

    bootstrap = await env.client.get("/api/projects/bootstrap-options")
    assert bootstrap.status_code == 200, bootstrap.text
    bootstrap_payload = bootstrap.json()
    assert "skills" not in bootstrap_payload
    assert "mcp_servers" not in bootstrap_payload
    capabilities = bootstrap_payload["capabilities"]
    tool_options = bootstrap_payload["tools"]
    shared_mcp_tool_option = next(
        item for item in tool_options if item["id"] == str(shared_tool.id)
    )
    worker_mcp_tool_option = next(
        item
        for item in tool_options
        if item["id"] == str(worker_tool_id)
        and item["installed_by_agent_id"] == str(env.source_worker_id)
    )
    worker_skill_option = next(
        item
        for item in capabilities
        if item["capability_id"] == str(worker_skill_id)
        and item["owner_agent_id"] == str(env.source_worker_id)
    )
    assert shared_mcp_tool_option["source"] == "admin"
    assert shared_mcp_tool_option["installed_by_agent_id"] is None
    assert shared_mcp_tool_option["enabled"] is False
    assert worker_mcp_tool_option["source"] == "agent"
    assert worker_mcp_tool_option["agent_tool_source"] == "user_installed"
    assert worker_mcp_tool_option["enabled"] is False
    assert all(item["type"] != "mcp" for item in capabilities)
    assert worker_skill_option["owner_agent_id"] == str(env.source_worker_id)
    assert all(item["capability_id"] != str(draft_market_skill_id) for item in capabilities)
    assert all(item["capability_id"] != str(offline_worker_skill_id) for item in capabilities)
    assert all(item["capability_id"] != str(draft_worker_skill_id) for item in capabilities)
    assert all("key" not in item for item in capabilities)

    async def create_with(
        *,
        member_id: uuid.UUID,
        mcp_ids: list[uuid.UUID] | None = None,
        skill_ids: list[uuid.UUID] | None = None,
        tool_ids: list[uuid.UUID] | None = None,
    ):
        return await env.client.post(
            "/api/projects",
            json={
                "name": f"Capability boundary {uuid.uuid4().hex[:8]}",
                "members": [
                    {
                        "agent_id": str(member_id),
                        "is_leader": True,
                        "settings": {
                            "tools": [
                                {"tool_id": str(value), "enabled": True}
                                for value in (tool_ids or [])
                            ],
                            "mcp_capability_ids": [str(value) for value in (mcp_ids or [])],
                            "skill_capability_ids": [str(value) for value in (skill_ids or [])],
                        },
                    }
                ],
            },
        )

    before_projects = await env.db.scalar(select(func.count()).select_from(Project))
    before_agents = await env.db.scalar(select(func.count()).select_from(Agent))
    before_bindings = await env.db.scalar(
        select(func.count()).select_from(ProjectCapabilityBinding)
    )
    rejected = [
        await create_with(member_id=env.source_leader_id, tool_ids=[worker_tool_id]),
        await create_with(member_id=env.source_leader_id, mcp_ids=[worker_mcp_id]),
        await create_with(member_id=env.source_leader_id, skill_ids=[worker_skill_id]),
        await create_with(member_id=env.source_leader_id, skill_ids=[draft_market_skill_id]),
        await create_with(member_id=env.source_worker_id, skill_ids=[offline_worker_skill_id]),
        await create_with(member_id=env.source_worker_id, skill_ids=[draft_worker_skill_id]),
        await env.client.post(
            "/api/projects",
            json={
                "name": f"Forged inherited capability {uuid.uuid4().hex[:8]}",
                "members": [{"agent_id": str(env.source_leader_id), "is_leader": True}],
                "capabilities": [
                    {
                        "capability_type": "skill",
                        "capability_id": str(worker_skill_id),
                        "source": "inherited",
                        "inherited_from_agent_id": str(env.source_leader_id),
                    }
                ],
            },
        ),
        await env.client.post(
            "/api/projects",
            json={
                "name": f"Forged shared capability {uuid.uuid4().hex[:8]}",
                "members": [{"agent_id": str(env.source_leader_id), "is_leader": True}],
                "shared_capability_ids": [str(worker_skill_id)],
            },
        ),
    ]
    assert [response.status_code for response in rejected] == [422] * 8
    assert await env.db.scalar(select(func.count()).select_from(Project)) == before_projects
    assert await env.db.scalar(select(func.count()).select_from(Agent)) == before_agents
    assert (
        await env.db.scalar(select(func.count()).select_from(ProjectCapabilityBinding)
        ) == before_bindings
    )
    tenant_storage = env.storage_root / "_projects" / str(env.tenant_id)
    assert not tenant_storage.exists() or not any(tenant_storage.iterdir())

    leader_project_response = await create_with(member_id=env.source_leader_id)
    assert leader_project_response.status_code == 201, leader_project_response.text
    leader_project_id = uuid.UUID(leader_project_response.json()["id"])
    leader_project_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == leader_project_id
            )
        )
    ).scalar_one()
    leader_project_agent_id = leader_project_member.agent_id
    binding_count = await env.db.scalar(
        select(func.count()).select_from(ProjectCapabilityBinding)
    )
    cross_member_adds = [
        await env.client.post(
            f"/api/projects/{leader_project_id}/capabilities",
            json={
                "capability_type": capability_type,
                "capability_id": str(capability_id),
                "source": "inherited",
                "inherited_from_agent_id": str(leader_project_agent_id),
            },
        )
        for capability_type, capability_id in (
            ("mcp", worker_mcp_id),
            ("skill", worker_skill_id),
        )
    ]
    assert [response.status_code for response in cross_member_adds] == [422, 422]
    cross_member_import = await env.client.post(
        f"/api/agents/{leader_project_agent_id}/files/import-skill",
        json={"skill_id": str(worker_skill_id)},
    )
    assert cross_member_import.status_code == 422
    assert (
        await env.db.scalar(select(func.count()).select_from(ProjectCapabilityBinding))
        == binding_count
    )

    allowed = await create_with(
        member_id=env.source_worker_id,
        mcp_ids=[shared_mcp_id, worker_mcp_id],
        skill_ids=[market_skill_id, worker_skill_id],
    )
    assert allowed.status_code == 201, allowed.text
    project_id = uuid.UUID(allowed.json()["id"])
    project_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(ProjectMemberSnapshot.project_id == project_id)
        )
    ).scalar_one()
    bindings = list(
        (
            await env.db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project_id,
                    ProjectCapabilityBinding.inherited_from_agent_id == project_member.agent_id,
                )
            )
        ).scalars()
    )
    assert {(binding.capability_type, binding.capability_id) for binding in bindings} >= {
        ("mcp", shared_mcp_id),
        ("mcp", worker_mcp_id),
        ("skill", market_skill_id),
        ("skill", worker_skill_id),
    }
    source_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == env.source_worker_id,
                AgentTool.tool_id == worker_tool_id,
            )
        )
    ).scalar_one()
    source_install = (
        await env.db.execute(
            select(SkillInstall).where(
                SkillInstall.agent_id == env.source_worker_id,
                SkillInstall.skill_id == worker_skill_id,
            )
        )
    ).scalar_one()
    assert source_assignment.enabled is True
    assert source_install.is_active is True

async def test_private_share_settings_and_audit_are_a_real_api_round_trip(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Private by default")
    project_id = project["id"]

    assert project["visibility"] == "private"
    assert project["shared_with"] == []
    assert project["status"] == "planning"
    assert project["access_role"] == "owner"
    assert project["is_project_owner"] is True
    assert project["can_delete"] is True
    assert project["can_manage_sharing"] is True
    assert project["can_manage_execution_user"] is False
    assert project["execution_user_id"] == str(env.owner_id)
    assert project["execution_user_name"] == "Owner"

    env.authenticate_as(env.viewer_id)
    hidden = await env.client.get(f"/api/projects/{project_id}")
    assert hidden.status_code == 404

    env.authenticate_as(env.owner_id)
    settings_response = await env.client.get(f"/api/projects/{project_id}/settings")
    assert settings_response.status_code == 200, settings_response.text
    settings = settings_response.json()
    assert settings["policies"]["approval"] == "risk"
    assert settings["runtime"]["monthly_budget"] == 300

    saved = await env.client.patch(
        f"/api/projects/{project_id}/settings",
        json={
            "policies": {"approval": "all_writes", "max_parallel_runs": 2},
            "runtime": {"mode": "quality", "monthly_budget": 450},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["policies"] == {"approval": "all_writes", "max_parallel_runs": 2}
    assert saved.json()["runtime"]["monthly_budget"] == 450
    rejected_mixed_settings = await env.client.patch(
        f"/api/projects/{project_id}/settings",
        json={
            "runtime": {"mode": "must-not-save", "monthly_budget": 999},
            "git": {"repository_mode": "external"},
        },
    )
    assert rejected_mixed_settings.status_code == 422
    settings_after_rejection = (await env.client.get(f"/api/projects/{project_id}/settings")).json()
    assert settings_after_rejection["runtime"] == saved.json()["runtime"]

    shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "visibility": "shared",
            "shared_with_user_ids": [str(env.viewer_id)],
        },
    )
    assert shared.status_code == 200, shared.text
    assert shared.json()["visibility"] == "shared"
    assert shared.json()["execution_user_id"] == str(env.owner_id)
    env.authenticate_as(env.org_admin_id)
    selected = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"execution_user_id": str(env.viewer_id)},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["can_manage_execution_user"] is True
    env.authenticate_as(env.owner_id)
    shared = selected
    assert shared.json()["execution_user_id"] == str(env.viewer_id)
    assert shared.json()["execution_user_name"] == "Viewer"
    assert shared.json()["shared_with_user_ids"] == [str(env.viewer_id)]
    assert [entry["user_id"] for entry in shared.json()["shared_with"]] == [str(env.viewer_id)]

    env.authenticate_as(env.viewer_id)
    visible = await env.client.get(f"/api/projects/{project_id}")
    assert visible.status_code == 200
    assert visible.json()["access_role"] == "view"
    assert (await env.client.get(f"/api/projects/{project_id}/settings")).status_code == 200
    viewer_settings_write = await env.client.patch(
        f"/api/projects/{project_id}/settings",
        json={"runtime": {"mode": "viewer-must-not-save"}},
    )
    assert viewer_settings_write.status_code == 404
    cannot_edit = await env.client.patch(f"/api/projects/{project_id}", json={"name": "Not allowed"})
    assert cannot_edit.status_code == 404

    grant = (
        await env.db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == uuid.UUID(project_id),
                ProjectAccessGrant.user_id == env.viewer_id,
            )
        )
    ).scalar_one()
    grant.role = "edit"
    await env.db.commit()
    editor_contract = await env.client.get(f"/api/projects/{project_id}")
    assert editor_contract.status_code == 200
    assert editor_contract.json()["access_role"] == "edit"
    normal_editor_update = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"description": "Editors may update ordinary project fields"},
    )
    assert normal_editor_update.status_code == 200
    editor_settings_update = await env.client.patch(
        f"/api/projects/{project_id}/settings",
        json={"current_signal": "Editor-visible delivery signal"},
    )
    assert editor_settings_update.status_code == 200
    editor_cannot_unshare = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "private"},
    )
    assert editor_cannot_unshare.status_code == 404
    editor_cannot_reshare = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"shared_with_user_ids": [str(env.viewer_id)]},
    )
    assert editor_cannot_reshare.status_code == 404

    env.authenticate_as(env.owner_id)
    retained_editor = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "shared_with_user_ids": [str(env.viewer_id)],
        },
    )
    assert retained_editor.status_code == 200, retained_editor.text
    assert retained_editor.json()["shared_with"] == [
        {
            "user_id": str(env.viewer_id),
            "display_name": "Viewer",
            "role": "edit",
        }
    ]
    env.authenticate_as(env.viewer_id)
    assert (await env.client.patch(f"/api/projects/{project_id}", json={"description": "Still editable"})).status_code == 200

    env.authenticate_as(env.owner_id)
    empty_shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "shared", "shared_with_user_ids": []},
    )
    assert empty_shared.status_code == 422

    outsider_tenant = Tenant(name="Outsider", slug=f"outsider-{uuid.uuid4().hex[:8]}")
    env.db.add(outsider_tenant)
    await env.db.flush()
    outsider = await _user(env.db, outsider_tenant, "Outsider")
    await env.db.commit()
    cross_tenant_share = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "visibility": "shared",
            "shared_with_user_ids": [str(env.viewer_id), str(outsider.id)],
        },
    )
    assert cross_tenant_share.status_code == 422

    private = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "private"},
    )
    assert private.status_code == 200
    assert private.json()["visibility"] == "private"
    assert private.json()["shared_with"] == []
    assert private.json()["execution_user_id"] == str(env.owner_id)
    assert private.json()["execution_user_name"] == "Owner"
    remaining_grants = (
        (await env.db.execute(select(ProjectAccessGrant).where(ProjectAccessGrant.project_id == uuid.UUID(project_id))))
        .scalars()
        .all()
    )
    assert remaining_grants == []
    env.authenticate_as(env.viewer_id)
    assert (await env.client.get(f"/api/projects/{project_id}")).status_code == 404
    assert (await env.client.get(f"/api/projects/{project_id}/settings")).status_code == 404
    env.authenticate_as(env.owner_id)

    events_response = await env.client.get(f"/api/projects/{project_id}/events")
    assert events_response.status_code == 200
    event_types = {event["event_type"] for event in events_response.json()}
    assert {
        "project.created",
        "project.initialized",
        "project.settings.updated",
        "project.updated",
        "project.execution_user.changed",
    } <= event_types
    automatic_fallback = next(
        event
        for event in events_response.json()
        if event["event_type"] == "project.execution_user.changed"
        and event["event_metadata"]["automatic_fallback"] is True
    )
    assert automatic_fallback["event_metadata"] == {
        "previous_execution_user_id": str(env.viewer_id),
        "execution_user_id": str(env.owner_id),
        "automatic_fallback": True,
    }

async def test_shared_project_execution_user_is_validated_atomically(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Shared execution identity")
    project_id = project["id"]

    shared_by_owner = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "shared", "shared_with_user_ids": [str(env.viewer_id)]},
    )
    assert shared_by_owner.status_code == 200, shared_by_owner.text
    assert shared_by_owner.json()["execution_user_id"] == str(env.owner_id)

    owner_cannot_select = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"execution_user_id": str(env.viewer_id)},
    )
    assert owner_cannot_select.status_code == 403

    env.authenticate_as(env.org_admin_id)
    configured = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "visibility": "shared",
            "shared_with_user_ids": [str(env.viewer_id)],
            "execution_user_id": str(env.viewer_id),
        },
    )
    assert configured.status_code == 200, configured.text

    tenant = await env.db.get(Tenant, env.tenant_id)
    assert tenant is not None
    alternate = await _user(env.db, tenant, "Alternate")
    await env.db.commit()
    alternate_id = alternate.id
    env.authenticate_as(env.owner_id)
    added_alternate = await env.client.post(
        f"/api/projects/{project_id}/access-grants",
        json={"user_id": str(alternate_id), "role": "view"},
    )
    assert added_alternate.status_code == 201, added_alternate.text
    viewer_grant = await env.db.scalar(
        select(ProjectAccessGrant).where(
            ProjectAccessGrant.project_id == uuid.UUID(project_id),
            ProjectAccessGrant.user_id == env.viewer_id,
        )
    )
    assert viewer_grant is not None
    direct_removal = await env.client.delete(
        f"/api/projects/{project_id}/access-grants/{viewer_grant.id}"
    )
    assert direct_removal.status_code == 409
    invalid_replacement = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"shared_with_user_ids": [str(alternate_id)]},
    )
    assert invalid_replacement.status_code == 409
    still_configured = (await env.client.get(f"/api/projects/{project_id}")).json()
    assert {entry["user_id"] for entry in still_configured["shared_with"]} == {
        str(env.viewer_id),
        str(alternate_id),
    }
    assert still_configured["execution_user_id"] == str(env.viewer_id)

    alternate = await env.db.get(User, alternate_id)
    assert alternate is not None
    alternate.is_active = False
    await env.db.commit()
    env.authenticate_as(env.org_admin_id)
    inactive_selection = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "shared_with_user_ids": [str(env.viewer_id), str(alternate_id)],
            "execution_user_id": str(alternate_id),
        },
    )
    assert inactive_selection.status_code == 422
    alternate = await env.db.get(User, alternate_id)
    assert alternate is not None
    alternate.is_active = True
    await env.db.commit()

    replaced = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "shared_with_user_ids": [str(alternate_id)],
            "execution_user_id": str(alternate_id),
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["execution_user_id"] == str(alternate_id)
    assert replaced.json()["execution_user_name"] == "Alternate"
    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    changes = [event for event in events if event["event_type"] == "project.execution_user.changed"]
    latest_change = next(
        event for event in changes if event["event_metadata"]["execution_user_id"] == str(alternate_id)
    )
    assert latest_change["event_metadata"] == {
        "previous_execution_user_id": str(env.viewer_id),
        "execution_user_id": str(alternate_id),
        "automatic_fallback": False,
    }
