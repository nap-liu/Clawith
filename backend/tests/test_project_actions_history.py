from project_actions_support import *  # noqa: F401,F403

async def test_project_subagent_reply_materializes_without_resuming_group_root(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.project import ProjectRun
    from app.models.subagent_run import SubagentRun
    from app.services import subagent_runtime

    env = project_api
    live_events: list[tuple[str, str, dict]] = []

    async def capture_live(agent_id, conversation_id, payload):
        live_events.append((str(agent_id), str(conversation_id), payload))

    monkeypatch.setattr(
        "app.api.websocket.manager.send_to_session",
        capture_live,
    )
    project = await _create_project(env, name="Passive child reply")
    await _mark_project_running(env, project["id"])
    group = (await env.client.get(f"/api/projects/{project['id']}/group-session")).json()
    wake = await env.client.post(
        f"/api/projects/{project['id']}/group-sessions/{group['id']}/messages",
        json={"content": "Worker answer once", "mentions": [str(env.worker_id)]},
    )
    assert wake.status_code == 201, wake.text
    group_user_events = [
        payload
        for _agent_id, conversation_id, payload in live_events
        if conversation_id == group["id"]
        and payload.get("type") == "user_message_committed"
    ]
    assert len(group_user_events) == 1
    assert group_user_events[0]["content"] == "Worker answer once"
    assert group_user_events[0]["turn"]["phase"] == "active"
    worker_wake = next(row for row in wake.json()["subagent_runs"] if row["agent_id"] == str(env.worker_id))
    child_id = uuid.UUID(worker_wake["session_id"])
    project_run_id = uuid.UUID(worker_wake["project_run_id"])
    child_input = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
            )
        )
    ).scalar_one()
    child_input_id = child_input.id
    child_input.message_meta = {
        **dict(child_input.message_meta or {}),
        "subagent_input_state": "processing",
        "subagent_turn_anchor_id": str(child_input.id),
        "turn_status": "running",
    }
    durable_run = await env.db.get(SubagentRun, child_id)
    assert durable_run is not None
    durable_run.status = "running"
    durable_run.lease_owner = subagent_runtime.settings.INSTANCE_ID
    await env.db.commit()

    terminal = await subagent_runtime._finish_subagent_turn(
        run_id=child_id,
        anchor_id=child_input_id,
        reply="Worker result",
        failed=False,
    )
    assert terminal is True
    env.db.expire_all()
    project_run = await env.db.get(ProjectRun, project_run_id)
    assert project_run is not None and project_run.status == "succeeded"
    assert project_run.finished_at is not None
    assert project_run.output["subagent_session_id"] == str(child_id)
    assert project_run.output["result"] == "Worker result"
    completion = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["kind"].as_string() == "subagent_completion",
            )
        )
    ).scalar_one()

    async def forbidden_resume(_anchor):
        raise AssertionError("project group completion must not resume root LLM")

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", forbidden_resume)
    assert await subagent_runtime._dispatch_parent_event(completion.id) is True
    materialized = (
        await env.db.execute(
            select(ChatMessage).where(ChatMessage.external_event_key == f"project-subagent:{completion.id}")
        )
    ).scalar_one()
    assert materialized.conversation_id == group["id"]
    assert materialized.sender_agent_id == env.worker_id
    assert materialized.message_meta["visible_to_group"] is True
    assert materialized.message_meta["mentions"] == []
    assert materialized.message_meta["awakened_agent_ids"] == []
    assert materialized.message_meta["leader_batch_state"] == "pending"
    assert materialized.message_meta["default_leader_agent_id"] == str(env.leader_id)
    assert materialized.message_meta["timeline_anchor_id"] == wake.json()["message"]["id"]
    assert materialized.message_meta["producer_scope"] == (
        f"project:{child_id}:{child_input_id}"
    )
    committed_events = [
        payload
        for _agent_id, conversation_id, payload in live_events
        if conversation_id == group["id"]
        and payload.get("type") == "assistant_message_committed"
    ]
    assert len(committed_events) == 1
    committed_event = committed_events[0]
    assert committed_event["message_id"] == str(materialized.id)
    assert committed_event["transient_message_id"] == (
        f"project-stream:{child_id}:{child_input_id}"
    )
    assert committed_event["producer_scope"] == (
        f"project:{child_id}:{child_input_id}"
    )
    assert committed_event["timeline_anchor_id"] == wake.json()["message"]["id"]
    current_group = await env.client.get(
        f"/api/projects/{project['id']}/group-sessions/{group['id']}/messages"
    )
    assert current_group.status_code == 200
    assert committed_event["turn"]["status"] == current_group.json()["turn"]["status"]
    parent = await env.db.get(ChatSession, uuid.UUID(group["id"]))
    assert parent is not None and parent.source_channel == "project"

async def test_project_group_timeline_reuses_standard_child_message_contract(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from datetime import timedelta

    from app.models.audit import ChatMessage
    from app.models.project import ProjectRun
    from app.models.subagent_run import SubagentRun

    env = project_api
    live_events: list[tuple[str, str, dict]] = []

    async def capture_live(agent_id, conversation_id, payload):
        live_events.append((str(agent_id), str(conversation_id), payload))

    monkeypatch.setattr("app.api.websocket.manager.send_to_session", capture_live)
    project = await _create_project(env, name="Standard group timeline")
    project_id = project["id"]
    await _mark_project_running(env, project_id)
    group = (await env.client.get(f"/api/projects/{project_id}/group-session")).json()
    wake = await env.client.post(
        f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
        json={"content": "Show the full execution turn", "mentions": [str(env.worker_id)]},
    )
    assert wake.status_code == 201, wake.text
    worker_wake = next(row for row in wake.json()["subagent_runs"] if row["agent_id"] == str(env.worker_id))
    child_id = uuid.UUID(worker_wake["session_id"])
    project_run_id = uuid.UUID(worker_wake["project_run_id"])
    child_input = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["project_run_id"].as_string() == str(project_run_id),
            )
        )
    ).scalar_one()
    child_input.message_meta = {
        **dict(child_input.message_meta or {}),
        "subagent_input_state": "processing",
        "subagent_turn_anchor_id": str(child_input.id),
        "turn_status": "running",
    }
    base_time = child_input.created_at + timedelta(seconds=1)
    running_tool = ChatMessage(
        agent_id=env.worker_id,
        role="tool_call",
        content=json.dumps(
            {
                "name": "read_file",
                "call_id": "worker-read-1",
                "args": {"path": "README.md"},
                "status": "running",
                "result": "",
                "reasoning_content": "Inspecting the project evidence",
            }
        ),
        conversation_id=str(child_id),
        message_meta={"turn_anchor_id": str(child_input.id)},
        created_at=base_time,
    )
    done_tool = ChatMessage(
        agent_id=env.worker_id,
        role="tool_call",
        content=json.dumps(
            {
                "name": "read_file",
                "call_id": "worker-read-1",
                "args": {"path": "README.md"},
                "status": "done",
                "result": "# Project evidence",
                "reasoning_content": "Inspecting the project evidence",
            }
        ),
        conversation_id=str(child_id),
        message_meta={"turn_anchor_id": str(child_input.id)},
        created_at=base_time + timedelta(seconds=1),
    )
    confirmation = ChatMessage(
        agent_id=env.worker_id,
        role="tool_call",
        content=json.dumps(
            {
                "name": "request_confirmation",
                "args": {"title": "Approve delivery", "summary": "Publish the evidence"},
                "status": "pending",
                "result": "",
            }
        ),
        conversation_id=str(child_id),
        message_meta={"turn_anchor_id": str(child_input.id), "turn_status": "suspended"},
        created_at=base_time + timedelta(seconds=2),
    )
    fork_context = ChatMessage(
        agent_id=env.worker_id,
        role="assistant",
        content="Fork context must stay in the child session",
        conversation_id=str(child_id),
        message_meta={"kind": "subagent_fork_context"},
        created_at=base_time + timedelta(seconds=3),
    )
    child_final = ChatMessage(
        agent_id=env.worker_id,
        sender_agent_id=env.worker_id,
        role="assistant",
        content="Worker final answer",
        thinking="Checked the evidence before answering",
        conversation_id=str(child_id),
        message_meta={
            "kind": "subagent_completion",
            "turn_anchor_id": str(child_input.id),
            "project_run_ids": [str(project_run_id)],
            "attachments": [],
        },
        created_at=base_time + timedelta(seconds=4),
    )
    later_human = ChatMessage(
        agent_id=env.leader_id,
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Later Human message C",
        conversation_id=group["id"],
        message_meta={
            "kind": "project_group_message",
            "project_id": str(project_id),
            "visible_to_group": True,
        },
        created_at=base_time + timedelta(seconds=3, milliseconds=500),
    )
    env.db.add_all(
        [running_tool, done_tool, confirmation, fork_context, child_final, later_human]
    )
    await env.db.flush()
    materialized = ChatMessage(
        agent_id=env.leader_id,
        sender_agent_id=env.worker_id,
        role="assistant",
        content=child_final.content,
        conversation_id=group["id"],
        external_event_key=f"project-subagent:{child_final.id}",
        message_meta={
            "kind": "project_subagent_reply",
            "visible_to_group": True,
            "child_message_id": str(child_final.id),
            "subagent_id": str(child_id),
            "source_project_run_ids": [str(project_run_id)],
            "attachments": [],
        },
        created_at=base_time + timedelta(seconds=5),
    )
    env.db.add(materialized)
    await env.db.commit()

    paged_items: list[dict] = []
    before = None
    for _page in range(10):
        params = {"limit": 1}
        if before:
            params["before"] = before
        page = await env.client.get(
            f"/api/projects/{project_id}/group-sessions/{group['id']}/messages",
            params=params,
        )
        assert page.status_code == 200, page.text
        page_payload = page.json()
        paged_items.extend(page_payload["items"])
        if any(
            item.get("role") == "user"
            and item.get("content") == "Show the full execution turn"
            for item in page_payload["items"]
        ):
            break
        before = page_payload["next_cursor"]
        assert before is not None
    else:
        raise AssertionError("group anchor page was not reached")

    paged_finals = [
        item for item in paged_items if item.get("content") == child_final.content
    ]
    assert [item["id"] for item in paged_finals] == [str(materialized.id)]
    assert paged_finals[0]["turnAnchorId"] == str(
        wake.json()["message"]["id"]
    )
    paged_tools = [item for item in paged_items if item.get("role") == "tool_call"]
    assert paged_tools
    assert {
        item["turnAnchorId"] for item in paged_tools
    } == {str(wake.json()["message"]["id"])}

    response = await env.client.get(f"/api/projects/{project_id}/group-sessions/{group['id']}/messages")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len([item for item in items if item["role"] == "user"]) == 2
    assert all(item["content"] != fork_context.content for item in items)

    tools = [item for item in items if item["role"] == "tool_call"]
    read_tools = [item for item in tools if item.get("toolName") == "read_file"]
    assert len(read_tools) == 1
    assert read_tools[0]["toolCallId"] == "worker-read-1"
    assert read_tools[0]["toolStatus"] == "done"
    assert read_tools[0]["toolResult"] == "# Project evidence"
    assert read_tools[0]["content"] == ""
    assert read_tools[0]["display_content"] == ""
    assert read_tools[0]["sender_agent_id"] == str(env.worker_id)
    group_anchor_id = next(
        item["id"]
        for item in items
        if item["role"] == "user" and item["content"] == "Show the full execution turn"
    )
    expected_producer_scope = f"project:{child_id}:{child_input.id}"
    assert read_tools[0]["turnAnchorId"] == group_anchor_id
    assert read_tools[0]["producerScope"] == expected_producer_scope
    assert read_tools[0]["message_meta"]["producer_scope"] == expected_producer_scope

    confirmation_item = next(item for item in tools if item.get("toolName") == "request_confirmation")
    assert confirmation_item["toolCallId"] == str(confirmation.id)
    assert confirmation_item["toolStatus"] == "pending"
    assert confirmation_item["sender_agent_id"] == str(env.worker_id)

    confirmation_payload = json.loads(confirmation.content)
    confirmation_payload.update({"status": "done", "result": "approved"})
    confirmation.content = json.dumps(confirmation_payload)
    durable_child = await env.db.get(SubagentRun, child_id)
    assert durable_child is not None
    durable_child.status = "running"
    project_run = await env.db.get(ProjectRun, project_run_id)
    assert project_run is not None
    project_run.status = "queued"
    await env.db.commit()
    from app.services.subagent_runtime import resume_subagent_after_confirmation

    assert await resume_subagent_after_confirmation(
        child_id,
        resolved_tool_payload={
            "type": "tool_call",
            "name": "request_confirmation",
            "call_id": str(confirmation.id),
            "args": confirmation_payload["args"],
            "status": "done",
            "result": "approved",
        },
        child_turn_anchor_id=child_input.id,
    ) is True
    group_resolved = [
        payload
        for _agent_id, conversation_id, payload in live_events
        if conversation_id == group["id"]
        and payload.get("type") == "tool_call"
        and payload.get("call_id") == str(confirmation.id)
    ]
    assert len(group_resolved) == 1
    assert group_resolved[0]["status"] == "done"
    assert group_resolved[0]["timeline_anchor_id"] == group_anchor_id
    assert group_resolved[0]["producer_scope"] == expected_producer_scope
    assert group_resolved[0]["turn"]["phase"] == "active"

    final_items = [item for item in items if item["content"] == child_final.content]
    assert len(final_items) == 1
    assert final_items[0]["id"] == str(materialized.id)
    assert final_items[0]["thinking"] == child_final.thinking
    assert final_items[0]["sender_agent_id"] == str(env.worker_id)
    assert final_items[0]["turnAnchorId"] == group_anchor_id
    assert final_items[0]["producerScope"] == expected_producer_scope
    assert final_items[0]["canonicalDone"] is True
    item_ids = [item["id"] for item in items]
    later_human_index = item_ids.index(str(later_human.id))
    assert item_ids.index(str(materialized.id)) < later_human_index
    assert item_ids.index(str(confirmation.id)) < later_human_index

async def test_project_group_history_uses_shared_tool_projection(
    project_api: ProjectApiEnv,
):
    env = project_api
    project = await _create_project(env, name="Shared tool projection")
    group = (await env.client.get(f"/api/projects/{project['id']}/group-session")).json()
    anchor = ChatMessage(
        agent_id=env.leader_id,
        user_id=env.owner_id,
        sender_user_id=env.owner_id,
        role="user",
        content="Run the batch",
        conversation_id=group["id"],
        message_meta={"kind": "project_group_message", "visible_to_group": True},
    )
    env.db.add(anchor)
    await env.db.flush()
    tool_base = {
        "name": "toolscall",
        "call_id": "project-batch-1",
        "args": {"table_id": 18, "rows": [{"name": "A"}]},
        "result": "",
    }
    running = ChatMessage(
        agent_id=env.leader_id,
        sender_agent_id=env.leader_id,
        role="tool_call",
        content=json.dumps({**tool_base, "status": "running"}),
        conversation_id=group["id"],
        message_meta={"turn_anchor_id": str(anchor.id)},
    )
    done = ChatMessage(
        agent_id=env.leader_id,
        sender_agent_id=env.leader_id,
        role="tool_call",
        content=json.dumps({**tool_base, "status": "done", "result": "created"}),
        conversation_id=group["id"],
        message_meta={"turn_anchor_id": str(anchor.id)},
    )
    env.db.add_all([running, done])
    await env.db.commit()

    response = await env.client.get(
        f"/api/projects/{project['id']}/group-sessions/{group['id']}/messages"
    )
    assert response.status_code == 200, response.text
    tools = [item for item in response.json()["items"] if item["role"] == "tool_call"]
    assert len(tools) == 1
    assert tools[0]["toolCallId"] == "project-batch-1"
    assert tools[0]["toolStatus"] == "done"
    assert tools[0]["toolResult"] == "created"
    assert tools[0]["toolArgs"] == {"table_id": 18, "rows": [{"name": "A"}]}
    assert tools[0]["content"] == ""
    assert tools[0]["display_content"] == ""
