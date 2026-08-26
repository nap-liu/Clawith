"""PostgreSQL behavior coverage for a safe rollback to a pre-project binary."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 - register ChatSession FK target
from app.models.project import Project, ProjectMemberSnapshot
from app.models.schedule import AgentSchedule
from app.models.subagent_run import SubagentRun
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.user import Identity, User
from app.scripts import project_legacy_rollback as rollback

pytestmark = pytest.mark.asyncio


async def _business_snapshot(ids: dict[str, uuid.UUID]) -> dict[str, list[str]]:
    tables = {
        "agents": ("id", [ids["standard_agent"], ids["project_agent"]]),
        "projects": ("id", [ids["project"]]),
        "project_member_snapshots": ("id", [ids["member"]]),
        "chat_sessions": (
            "id",
            [
                ids["standard_parent_session"],
                ids["standard_child_session"],
                ids["project_parent_session"],
                ids["project_child_session"],
            ],
        ),
        "subagent_runs": ("id", [ids["standard_child_session"], ids["project_child_session"]]),
        "tasks": ("id", [ids["project_task"]]),
        "agent_schedules": ("id", [ids["project_schedule"]]),
        "agent_triggers": ("id", [ids["project_trigger"]]),
    }
    snapshot: dict[str, list[str]] = {}
    async with engine.connect() as connection:
        for table_name, (column_name, expected_ids) in tables.items():
            values = (
                await connection.execute(
                    text(
                        f'SELECT CAST("{column_name}" AS text) FROM "{table_name}" '
                        f'WHERE "{column_name}" = ANY(:ids) ORDER BY "{column_name}"'
                    ),
                    {"ids": expected_ids},
                )
            ).scalars()
            snapshot[table_name] = list(values)
    return snapshot


async def test_apply_status_restore_isolate_project_rows_and_worker_queue() -> None:
    if make_url(get_settings().DATABASE_URL).get_backend_name() != "postgresql":
        pytest.skip("legacy rollback boundary requires PostgreSQL RLS")

    suffix = uuid.uuid4().hex[:12]
    ids = {
        "tenant": uuid.uuid4(),
        "identity": uuid.uuid4(),
        "user": uuid.uuid4(),
        "project": uuid.uuid4(),
        "standard_agent": uuid.uuid4(),
        "project_agent": uuid.uuid4(),
        "member": uuid.uuid4(),
        "standard_parent_session": uuid.uuid4(),
        "standard_child_session": uuid.UUID(f"ffffffff-ffff-4fff-8fff-{suffix}"),
        "project_parent_session": uuid.uuid4(),
        "project_child_session": uuid.UUID(f"00000000-0000-4000-8000-{suffix}"),
        "project_task": uuid.uuid4(),
        "project_schedule": uuid.uuid4(),
        "project_trigger": uuid.uuid4(),
    }

    # Status is deliberately read-only even before the helper has ever run.
    async with engine.begin() as connection:
        state_exists = bool(
            await connection.scalar(
                text("SELECT to_regclass(:name) IS NOT NULL"),
                {"name": f"public.{rollback.STATE_TABLE}"},
            )
        )
        if state_exists:
            active = bool(
                await connection.scalar(text(f'SELECT EXISTS (SELECT 1 FROM public."{rollback.STATE_TABLE}")'))
            )
            if active:
                pytest.fail("legacy rollback helper is already active in the isolated test database")
            await connection.execute(text(f'DROP TABLE public."{rollback.STATE_TABLE}"'))
        if await rollback._role_exists(connection):
            pytest.fail("dedicated legacy role already exists in the isolated test cluster")
    assert (await rollback.status())["active"] is False
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT to_regclass(:name) IS NULL"),
                {"name": f"public.{rollback.STATE_TABLE}"},
            )
        ) is True

    # A fixed-name role not owned by an active helper may carry arbitrary
    # cluster privileges. First apply must not alter or adopt it.
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE ROLE "{rollback.LEGACY_ROLE}" LOGIN'))
    with pytest.raises(RuntimeError, match="refusing to alter an unmanaged role"):
        await rollback.apply(f"unmanaged-{suffix}")
    async with engine.begin() as connection:
        attributes = (
            await connection.execute(
                text(
                    """
                    SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit,
                           rolreplication, rolbypassrls
                    FROM pg_roles WHERE rolname = :role
                    """
                ),
                {"role": rollback.LEGACY_ROLE},
            )
        ).one()
        assert tuple(attributes) == (False, False, False, True, False, False)
        await connection.execute(text(f'DROP ROLE "{rollback.LEGACY_ROLE}"'))

    async with async_session() as db:
        tenant = Tenant(id=ids["tenant"], name=f"rollback-{suffix}", slug=f"rollback-{suffix}")
        identity = Identity(
            id=ids["identity"],
            username=f"rollback-{suffix}",
            email=f"rollback-{suffix}@test.local",
            password_hash="unused",
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            id=ids["user"],
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Rollback tester",
            role="member",
        )
        db.add(user)
        await db.flush()
        project = Project(
            id=ids["project"],
            tenant_id=tenant.id,
            owner_user_id=user.id,
            execution_user_id=user.id,
            name=f"Rollback project {suffix}",
        )
        standard_agent = Agent(
            id=ids["standard_agent"],
            name=f"Standard {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            scope="standard",
            status="idle",
        )
        db.add_all([project, standard_agent])
        await db.flush()
        project_agent = Agent(
            id=ids["project_agent"],
            name=f"Project {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            scope="project",
            project_id=project.id,
            agent_dir=f".agents/{ids['project_agent']}",
            status="idle",
        )
        db.add(project_agent)
        await db.flush()
        member = ProjectMemberSnapshot(
            id=ids["member"],
            tenant_id=tenant.id,
            project_id=project.id,
            agent_id=project_agent.id,
            name_snapshot=project_agent.name,
        )
        sessions = [
            ChatSession(
                id=ids["standard_parent_session"],
                agent_id=standard_agent.id,
                user_id=user.id,
                title="Standard parent",
                source_channel="web",
            ),
            ChatSession(
                id=ids["standard_child_session"],
                agent_id=standard_agent.id,
                user_id=user.id,
                title="Standard child",
                source_channel="subagent",
            ),
            ChatSession(
                id=ids["project_parent_session"],
                project_id=project.id,
                agent_id=project_agent.id,
                title="Project parent",
                source_channel="project",
            ),
            ChatSession(
                id=ids["project_child_session"],
                project_id=project.id,
                agent_id=project_agent.id,
                user_id=user.id,
                title="Project child",
                source_channel="subagent",
            ),
        ]
        db.add_all([member, *sessions])
        await db.flush()
        db.add_all(
            [
                SubagentRun(
                    id=ids["standard_child_session"],
                    parent_session_id=ids["standard_parent_session"],
                    execution_user_id=user.id,
                    origin_tool_call_id=f"standard-{suffix}",
                    mode="run",
                    status="queued",
                ),
                SubagentRun(
                    id=ids["project_child_session"],
                    parent_session_id=ids["project_parent_session"],
                    project_id=project.id,
                    project_member_id=member.id,
                    execution_user_id=user.id,
                    origin_tool_call_id=f"project-{suffix}",
                    mode="run",
                    status="queued",
                ),
                Task(
                    id=ids["project_task"],
                    agent_id=project_agent.id,
                    title="Hidden project task",
                    created_by=user.id,
                ),
                AgentSchedule(
                    id=ids["project_schedule"],
                    agent_id=project_agent.id,
                    name="Hidden project schedule",
                    instruction="Do not run in legacy service",
                    cron_expr="* * * * *",
                    created_by=user.id,
                ),
                AgentTrigger(
                    id=ids["project_trigger"],
                    agent_id=project_agent.id,
                    created_by_user_id=user.id,
                    name=f"hidden-{suffix}",
                    type="interval",
                    config={"minutes": 1},
                    reason="Do not run in legacy service",
                ),
            ]
        )
        await db.commit()

    before = await _business_snapshot(ids)
    password = f"rollback-test-{suffix}"
    legacy_engine = None
    try:
        applied = await rollback.apply(password)
        assert applied["applied"] is True
        assert applied["owner_database_url_allowed"] is False
        assert (await rollback.apply())["applied"] is False
        active = await rollback.status()
        assert active["active"] is True
        assert active["role_exists"] is True
        assert active["password_stored"] is False

        owner_url = make_url(get_settings().DATABASE_URL)
        legacy_url = owner_url.set(username=rollback.LEGACY_ROLE, password=password)
        legacy_engine = create_async_engine(legacy_url)
        async with legacy_engine.connect() as connection:
            visible_agents = set(
                (
                    await connection.execute(
                        select(Agent.id).where(Agent.id.in_([ids["standard_agent"], ids["project_agent"]]))
                    )
                ).scalars()
            )
            assert visible_agents == {ids["standard_agent"]}
            assert (
                await connection.scalar(select(ChatSession.id).where(ChatSession.id == ids["project_parent_session"]))
            ) is None
            for table_name, row_id in (
                ("tasks", ids["project_task"]),
                ("agent_schedules", ids["project_schedule"]),
                ("agent_triggers", ids["project_trigger"]),
            ):
                assert (
                    await connection.scalar(
                        text(f'SELECT id FROM "{table_name}" WHERE id = :row_id'),
                        {"row_id": row_id},
                    )
                ) is None

            # This is the legacy worker's observable claim ordering. The lower
            # project UUID would be head-of-line without the project_id RLS
            # boundary; the standard run remains claimable instead.
            claimed = await connection.scalar(
                text(
                    """
                    SELECT id FROM subagent_runs
                    WHERE status = 'queued'
                       OR (status = 'running' AND lease_expires_at < now())
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
            )
            assert claimed == ids["standard_child_session"]
            assert (
                await connection.scalar(select(SubagentRun.id).where(SubagentRun.id == ids["project_child_session"]))
            ) is None
            # A standard row remains writable by the dedicated runtime role.
            assert (
                await connection.scalar(
                    text("UPDATE agents SET role_description = role_description WHERE id = :id RETURNING id"),
                    {"id": ids["standard_agent"]},
                )
            ) == ids["standard_agent"]
            await connection.rollback()

        restored = await rollback.restore()
        assert restored["restored"] is True
        assert (await rollback.restore())["restored"] is False
        assert (await rollback.status())["active"] is False
        assert await _business_snapshot(ids) == before
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(SubagentRun.status).where(SubagentRun.id == ids["project_child_session"])
                )
            ) == "queued"
    finally:
        if legacy_engine is not None:
            await legacy_engine.dispose()
        if (await rollback.status())["active"]:
            await rollback.restore()
        async with async_session() as db:
            await db.execute(
                delete(SubagentRun).where(
                    SubagentRun.id.in_([ids["standard_child_session"], ids["project_child_session"]])
                )
            )
            await db.execute(
                delete(ChatSession).where(
                    ChatSession.id.in_(
                        [
                            ids["standard_parent_session"],
                            ids["standard_child_session"],
                            ids["project_parent_session"],
                            ids["project_child_session"],
                        ]
                    )
                )
            )
            await db.execute(delete(Task).where(Task.id == ids["project_task"]))
            await db.execute(delete(AgentSchedule).where(AgentSchedule.id == ids["project_schedule"]))
            await db.execute(delete(AgentTrigger).where(AgentTrigger.id == ids["project_trigger"]))
            await db.execute(delete(ProjectMemberSnapshot).where(ProjectMemberSnapshot.id == ids["member"]))
            await db.execute(delete(Agent).where(Agent.id.in_([ids["standard_agent"], ids["project_agent"]])))
            await db.execute(delete(Project).where(Project.id == ids["project"]))
            await db.execute(delete(User).where(User.id == ids["user"]))
            await db.execute(delete(Identity).where(Identity.id == ids["identity"]))
            await db.execute(delete(Tenant).where(Tenant.id == ids["tenant"]))
            await db.commit()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP TABLE IF EXISTS public."{rollback.STATE_TABLE}"'))
        await engine.dispose()
