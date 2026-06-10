"""Web-chat turn lifecycle vs. transport lifecycle (`_await_turn_with_abort`).

Regression guards for the "agent gave no reply" bug: a web turn that is mid
tool-loop when the browser WebSocket drops used to be *cancelled*, discarding the
assistant reply (only the incremental tool rows survived). The turn is a unit of
work and must run to completion + persist regardless of the socket — parity with
the IM / trigger channels, which are connection-independent.

These tests pin the drive-loop invariant directly (no live socket needed):

- explicit user **abort** → cancel the turn, return the partial + stop marker
- client **disconnect** → DO NOT cancel; let the turn finish, return its real reply
- normal **completion** → return the reply as "completed"
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.websocket import _await_turn_with_abort

pytestmark = pytest.mark.asyncio


async def test_disconnect_does_not_cancel_turn_and_keeps_reply():
    """A mid-turn disconnect must let the turn finish and surface its real reply.

    This is the core regression: previously the disconnect path called
    ``llm_task.cancel()`` + re-raised, so the assistant reply (line ~939 persist)
    never ran. Now the task runs to completion and its result is returned for
    the caller to persist."""

    async def _turn():
        await asyncio.sleep(0.05)  # still "running" when the client drops
        return "最终回复"

    task = asyncio.create_task(_turn())

    async def _recv():
        # Simulate the browser connection dropping mid-turn.
        raise WebSocketDisconnect(code=1006)

    resp, outcome = await _await_turn_with_abort(task, _recv, [])

    assert outcome == "disconnected"
    assert resp == "最终回复", "the reply must NOT be lost on disconnect"
    assert task.done() and not task.cancelled(), "the turn must run to completion"


async def test_abort_cancels_turn_and_returns_partial():
    """An explicit user abort still cancels and returns the partial + marker."""

    async def _turn():
        await asyncio.sleep(5)  # long-running; expected to be cancelled
        return "should not reach"

    task = asyncio.create_task(_turn())

    async def _recv():
        return {"type": "abort"}

    resp, outcome = await _await_turn_with_abort(task, _recv, ["写了一半"])

    assert outcome == "aborted"
    assert "写了一半" in resp and "*[Generation stopped]*" in resp
    assert task.cancelled(), "an explicit abort must cancel the turn"


async def test_abort_with_no_partial_returns_stop_marker_only():
    async def _turn():
        await asyncio.sleep(5)
        return "nope"

    task = asyncio.create_task(_turn())

    async def _recv():
        return {"type": "abort"}

    resp, outcome = await _await_turn_with_abort(task, _recv, [])
    assert outcome == "aborted"
    assert resp == "*[Generation stopped]*"


async def test_normal_completion_returns_reply():
    """When the turn finishes before any abort/disconnect, it returns 'completed'."""

    async def _turn():
        return "正常回复"

    task = asyncio.create_task(_turn())

    async def _recv():
        # The client stays quiet; recv never resolves within the turn.
        await asyncio.sleep(10)
        return {}

    resp, outcome = await _await_turn_with_abort(task, _recv, [])
    assert outcome == "completed"
    assert resp == "正常回复"
