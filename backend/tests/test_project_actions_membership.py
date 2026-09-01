from project_actions_support import *  # noqa: F401,F403

async def test_project_delete_cleans_bidirectional_agent_and_session_references(
    project_api: ProjectApiEnv,
):
    env = project_api
    project = await _create_project(env, name="Delete used project")
    project_id = uuid.UUID(project["id"])
    parent = ChatSession(
        project_id=project_id,
        agent_id=env.leader_id,
        title="Delete parent",
        source_channel="project",
        external_conv_id=f"delete-parent:{project_id}",
        is_group=True,
    )
    child = ChatSession(
        project_id=project_id,
        agent_id=env.worker_id,
        peer_agent_id=env.leader_id,
        title="Delete child",
        source_channel="subagent",
        external_conv_id=f"delete-child:{project_id}",
        is_group=False,
    )
    env.db.add_all([parent, child])
    await env.db.flush()
    child_run = SubagentRun(
        id=child.id,
        parent_session_id=parent.id,
        project_id=project_id,
        execution_user_id=env.owner_id,
        origin_tool_call_id="delete-completed-child",
        mode="run",
        status="completed",
    )
    gateway = GatewayMessage(
        agent_id=env.source_leader_id,
        sender_agent_id=env.leader_id,
        conversation_id=str(parent.id),
        content="Project agent sent to a standard agent",
        status="completed",
    )
    env.db.add_all([child_run, gateway])
    # Production historically created this foreign key without ON DELETE
    # CASCADE even though the ORM model declares it. Keep the API cleanup
    # compatible with that deployed schema instead of relying on fresh-schema
    # behavior in this test.
    await env.db.execute(
        text(
            "ALTER TABLE daily_token_usage "
            "DROP CONSTRAINT daily_token_usage_agent_id_fkey"
        )
    )
    await env.db.execute(
        text(
            "ALTER TABLE daily_token_usage "
            "ADD CONSTRAINT daily_token_usage_agent_id_fkey "
            "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE NO ACTION"
        )
    )
    usage = DailyTokenUsage(
        tenant_id=env.tenant_id,
        agent_id=env.leader_id,
        date=datetime.now(UTC),
        tokens_used=17,
        input_tokens=11,
        output_tokens=6,
    )
    env.db.add(usage)
    await env.db.commit()

    deleted = await env.client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 204, deleted.text
    assert await env.db.get(Project, project_id) is None
    assert await env.db.get(SubagentRun, child.id) is None
    assert await env.db.get(ChatSession, parent.id) is None
    assert await env.db.get(ChatSession, child.id) is None
    assert await env.db.scalar(select(GatewayMessage.id).where(GatewayMessage.id == gateway.id)) is None
    assert await env.db.scalar(select(DailyTokenUsage.id).where(DailyTokenUsage.id == usage.id)) is None
    assert await env.db.scalar(select(Agent.id).where(Agent.id == env.leader_id)) is None
    assert not project_repo_path(env.tenant_id, project_id).exists()

async def test_project_delete_enforces_role_tenant_and_active_work_boundaries(
    project_api: ProjectApiEnv,
):
    env = project_api
    viewer_project = await _create_project(env, name="Viewer cannot delete")
    env.authenticate_as(env.viewer_id)
    assert (await env.client.delete(f"/api/projects/{viewer_project['id']}")).status_code == 404

    env.authenticate_as(env.org_admin_id)
    assert (await env.client.delete(f"/api/projects/{viewer_project['id']}")).status_code == 204

    env.authenticate_as(env.owner_id)
    running = await _create_project(env, name="Running cannot delete")
    running_row = await env.db.get(Project, uuid.UUID(running["id"]))
    assert running_row is not None
    running_row.status = "running"
    await env.db.commit()
    assert (await env.client.delete(f"/api/projects/{running['id']}")).status_code == 409

    waiting = await _create_project(env, name="Waiting child cannot delete")
    waiting_id = uuid.UUID(waiting["id"])
    waiting_row = await env.db.get(Project, waiting_id)
    assert waiting_row is not None
    waiting_row.status = "paused"
    parent = ChatSession(
        project_id=waiting_id,
        agent_id=env.leader_id,
        title="Waiting parent",
        source_channel="project",
        external_conv_id=f"waiting-parent:{waiting_id}",
        is_group=True,
    )
    child = ChatSession(
        project_id=waiting_id,
        agent_id=env.worker_id,
        title="Waiting child",
        source_channel="subagent",
        external_conv_id=f"waiting-child:{waiting_id}",
        is_group=False,
    )
    env.db.add_all([parent, child])
    await env.db.flush()
    env.db.add(
        SubagentRun(
            id=child.id,
            parent_session_id=parent.id,
            project_id=waiting_id,
            execution_user_id=env.owner_id,
            origin_tool_call_id="waiting-confirmation-child",
            mode="run",
            status="waiting_confirmation",
        )
    )
    await env.db.commit()
    assert (await env.client.delete(f"/api/projects/{waiting_id}")).status_code == 409

    other_tenant = Tenant(name="Other company", slug=f"other-{uuid.uuid4().hex[:8]}")
    env.db.add(other_tenant)
    await env.db.flush()
    owner_identity_id = await env.db.scalar(select(User.identity_id).where(User.id == env.owner_id))
    identity = await env.db.get(Identity, owner_identity_id)
    assert identity is not None
    identity.is_platform_admin = True
    switched_user = User(
        identity_id=identity.id,
        tenant_id=other_tenant.id,
        display_name="Owner switched company",
        role="member",
        is_active=True,
    )
    env.db.add(switched_user)
    await env.db.flush()
    cross_tenant_project = Project(
        tenant_id=other_tenant.id,
        owner_user_id=switched_user.id,
        name="Tenant switched delete",
        description="",
        goal="",
        visibility="private",
        status="planning",
        settings={},
    )
    env.db.add(cross_tenant_project)
    await env.db.commit()
    switched_user_id = switched_user.id
    cross_tenant_project_id = cross_tenant_project.id

    env.authenticate_as(env.owner_id)
    assert (await env.client.delete(f"/api/projects/{cross_tenant_project_id}")).status_code == 404
    env.authenticate_as(switched_user_id)
    target_summary = await env.client.get(f"/api/projects/{cross_tenant_project_id}")
    assert target_summary.status_code == 200, target_summary.text
    assert target_summary.json()["can_delete"] is True
    assert target_summary.json()["can_manage_execution_user"] is True
    assert (await env.client.delete(f"/api/projects/{cross_tenant_project_id}")).status_code == 204

async def test_project_owner_directory_is_tenant_scoped_and_excludes_owner(
    project_api: ProjectApiEnv,
):
    env = project_api
    project = await _create_project(env, name="Project share directory")
    project_id = project["id"]

    root = OrgDepartment(
        tenant_id=env.tenant_id,
        name="Product",
        path="Product",
        status="active",
    )
    env.db.add(root)
    await env.db.flush()
    child = OrgDepartment(
        tenant_id=env.tenant_id,
        name="Research",
        path="Product/Research",
        parent_id=root.id,
        status="active",
    )
    env.db.add(child)
    await env.db.flush()
    env.db.add_all(
        [
            OrgMember(
                tenant_id=env.tenant_id,
                user_id=env.owner_id,
                name="Owner",
                email="owner@project.test",
                department_id=root.id,
                department_path=root.path,
                status="active",
            ),
            OrgMember(
                tenant_id=env.tenant_id,
                user_id=env.viewer_id,
                name="Viewer",
                email="viewer@project.test",
                department_id=child.id,
                department_path=child.path,
                status="active",
            ),
        ]
    )
    outsider_tenant = Tenant(
        name="Directory outsider",
        slug=f"directory-outsider-{uuid.uuid4().hex[:8]}",
    )
    env.db.add(outsider_tenant)
    await env.db.flush()
    outsider = await _user(env.db, outsider_tenant, "DirectoryOutsider")
    outsider_department = OrgDepartment(
        tenant_id=outsider_tenant.id,
        name="Other company",
        path="Other company",
        status="active",
    )
    env.db.add(outsider_department)
    await env.db.flush()
    env.db.add(
        OrgMember(
            tenant_id=outsider_tenant.id,
            user_id=outsider.id,
            name="Directory outsider",
            department_id=outsider_department.id,
            department_path=outsider_department.path,
            status="active",
        )
    )
    await env.db.commit()

    departments = await env.client.get(
        f"/api/projects/{project_id}/directory/departments"
    )
    assert departments.status_code == 200, departments.text
    assert [item["id"] for item in departments.json()["items"]] == [str(root.id)]
    assert departments.json()["items"][0]["has_children"] is True
    assert departments.json()["my_department"]["id"] == str(root.id)

    members = await env.client.get(
        f"/api/projects/{project_id}/directory/members",
        params={"department_id": str(root.id), "include_descendants": "true"},
    )
    assert members.status_code == 200, members.text
    assert [item["id"] for item in members.json()["items"]] == [
        str(env.viewer_id)
    ]
    assert str(env.owner_id) not in {item["id"] for item in members.json()["items"]}
    assert str(outsider.id) not in {item["id"] for item in members.json()["items"]}

    cross_tenant_department = await env.client.get(
        f"/api/projects/{project_id}/directory/members",
        params={"department_id": str(outsider_department.id)},
    )
    assert cross_tenant_department.status_code == 404

    env.authenticate_as(env.viewer_id)
    denied_departments = await env.client.get(
        f"/api/projects/{project_id}/directory/departments"
    )
    denied_members = await env.client.get(
        f"/api/projects/{project_id}/directory/members",
        params={"search": "Owner"},
    )
    assert denied_departments.status_code == 404
    assert denied_members.status_code == 404

async def test_project_owner_directory_lists_active_users_without_synced_org_profiles(
    project_api: ProjectApiEnv,
):
    env = project_api
    project = await _create_project(env, name="Project share without org sync")

    departments = await env.client.get(
        f"/api/projects/{project['id']}/directory/departments"
    )
    assert departments.status_code == 200, departments.text
    assert departments.json() == {"items": [], "my_department": None}

    members = await env.client.get(
        f"/api/projects/{project['id']}/directory/members"
    )
    assert members.status_code == 200, members.text
    assert members.json()["total"] == 2
    items_by_id = {item["id"]: item for item in members.json()["items"]}
    assert items_by_id[str(env.viewer_id)] == {
        "id": str(env.viewer_id),
        "member_id": None,
        "name": "Viewer",
        "nickname": None,
        "department_id": None,
        "department_path": "",
        "title": "",
        "avatar_url": None,
        "email": env.viewer.email,
    }
    assert items_by_id[str(env.org_admin_id)]["name"] == "Org Admin"
    assert str(env.owner_id) not in {
        item["id"] for item in members.json()["items"]
    }

async def test_member_and_run_snapshots_are_isolated_and_a2a_bypasses_leader(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Snapshots and mesh")
    project_id = project["id"]
    await _mark_project_running(env, project_id)
    worker_id = env.worker_id
    reviewer_id = env.reviewer_id
    source_before = {
        "name": env.worker.name,
        "role_description": env.worker.role_description,
        "autonomy_policy": dict(env.worker.autonomy_policy or {}),
        "max_tool_rounds": env.worker.max_tool_rounds,
    }
    review_item = await _create_work_item(
        env,
        project_id,
        title="Review acceptance evidence",
        assignee_agent_id=reviewer_id,
    )

    members_response = await env.client.get(f"/api/projects/{project_id}/members")
    assert members_response.status_code == 200
    members = members_response.json()
    worker_snapshot = next(member for member in members if member["agent_id"] == str(worker_id))
    changed_snapshot = {
        **worker_snapshot["config_snapshot"],
        "autonomy_policy": {"write": "allow-in-project"},
        "project_instruction": "Only exists in this project",
    }
    patched_member = await env.client.patch(
        f"/api/projects/{project_id}/members/{worker_snapshot['id']}",
        json={"config_snapshot": changed_snapshot},
    )
    assert patched_member.status_code == 200, patched_member.text
    assert patched_member.json()["config_snapshot"]["project_instruction"] == "Only exists in this project"

    env.db.expire(env.worker)
    source_worker = await env.db.get(Agent, worker_id)
    assert source_worker is not None
    assert {
        "name": source_worker.name,
        "role_description": source_worker.role_description,
        "autonomy_policy": dict(source_worker.autonomy_policy or {}),
        "max_tool_rounds": source_worker.max_tool_rounds,
    } == source_before

    # Worker wakes Reviewer directly. Neither side is the project Leader and
    # no global Agent relationship is created or required.
    a2a_response = await env.client.post(
        f"/api/projects/{project_id}/a2a",
        json={
            "from_agent_id": str(worker_id),
            "to_agent_id": str(reviewer_id),
            "title": "Review acceptance evidence",
            "message": "Review the acceptance evidence directly",
            "mode": "review",
            "expected_output": "A cited pass/fail review decision",
            "work_item_id": review_item["id"],
        },
    )
    assert a2a_response.status_code == 202, a2a_response.text
    assert a2a_response.json()["from_agent_id"] == str(worker_id)
    assert a2a_response.json()["to_agent_id"] == str(reviewer_id)

    run_response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(worker_id), "trigger_type": "manual", "input": {"objective": "Build v1"}},
    )
    assert run_response.status_code == 201, run_response.text
    run_id = run_response.json()["id"]
    assert run_response.json()["input"]["title"] == "Build v1"
    assert run_response.json()["output"]["subagent_session_id"]

    frozen_response = await env.client.get(f"/api/projects/{project_id}/runs/{run_id}/member-snapshots")
    assert frozen_response.status_code == 200
    frozen = frozen_response.json()
    assert len(frozen) == 3
    assert sum(item["is_leader"] for item in frozen) == 1
    worker_frozen = next(item for item in frozen if item["agent_id"] == str(worker_id))
    reviewer_frozen = next(item for item in frozen if item["agent_id"] == str(reviewer_id))
    assert {item["name"] for item in worker_frozen["member_snapshot"]["capabilities"]["items"]} == {
        "project-shell",
        "worker-private-tool",
    }
    assert {item["name"] for item in reviewer_frozen["member_snapshot"]["capabilities"]["items"]} == {
        "project-shell"
    }
    assert "member_config_snapshot" not in worker_frozen
    assert "capability_snapshot" not in worker_frozen

    capabilities = (await env.client.get(f"/api/projects/{project_id}/capabilities")).json()
    shared_capability = next(item for item in capabilities if item["capability_name"] == "project-shell")
    disabled = await env.client.patch(
        f"/api/projects/{project_id}/capabilities/{shared_capability['id']}",
        json={"is_enabled": False},
    )
    assert disabled.status_code == 200
    frozen_again = (await env.client.get(f"/api/projects/{project_id}/runs/{run_id}/member-snapshots")).json()
    assert frozen_again == frozen

    events = (await env.client.get(f"/api/projects/{project_id}/events")).json()
    a2a_event = next(event for event in events if event["event_type"] == "a2a.queued")
    assert a2a_event["from_agent_id"] == str(worker_id)
    assert a2a_event["to_agent_id"] == str(reviewer_id)
    assert {"run.queued", "capability.updated", "member.snapshot.updated"} <= {event["event_type"] for event in events}

async def test_run_event_and_dashboard_expose_frozen_product_summary(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Product snapshot summary")
    project_id = project["id"]
    await _mark_project_running(env, project_id)

    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    worker = next(member for member in members if member["agent_id"] == str(env.worker_id))
    model = (
        await env.db.execute(select(LLMModel).where(LLMModel.tenant_id == env.tenant_id))
    ).scalar_one()
    updated_config = {
        **worker["config_snapshot"],
        "primary_model_id": str(model.id),
        "max_tool_rounds": 23,
        "project_instruction": "Use the project acceptance criteria.",
    }
    patched = await env.client.patch(
        f"/api/projects/{project_id}/members/{worker['id']}",
        json={"config_snapshot": updated_config},
    )
    assert patched.status_code == 200, patched.text

    created = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"agent_id": str(env.worker_id), "input": {"objective": "Produce the delivery"}},
    )
    assert created.status_code == 201, created.text
    run = created.json()
    summary = run["member_snapshot"]
    assert summary["name"] == "Worker"
    assert summary["configuration"] == {
        "primary_model": {"name": "Project tenant default", "availability": "available"},
        "fallback_model": None,
        "max_tool_rounds": 23,
        "has_project_instruction": True,
    }
    assert summary["capabilities"]["total"] == len(summary["capabilities"]["items"])
    assert {item["source"] for item in summary["capabilities"]["items"]} <= {"project", "member"}
    assert "member_config_snapshot" not in summary
    assert "capability_snapshot" not in summary

    snapshots = (
        await env.client.get(f"/api/projects/{project_id}/runs/{run['id']}/member-snapshots")
    ).json()
    worker_snapshot = next(item for item in snapshots if item["agent_id"] == str(env.worker_id))
    assert worker_snapshot["member_snapshot"] == summary
    assert "member_config_snapshot" not in worker_snapshot
    assert "capability_snapshot" not in worker_snapshot

    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    queued_event = next(event for event in events if event["event_type"] == "run.queued")
    assert queued_event["member_snapshot"] == summary

    dashboard = (await env.client.get(f"/api/projects/{project_id}/dashboard")).json()
    dashboard_run = next(item for item in dashboard["runs"] if item["id"] == run["id"])
    dashboard_event = next(item for item in dashboard["events"] if item["event_type"] == "run.queued")
    assert dashboard_run["member_snapshot"] == summary
    assert dashboard_event["member_snapshot"] == summary
