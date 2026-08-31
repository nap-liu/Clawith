from project_actions_support import *  # noqa: F401,F403

async def test_project_create_validates_before_managed_storage(project_api: ProjectApiEnv):
    env = project_api
    response = await env.client.post(
        "/api/projects",
        json={"name": "Invalid share", "visibility": "shared", "shared_with_user_ids": []},
    )

    assert response.status_code == 422
    tenant_storage = env.storage_root / "_projects" / str(env.tenant_id)
    assert not tenant_storage.exists() or not any(tenant_storage.iterdir())

async def test_project_create_removes_managed_storage_after_service_failure(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    env = project_api

    async def fail_agent_copy(*_args, **_kwargs):
        raise RuntimeError("forced project Agent failure")

    monkeypatch.setattr("app.services.project_agent_service.create_project_agent", fail_agent_copy)
    with pytest.raises(RuntimeError, match="forced project Agent failure"):
        await env.client.post(
            "/api/projects",
            json={
                "name": "Compensated failure",
                "members": [{"agent_id": str(env.leader_id), "is_leader": True}],
            },
        )

    tenant_storage = env.storage_root / "_projects" / str(env.tenant_id)
    assert not tenant_storage.exists() or not any(tenant_storage.iterdir())
    assert await env.db.scalar(select(func.count()).select_from(Project)) == 0

async def test_project_create_removes_managed_storage_after_commit_failure(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    env = project_api

    async def fail_commit():
        raise RuntimeError("forced commit failure")

    monkeypatch.setattr(env.db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced commit failure"):
        await env.client.post(
            "/api/projects",
            json={
                "name": "Commit failure",
                "members": [{"agent_id": str(env.leader_id), "is_leader": True}],
            },
        )

    tenant_storage = env.storage_root / "_projects" / str(env.tenant_id)
    assert not tenant_storage.exists() or not any(tenant_storage.iterdir())
    assert await env.db.scalar(select(func.count()).select_from(Project)) == 0

async def test_project_create_applies_member_settings_only_to_project_agent(
    project_api: ProjectApiEnv,
):
    from app.models.mcp_server import MCPServerOverride

    env = project_api
    model = LLMModel(
        tenant_id=env.tenant_id,
        provider="openai",
        model="project-member-model",
        api_key_encrypted="test-only",
        label="Project member model",
        enabled=True,
        context_window=128000,
    )
    tool = Tool(
        name=f"project_member_tool_{uuid.uuid4().hex[:8]}",
        display_name="Project member tool",
        description="Project-local test tool",
        type="builtin",
        category="general",
        parameters_schema={"type": "object", "properties": {}},
        config_schema={"fields": [{"key": "mode", "type": "text"}]},
        enabled=True,
        is_default=False,
        source="builtin",
        tenant_id=env.tenant_id,
    )
    mcp = MCPServer(
        tenant_id=env.tenant_id,
        name=f"project-member-mcp-{uuid.uuid4().hex[:8]}",
        display_name="Project member MCP",
        base_url_template="https://mcp.project.test",
        headers_template={},
        created_by_user_id=env.viewer_id,
    )
    skill = Skill(
        tenant_id=env.tenant_id,
        name="Project member Skill",
        description="Project-local Skill",
        category="general",
        folder_name=f"project-member-skill-{uuid.uuid4().hex[:8]}",
        visibility="tenant",
        status="published",
        publisher_agent_id=env.source_leader_id,
    )
    env.db.add_all([model, tool, mcp, skill])
    await env.db.flush()
    mcp_tool = Tool(
            name=f"project_member_mcp_tool_{uuid.uuid4().hex[:8]}",
            display_name="Project member MCP tool",
            description="Project-local MCP tool",
            type="mcp",
            category="general",
            parameters_schema={"type": "object", "properties": {}},
            enabled=True,
            is_default=False,
            source="admin",
            tenant_id=env.tenant_id,
            mcp_server_id=mcp.id,
            mcp_server_name=mcp.display_name,
            mcp_tool_name="project_member_action",
    )
    disabled_mcp_tool = Tool(
        name=f"project_member_mcp_disabled_{uuid.uuid4().hex[:8]}",
        display_name="Project member disabled MCP tool",
        description="Second independently selectable MCP tool",
        type="mcp",
        category="general",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        is_default=False,
        source="admin",
        tenant_id=env.tenant_id,
        mcp_server_id=mcp.id,
        mcp_server_name=mcp.display_name,
        mcp_tool_name="project_member_disabled_action",
    )
    env.db.add_all([mcp_tool, disabled_mcp_tool])
    env.db.add(
        SkillFile(
            skill_id=skill.id,
            path="SKILL.md",
            content="---\nname: Project member Skill\ndescription: Project-local Skill\n---\n",
        )
    )
    env.db.add(
        AgentTool(
            agent_id=env.source_leader_id,
            tool_id=tool.id,
            enabled=False,
            config={"mode": "source"},
            source="user_installed",
        )
    )
    await env.db.commit()
    mcp_id = mcp.id

    response = await env.client.post(
        "/api/projects",
        json={
            "name": "Create settings isolation",
            "members": [
                {
                    "agent_id": str(env.source_leader_id),
                    "is_leader": True,
                    "settings": {
                        "config_snapshot": {
                            "primary_model_id": str(model.id),
                            "max_tool_rounds": 33,
                            "project_instruction": "Only for this project",
                        },
                        "tools": [
                            {
                                "tool_id": str(tool.id),
                                "enabled": True,
                                "config": {"mode": "project"},
                            },
                            {
                                "tool_id": str(mcp_tool.id),
                                "enabled": True,
                                "config": {"scope": "project-only"},
                            },
                            {
                                "tool_id": str(disabled_mcp_tool.id),
                                "enabled": False,
                                "config": {},
                            },
                        ],
                        "mcp_server_overrides": [
                            {
                                "server_id": str(mcp.id),
                                "system_prompt_block": "Use the project evidence scope only.",
                                "headers_template": {"X-Project-Scope": "${agent.id}"},
                                "credential_template": "project-only-test-token",
                            }
                        ],
                        "skill_capability_ids": [str(skill.id)],
                    },
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    project_id = uuid.UUID(response.json()["id"])
    created_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(ProjectMemberSnapshot.project_id == project_id)
        )
    ).scalar_one()
    project_agent_id = created_member.agent_id
    assert created_member.agent_id != env.source_leader_id
    assert created_member.config_snapshot["primary_model_id"] == str(model.id)
    assert created_member.config_snapshot["max_tool_rounds"] == 33
    assert created_member.config_snapshot["project_instruction"] == "Only for this project"

    source_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == env.source_leader_id,
                AgentTool.tool_id == tool.id,
            )
        )
    ).scalar_one()
    project_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == created_member.agent_id,
                AgentTool.tool_id == tool.id,
            )
        )
    ).scalar_one()
    assert source_assignment.enabled is False
    assert source_assignment.config == {"mode": "source"}
    assert project_assignment.enabled is True
    assert project_assignment.config == {"mode": "project"}
    project_mcp_assignments = {
        assignment.tool_id: assignment
        for assignment in (
            await env.db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == created_member.agent_id,
                    AgentTool.tool_id.in_([mcp_tool.id, disabled_mcp_tool.id]),
                )
            )
        ).scalars()
    }
    assert project_mcp_assignments[mcp_tool.id].enabled is True
    assert project_mcp_assignments[mcp_tool.id].config == {"scope": "project-only"}
    assert project_mcp_assignments[disabled_mcp_tool.id].enabled is False
    project_override = (
        await env.db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == mcp.id,
                MCPServerOverride.scope_type == "agent",
                MCPServerOverride.scope_id == created_member.agent_id,
            )
        )
    ).scalar_one()
    assert project_override.system_prompt_block == "Use the project evidence scope only."
    assert project_override.headers_template == {"X-Project-Scope": "${agent.id}"}
    assert project_override.credential_template == "project-only-test-token"

    source_override = (
        await env.db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == mcp.id,
                MCPServerOverride.scope_type == "agent",
                MCPServerOverride.scope_id == env.source_leader_id,
            )
        )
    ).scalar_one_or_none()
    assert source_override is None
    bindings = (
        await env.db.execute(
            select(ProjectCapabilityBinding).where(
                ProjectCapabilityBinding.project_id == project_id,
                ProjectCapabilityBinding.inherited_from_agent_id == created_member.agent_id,
            )
        )
    ).scalars().all()
    assert {(binding.capability_type, binding.capability_id) for binding in bindings} >= {
        ("mcp", mcp.id),
        ("skill", skill.id),
    }
    mcp_binding = next(
        binding
        for binding in bindings
        if binding.capability_type == "mcp" and binding.capability_id == mcp.id
    )
    assert mcp_binding.is_enabled is True

    disable_selected = await env.client.put(
        f"/api/projects/{project_id}/members/{created_member.id}/tools",
        json=[{"tool_id": str(mcp_tool.id), "enabled": False}],
    )
    assert disable_selected.status_code == 200, disable_selected.text
    await env.db.refresh(mcp_binding)
    assert mcp_binding.is_enabled is False

    enable_other = await env.client.put(
        f"/api/projects/{project_id}/members/{created_member.id}/tools",
        json=[{"tool_id": str(disabled_mcp_tool.id), "enabled": True}],
    )
    assert enable_other.status_code == 200, enable_other.text
    await env.db.refresh(mcp_binding)
    assert mcp_binding.is_enabled is True
    project_mcp_state = {
        assignment.tool_id: assignment.enabled
        for assignment in (
            await env.db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == created_member.agent_id,
                    AgentTool.tool_id.in_([mcp_tool.id, disabled_mcp_tool.id]),
                )
            )
        ).scalars()
    }
    assert project_mcp_state == {
        mcp_tool.id: False,
        disabled_mcp_tool.id: True,
    }
    source_mcp_assignments = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == env.source_leader_id,
                AgentTool.tool_id.in_([mcp_tool.id, disabled_mcp_tool.id]),
            )
        )
    ).scalars().all()
    assert source_mcp_assignments == []
    invalid_override = await env.client.post(
        "/api/projects",
        json={
            "name": "Reject detached MCP configuration",
            "members": [
                {
                    "agent_id": str(env.source_leader_id),
                    "is_leader": True,
                    "settings": {
                        "tools": [
                            {
                                "tool_id": str(mcp_tool.id),
                                "enabled": False,
                                "config": {},
                            }
                        ],
                        "mcp_server_overrides": [
                            {
                                "server_id": str(mcp.id),
                                "headers_template": {"X-Project-Scope": "blocked"},
                            }
                        ],
                    },
                }
            ],
        },
    )
    assert invalid_override.status_code == 422, invalid_override.text
    assert "requires at least one enabled tool" in invalid_override.json()["detail"]

    # Project editors can maintain the isolated Agent override without gaining
    # authority over the company MCP definition or its shared tool catalog.
    agent_override_url = (
        f"/api/admin/mcp-servers/{mcp_id}/overrides/agent/{project_agent_id}"
    )
    override_view = await env.client.get(
        f"/api/admin/mcp-servers/{mcp_id}/overrides",
        params={"agent_id": str(project_agent_id)},
    )
    assert override_view.status_code == 200, override_view.text
    assert [item["scope_id"] for item in override_view.json()["agent"]] == [
        str(project_agent_id)
    ]
    override_update = await env.client.put(
        agent_override_url,
        json={"headers_template": {"X-Project-Scope": "updated-project-only"}},
    )
    assert override_update.status_code == 200, override_update.text
    assert override_update.json()["headers_template"] == {
        "X-Project-Scope": "updated-project-only"
    }
    global_patch = await env.client.patch(
        f"/api/admin/mcp-servers/{mcp_id}",
        json={"display_name": "Must not change globally"},
    )
    assert global_patch.status_code == 403, global_patch.text
    refresh_global_catalog = await env.client.post(
        f"/api/admin/mcp-servers/{mcp_id}/refresh-tools",
        params={"agent_id": str(project_agent_id)},
    )
    assert refresh_global_catalog.status_code == 403, refresh_global_catalog.text

    detached_mcp = MCPServer(
        tenant_id=env.tenant_id,
        name=f"detached-project-mcp-{uuid.uuid4().hex[:8]}",
        display_name="Detached project MCP",
        base_url_template="https://detached.project.test",
        headers_template={},
        created_by_user_id=env.viewer_id,
    )
    env.db.add(detached_mcp)
    await env.db.commit()
    detached_mcp_id = detached_mcp.id
    detached_override = await env.client.put(
        f"/api/admin/mcp-servers/{detached_mcp_id}/overrides/agent/{project_agent_id}",
        json={"headers_template": {"X-Project-Scope": "forged"}},
    )
    assert detached_override.status_code == 404, detached_override.text
    detached_row = (
        await env.db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == detached_mcp_id,
                MCPServerOverride.scope_type == "agent",
                MCPServerOverride.scope_id == project_agent_id,
            )
        )
    ).scalar_one_or_none()
    assert detached_row is None

async def test_project_private_mcp_credential_is_referenced_only_for_its_creator_and_not_exported(
    project_api: ProjectApiEnv,
):
    from app.models.mcp_server import MCPServerOverride
    from app.services.mcp_server_service import (
        lookup_overrides,
        lookup_project_source_tool_config,
    )
    from app.services.project_agent_template_service import export_project_capabilities_for_template

    env = project_api
    server = MCPServer(
        tenant_id=env.tenant_id,
        name=f"private-project-mcp-{uuid.uuid4().hex[:8]}",
        display_name="Private project MCP",
        base_url_template="https://private.project.test/mcp",
        headers_template={},
        created_by_user_id=env.owner_id,
    )
    env.db.add(server)
    await env.db.flush()
    tool = Tool(
        name=f"private_project_mcp_tool_{uuid.uuid4().hex[:8]}",
        display_name="Private project MCP tool",
        description="Private source tool",
        type="mcp",
        category="general",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        is_default=False,
        source="admin",
        tenant_id=env.tenant_id,
        mcp_server_id=server.id,
        mcp_server_name=server.display_name,
        mcp_tool_name="private_action",
    )
    env.db.add(tool)
    await env.db.flush()
    env.db.add_all(
        [
            AgentTool(
                agent_id=env.source_leader_id,
                tool_id=tool.id,
                enabled=True,
                config={"api_key": "private-agent-tool-secret"},
                source="user_installed",
                installed_by_agent_id=env.source_leader_id,
            ),
            MCPServerOverride(
                mcp_server_id=server.id,
                scope_type="agent",
                scope_id=env.source_leader_id,
                credential_template="private-source-secret",
                headers_template={"Authorization": "Bearer private-source-secret"},
                last_modified_by_user_id=env.owner_id,
            ),
        ]
    )
    await env.db.commit()

    created = await env.client.post(
        "/api/projects",
        json={
            "name": "Private MCP reference",
            "members": [
                {
                    "agent_id": str(env.source_leader_id),
                    "is_leader": True,
                    "settings": {
                        "tools": [
                            {
                                "tool_id": str(tool.id),
                                "enabled": True,
                                "config": {
                                    "scope": "project-only",
                                    "api_key": "submitted-project-secret",
                                    "token": "submitted-token",
                                    "auth": "submitted-auth",
                                    "headers": {"Authorization": "submitted-header-secret"},
                                },
                            }
                        ],
                    },
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    project_id = uuid.UUID(created.json()["id"])
    project = await env.db.get(Project, project_id)
    member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(ProjectMemberSnapshot.project_id == project_id)
        )
    ).scalar_one()
    project_override = (
        await env.db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == server.id,
                MCPServerOverride.scope_type == "agent",
                MCPServerOverride.scope_id == member.agent_id,
            )
        )
    ).scalar_one_or_none()
    assert project_override is None
    project_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == member.agent_id,
                AgentTool.tool_id == tool.id,
            )
        )
    ).scalar_one()
    assert project_assignment.config == {"scope": "project-only"}
    assert await lookup_project_source_tool_config(
        env.db,
        project_agent_id=member.agent_id,
        tool_id=tool.id,
        execution_user_id=env.owner_id,
    ) == {"api_key": "private-agent-tool-secret"}
    assert await lookup_project_source_tool_config(
        env.db,
        project_agent_id=member.agent_id,
        tool_id=tool.id,
        execution_user_id=env.viewer_id,
    ) == {}
    env.db.add(
        MCPServerOverride(
            mcp_server_id=server.id,
            scope_type="agent",
            scope_id=member.agent_id,
            credential_template="legacy-copied-secret",
            headers_template={"Authorization": "Bearer legacy-copied-secret"},
            last_modified_by_user_id=env.owner_id,
        )
    )
    await env.db.commit()

    _tenant_override, creator_override = await lookup_overrides(
        env.db,
        server.id,
        env.tenant_id,
        member.agent_id,
        execution_user_id=env.owner_id,
        allow_project_source_reference=True,
    )
    assert creator_override is not None
    assert creator_override.scope_id == env.source_leader_id
    assert creator_override.credential_template == "private-source-secret"

    _tenant_override, other_override = await lookup_overrides(
        env.db,
        server.id,
        env.tenant_id,
        member.agent_id,
        execution_user_id=env.viewer_id,
        allow_project_source_reference=True,
    )
    assert other_override is None

    exported = await export_project_capabilities_for_template(env.db, project)
    assert any(item.get("capability_id") == str(server.id) for item in exported)
    assert "private-source-secret" not in json.dumps(exported, ensure_ascii=False)
    assert "legacy-copied-secret" not in json.dumps(exported, ensure_ascii=False)
    assert "private-agent-tool-secret" not in json.dumps(exported, ensure_ascii=False)
    assert "submitted-project-secret" not in json.dumps(exported, ensure_ascii=False)

async def test_project_rejects_copying_private_mcp_credentials(project_api: ProjectApiEnv):
    env = project_api
    server = MCPServer(
        tenant_id=env.tenant_id,
        name=f"private-copy-mcp-{uuid.uuid4().hex[:8]}",
        display_name="Private copy MCP",
        base_url_template="https://private-copy.project.test/mcp",
        headers_template={},
        created_by_user_id=env.owner_id,
    )
    env.db.add(server)
    await env.db.flush()
    tool = Tool(
        name=f"private_copy_mcp_tool_{uuid.uuid4().hex[:8]}",
        display_name="Private copy MCP tool",
        description="Private source tool",
        type="mcp",
        category="general",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        is_default=False,
        source="agent",
        tenant_id=env.tenant_id,
        mcp_server_id=server.id,
        mcp_server_name=server.display_name,
        mcp_tool_name="private_copy_action",
    )
    env.db.add(tool)
    await env.db.flush()
    env.db.add(
        AgentTool(
            agent_id=env.source_leader_id,
            tool_id=tool.id,
            enabled=True,
            source="user_installed",
            installed_by_agent_id=env.source_leader_id,
        )
    )
    await env.db.commit()

    response = await env.client.post(
        "/api/projects",
        json={
            "name": "Reject private MCP copy",
            "members": [
                {
                    "agent_id": str(env.source_leader_id),
                    "is_leader": True,
                    "settings": {
                        "tools": [{"tool_id": str(tool.id), "enabled": True, "config": {}}],
                        "mcp_server_overrides": [
                            {
                                "server_id": str(server.id),
                                "credential_template": "must-not-copy",
                            }
                        ],
                    },
                }
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert "cannot be copied" in response.json()["detail"]
