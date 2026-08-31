from project_actions_support import *  # noqa: F401,F403

async def test_project_runtime_tools_are_role_projected_and_double_enforced(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.chat_session import ChatSession
    from app.services.agent_tools import execute_tool
    from app.services.project_runtime_tools import execute_project_runtime_tool
    from app.services.subagent_runtime import prepare_subagent_tools

    env = project_api

    collaboration_bypass_names = {
        "send_message_to_agent",
        "send_file_to_agent",
        "send_message_to_parent",
        "send_session_message",
    }
    standard_file_names = {
        "delete_file",
        "edit_file",
        "find_files",
        "list_files",
        "move_file",
        "read_file",
        "search_files",
        "write_file",
    }
    structured_read_names = {"read_document", "read_image"}
    sandbox_names = {"execute_code", "execute_code_e2b", "execute_code_aio"}
    project_sandbox_names = {"execute_code"}

    async def normal_tools_with_collaboration_bypasses(_agent_id, *, assignment_snapshot=None):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Generic collaboration path",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in (
                collaboration_bypass_names
                | structured_read_names
                | standard_file_names
                | sandbox_names
            )
        ]

    monkeypatch.setattr(
        "app.services.agent_tools.get_agent_tools_for_llm",
        normal_tools_with_collaboration_bypasses,
    )
    project = await _create_project(env, name="Role projected tools")
    project_id = project["id"]
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()

    assigned = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": "Worker item",
            "description": "Worker-owned delivery",
            "assignee_agent_id": str(env.worker_id),
            "acceptance_criteria": ["Evidence attached"],
        },
    )
    other = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": "Reviewer item",
            "description": "Reviewer-owned delivery",
            "assignee_agent_id": str(env.reviewer_id),
            "acceptance_criteria": ["Review complete"],
        },
    )
    assert assigned.status_code == other.status_code == 201

    worker_wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Worker runtime", "mentions": [str(env.worker_id)]},
    )
    leader_wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Leader runtime", "mentions": [str(env.leader_id)]},
    )
    assert worker_wake.status_code == leader_wake.status_code == 201
    worker_child_id = uuid.UUID(
        next(row for row in worker_wake.json()["subagent_runs"] if row["agent_id"] == str(env.worker_id))["session_id"]
    )
    leader_child_id = uuid.UUID(
        next(row for row in leader_wake.json()["subagent_runs"] if row["agent_id"] == str(env.leader_id))["session_id"]
    )

    worker_tools = await prepare_subagent_tools(
        env.worker_id,
        worker_child_id,
        execution_user_id=env.owner_id,
    )
    worker_names = {item["function"]["name"] for item in worker_tools}
    leader_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.leader_id,
            leader_child_id,
            execution_user_id=env.owner_id,
        )
    }
    assert {"project_get_context", "project_list_work_items", "project_update_work_item", "project_message_agent"} <= worker_names
    assert {"project_list_files", "project_read_file", "project_write_file"}.isdisjoint(worker_names)
    assert standard_file_names <= worker_names
    for tool in worker_tools:
        if tool["function"]["name"] not in (
            standard_file_names | structured_read_names | project_sandbox_names
        ):
            continue
        parameters = tool["function"]["parameters"]
        assert "workspace" in parameters["required"]
        assert parameters["properties"]["workspace"]["enum"] == ["agent", "project"]
    assert "project_create_work_item" not in worker_names
    assert "project_update_plan" not in worker_names
    assert "project_set_status" not in worker_names
    assert worker_names.isdisjoint(collaboration_bypass_names)
    assert leader_names.isdisjoint(collaboration_bypass_names)
    assert structured_read_names | sandbox_names <= worker_names
    assert structured_read_names | sandbox_names <= leader_names
    e2b_tool = next(item for item in worker_tools if item["function"]["name"] == "execute_code_e2b")
    assert "workspace" not in e2b_tool["function"]["parameters"].get("properties", {})
    aio_tool = next(item for item in worker_tools if item["function"]["name"] == "execute_code_aio")
    assert "workspace" not in aio_tool["function"]["parameters"].get("properties", {})
    assert standard_file_names <= leader_names
    assert {
        "project_create_work_item",
        "project_update_plan",
        "project_restore_commit",
        "project_set_status",
    } <= leader_names

    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await prepare_subagent_tools(
            env.worker_id,
            worker_child_id,
            execution_user_id=env.viewer_id,
        )
    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await prepare_subagent_tools(
            env.reviewer_id,
            worker_child_id,
            execution_user_id=env.owner_id,
        )
    with pytest.raises(ValueError, match="authorized project Subagent runtime"):
        await execute_project_runtime_tool(
            "project_get_context",
            {},
            agent_id=env.worker_id,
            execution_user_id=env.viewer_id,
            session_id=str(worker_child_id),
            tool_call_id="wrong-execution-user",
            turn_anchor_id=None,
        )

    settings_update = await env.client.patch(
        f"/api/projects/{project_id}/settings",
        json={"policies": {"project_tools": {"participant_disabled": ["project_message_agent"]}}},
    )
    members = (await env.client.get(f"/api/projects/{project_id}/members")).json()
    worker_member = next(item for item in members if item["agent_id"] == str(env.worker_id))
    member_update = await env.client.patch(
        f"/api/projects/{project_id}/members/{worker_member['id']}",
        json={
            "config_snapshot": {
                **worker_member["config_snapshot"],
                "disabled_project_tools": ["project_update_work_item"],
            }
        },
    )
    assert settings_update.status_code == member_update.status_code == 200
    projected_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.worker_id,
            worker_child_id,
            execution_user_id=env.owner_id,
        )
    }
    assert "project_message_agent" not in projected_names
    assert "project_update_work_item" in projected_names
    refreshed_wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Worker runtime after member policy change", "mentions": [str(env.worker_id)]},
    )
    assert refreshed_wake.status_code == 201, refreshed_wake.text
    refreshed_child_id = uuid.UUID(
        next(
            row
            for row in refreshed_wake.json()["subagent_runs"]
            if row["agent_id"] == str(env.worker_id)
        )["session_id"]
    )
    refreshed_names = {
        item["function"]["name"]
        for item in await prepare_subagent_tools(
            env.worker_id,
            refreshed_child_id,
            execution_user_id=env.owner_id,
        )
    }
    assert "project_message_agent" not in refreshed_names
    assert "project_update_work_item" not in refreshed_names
    with pytest.raises(ValueError, match="not allowed"):
        await execute_project_runtime_tool(
            "project_message_agent",
            {},
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(worker_child_id),
            tool_call_id="worker-policy-bypass",
            turn_anchor_id=None,
        )
    restore_member_tools = await env.client.patch(
        f"/api/projects/{project_id}/members/{worker_member['id']}",
        json={
            "config_snapshot": {
                **worker_member["config_snapshot"],
                "disabled_project_tools": [],
            }
        },
    )
    assert restore_member_tools.status_code == 200, restore_member_tools.text

    long_progress = "Implementation started: " + ("progress " * 100)
    long_evidence = [f"docs/progress-{index}.md:" + ("e" * 400) for index in range(8)]
    updated = await execute_project_runtime_tool(
        "project_update_work_item",
        {
            "work_item_id": assigned.json()["id"],
            "status": "in_progress",
            "progress_note": long_progress,
            "evidence": long_evidence,
        },
        agent_id=env.worker_id,
        execution_user_id=env.owner_id,
        session_id=str(worker_child_id),
        tool_call_id="worker-update",
        turn_anchor_id=None,
    )
    assert json.loads(updated)["status"] == "in_progress"

    with pytest.raises(ValueError, match="only status, progress_note and evidence"):
        await execute_project_runtime_tool(
            "project_update_work_item",
            {"work_item_id": assigned.json()["id"], "title": "Privilege escalation"},
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(worker_child_id),
            tool_call_id="worker-title",
            turn_anchor_id=None,
        )
    with pytest.raises(ValueError, match="assigned to themselves"):
        await execute_project_runtime_tool(
            "project_update_work_item",
            {"work_item_id": other.json()["id"], "status": "done"},
            agent_id=env.worker_id,
            execution_user_id=env.owner_id,
            session_id=str(worker_child_id),
            tool_call_id="worker-other",
            turn_anchor_id=None,
        )

    denied = await execute_tool(
        "project_update_plan",
        {"goal": "Participant must not change this"},
        env.worker_id,
        env.owner_id,
        session_id=str(worker_child_id),
        tool_call_id="worker-plan",
    )
    assert denied.startswith("❌")
    worker_member = (
        await env.db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == uuid.UUID(project_id),
                ProjectMemberSnapshot.agent_id == env.worker_id,
            )
        )
    ).scalar_one()
    stored_professional_role = ("Build traceable deliverables. " + ("role " * 100))[:500]
    worker_member.role_snapshot = stored_professional_role
    await env.db.commit()
    context = await execute_tool(
        "project_get_context",
        {},
        env.worker_id,
        env.owner_id,
        session_id=str(worker_child_id),
        tool_call_id="worker-context",
    )
    context_payload = json.loads(context)
    assert context_payload["id"] == project_id
    context_worker = next(row for row in context_payload["members"] if row["agent_id"] == str(env.worker_id))
    context_leader = next(row for row in context_payload["members"] if row["agent_id"] == str(env.leader_id))
    assert context_worker["project_role"] == "participant"
    assert context_leader["project_role"] == "owner"
    assert context_worker["professional_role"] == stored_professional_role.rstrip()

    work_items = json.loads(
        await execute_tool(
            "project_list_work_items",
            {"mine_only": True},
            env.worker_id,
            env.owner_id,
            session_id=str(worker_child_id),
            tool_call_id="worker-items",
        )
    )
    assert len(work_items) == 1
    assert work_items[0]["assignee_name"] == worker_member.name_snapshot
    assert work_items[0]["professional_role"] == context_worker["professional_role"]
    assert len(work_items[0]["progress"]) == 600
    assert work_items[0]["progress"].endswith("…")
    assert len(work_items[0]["evidence"]) == 6
    assert all(len(value) == 300 and value.endswith("…") for value in work_items[0]["evidence"])
    assert await env.db.get(ChatSession, worker_child_id) is not None

async def test_work_item_mutations_update_dashboard_and_audit(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Work item audit")
    project_id = project["id"]

    parent = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={"title": "Prepare evidence", "priority": "high"},
    )
    assert parent.status_code == 201, parent.text
    child = await env.client.post(
        f"/api/projects/{project_id}/work-items",
        json={
            "title": "Review evidence",
            "parent_id": parent.json()["id"],
            "dependency_ids": [parent.json()["id"]],
            "assignee_agent_id": str(env.reviewer_id),
            "status": "todo",
        },
    )
    assert child.status_code == 201, child.text
    completed = await env.client.patch(
        f"/api/projects/{project_id}/work-items/{parent.json()['id']}",
        json={"status": "done", "priority": "urgent"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "done"

    dashboard = (await env.client.get(f"/api/projects/{project_id}/dashboard")).json()
    assert dashboard["progress"] == 50
    assert dashboard["work_item_counts"] == {"done": 1, "todo": 1}
    assert len(dashboard["work_items"]) == 2

    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    created = [event for event in events if event["event_type"] == "work_item.created"]
    updated = next(event for event in events if event["event_type"] == "work_item.updated")
    assert len(created) == 2
    assert updated["work_item_id"] == parent.json()["id"]
    assert updated["event_metadata"]["after"]["status"] == "done"
    assert {"status", "priority"} <= set(updated["event_metadata"]["changed_fields"])
