"""Real child-process regressions found by the independent lifecycle review."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.task import Task
from app.services.active_turns import list_active_turns, reserve_active_turn_stop
from tests.execution_process_fixtures import admit_nested, create_todo
from tests.execution_provider_fixture import provider
from tests.test_agent_execution_integration import seed


@pytest.fixture(autouse=True)
async def isolated_engine(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "1")
    await engine.dispose()
    yield
    await engine.dispose()


async def test_todo_finishes_after_creating_turn_exits():
    gate = asyncio.Event()
    async with provider([{"content": "Independent task completed.", "_wait_for": gate}]) as (url, requests):
        aid, uid, sid, _, _ = await seed(url)
        reply = await create_todo(aid, uid, str(sid))
        assert "auto-execution started" in reply
        try:
            async with asyncio.timeout(30):
                while not requests:
                    await asyncio.sleep(0.05)
            async with async_session() as db:
                task = await db.scalar(select(Task).where(Task.agent_id == aid))
                assert task.status == "doing"
            gate.set()
            async with asyncio.timeout(30):
                while True:
                    async with async_session() as db:
                        task = await db.get(Task, task.id)
                        if task.status == "done":
                            break
                    await asyncio.sleep(0.05)
            assert len(requests) == 1
        finally:
            gate.set()


async def test_stop_waits_for_child_commit_and_cancels_all_durable_anchors():
    from app.mcp_server.tools_turns import _commit_reserved_stop

    aid, uid, sid, anchor, _ = await seed("http://127.0.0.1:1")
    nested_sid, nested_anchor = uuid.uuid4(), uuid.uuid4()
    before_commit, release_commit = asyncio.Event(), asyncio.Event()

    async def callback(stage, pid):
        assert stage == "before_commit"
        before_commit.set()
        await release_commit.wait()

    root = asyncio.create_task(admit_nested(aid, uid, str(sid), anchor,
                                           nested_sid, nested_anchor, callback))
    try:
        await asyncio.wait_for(before_commit.wait(), 30)
        records = await list_active_turns(owner_user_id=uid)
        assert len(records) == 1
        reservation = asyncio.create_task(reserve_active_turn_stop(records[0].turn_id, owner_user_id=uid))
        await asyncio.sleep(0.05)
        assert not reservation.done()
        release_commit.set()
        record, token = await asyncio.wait_for(reservation, 5)
        assert {item.message_id for item in record.durable_anchors} == {anchor, nested_anchor}
        await _commit_reserved_stop(record=record, stop_token=token, actor_user_id=uid, is_admin=False)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(root, 5)
        async with async_session() as db:
            rows = (await db.scalars(select(ChatMessage).where(
                ChatMessage.id.in_([anchor, nested_anchor])))).all()
            assert len(rows) == 2
            assert all(row.message_meta["turn_status"] == "cancelled" for row in rows)
    finally:
        release_commit.set()
        if not root.done():
            root.cancel()
        await asyncio.gather(root, return_exceptions=True)


async def test_legacy_default_identity_and_session_keep_cancellable_root():
    from app.services.active_turns import active_turn_boundary, cancel_active_turn
    from app.services.llm import call_agent_llm_with_tools

    gate = asyncio.Event()
    async with provider([{"content": "Complete.", "_wait_for": gate}]) as (url, requests):
        aid, uid, _, _, _ = await seed(url)

        async def run():
            async with active_turn_boundary(), async_session() as db:
                return await call_agent_llm_with_tools(db, aid, "Assistant", "Work")

        root = asyncio.create_task(run())
        try:
            async with asyncio.timeout(30):
                while not requests:
                    await asyncio.sleep(0.05)
            records = await list_active_turns(owner_user_id=uid)
            assert len(records) == 1
            assert records[0].task is root
            assert records[0].session_id.startswith("background:")
            await cancel_active_turn(records[0].turn_id, owner_user_id=uid)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(root, 5)
            assert not await list_active_turns(owner_user_id=uid)
        finally:
            gate.set()
            if not root.done():
                root.cancel()
            await asyncio.gather(root, return_exceptions=True)
