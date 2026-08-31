from project_actions_support import *  # noqa: F401,F403

async def test_manual_run_requires_kickoff_and_dispatches_default_leader(project_api: ProjectApiEnv):
    from app.models.chat_session import ChatSession
    from app.models.project import Project, ProjectRun
    from app.models.subagent_run import SubagentRun

    env = project_api
    project = await _create_project(env, name="Manual execution contract")
    project_id = uuid.UUID(project["id"])

    before_kickoff = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"trigger_type": "manual", "input": {"objective": "Continue delivery"}},
    )
    assert before_kickoff.status_code == 409

    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    await env.db.commit()

    empty = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"trigger_type": "manual", "input": {}},
    )
    assert empty.status_code == 422
    assert "explicit task/objective/message" in empty.text

    response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"trigger_type": "manual", "input": {"objective": "Continue delivery"}},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["agent_id"] == str(env.leader_id)
    assert payload["status"] in {"queued", "running"}
    assert payload["input"]["dispatch"]["task"] == "Continue delivery"
    assert payload["output"]["subagent_session_id"]

    run = await env.db.get(ProjectRun, uuid.UUID(payload["id"]))
    child = await env.db.get(SubagentRun, uuid.UUID(payload["output"]["subagent_session_id"]))
    assert child is not None
    child_session = await env.db.get(ChatSession, child.id)
    assert child_session is not None
    assert child_session.agent_id == env.leader_id
    assert run is not None and run.output["subagent_run_id"] == str(child.id)
    assert child.project_id == project_id
    tenant = await env.db.get(Tenant, env.tenant_id)
    anchor = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child.id),
                ChatMessage.message_meta["project_run_id"].as_string() == str(run.id),
            )
        )
    ).scalar_one()
    assert tenant is not None and tenant.default_model_id is not None
    tenant_model = await env.db.get(LLMModel, tenant.default_model_id)
    assert tenant_model is not None
    assert child.model is None
    from app.services.chat_model_selection import resolve_project_member_runtime_models

    project_agent = await env.db.get(Agent, env.leader_id)
    assert project_agent is not None
    resolved_models = await resolve_project_member_runtime_models(
        env.db,
        agent=project_agent,
        member_config=child_session.im_config["member_config_snapshot"],
        project_settings=stored_project.settings,
    )
    assert resolved_models.primary_model is not None
    assert resolved_models.primary_model.id == tenant_model.id
    assert "model_id" not in anchor.message_meta

async def test_project_run_and_leader_batch_use_frozen_shared_execution_user(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import Project, ProjectRun
    from app.models.subagent_run import SubagentRun
    from app.services import subagent_runtime

    env = project_api
    project = await _create_project(env, name="Frozen shared execution")
    project_id = uuid.UUID(project["id"])
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
    env.authenticate_as(env.owner_id)
    await _mark_project_running(env, project_id)

    real_created = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={"trigger_type": "manual", "input": {"objective": "Use the selected shared identity"}},
    )
    assert real_created.status_code == 201, real_created.text
    real_child = await env.db.get(
        SubagentRun,
        uuid.UUID(real_created.json()["output"]["subagent_session_id"]),
    )
    assert real_child is not None and real_child.execution_user_id == env.viewer_id

    real_dispatch = subagent_runtime.dispatch_project_run

    async def defer_dispatch(_run_id: uuid.UUID) -> dict[str, str]:
        return {"status": "queued"}

    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", defer_dispatch)
    created = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={
            "agent_id": str(env.worker_id),
            "trigger_type": "manual",
            "input": {"objective": "Freeze this execution identity"},
        },
    )
    assert created.status_code == 201, created.text
    run_id = uuid.UUID(created.json()["id"])
    frozen_run = await env.db.get(ProjectRun, run_id)
    assert frozen_run is not None
    assert frozen_run.initiated_by_user_id == env.owner_id
    assert frozen_run.execution_user_id == env.viewer_id

    tenant = await env.db.get(Tenant, env.tenant_id)
    assert tenant is not None
    alternate = await _user(env.db, tenant, "Dispatch Alternate")
    await env.db.commit()
    alternate_id = alternate.id
    env.authenticate_as(env.org_admin_id)
    changed = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "shared_with_user_ids": [str(env.viewer_id), str(alternate_id)],
            "execution_user_id": str(alternate_id),
        },
    )
    assert changed.status_code == 200, changed.text
    env.authenticate_as(env.owner_id)

    dispatched_users: list[uuid.UUID] = []

    async def capture_subagent(**kwargs):
        dispatched_users.append(kwargs["execution_user_id"])
        return SimpleNamespace(id=uuid.uuid4(), status="queued"), True

    monkeypatch.setattr(subagent_runtime, "create_subagent", capture_subagent)
    dispatched = await real_dispatch(run_id)
    assert dispatched["status"] == "queued"
    assert dispatched_users == [env.viewer_id]

    env.db.expire_all()
    frozen_run = await env.db.get(ProjectRun, run_id)
    assert frozen_run is not None
    group_id = uuid.UUID(frozen_run.input["dispatch"]["group_session_id"])
    group = await env.db.get(ChatSession, group_id)
    assert group is not None
    env.db.add(
        ChatMessage(
            agent_id=group.agent_id,
            sender_agent_id=env.worker_id,
            role="assistant",
            content="Participant evidence is ready",
            conversation_id=str(group.id),
            message_meta={
                "kind": "project_subagent_reply",
                "leader_batch_state": "pending",
                "source_project_run_ids": [str(run_id)],
            },
            created_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await env.db.commit()

    assert await subagent_runtime._dispatch_project_leader_batch(group.id, debounce_seconds=0) is True
    assert dispatched_users == [env.viewer_id, alternate_id]
    batch_run = (
        await env.db.execute(
            select(ProjectRun).where(
                ProjectRun.project_id == project_id,
                ProjectRun.trigger_type == "leader_reply_batch",
            )
        )
    ).scalar_one()
    assert batch_run.initiated_by_user_id == env.owner_id
    assert batch_run.execution_user_id == alternate_id
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None and stored_project.execution_user_id == alternate_id

async def test_project_run_without_agent_model_uses_exact_project_model(
    project_api: ProjectApiEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.project import ProjectRun
    from app.models.subagent_run import SubagentRun

    env = project_api
    selected = LLMModel(
        tenant_id=env.tenant_id,
        provider="openai",
        model="project-explicit-model",
        api_key_encrypted="project-test-only",
        label="Project explicit model",
        enabled=True,
        context_window=64000,
    )
    env.db.add(selected)
    await env.db.flush()
    project = await _create_project(env, name="Project model fallback")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "runtime": {"model": str(selected.id)},
    }
    await env.db.commit()

    response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={
            "agent_id": str(env.worker_id),
            "trigger_type": "manual",
            "input": {"objective": "Use the project model"},
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    child_id = uuid.UUID(payload["output"]["subagent_session_id"])
    child = await env.db.get(SubagentRun, child_id)
    project_run = await env.db.get(ProjectRun, uuid.UUID(payload["id"]))
    anchor = (
        await env.db.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(child_id),
                ChatMessage.message_meta["project_run_id"].as_string() == payload["id"],
            )
        )
    ).scalar_one()
    assert child is not None and child.project_id == project_id
    assert project_run is not None and project_run.status in {"queued", "running"}
    assert child.model is None
    assert "model_id" not in anchor.message_meta

    # Historical project child inputs did not carry a per-turn model snapshot.
    # The unified channel path must still resolve the same project model rather
    # than falling back to an absent Agent-level configuration.
    anchor.message_meta = {key: value for key, value in dict(anchor.message_meta or {}).items() if key != "model_id"}
    await env.db.commit()
    captured: dict = {}

    async def _fake_scene(*_args, **_kwargs):
        return {}

    async def _fake_llm(**kwargs):
        captured.update(kwargs)
        return "project model works"

    monkeypatch.setattr("app.services.scene_service.load_turn_scene_context", _fake_scene)
    monkeypatch.setattr("app.services.llm.call_llm_with_failover", _fake_llm)
    monkeypatch.setattr("app.services.channel_llm.is_agent_expired", lambda _agent: False)
    from app.services.channel_llm import _call_agent_llm
    from app.services.agent_runtime_workspace import resolve_agent_runtime_workspace

    async with env.session_factory() as runtime_db:
        runtime_agent = await runtime_db.get(Agent, env.worker_id)
        runtime_session = await runtime_db.get(ChatSession, child_id)
        assert runtime_agent is not None and runtime_session is not None
        runtime_workspace = resolve_agent_runtime_workspace(
            agent_id=runtime_agent.id,
            agent_scope=runtime_agent.scope,
            agent_project_id=runtime_agent.project_id,
            tenant_id=runtime_agent.tenant_id,
            session_project_id=runtime_session.project_id,
            session_config=runtime_session.im_config,
        )
        reply = await _call_agent_llm(
            runtime_db,
            env.worker_id,
            anchor.content,
            session_id=str(child_id),
            user_id=env.owner_id,
            turn_anchor_id=anchor.id,
            prepared_tools=[],
            broadcast_web=False,
            runtime_session=runtime_session,
            runtime_workspace=runtime_workspace,
        )
    assert reply == "project model works"
    assert captured["primary_model"].id == selected.id

async def test_project_run_without_any_tenant_model_keeps_a_durable_unresolved_child(project_api: ProjectApiEnv):
    from app.models.project import ProjectRun
    from app.models.subagent_run import SubagentRun

    env = project_api
    tenant_models = (await env.db.execute(select(LLMModel).where(LLMModel.tenant_id == env.tenant_id))).scalars().all()
    for model in tenant_models:
        model.enabled = False

    foreign_tenant = Tenant(name="Foreign", slug=f"foreign-{uuid.uuid4().hex[:8]}")
    env.db.add(foreign_tenant)
    await env.db.flush()
    foreign_model = LLMModel(
        tenant_id=foreign_tenant.id,
        provider="openai",
        model="foreign-model",
        api_key_encrypted="must-not-cross-tenant",
        label="Foreign model",
        enabled=True,
        context_window=64000,
    )
    env.db.add(foreign_model)
    await env.db.flush()

    project = await _create_project(env, name="No model project")
    project_id = uuid.UUID(project["id"])
    stored_project = await env.db.get(Project, project_id)
    assert stored_project is not None
    stored_project.status = "running"
    stored_project.settings = {
        **dict(stored_project.settings or {}),
        "runtime": {"model": str(foreign_model.id)},
    }
    await env.db.commit()

    response = await env.client.post(
        f"/api/projects/{project_id}/runs",
        json={
            "agent_id": str(env.worker_id),
            "trigger_type": "manual",
            "input": {"objective": "Must fail explicitly"},
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    project_run = await env.db.get(ProjectRun, uuid.UUID(payload["id"]))
    children = (await env.db.execute(select(SubagentRun).where(SubagentRun.project_id == project_id))).scalars().all()
    assert project_run is not None and project_run.status in {"queued", "running"}
    assert project_run.error is None
    assert payload["output"].get("subagent_session_id")
    assert len(children) == 1 and children[0].model is None

async def test_project_run_reconcile_persists_finished_terminal_state(project_api: ProjectApiEnv):
    from app.models.project import ProjectRun

    env = project_api
    project = await _create_project(env, name="Repair stale run")
    run = ProjectRun(
        tenant_id=env.tenant_id,
        project_id=uuid.UUID(project["id"]),
        initiated_by_user_id=env.owner_id,
        status="running",
        trigger_type="manual",
        finished_at=datetime.now(UTC),
        output={"result": "Already finished"},
    )
    env.db.add(run)
    await env.db.commit()

    response = await env.client.get(f"/api/projects/{project['id']}/runs")
    assert response.status_code == 200, response.text
    repaired_payload = next(item for item in response.json() if item["id"] == str(run.id))
    assert repaired_payload["status"] == "succeeded"

    # Verify the GET-owned reconciliation committed, rather than merely
    # changing the request session identity map.
    async with env.session_factory() as independent_db:
        persisted = await independent_db.get(ProjectRun, run.id)
        assert persisted is not None and persisted.status == "succeeded"
