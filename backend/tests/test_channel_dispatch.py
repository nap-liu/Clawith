"""统一通道派发层 channel_dispatch 的单元测试。

覆盖: ChannelReactions 接口、run_channel_message 的命令旁路/串行/钩子时序/
钩子异常吞掉、per-session 锁的同 key 串行与异 key 并发、与 compactor 锁互不死锁。
"""

import asyncio

import pytest

import app.services.channel_dispatch as cd


def test_channel_reactions_defaults_all_none():
    r = cd.ChannelReactions()
    assert r.on_consume is None
    assert r.on_complete is None
    assert r.on_error is None
    assert r.on_tool_call is None
    assert r.on_thinking is None
    assert r.on_chunk is None


def test_channel_reactions_partial_assignment():
    async def _noop():
        return None

    r = cd.ChannelReactions(on_consume=_noop)
    assert r.on_consume is _noop
    assert r.on_complete is None


async def test_get_session_lock_same_key_returns_same_lock():
    a = await cd._get_session_lock("dingtalk:dingtalk_p2p_1")
    b = await cd._get_session_lock("dingtalk:dingtalk_p2p_1")
    assert a is b


async def test_get_session_lock_different_keys_differ():
    a = await cd._get_session_lock("dingtalk:dingtalk_p2p_1")
    b = await cd._get_session_lock("dingtalk:dingtalk_p2p_2")
    assert a is not b


async def test_same_key_serializes_work():
    """同一 lock_key 下两段工作不会重叠执行。"""
    order = []
    lock = await cd._get_session_lock("k:serial")

    async def worker(tag):
        async with lock:
            order.append(f"{tag}-start")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-end")

    await asyncio.gather(worker("A"), worker("B"))
    assert order in (
        ["A-start", "A-end", "B-start", "B-end"],
        ["B-start", "B-end", "A-start", "A-end"],
    )


async def test_command_bypasses_lock_and_reactions():
    """is_command=True: 直接执行 work,不触发 on_consume/on_complete。"""
    events = []

    async def on_consume():
        events.append("consume")

    async def on_complete(reply):
        events.append(f"complete:{reply}")

    async def work():
        events.append("work")
        return "已开启新对话"

    reactions = cd.ChannelReactions(on_consume=on_consume, on_complete=on_complete)
    result = await cd.run_channel_message(
        "k:cmd", is_command=True, reactions=reactions, work=work
    )
    assert result == "已开启新对话"
    assert events == ["work"]


async def test_normal_message_fires_consume_then_work_then_complete():
    events = []

    async def on_consume():
        events.append("consume")

    async def on_complete(reply):
        events.append(f"complete:{reply}")

    async def work():
        events.append("work")
        return "回复内容"

    reactions = cd.ChannelReactions(on_consume=on_consume, on_complete=on_complete)
    result = await cd.run_channel_message(
        "k:normal", is_command=False, reactions=reactions, work=work
    )
    assert result == "回复内容"
    assert events == ["consume", "work", "complete:回复内容"]


async def test_work_exception_fires_on_error_and_reraises():
    events = []

    async def on_error(exc):
        events.append(f"error:{type(exc).__name__}")

    async def work():
        raise ValueError("boom")

    reactions = cd.ChannelReactions(on_error=on_error)
    with pytest.raises(ValueError):
        await cd.run_channel_message(
            "k:err", is_command=False, reactions=reactions, work=work
        )
    assert events == ["error:ValueError"]


async def test_hook_exception_is_swallowed_and_does_not_break_turn():
    """钩子抛错必须被吞掉,不影响主流程返回。"""

    async def on_consume():
        raise RuntimeError("reaction api down")

    async def work():
        return "ok"

    reactions = cd.ChannelReactions(on_consume=on_consume)
    result = await cd.run_channel_message(
        "k:hookerr", is_command=False, reactions=reactions, work=work
    )
    assert result == "ok"


async def test_normal_message_holds_session_lock():
    """普通消息执行期间持有该 session 锁(并发同 key 不重叠)。"""
    order = []

    def make_work(tag):
        async def _w():
            order.append(f"{tag}-start")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-end")
            return tag
        return _w

    r = cd.ChannelReactions()
    await asyncio.gather(
        cd.run_channel_message("k:hold", is_command=False, reactions=r, work=make_work("A")),
        cd.run_channel_message("k:hold", is_command=False, reactions=r, work=make_work("B")),
    )
    assert order in (
        ["A-start", "A-end", "B-start", "B-end"],
        ["B-start", "B-end", "A-start", "A-end"],
    )


async def test_processing_lock_independent_from_compactor_lock():
    """持有 channel_dispatch 的 session 锁时,仍能取到 compactor 的 session 锁。

    模拟工具循环内 maybe_compact 取压缩锁的场景 —— 两者必须是不同注册表,
    否则 asyncio.Lock 不可重入会死锁。
    """
    from app.services.llm import compactor

    key = "deadlock-probe"
    proc_lock = await cd._get_session_lock(key)
    comp_lock = await compactor._get_session_lock(key)
    assert proc_lock is not comp_lock

    async with proc_lock:
        await asyncio.wait_for(comp_lock.acquire(), timeout=0.5)
        comp_lock.release()


async def test_different_keys_run_concurrently():
    """不同 lock_key 的 run_channel_message 应当并发执行(互不阻塞)。"""
    order = []
    r = cd.ChannelReactions()

    def make_w(tag):
        async def _w():
            order.append(f"{tag}-start")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-end")
            return tag
        return _w

    await asyncio.gather(
        cd.run_channel_message("k:key1", is_command=False, reactions=r, work=make_w("A")),
        cd.run_channel_message("k:key2", is_command=False, reactions=r, work=make_w("B")),
    )
    # 不同 key 并发 → 两个 start 都先于两个 end
    assert order[:2] == ["A-start", "B-start"] or order[:2] == ["B-start", "A-start"]
    assert set(order[2:]) == {"A-end", "B-end"}


async def test_cancel_running_turn_cancels_registered_work():
    """普通 IM turn 执行期间应可被 /stop 通过 lock_key 取消。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "should-not-return"

    task = asyncio.create_task(
        cd.run_channel_message("k:cancel", is_command=False, reactions=cd.ChannelReactions(), work=work)
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await cd.cancel_running_turn("k:cancel") is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert await cd.cancel_running_turn("k:cancel") is False


async def test_cancel_running_turn_only_affects_matching_key():
    """`/stop` 必须只取消同一 IM 会话,不能误杀其他会话的 turn。"""
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    cancelled_a = asyncio.Event()
    finished_b = asyncio.Event()

    async def work_a():
        started_a.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled_a.set()
            raise

    async def work_b():
        started_b.set()
        await asyncio.sleep(0.05)
        finished_b.set()
        return "b"

    task_a = asyncio.create_task(
        cd.run_channel_message("k:cancel-a", is_command=False, reactions=cd.ChannelReactions(), work=work_a)
    )
    task_b = asyncio.create_task(
        cd.run_channel_message("k:cancel-b", is_command=False, reactions=cd.ChannelReactions(), work=work_b)
    )
    await asyncio.wait_for(started_a.wait(), timeout=1)
    await asyncio.wait_for(started_b.wait(), timeout=1)

    assert await cd.cancel_running_turn("k:cancel-a") is True
    with pytest.raises(asyncio.CancelledError):
        await task_a
    assert await task_b == "b"
    assert cancelled_a.is_set()
    assert finished_b.is_set()


async def test_cancel_running_turn_cancels_queued_turn_for_same_key():
    """`/stop` 应取消同一 IM 会话中已排队但尚未开始执行的 turn。"""
    running_started = asyncio.Event()
    release_running = asyncio.Event()
    queued_work_started = asyncio.Event()
    running_cancelled = asyncio.Event()
    queued_cancelled = asyncio.Event()

    async def running_work():
        running_started.set()
        try:
            await release_running.wait()
        except asyncio.CancelledError:
            running_cancelled.set()
            raise
        return "running"

    async def queued_work():
        queued_work_started.set()
        return "queued"

    task_running = asyncio.create_task(
        cd.run_channel_message("k:queued-cancel", is_command=False, reactions=cd.ChannelReactions(), work=running_work)
    )
    await asyncio.wait_for(running_started.wait(), timeout=1)
    task_queued = asyncio.create_task(
        cd.run_channel_message("k:queued-cancel", is_command=False, reactions=cd.ChannelReactions(), work=queued_work)
    )
    await asyncio.sleep(0)

    assert await cd.cancel_running_turn("k:queued-cancel") is True
    try:
        await task_queued
    except asyncio.CancelledError:
        queued_cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        await task_running

    assert running_cancelled.is_set()
    assert queued_cancelled.is_set()
    assert not queued_work_started.is_set()


async def test_cancelled_turn_runs_error_reaction_before_reraising():
    """取消当前 turn 时也要触发 on_error,用于 IM 通道立刻清理 loading/reaction。"""
    started = asyncio.Event()
    errors: list[BaseException] = []

    async def work():
        started.set()
        await asyncio.sleep(10)

    async def on_error(exc: BaseException):
        errors.append(exc)

    task = asyncio.create_task(
        cd.run_channel_message(
            "k:cancel-reaction",
            is_command=False,
            reactions=cd.ChannelReactions(on_error=on_error),
            work=work,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await cd.cancel_running_turn("k:cancel-reaction") is True
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
