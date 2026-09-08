"""Observable isolation, callback ordering, cancellation and failure contracts."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.services.agent_execution.runtime import run_isolated

FIXTURES = "tests.execution_process_fixtures:"


async def invoke(name, arguments, *, agent="agent-a"):
    return await run_isolated(FIXTURES + name, arguments, agent_id=agent, context={})


async def test_process_and_callback_mutations():
    async def callback(value):
        value["receipt"] = {"status": "sent"}

    result = await invoke("echo", {"value": {"receipt": None}, "callback": callback})
    assert result["pid"] != os.getpid()
    assert result["value"] == {"receipt": {"status": "sent"}}


async def test_nested_inbox_callback_preserves_order():
    from uuid import uuid4
    from app.services.active_turns import (
        active_turn_boundary, commit_current_turn_anchor, ensure_active_turn, list_active_turns,
    )

    aid, uid, sid, anchor = uuid4(), uuid4(), str(uuid4()), uuid4()
    committed = []

    async def before_round(round_i, *, before_injection):
        assert round_i == 2
        await before_injection()

        async def commit():
            committed.append(anchor)

        await commit_current_turn_anchor(commit, agent_id=aid, session_id=sid, message_id=anchor)
        return [{"role": "user", "content": "follow up"}]

    async with active_turn_boundary():
        root = await ensure_active_turn(owner_user_id=uid, agent_id=aid, session_id=sid)
        events, messages = await invoke("nested_callback", {"callback": before_round})
        assert await list_active_turns(owner_user_id=uid) == [root]
        assert [item.message_id for item in root.durable_anchors] == [anchor]
    assert committed == [anchor]
    assert events == ["durable"]
    assert messages == [{"role": "user", "content": "follow up"}]


async def test_typed_failure_retains_no_retry_policy():
    result = await invoke("typed_failure", {})
    assert isinstance(result, str)
    assert result.code == "model_response_idle_timeout"
    assert result.retryable is False
    assert result.allow_failover is False


async def test_blocked_agent_does_not_block_other_agent_or_supervisor():
    started = asyncio.Event()
    process_ids = []

    async def callback(pid):
        process_ids.append(pid)
        started.set()

    blocked = asyncio.create_task(invoke("block", {"callback": callback}))
    await asyncio.wait_for(started.wait(), 30)
    tick = asyncio.get_running_loop().time()
    await asyncio.sleep(0.05)
    assert asyncio.get_running_loop().time() - tick < 0.5
    other = await asyncio.wait_for(invoke("echo", {"value": "ok"}, agent="agent-b"), 15)
    assert other["value"] == "ok"
    assert not blocked.done()
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    with pytest.raises(ProcessLookupError):
        os.kill(process_ids[0], 0)


async def test_cancel_reaps_child(monkeypatch):
    from uuid import uuid4
    from tests.execution_process_fixtures import cancellable_turn
    from app.services.active_turns import cancel_active_turn, list_active_turns

    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "1")
    started = asyncio.Event()
    process_ids = []
    aid, uid, sid = uuid4(), uuid4(), str(uuid4())

    async def callback(pid):
        process_ids.append(pid)
        started.set()

    task = asyncio.create_task(cancellable_turn(aid, uid, sid, callback))
    await asyncio.wait_for(started.wait(), 30)
    records = await list_active_turns(owner_user_id=uid)
    assert len(records) == 1
    assert await cancel_active_turn(records[0].turn_id, owner_user_id=uuid4()) is None
    assert not task.done()
    await cancel_active_turn(records[0].turn_id, owner_user_id=uid)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    with pytest.raises(ProcessLookupError):
        os.kill(process_ids[0], 0)


async def test_provider_limit_is_shared_between_execution_children(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_MAX_IN_FLIGHT", "1")
    model = SimpleNamespace(provider="test", base_url="test", api_key_encrypted="test")
    entries = []

    async def callback(value):
        entries.append(asyncio.get_running_loop().time())

    results = await asyncio.gather(*[
        invoke("provider_wait", {"model": model, "callback": callback}, agent=str(index))
        for index in range(2)
    ])
    assert results == ["complete", "complete"]
    assert entries[1] - entries[0] >= 0.18


@pytest.mark.parametrize("error_type", [OSError, TimeoutError, ConnectionError])
async def test_callback_exception_preserves_error_contract(error_type):
    async def callback(value):
        raise error_type(5, "Test IO error", "/tmp/report")

    with pytest.raises(error_type) as error:
        await invoke("echo", {"value": None, "callback": callback})
    assert error.value.errno == 5
    assert error.value.filename == "/tmp/report"


async def test_repeated_cancel_still_kills_a_blocked_child():
    started = asyncio.Event()
    pids = []

    async def callback(pid):
        pids.append(pid)
        started.set()

    task = asyncio.create_task(invoke("block", {"callback": callback}))
    await asyncio.wait_for(started.wait(), 30)
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
