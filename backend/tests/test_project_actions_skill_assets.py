from project_actions_support import *  # noqa: F401,F403

async def test_project_skill_assets_are_owner_managed_and_template_portable(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.mcp_server import MCPServer
    from app.models.project import ProjectCapabilityBinding, ProjectTemplate
    from app.models.skill import Skill, SkillFile
    from app.models.tool import AgentTool
    from app.services.project_agent_workspace import project_agent_workspace
    from app.services.storage import get_storage_backend, normalize_storage_key

    env = project_api
    source = await _create_project(env, name="Project Skill source")
    source_project_id = uuid.UUID(source["id"])
    worker_role = env.worker.role_description
    env.worker.autonomy_policy = {"write": "L2"}
    source_tool = (await env.db.execute(select(Tool).where(Tool.name == "send_message_to_parent"))).scalar_one()
    source_tool_id = source_tool.id
    source_tool_description = source_tool.description
    env.db.add(
        AgentTool(
            agent_id=env.source_worker_id,
            tool_id=source_tool_id,
            enabled=True,
            config={"api_key": "must-not-cross-project-boundary"},
            source="user_installed",
        )
    )
    mcp_server = MCPServer(
        tenant_id=env.tenant_id,
        name=f"private-runtime-{uuid.uuid4().hex[:8]}",
        display_name="Private Runtime MCP",
        base_url_template="https://example.invalid/mcp",
        headers_template={},
        instructions="Internal protocol instructions must not be product copy.",
    )
    shared_mcp_server = MCPServer(
        tenant_id=env.tenant_id,
        name=f"shared-runtime-{uuid.uuid4().hex[:8]}",
        display_name="Shared Runtime MCP",
        base_url_template="https://shared.example.invalid/mcp",
        headers_template={},
    )
    env.db.add_all([mcp_server, shared_mcp_server])
    await env.db.flush()
    source_mcp_tool = Tool(
        name=f"mcp_release_evidence_{uuid.uuid4().hex[:8]}",
        display_name="Release evidence query",
        description="Read release evidence from an approved connection.",
        type="mcp",
        category="engineering",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        source="agent",
        tenant_id=env.tenant_id,
        mcp_server_id=mcp_server.id,
    )
    shared_mcp_tool = Tool(
        name=f"shared_mcp_release_evidence_{uuid.uuid4().hex[:8]}",
        display_name="Shared release evidence query",
        description="Read release evidence from a shared connection.",
        type="mcp",
        category="engineering",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        source="admin",
        tenant_id=env.tenant_id,
        mcp_server_id=shared_mcp_server.id,
    )
    env.db.add_all([source_mcp_tool, shared_mcp_tool])
    await env.db.flush()
    source_mcp_assignment = AgentTool(
        agent_id=env.source_worker_id,
        tool_id=source_mcp_tool.id,
        enabled=True,
        config={"token": "must-not-cross-project-boundary"},
        source="user_installed",
    )
    env.db.add(source_mcp_assignment)
    await env.db.commit()

    bootstrap_response = await env.client.get("/api/projects/bootstrap-options")
    assert bootstrap_response.status_code == 200, bootstrap_response.text
    bootstrap_payload = bootstrap_response.json()
    bootstrap_capabilities = bootstrap_payload["capabilities"]
    bootstrap_tool = next(
        item for item in bootstrap_payload["tools"] if item["id"] == str(source_tool_id)
    )
    assert bootstrap_tool["name"] == source_tool.name
    assert bootstrap_tool["agent_config"] == {}
    bootstrap_mcp_tool = next(
        item
        for item in bootstrap_payload["tools"]
        if item["id"] == str(source_mcp_tool.id)
        and item["installed_by_agent_id"] == str(env.source_worker_id)
    )
    assert bootstrap_mcp_tool["mcp_server_name"] == mcp_server.display_name
    assert bootstrap_mcp_tool["agent_tool_source"] == "user_installed"
    assert bootstrap_mcp_tool["agent_config"] == {}
    assert bootstrap_mcp_tool["enabled"] is False
    assert all(item["type"] != "mcp" for item in bootstrap_capabilities)
    folder = f"release-check-{uuid.uuid4().hex[:8]}"
    source_prefix = normalize_storage_key(f"{env.source_worker_id}/skills/{folder}")
    storage = get_storage_backend()
    manifest_content = (
        "---\n"
        "name: Release Check\n"
        "version: 3\n"
        "description: Verify release evidence\n"
        "---\n\n"
        "# Release Check\n"
    )
    await storage.write_text(f"{source_prefix}/SKILL.md", manifest_content)
    await storage.write_text(f"{source_prefix}/references/checklist.md", "# Checklist\n")

    created_response = await env.client.post(
        f"/api/projects/{source_project_id}/agents",
        json={"source_agent_id": str(env.source_worker_id), "name": "Project release owner"},
    )
    assert created_response.status_code == 201, created_response.text
    project_agent = created_response.json()
    project_agent_id = uuid.UUID(project_agent["id"])

    explicit_tool_binding = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "tool",
            "capability_id": str(source_tool_id),
            "capability_name": source_tool.display_name,
            "source": "inherited",
            "inherited_from_agent_id": str(project_agent_id),
        },
    )
    explicit_mcp_binding = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "mcp",
            "capability_id": str(mcp_server.id),
            "capability_name": mcp_server.display_name,
            "source": "inherited",
            "inherited_from_agent_id": str(project_agent_id),
        },
    )
    shared_mcp_binding = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "mcp",
            "capability_id": str(shared_mcp_server.id),
            "capability_name": shared_mcp_server.display_name,
            "source": "inherited",
            "inherited_from_agent_id": str(project_agent_id),
        },
    )
    assert (
        explicit_tool_binding.status_code
        == explicit_mcp_binding.status_code
        == shared_mcp_binding.status_code
        == 201
    )

    capabilities_response = await env.client.get(f"/api/projects/{source_project_id}/capabilities")
    assert capabilities_response.status_code == 200, capabilities_response.text
    skill_binding = next(
        item
        for item in capabilities_response.json()
        if item["capability_type"] == "skill" and item["inherited_from_agent_id"] == str(project_agent_id)
    )
    tool_binding = next(
        item
        for item in capabilities_response.json()
        if item["capability_type"] == "tool" and item["inherited_from_agent_id"] == str(project_agent_id)
    )
    mcp_binding = next(
        item
        for item in capabilities_response.json()
        if item["capability_type"] == "mcp" and item["inherited_from_agent_id"] == str(project_agent_id)
    )
    assert tool_binding["capability_id"] == str(source_tool_id)
    assert tool_binding["key"] == source_tool.name
    assert tool_binding["availability"] == "available"
    assert tool_binding["description"] == source_tool_description
    assert tool_binding["config"] == {}
    copied_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == project_agent_id,
                AgentTool.tool_id == source_tool_id,
            )
        )
    ).scalar_one()
    assert copied_assignment.enabled is True
    assert copied_assignment.config == {}
    copied_mcp_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == project_agent_id,
                AgentTool.tool_id == source_mcp_tool.id,
            )
        )
    ).scalar_one()
    assert copied_mcp_assignment.enabled is True
    assert copied_mcp_assignment.config == {}
    disabled_tool_binding = await env.client.patch(
        f"/api/projects/{source_project_id}/capabilities/{tool_binding['id']}",
        json={"is_enabled": False},
    )
    disabled_mcp_binding = await env.client.patch(
        f"/api/projects/{source_project_id}/capabilities/{mcp_binding['id']}",
        json={"is_enabled": False},
    )
    assert disabled_tool_binding.status_code == disabled_mcp_binding.status_code == 200
    await env.db.refresh(copied_assignment)
    await env.db.refresh(copied_mcp_assignment)
    await env.db.refresh(source_mcp_assignment)
    assert copied_assignment.enabled is False
    assert copied_mcp_assignment.enabled is False
    assert source_mcp_assignment.enabled is True
    source_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == env.source_worker_id,
                AgentTool.tool_id == source_tool_id,
            )
        )
    ).scalar_one()
    assert source_assignment.enabled is True
    assert source_tool.enabled is True
    assert source_mcp_tool.enabled is True
    assert await env.db.get(MCPServer, mcp_server.id) is not None
    assert (
        await env.client.patch(
            f"/api/projects/{source_project_id}/capabilities/{tool_binding['id']}",
            json={"is_enabled": True},
        )
    ).status_code == 200
    assert (
        await env.client.patch(
            f"/api/projects/{source_project_id}/capabilities/{mcp_binding['id']}",
            json={"is_enabled": True},
        )
    ).status_code == 200
    source_tool_row = await env.db.get(Tool, source_tool_id)
    assert source_tool_row is not None
    source_tool_row.enabled = False
    await env.db.commit()
    restricted_capabilities = (
        await env.client.get(f"/api/projects/{source_project_id}/capabilities")
    ).json()
    assert next(item for item in restricted_capabilities if item["id"] == tool_binding["id"])[
        "availability"
    ] == "restricted"
    source_tool_row = await env.db.get(Tool, source_tool_id)
    assert source_tool_row is not None
    source_tool_row.enabled = True
    await env.db.commit()
    stored_source_binding = await env.db.get(ProjectCapabilityBinding, uuid.UUID(skill_binding["id"]))
    assert stored_source_binding is not None
    metadata = stored_source_binding.config["skill_asset"]
    assert metadata == {
        "schema_version": 2,
        "asset_id": metadata["asset_id"],
        "version": "3",
        "sha256": metadata["sha256"],
        "path": f"skills/{folder}",
        "source": "agent",
        "source_agent_id": str(env.source_worker_id),
        "file_count": 2,
        "size_bytes": len(manifest_content.encode()) + len("# Checklist\n".encode()),
    }
    assert skill_binding["config"] == {}
    assert skill_binding["availability"] == "available"
    assert skill_binding["description"] == "Verify release evidence"
    assert skill_binding["version"] == "3"
    assert skill_binding["file_count"] == 2
    assert skill_binding["size_bytes"] == metadata["size_bytes"]
    source_project = await env.db.get(Project, source_project_id)
    assert source_project is not None
    source_layout = project_agent_workspace(
        project_repo_path(source_project.tenant_id, source_project.id),
        project_agent_id,
    )
    copied_manifest = source_layout.root / "skills" / folder / "SKILL.md"
    assert copied_manifest.read_text(encoding="utf-8") == manifest_content

    library_folder = f"library-check-{uuid.uuid4().hex[:8]}"
    library_skill = Skill(
        tenant_id=env.tenant_id,
        name="Library Check",
        description="Validate a library-backed release check",
        category="engineering",
        folder_name=library_folder,
        version=5,
        visibility="tenant",
        status="published",
        publisher_agent_id=env.source_worker_id,
    )
    env.db.add(library_skill)
    await env.db.flush()
    library_skill_id = library_skill.id
    env.db.add_all(
        [
            SkillFile(
                skill_id=library_skill_id,
                path="SKILL.md",
                content="---\nname: Library Check\ndescription: Validate a library check\n---\n",
            ),
            SkillFile(skill_id=library_skill_id, path="references/guide.md", content="# Guide\n"),
        ]
    )
    await env.db.commit()
    rejected_shared_skill = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={"capability_type": "skill", "capability_id": str(library_skill_id), "source": "shared"},
    )
    assert rejected_shared_skill.status_code == 422
    library_binding_response = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "skill",
            "capability_id": str(library_skill_id),
            "source": "inherited",
            "inherited_from_agent_id": str(project_agent_id),
        },
    )
    assert library_binding_response.status_code == 201, library_binding_response.text
    library_binding = library_binding_response.json()
    assert library_binding["availability"] == "available"
    assert library_binding["version"] == "5"
    assert library_binding["file_count"] == 2
    assert library_binding["description"] == "Validate a library-backed release check"
    assert library_binding["config"] == {}
    library_root = source_layout.root / "skills" / library_folder
    assert (library_root / "SKILL.md").is_file()
    hidden_library_root = library_root.with_name(f".{library_root.name}.missing")
    os.replace(library_root, hidden_library_root)
    missing_capabilities = (await env.client.get(f"/api/projects/{source_project_id}/capabilities")).json()
    assert next(item for item in missing_capabilities if item["id"] == library_binding["id"])[
        "availability"
    ] == "missing"
    os.replace(hidden_library_root, library_root)
    duplicate_library_binding = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "skill",
            "capability_id": str(library_skill_id),
            "source": "inherited",
            "inherited_from_agent_id": str(project_agent_id),
        },
    )
    assert duplicate_library_binding.status_code == 409
    secret_folder = f"secret-skill-{uuid.uuid4().hex[:8]}"
    secret_skill = Skill(
        tenant_id=env.tenant_id,
        name="Unsafe Skill",
        description="Must not cross the project boundary",
        category="engineering",
        folder_name=secret_folder,
        version=1,
        visibility="tenant",
        status="published",
    )
    env.db.add(secret_skill)
    await env.db.flush()
    secret_skill_id = secret_skill.id
    env.db.add(
        SkillFile(
            skill_id=secret_skill_id,
            path="SKILL.md",
            content="---\nname: Unsafe Skill\n---\napi_key = 'abcdefghijklmnop123456'\n",
        )
    )
    await env.db.commit()
    rejected_secret = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "skill",
            "capability_id": str(secret_skill_id),
            "source": "inherited",
            "inherited_from_agent_id": str(project_agent_id),
        },
    )
    assert rejected_secret.status_code == 422
    assert not (source_layout.root / "skills" / secret_folder).exists()

    manifest_response = await env.client.get(f"/api/projects/{source_project_id}/template-manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_skill = next(
        item for item in manifest_response.json()["skills"] if item["binding_id"] == skill_binding["id"]
    )
    assert manifest_skill["selected"] is False
    assert manifest_skill["selection_state"] == "unselected"
    assert manifest_skill["affected_member_count"] == 1
    assert {"path", "sha256", "asset_id"}.isdisjoint(manifest_skill)
    manifest_tool = next(
        item
        for item in manifest_response.json()["capabilities"]
        if item["type"] == "tool" and item["key"] == source_tool.name
    )
    assert manifest_tool["selected"] is True
    assert manifest_tool["affected_member_count"] == 1
    assert {
        key: manifest_skill[key]
        for key in (
            "binding_id",
            "member_id",
            "member_agent_id",
            "member_name",
            "member_role",
            "name",
            "version",
            "file_count",
            "size_bytes",
        )
    } == {
        "binding_id": skill_binding["id"],
        "member_id": project_agent["member_id"],
        "member_agent_id": str(project_agent_id),
        "member_name": "Project release owner",
        "member_role": worker_role,
        "name": "Release Check",
        "version": "3",
        "file_count": 2,
        "size_bytes": metadata["size_bytes"],
    }

    shared = await env.client.patch(
        f"/api/projects/{source_project_id}",
        json={
            "visibility": "shared",
            "shared_with_user_ids": [str(env.viewer_id)],
        },
    )
    assert shared.status_code == 200, shared.text
    grant = (
        await env.db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == source_project_id,
                ProjectAccessGrant.user_id == env.viewer_id,
            )
        )
    ).scalar_one()
    grant.role = "edit"
    await env.db.commit()
    env.authenticate_as(env.viewer_id)
    assert (await env.client.get(f"/api/projects/{source_project_id}/capabilities")).status_code == 200
    denied_create = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={"capability_type": "tool", "capability_name": "editor-tool"},
    )
    assert denied_create.status_code == 404
    denied_patch = await env.client.patch(
        f"/api/projects/{source_project_id}/capabilities/{skill_binding['id']}",
        json={"is_enabled": False},
    )
    assert denied_patch.status_code == 404
    env.authenticate_as(env.owner_id)

    default_template_response = await env.client.post(
        f"/api/projects/{source_project_id}/templates",
        json={"name": "No Skills by default", "version": "1.0.0"},
    )
    assert default_template_response.status_code == 201, default_template_response.text
    default_template = await env.db.get(ProjectTemplate, uuid.UUID(default_template_response.json()["id"]))
    assert default_template is not None
    assert default_template.definition["skill_assets"] == []
    assert default_template_response.json()["skills"] == []

    selected_template_response = await env.client.post(
        f"/api/projects/{source_project_id}/templates",
        json={
            "name": "Release Skill template",
            "version": "1.0.0",
            "is_published": True,
            "included_skill_binding_ids": [skill_binding["id"]],
        },
    )
    assert selected_template_response.status_code == 201, selected_template_response.text
    selected_template_body = selected_template_response.json()
    assert selected_template_body["skills"] == [
        {
            "name": "Release Check",
            "version": "3",
            "member_name": "Project release owner",
            "member_role": worker_role,
            "file_count": 2,
            "size_bytes": metadata["size_bytes"],
        }
    ]
    selected_template = await env.db.get(ProjectTemplate, uuid.UUID(selected_template_body["id"]))
    assert selected_template is not None
    packaged_skill = selected_template.definition["skill_assets"][0]
    assert packaged_skill["sha256"] == metadata["sha256"]
    assert packaged_skill["digital_employee_index"] == 3
    assert skill_binding["id"] not in str(packaged_skill)
    assert str(env.source_worker_id) not in str(packaged_skill)
    assert "capability_id" not in packaged_skill
    packaged_tool = next(
        item for item in selected_template.definition["capabilities"] if item["capability_type"] == "tool"
    )
    packaged_mcp = [
        item for item in selected_template.definition["capabilities"] if item["capability_type"] == "mcp"
    ]
    assert packaged_tool["capability_id"] == str(source_tool_id)
    assert [item["capability_id"] for item in packaged_mcp] == [str(shared_mcp_server.id)]
    assert packaged_mcp[0]["source"] == "inherited"
    assert packaged_mcp[0]["digital_employee_index"] == 3
    assert str(mcp_server.id) not in str(selected_template.definition["capabilities"])
    assert "config" not in packaged_tool
    assert "must-not-cross-project-boundary" not in str(selected_template.definition)

    # Older published snapshots may still contain registry identifiers that
    # were portable before the canonical authorization boundary existed.
    # They must not become authority when another user instantiates them.
    selected_template.definition = {
        **selected_template.definition,
        "capabilities": [
            *selected_template.definition["capabilities"],
            {
                "schema_version": 1,
                "capability_type": "mcp",
                "capability_id": str(mcp_server.id),
                "capability_name": mcp_server.display_name,
                "source": "inherited",
                "digital_employee_index": 3,
                "is_enabled": True,
                "scope": {},
            },
                {
                    "schema_version": 1,
                    "capability_type": "skill",
                    "capability_id": str(library_skill_id),
                    "capability_name": "Library Check",
                "source": "inherited",
                "digital_employee_index": 3,
                "is_enabled": True,
                "scope": {},
            },
        ],
    }
    await env.db.commit()

    env.authenticate_as(env.viewer_id)
    restored_response = await env.client.post(
        "/api/projects/from-template",
        json={"template_id": selected_template_body["id"], "name": "Restored Skill project"},
    )
    assert restored_response.status_code == 201, restored_response.text
    restored = restored_response.json()
    restored_project_id = uuid.UUID(restored["id"])
    assert restored["template_setup_summary"]["restored_skill_count"] == 1
    restored_agents = (await env.client.get(f"/api/projects/{restored_project_id}/agents")).json()
    assert len(restored_agents) == 4
    restored_agent_id = uuid.UUID(
        next(item for item in restored_agents if item["name"] == "Project release owner")["id"]
    )
    assert restored_agent_id != project_agent_id
    restored_binding = (
        await env.db.execute(
            select(ProjectCapabilityBinding).where(
                ProjectCapabilityBinding.project_id == restored_project_id,
                ProjectCapabilityBinding.capability_type == "skill",
            )
        )
    ).scalar_one()
    assert restored_binding.capability_id is None
    assert restored_binding.inherited_from_agent_id == restored_agent_id
    assert restored_binding.config["skill_asset"]["source"] == "template"
    assert restored_binding.config["skill_asset"]["source_agent_id"] is None
    restored_tool_bindings = list(
        (
            await env.db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == restored_project_id,
                    ProjectCapabilityBinding.capability_type == "tool",
                )
            )
        ).scalars()
    )
    restored_tool_binding = next(
        item for item in restored_tool_bindings if item.inherited_from_agent_id == restored_agent_id
    )
    assert restored_tool_binding.inherited_from_agent_id == restored_agent_id
    restored_assignment = (
        await env.db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == restored_agent_id,
                AgentTool.tool_id == source_tool_id,
            )
        )
    ).scalar_one()
    assert restored_assignment.enabled is True
    assert restored_assignment.config == {}
    restored_mcp_bindings = list(
        (
            await env.db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == restored_project_id,
                    ProjectCapabilityBinding.capability_type == "mcp",
                )
            )
        ).scalars()
    )
    assert [(item.capability_id, item.source) for item in restored_mcp_bindings] == [
        (shared_mcp_server.id, "inherited")
    ]
    assert restored_mcp_bindings[0].inherited_from_agent_id == restored_agent_id
    restored_project = await env.db.get(Project, restored_project_id)
    assert restored_project is not None
    restored_layout = project_agent_workspace(
        project_repo_path(restored_project.tenant_id, restored_project.id),
        restored_agent_id,
    )
    assert (restored_layout.root / "skills" / folder / "SKILL.md").read_text(encoding="utf-8") == manifest_content

    await _exercise_project_skill_assets_followup(
        env=env,
        source_project_id=source_project_id,
        project_agent_id=project_agent_id,
        copied_manifest=copied_manifest,
        manifest_content=manifest_content,
        skill_binding=skill_binding,
        source_project=source_project,
        metadata=metadata,
        folder=folder,
        library_skill_id=library_skill_id,
        library_binding=library_binding,
        library_folder=library_folder,
        library_root=library_root,
        selected_template_body=selected_template_body,
        monkeypatch=monkeypatch,
    )
