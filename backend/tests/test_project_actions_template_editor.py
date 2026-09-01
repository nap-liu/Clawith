from project_actions_support import *  # noqa: F401,F403

@pytest.mark.parametrize("invalid_agents", [None, {}, "not-a-list"])
async def test_generic_project_template_rejects_non_list_agent_assets(
    project_api: ProjectApiEnv,
    invalid_agents: object,
):
    response = await project_api.client.post(
        "/api/projects/templates",
        json={
            "name": "Invalid project Agent template",
            "definition": {"agents": invalid_agents},
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Project template Agents must be a list"

async def test_generic_project_template_persists_only_sanitized_agent_assets(
    project_api: ProjectApiEnv,
):
    from app.models.project import ProjectTemplate

    secret_uuid = uuid.uuid4()
    secret_email = "operator@example.test"
    secret_token = "super-secret-bearer-token"
    secret_password = "database-password"
    response = await project_api.client.post(
        "/api/projects/templates",
        json={
            "name": "Sanitized project Agent template",
            "definition": {
                "goal": "Safe reusable workflow",
                "agents": [
                    {
                        "name": "Release operator",
                        "role_description": f"Coordinate with {secret_email}",
                        "soul": f"Identity {secret_uuid}; password={secret_password}",
                        "core_memory": f"Authorization: Bearer {secret_token}",
                        "workspace_files": [
                            {
                                "path": "release/notes.md",
                                "content": f"api_key={secret_token}\nowner={secret_email}\n",
                            }
                        ],
                    }
                ],
            },
        },
    )
    assert response.status_code == 201, response.text
    definition = response.json()["definition"]
    serialized_definition = json.dumps(definition)
    for secret in (str(secret_uuid), secret_email, secret_token, secret_password):
        assert secret not in serialized_definition
    assert "[redacted-id]" in serialized_definition
    assert "[redacted-email]" in serialized_definition
    assert "[redacted]" in serialized_definition

    stored = await project_api.db.get(ProjectTemplate, uuid.UUID(response.json()["id"]))
    assert stored is not None
    assert stored.definition == definition

async def test_template_market_admin_visibility_and_cross_tenant_management_boundaries(
    project_api: ProjectApiEnv,
):
    env = project_api
    source_project = await _create_project(env, name="Template boundary source")
    company_admin = await _user(
        env.db,
        await env.db.get(Tenant, env.tenant_id),
        "CompanyAdmin",
    )
    company_admin.role = "org_admin"
    platform_admin = await _user(
        env.db,
        await env.db.get(Tenant, env.tenant_id),
        "PlatformAdmin",
    )
    platform_admin.role = "platform_admin"
    other_tenant = Tenant(name="Other company", slug=f"other-{uuid.uuid4().hex[:8]}")
    env.db.add(other_tenant)
    await env.db.flush()
    other_creator = await _user(env.db, other_tenant, "OtherCreator")
    current_private = ProjectTemplate(
        tenant_id=env.tenant_id,
        created_by_user_id=env.viewer_id,
        name="Company private template",
        is_published=False,
        definition={"goal": "Company-only draft"},
    )
    current_public = ProjectTemplate(
        tenant_id=env.tenant_id,
        created_by_user_id=env.owner_id,
        name="Company public template",
        is_published=True,
        definition={"goal": "Published by this company"},
    )
    other_public = ProjectTemplate(
        tenant_id=other_tenant.id,
        created_by_user_id=other_creator.id,
        name="Other public template",
        is_published=True,
        definition={"goal": "Published by another company"},
    )
    other_private = ProjectTemplate(
        tenant_id=other_tenant.id,
        created_by_user_id=other_creator.id,
        name="Other private template",
        is_published=False,
        definition={"goal": "Other company draft"},
    )
    env.db.add_all((current_private, current_public, other_public, other_private))
    await env.db.commit()
    current_private_id = current_private.id
    current_public_id = current_public.id
    other_public_id = other_public.id
    other_private_id = other_private.id
    company_admin_id = company_admin.id
    platform_admin_id = platform_admin.id

    env.authenticate_as(company_admin_id)
    admin_response = await env.client.get("/api/projects/templates")
    assert admin_response.status_code == 200, admin_response.text
    admin_templates = {uuid.UUID(item["id"]): item for item in admin_response.json()}
    assert current_private_id in admin_templates
    assert current_public_id in admin_templates
    assert other_public_id in admin_templates
    assert other_private_id not in admin_templates
    assert admin_templates[current_private_id]["can_edit"] is True
    assert admin_templates[current_private_id]["can_delete"] is True
    assert admin_templates[other_public_id]["can_edit"] is False
    assert admin_templates[other_public_id]["can_delete"] is False

    market_overwrite = await env.client.put(
        f"/api/projects/templates/{other_public_id}/from-project/{source_project['id']}"
    )
    assert market_overwrite.status_code == 404, market_overwrite.text
    company_overwrite = await env.client.put(
        f"/api/projects/templates/{current_public_id}/from-project/{source_project['id']}"
    )
    assert company_overwrite.status_code == 200, company_overwrite.text
    assert company_overwrite.json()["can_edit"] is True

    env.authenticate_as(env.viewer_id)
    creator_response = await env.client.get(f"/api/projects/templates/{current_private_id}")
    assert creator_response.status_code == 200, creator_response.text
    assert creator_response.json()["can_edit"] is True
    creator_delete = await env.client.delete(f"/api/projects/templates/{current_private_id}")
    assert creator_delete.status_code == 204, creator_delete.text
    assert await env.db.get(ProjectTemplate, current_private_id) is None

    env.authenticate_as(platform_admin_id)
    platform_response = await env.client.get("/api/projects/templates")
    assert platform_response.status_code == 200, platform_response.text
    platform_templates = {uuid.UUID(item["id"]): item for item in platform_response.json()}
    assert other_private_id in platform_templates
    assert platform_templates[other_private_id]["can_edit"] is True
    assert platform_templates[other_private_id]["can_delete"] is True
    platform_delete = await env.client.delete(f"/api/projects/templates/{other_private_id}")
    assert platform_delete.status_code == 204, platform_delete.text
    assert await env.db.get(ProjectTemplate, other_private_id) is None

async def test_deleting_all_platform_templates_does_not_recreate_them_on_list(
    project_api: ProjectApiEnv,
):
    env = project_api
    platform_admin = await _user(
        env.db,
        await env.db.get(Tenant, env.tenant_id),
        "PlatformTemplateAdmin",
    )
    platform_admin.role = "platform_admin"
    templates = [
        ProjectTemplate(
            tenant_id=None,
            created_by_user_id=None,
            name=f"Deletable platform template {index}",
            is_published=True,
            definition={"goal": f"Delete template {index}"},
        )
        for index in range(2)
    ]
    env.db.add_all(templates)
    await env.db.commit()
    template_ids = [template.id for template in templates]

    env.authenticate_as(platform_admin.id)
    for template_id in template_ids:
        response = await env.client.delete(f"/api/projects/templates/{template_id}")
        assert response.status_code == 204, response.text

    response = await env.client.get("/api/projects/templates")
    assert response.status_code == 200, response.text
    assert not {str(item["id"]) for item in response.json()} & {str(item) for item in template_ids}
    remaining_platform_templates = await env.db.scalar(
        select(func.count(ProjectTemplate.id)).where(ProjectTemplate.tenant_id.is_(None))
    )
    assert remaining_platform_templates == 0

async def test_template_editor_is_hidden_and_overwrite_uses_same_project_snapshot(
    project_api: ProjectApiEnv,
):
    env = project_api
    source = await _create_project(env, name="Template editor source")
    template_response = await env.client.post(
        f"/api/projects/{source['id']}/templates",
        json={
            "name": "Editable project template",
            "description": "Original description",
            "category": "operations",
            "version": "2.0.0",
            "is_published": False,
        },
    )
    assert template_response.status_code == 201, template_response.text
    template = template_response.json()
    template_id = uuid.UUID(template["id"])

    env.authenticate_as(env.viewer_id)
    denied_editor = await env.client.post(f"/api/projects/templates/{template_id}/editor")
    assert denied_editor.status_code == 404

    env.authenticate_as(env.owner_id)
    editor_response = await env.client.post(f"/api/projects/templates/{template_id}/editor")
    assert editor_response.status_code == 201, editor_response.text
    editor = editor_response.json()
    editor_id = uuid.UUID(editor["id"])
    assert editor["settings"]["template_editor"] == {"template_id": str(template_id)}

    ordinary_projects = await env.client.get("/api/projects", params={"scope": "all"})
    assert ordinary_projects.status_code == 200, ordinary_projects.text
    assert editor_id not in {uuid.UUID(item["id"]) for item in ordinary_projects.json()}
    direct_editor = await env.client.get(f"/api/projects/{editor_id}")
    assert direct_editor.status_code == 200, direct_editor.text

    changed = await env.client.patch(
        f"/api/projects/{editor_id}",
        json={"goal": "Updated through the template editor project"},
    )
    assert changed.status_code == 200, changed.text
    overwrite = await env.client.put(
        f"/api/projects/templates/{template_id}/from-project/{editor_id}"
    )
    assert overwrite.status_code == 200, overwrite.text
    updated_template = overwrite.json()
    assert updated_template["name"] == "Editable project template"
    assert updated_template["description"] == "Original description"
    assert updated_template["category"] == "operations"
    assert updated_template["version"] == "2.0.0"
    assert updated_template["is_published"] is False
    assert updated_template["definition"]["goal"] == "Updated through the template editor project"
    assert "template_editor" not in updated_template["definition"].get("settings", {})

    deleted = await env.client.delete(f"/api/projects/templates/{template_id}")
    assert deleted.status_code == 204, deleted.text
    env.db.expire_all()
    persisted_editor = await env.db.get(Project, editor_id)
    assert persisted_editor is not None
    assert persisted_editor.template_id is None
    assert "template_editor" not in persisted_editor.settings
    ordinary_projects = await env.client.get("/api/projects", params={"scope": "all"})
    assert editor_id in {uuid.UUID(item["id"]) for item in ordinary_projects.json()}

async def test_template_editor_normalizes_structured_roles_into_digital_employees(
    project_api: ProjectApiEnv,
):
    env = project_api
    template_response = await env.client.post(
        "/api/projects/templates",
        json={
            "name": "Structured role template",
            "definition": {
                "roles": [
                    {
                        "key": "research_lead",
                        "name": "研究负责人",
                        "description": "统筹研究范围、证据质量与交付结论。",
                    },
                    {
                        "key": "fact_reviewer",
                        "name": "事实核查员",
                        "description": "核验关键事实与引用来源。",
                    },
                ]
            },
        },
    )
    assert template_response.status_code == 201, template_response.text
    template_id = template_response.json()["id"]

    editor_response = await env.client.post(f"/api/projects/templates/{template_id}/editor")
    assert editor_response.status_code == 201, editor_response.text
    editor_id = editor_response.json()["id"]
    members_response = await env.client.get(f"/api/projects/{editor_id}/members")
    assert members_response.status_code == 200, members_response.text
    members = members_response.json()
    assert [(item["name_snapshot"], item["role_snapshot"]) for item in members] == [
        ("研究负责人", "统筹研究范围、证据质量与交付结论。"),
        ("事实核查员", "核验关键事实与引用来源。"),
    ]
    assert members[0]["is_leader"] is True

    invalid_response = await env.client.post(
        "/api/projects/templates",
        json={"name": "Invalid role template", "definition": {"roles": "负责人"}},
    )
    assert invalid_response.status_code == 422
    assert invalid_response.json()["detail"] == "项目模板角色配置必须为列表。"
