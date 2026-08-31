from project_actions_support import *  # noqa: F401,F403

async def test_project_agent_template_api_round_trip_preserves_assets_with_fresh_identity(
    project_api: ProjectApiEnv,
):
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectCapabilityBinding, ProjectMemberSnapshot, ProjectTemplate

    env = project_api
    source = await _create_project(env, name="Template source")
    source_project_id = uuid.UUID(source["id"])

    created_agent_response = await env.client.post(
        f"/api/projects/{source_project_id}/agents",
        json={
            "name": "Project release specialist",
            "role_description": "Own release readiness inside this project",
            "soul": "# Release specialist\nKeep delivery evidence concise.\n",
            "core_memory": "# Durable context\nThe acceptance gate requires a signed release checklist.\n",
        },
    )
    assert created_agent_response.status_code == 201, created_agent_response.text
    source_agent = created_agent_response.json()

    portable_tool = (
        await env.db.execute(select(Tool).where(Tool.name == "send_message_to_parent"))
    ).scalar_one()
    inherited_tool_response = await env.client.post(
        f"/api/projects/{source_project_id}/capabilities",
        json={
            "capability_type": "tool",
            "capability_id": str(portable_tool.id),
            "source": "inherited",
            "inherited_from_agent_id": source_agent["id"],
        },
    )
    assert inherited_tool_response.status_code == 201, inherited_tool_response.text
    deactivated_source_agent = await env.client.post(
        f"/api/projects/{source_project_id}/agents/{source_agent['id']}/deactivate"
    )
    assert deactivated_source_agent.status_code == 200, deactivated_source_agent.text

    source_project = await env.db.get(Project, source_project_id)
    assert source_project is not None
    source_repo = project_repo_path(source_project.tenant_id, source_project.id)
    (source_repo / ".gitignore").write_text("frontend/dist/\n", encoding="utf-8")
    ignored_asset = source_repo / "frontend" / "dist" / "app.js"
    ignored_asset.parent.mkdir(parents=True, exist_ok=True)
    ignored_asset.write_text("console.log('portable build');\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source_repo), "add", ".gitignore"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source_repo), "add", "-f", "frontend/dist/app.js"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(source_repo),
            "-c",
            "user.name=Project test",
            "-c",
            "user.email=project@test.invalid",
            "commit",
            "-m",
            "Add portable ignored build asset",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    env.authenticate_as(env.viewer_id)
    forbidden_template_response = await env.client.post(
        f"/api/projects/{source_project_id}/templates",
        json={"name": "Unauthorized export"},
    )
    assert forbidden_template_response.status_code == 404
    env.authenticate_as(env.owner_id)

    template_response = await env.client.post(
        f"/api/projects/{source_project_id}/templates",
        json={
            "name": "Release readiness template",
            "description": "Reusable release workflow",
            "category": "engineering",
            "version": "1.0.0",
        },
    )
    assert template_response.status_code == 201, template_response.text
    template = template_response.json()
    assert "agents" not in template["definition"]
    assert len(template["definition"]["roles"]) == 4
    release_role = next(
        item for item in template["definition"]["roles"] if item["name"] == "Project release specialist"
    )
    assert release_role["description"] == "Own release readiness inside this project"
    assert template["definition"]["asset_summary"]["digital_employee_count"] == 4

    stored_template = await env.db.get(ProjectTemplate, uuid.UUID(template["id"]))
    assert stored_template is not None
    assert "frontend/dist/app.js" in {
        item["path"] for item in stored_template.definition["project_snapshot"]["files"]
    }
    exported_agents = stored_template.definition["agents"]
    assert len(exported_agents) == 4
    exported_agent = next(item for item in exported_agents if item["name"] == "Project release specialist")
    assert {
        key: exported_agent[key]
        for key in (
            "name",
            "role_description",
            "is_leader",
            "is_enabled",
            "soul",
            "core_memory",
            "workspace_files",
        )
    } == {
        "name": "Project release specialist",
        "role_description": "Own release readiness inside this project",
        "is_leader": False,
        "is_enabled": False,
        "soul": "# Release specialist\nKeep delivery evidence concise.\n",
        "core_memory": "# Durable context\nThe acceptance gate requires a signed release checklist.\n",
        "workspace_files": [],
    }
    assert exported_agent["runtime"]["max_tool_rounds"] > 0
    assert exported_agent["member_config"]["enabled_project_tools"] == []
    assert source_agent["id"] not in str(stored_template.definition)
    assert str(source_project_id) not in str(stored_template.definition)

    target_response = await env.client.post(
        "/api/projects/from-template",
        json={
            "template_id": template["id"],
            "name": "Template target",
            "visibility": "private",
        },
    )
    assert target_response.status_code == 201, target_response.text
    target = target_response.json()
    target_project_id = uuid.UUID(target["id"])
    assert target_project_id != source_project_id
    assert target["goal"] == source["goal"]
    assert target["success_criteria"] == source["success_criteria"]
    assert target["template_setup_summary"] == {
        "restored_file_count": len(stored_template.definition["project_snapshot"]["files"]),
        "restored_digital_employee_count": 4,
        "restored_skill_count": 0,
        "restored_connection_count": 0,
        "restored_tool_count": 1,
    }
    target_project = await env.db.get(Project, target_project_id)
    assert target_project is not None
    target_repo = project_repo_path(target_project.tenant_id, target_project.id)
    assert (target_repo / "frontend" / "dist" / "app.js").read_text(
        encoding="utf-8"
    ) == "console.log('portable build');\n"
    assert (
        subprocess.run(
            ["git", "-C", str(target_repo), "ls-files", "--error-unmatch", "frontend/dist/app.js"],
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )

    target_agents_response = await env.client.get(f"/api/projects/{target_project_id}/agents")
    assert target_agents_response.status_code == 200, target_agents_response.text
    target_agents = target_agents_response.json()
    assert len(target_agents) == 4
    target_agent = next(item for item in target_agents if item["name"] == source_agent["name"])
    assert target_agent["id"] != source_agent["id"]
    assert target_agent["name"] == source_agent["name"]
    assert target_agent["soul"] == source_agent["soul"]
    assert target_agent["core_memory"] == source_agent["core_memory"]
    assert target_agent["is_leader"] is False

    member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == target_project_id,
                ProjectMemberSnapshot.agent_id == uuid.UUID(target_agent["id"]),
            )
        )
    ).scalar_one()
    assert member.is_enabled is False
    assert member.is_leader is False
    restored_departed_capability = (
        await env.db.execute(
            select(ProjectCapabilityBinding).where(
                ProjectCapabilityBinding.project_id == target_project_id,
                ProjectCapabilityBinding.inherited_from_agent_id == uuid.UUID(target_agent["id"]),
                ProjectCapabilityBinding.capability_id == portable_tool.id,
            )
        )
    ).scalar_one()
    assert restored_departed_capability.is_enabled is False

    sessions = (
        (await env.db.execute(select(ChatSession).where(ChatSession.project_id == target_project_id))).scalars().all()
    )
    assert {(session.source_channel, session.is_group) for session in sessions} == {
        ("project", True),
        ("web", False),
    }
    target_leader = next(item for item in target_agents if item["is_leader"])
    assert {session.agent_id for session in sessions} == {uuid.UUID(target_leader["id"])}

async def test_project_skill_legacy_backfill_dry_run_apply_and_rollback_are_auditable(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectCapabilityBinding, ProjectEvent
    from app.services.project_agent_workspace import project_agent_workspace

    env = project_api
    source = await _create_project(env, name="Legacy Skill normalization")
    project_id = uuid.UUID(source["id"])
    created_agent_response = await env.client.post(
        f"/api/projects/{project_id}/agents",
        json={"name": "Legacy Skill owner", "role_description": "Maintain project knowledge"},
    )
    assert created_agent_response.status_code == 201, created_agent_response.text
    project_agent_id = uuid.UUID(created_agent_response.json()["id"])
    manifests = {
        "Legacy Zero": "---\nname: Legacy Zero\nversion: 7\n---\n\n# Zero\n",
        "Legacy One": "---\nname: Legacy One\nversion: 8\n---\n\n# One\n",
    }
    folders = {"Legacy Zero": "legacy-zero", "Legacy One": "legacy-one"}
    for name, content in manifests.items():
        response = await env.client.put(
            f"/api/agents/{project_agent_id}/files/content",
            params={"path": f"skills/{folders[name]}/SKILL.md"},
            json={"content": content},
        )
        assert response.status_code == 200, response.text

    bindings = list(
        (
            await env.db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project_id,
                    ProjectCapabilityBinding.capability_type == "skill",
                )
            )
        ).scalars()
    )
    by_name = {binding.capability_name: binding for binding in bindings}
    zero = by_name["Legacy Zero"]
    one = by_name["Legacy One"]
    disabled_one = await env.client.patch(
        f"/api/projects/{project_id}/capabilities/{one.id}",
        json={"is_enabled": False},
    )
    assert disabled_one.status_code == 200, disabled_one.text
    await env.db.refresh(one)
    current_one_metadata = dict(one.config["skill_asset"])
    v1_metadata = {key: value for key, value in current_one_metadata.items() if key != "asset_id"}
    v1_metadata["schema_version"] = 1
    project = await env.db.get(Project, project_id)
    assert project is not None
    layout = project_agent_workspace(project_repo_path(project.tenant_id, project.id), project_agent_id)
    current_disabled_root = (
        layout.root
        / ".disabled-skills"
        / current_one_metadata["asset_id"]
        / folders["Legacy One"]
    )
    legacy_disabled_root = (
        layout.root
        / ".disabled-skills"
        / current_one_metadata["sha256"]
        / folders["Legacy One"]
    )
    legacy_disabled_root.parent.mkdir(parents=True, exist_ok=True)
    os.replace(current_disabled_root, legacy_disabled_root)
    zero.config = {"legacy_note": "keep-zero"}
    one.config = {"legacy_note": "keep-one", "skill_asset": v1_metadata}
    await env.db.commit()

    env.authenticate_as(env.viewer_id)
    hidden = await env.client.post(
        f"/api/projects/{project_id}/capabilities/skill-backfill",
        json={"action": "dry_run"},
    )
    assert hidden.status_code == 404
    env.authenticate_as(env.owner_id)

    preview = await env.client.post(
        f"/api/projects/{project_id}/capabilities/skill-backfill",
        json={"action": "dry_run"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["operation_id"] is None
    assert preview.json()["ready_count"] == 2
    assert preview.json()["blocked_count"] == 0
    assert {item["previous_schema_version"] for item in preview.json()["items"]} == {0, 1}
    await env.db.refresh(zero)
    await env.db.refresh(one)
    assert zero.config == {"legacy_note": "keep-zero"}
    assert one.config == {"legacy_note": "keep-one", "skill_asset": v1_metadata}
    dry_run_events = list(
        (
            await env.db.execute(
                select(ProjectEvent).where(
                    ProjectEvent.project_id == project_id,
                    ProjectEvent.event_type.like("capability.skill_backfill.%"),
                )
            )
        ).scalars()
    )
    assert dry_run_events == []

    applied = await env.client.post(
        f"/api/projects/{project_id}/capabilities/skill-backfill",
        json={"action": "apply"},
    )
    assert applied.status_code == 200, applied.text
    applied_body = applied.json()
    assert applied_body["applied_count"] == 2
    operation_id = uuid.UUID(applied_body["operation_id"])
    await env.db.refresh(zero)
    await env.db.refresh(one)
    for binding, note in ((zero, "keep-zero"), (one, "keep-one")):
        assert binding.config["legacy_note"] == note
        assert binding.config["skill_asset"]["schema_version"] == 2
        uuid.UUID(binding.config["skill_asset"]["asset_id"])

    assert (layout.root / "skills" / folders["Legacy Zero"] / "SKILL.md").read_text(
        encoding="utf-8"
    ) == manifests["Legacy Zero"]
    applied_one_metadata = one.config["skill_asset"]
    assert (
        layout.root
        / ".disabled-skills"
        / applied_one_metadata["asset_id"]
        / folders["Legacy One"]
        / "SKILL.md"
    ).read_text(encoding="utf-8") == manifests["Legacy One"]
    apply_event = await env.db.get(ProjectEvent, operation_id)
    assert apply_event is not None
    assert apply_event.event_type == "capability.skill_backfill.applied"
    assert apply_event.actor_user_id == env.owner_id
    assert apply_event.event_metadata["applied_count"] == 2
    assert len(apply_event.event_metadata["rollback_entries"]) == 2
    assert all(
        {"path", "sha256", "config"}.isdisjoint(entry)
        for entry in apply_event.event_metadata["rollback_entries"]
    )

    no_op = await env.client.post(
        f"/api/projects/{project_id}/capabilities/skill-backfill",
        json={"action": "apply"},
    )
    assert no_op.status_code == 200, no_op.text
    assert no_op.json()["applied_count"] == 0
    assert no_op.json()["operation_id"] is None

    rolled_back = await env.client.post(
        f"/api/projects/{project_id}/capabilities/skill-backfill",
        json={"action": "rollback", "operation_id": str(operation_id)},
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["rolled_back_count"] == 2
    await env.db.refresh(zero)
    await env.db.refresh(one)
    assert zero.config == {"legacy_note": "keep-zero"}
    assert one.config == {"legacy_note": "keep-one", "skill_asset": v1_metadata}
    assert (legacy_disabled_root / "SKILL.md").read_text(encoding="utf-8") == manifests["Legacy One"]
    rollback_event = await env.db.get(ProjectEvent, uuid.UUID(rolled_back.json()["rollback_event_id"]))
    assert rollback_event is not None
    assert rollback_event.event_metadata["operation_id"] == str(operation_id)
    assert rollback_event.actor_user_id == env.owner_id
    duplicate_rollback = await env.client.post(
        f"/api/projects/{project_id}/capabilities/skill-backfill",
        json={"action": "rollback", "operation_id": str(operation_id)},
    )
    assert duplicate_rollback.status_code == 409

    missing_root = layout.root / "skills" / folders["Legacy Zero"]
    hidden_root = layout.root / ".legacy-zero-missing"
    os.replace(missing_root, hidden_root)
    try:
        blocked_preview = await env.client.post(
            f"/api/projects/{project_id}/capabilities/skill-backfill",
            json={"action": "dry_run"},
        )
        assert blocked_preview.status_code == 200, blocked_preview.text
        assert blocked_preview.json()["blocked_count"] == 1
        rejected_apply = await env.client.post(
            f"/api/projects/{project_id}/capabilities/skill-backfill",
            json={"action": "apply"},
        )
        assert rejected_apply.status_code == 409
        await env.db.refresh(one)
        assert one.config == {"legacy_note": "keep-one", "skill_asset": v1_metadata}
    finally:
        os.replace(hidden_root, missing_root)

async def test_template_manifest_and_restore_include_legacy_members_effective_platform_dependencies(
    project_api: ProjectApiEnv,
):
    from app.models.mcp_server import MCPServer
    from app.models.project import ProjectCapabilityBinding, ProjectTemplate
    from app.models.tool import AgentTool

    env = project_api
    for agent in (env.leader, env.worker, env.reviewer):
        agent.autonomy_policy = {"write": "L2"}
    common_tool = Tool(
        name=f"template-common-{uuid.uuid4().hex[:8]}",
        display_name="Shared delivery checklist",
        description="Review the delivery checklist assigned to a project member.",
        type="builtin",
        category="project",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        source="builtin",
    )
    server = MCPServer(
        tenant_id=env.tenant_id,
        name=f"template-evidence-{uuid.uuid4().hex[:8]}",
        display_name="Evidence catalog",
        base_url_template="https://evidence.example.test/mcp",
    )
    env.db.add_all([common_tool, server])
    await env.db.flush()
    mcp_tool = Tool(
        name=f"template-mcp-{uuid.uuid4().hex[:8]}",
        display_name="Read evidence catalog",
        description="Read evidence available to the organization.",
        type="mcp",
        category="project",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        source="admin",
        tenant_id=env.tenant_id,
        mcp_server_id=server.id,
        mcp_server_name=server.display_name,
    )
    env.db.add(mcp_tool)
    await env.db.flush()
    env.db.add_all(
        [
            AgentTool(
                agent_id=agent_id,
                tool_id=common_tool.id,
                enabled=True,
                source="user_installed",
                config={"access_token": "must-not-enter-template"},
            )
            for agent_id in (env.leader_id, env.worker_id, env.reviewer_id)
        ]
        + [
            AgentTool(
                agent_id=env.leader_id,
                tool_id=mcp_tool.id,
                enabled=True,
                source="user_installed",
                config={"credential": "must-not-enter-template"},
            )
        ]
    )
    await env.db.commit()

    source_response = await env.client.post(
        "/api/projects",
        json={
            "name": "Legacy member template source",
            "goal": "Preserve the actual member setup",
            "members": [
                {
                    "agent_id": str(env.leader_id),
                    "is_leader": True,
                    "enabled_inherited_capability_ids": [str(common_tool.id), str(mcp_tool.id)],
                },
                {
                    "agent_id": str(env.worker_id),
                    "enabled_inherited_capability_ids": [str(common_tool.id)],
                },
                {
                    "agent_id": str(env.reviewer_id),
                    "enabled_inherited_capability_ids": [str(common_tool.id)],
                },
            ],
            "capabilities": [],
        },
    )
    assert source_response.status_code == 201, source_response.text
    source_project_id = source_response.json()["id"]
    members_response = await env.client.get(f"/api/projects/{source_project_id}/members")
    assert members_response.status_code == 200, members_response.text
    member_agent_ids = {
        item["name_snapshot"]: item["agent_id"] for item in members_response.json()
    }
    reviewer_member = next(
        item for item in members_response.json() if item["name_snapshot"] == env.reviewer.name
    )
    disable_response = await env.client.put(
        f"/api/projects/{source_project_id}/members/{reviewer_member['id']}/tools",
        json=[{"tool_id": str(common_tool.id), "enabled": False}],
    )
    assert disable_response.status_code == 200, disable_response.text
    disabled_common = next(item for item in disable_response.json() if item["id"] == str(common_tool.id))
    assert disabled_common["enabled"] is False

    manifest_response = await env.client.get(f"/api/projects/{source_project_id}/template-manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()
    assert manifest["asset_summary"]["digital_employee_count"] == 3
    assert len(manifest["roles"]) == 3
    assert manifest["asset_summary"]["capability_count"] == 2
    common_manifest = next(
        item for item in manifest["capabilities"] if item["capability_id"] == str(common_tool.id)
    )
    assert common_manifest["key"] == common_tool.name
    assert common_manifest["description"] == common_tool.description
    assert common_manifest["availability"] == "available"
    assert common_manifest["affected_member_count"] == 2
    assert {item["agent_id"] for item in common_manifest["affected_members"]} == {
        member_agent_ids["Leader"],
        member_agent_ids["Worker"],
    }
    mcp_manifest = next(
        item for item in manifest["capabilities"] if item["capability_id"] == str(server.id)
    )
    assert mcp_manifest["key"] == server.name
    assert mcp_manifest["affected_member_count"] == 1
    assert mcp_manifest["affected_members"][0]["agent_id"] == member_agent_ids["Leader"]
    assert "must-not-enter-template" not in json.dumps(manifest)

    template_response = await env.client.post(
        f"/api/projects/{source_project_id}/templates",
        json={"name": "Legacy member setup", "version": "1.0.0"},
    )
    assert template_response.status_code == 201, template_response.text
    stored_template = await env.db.get(ProjectTemplate, uuid.UUID(template_response.json()["id"]))
    assert stored_template is not None
    assert len(stored_template.definition["agents"]) == 3
    assert len(stored_template.definition["capabilities"]) == 4
    assert "must-not-enter-template" not in json.dumps(stored_template.definition)

    restored_response = await env.client.post(
        "/api/projects/from-template",
        json={
            "template_id": str(stored_template.id),
            "name": "Legacy member template target",
            "visibility": "private",
        },
    )
    assert restored_response.status_code == 201, restored_response.text
    restored = restored_response.json()
    assert restored["template_setup_summary"]["restored_digital_employee_count"] == 3
    assert restored["template_setup_summary"]["restored_tool_count"] == 1
    assert restored["template_setup_summary"]["restored_connection_count"] == 1
    target_project_id = uuid.UUID(restored["id"])
    target_agents = (await env.client.get(f"/api/projects/{target_project_id}/agents")).json()
    assert len(target_agents) == 3
    assert {item["name"] for item in target_agents} == {"Leader", "Worker", "Reviewer"}
    target_agent_ids = {uuid.UUID(item["id"]) for item in target_agents}
    restored_bindings = list(
        (
            await env.db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == target_project_id,
                )
            )
        ).scalars()
    )
    assert len(restored_bindings) == 4
    restored_assignments = list(
        (
            await env.db.execute(select(AgentTool).where(AgentTool.agent_id.in_(target_agent_ids)))
        ).scalars()
    )
    restored_dependency_assignments = [
        item for item in restored_assignments if item.tool_id in {common_tool.id, mcp_tool.id}
    ]
    assert len(restored_dependency_assignments) == 4
    assert sum(item.enabled for item in restored_dependency_assignments) == 3
    assert all(item.config in ({}, None) for item in restored_dependency_assignments)

async def test_template_export_preserves_member_override_of_shared_tool(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectTemplate
    from app.models.tool import AgentTool

    env = project_api
    shared_tool = Tool(
        name=f"template-shared-{uuid.uuid4().hex[:8]}",
        display_name="Shared project tool",
        description="A shared tool with a member-level override.",
        type="builtin",
        category="project",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        source="builtin",
    )
    env.db.add(shared_tool)
    await env.db.commit()
    project = await _create_project(env, name="Shared tool override source")
    project_id = project["id"]
    capability_response = await env.client.post(
        f"/api/projects/{project_id}/capabilities",
        json={
            "capability_type": "tool",
            "capability_id": str(shared_tool.id),
            "capability_name": shared_tool.display_name,
            "source": "shared",
            "is_enabled": True,
        },
    )
    assert capability_response.status_code == 201, capability_response.text
    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    disabled_member = members[-1]
    disable_response = await env.client.put(
        f"/api/projects/{project_id}/members/{disabled_member['id']}/tools",
        json=[{"tool_id": str(shared_tool.id), "enabled": False}],
    )
    assert disable_response.status_code == 200, disable_response.text
    enable_response = await env.client.put(
        f"/api/projects/{project_id}/members/{disabled_member['id']}/tools",
        json=[{"tool_id": str(shared_tool.id), "enabled": True}],
    )
    assert enable_response.status_code == 200, enable_response.text
    disable_response = await env.client.put(
        f"/api/projects/{project_id}/members/{disabled_member['id']}/tools",
        json=[{"tool_id": str(shared_tool.id), "enabled": False}],
    )
    assert disable_response.status_code == 200, disable_response.text

    template_response = await env.client.post(
        f"/api/projects/{project_id}/templates",
        json={"name": "Shared tool override template"},
    )
    assert template_response.status_code == 201, template_response.text
    template = await env.db.get(ProjectTemplate, uuid.UUID(template_response.json()["id"]))
    assert template is not None
    exported = [
        item
        for item in template.definition["capabilities"]
        if item["capability_type"] == "tool" and item["capability_id"] == str(shared_tool.id)
    ]
    assert len(exported) == len(members)
    assert {item["source"] for item in exported} == {"inherited"}
    assert sum(bool(item["is_enabled"]) for item in exported) == len(members) - 1

    restored_response = await env.client.post(
        "/api/projects/from-template",
        json={"template_id": str(template.id), "name": "Shared tool override target"},
    )
    assert restored_response.status_code == 201, restored_response.text
    restored_agents = (await env.client.get(f"/api/projects/{restored_response.json()['id']}/agents")).json()
    restored_assignments = list(
        (
            await env.db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id.in_([uuid.UUID(item["id"]) for item in restored_agents]),
                    AgentTool.tool_id == shared_tool.id,
                )
            )
        ).scalars()
    )
    assert len(restored_assignments) == len(members)
    assert sum(item.enabled for item in restored_assignments) == len(members) - 1
