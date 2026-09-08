"""Behavior checks for bounded period-1/2 tool-loop detection."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.llm import caller
from app.services.llm.caller import (
    _repeating_tool_period,
    _tool_call_signature,
    _tool_round_fingerprint,
    _tool_round_observation,
)
from app.services.llm.client import LLMResponse
from app.services.llm.failure_outcome import LLMFailure
from tests.test_llm_throttle_retry import (
    _FakeModel,
    _patch_call_llm_collaborators,
    _ThrottleScriptClient,
)
from tests.test_turn_recovery import _make_agent_with_model


def _tc(name: str, arguments):
    return {"id": "x", "function": {"name": name, "arguments": arguments}}


def _observation(tool_calls, results):
    return _tool_round_observation(tool_calls, results)


def test_signature_normalizes_json_but_preserves_meaning():
    first = _tool_call_signature(_tc("q", '{"a": 1, "b": 2}'))
    reordered = _tool_call_signature(_tc("q", '{ "b": 2, "a": 1 }'))
    changed = _tool_call_signature(_tc("q", '{"a": 2, "b": 2}'))

    assert first == reordered
    assert first != changed


def test_round_fingerprint_is_order_independent_for_parallel_calls():
    first = [_tc("a", '{"v": 1}'), _tc("b", '{"v": 2}')]
    reversed_round = list(reversed(first))

    assert _tool_round_fingerprint(first) == _tool_round_fingerprint(reversed_round)


def test_period_one_stops_only_after_calls_and_results_repeat():
    same = [_tc("status", '{"job": 1}')]
    history = [
        _observation(same, ["pending"]),
        _observation(same, ["pending"]),
    ]

    assert _repeating_tool_period(history) == 1
    assert _repeating_tool_period(history, same) == 1


def test_period_two_detects_alternating_calls_with_unchanged_results():
    first = [_tc("lookup", '{"id": 1}')]
    second = [_tc("refresh", '{"id": 1}')]
    history = [
        _observation(first, ["missing"]),
        _observation(second, ["unchanged"]),
        _observation(first, ["missing"]),
        _observation(second, ["unchanged"]),
    ]

    assert _repeating_tool_period(history) == 2
    assert _repeating_tool_period(history, first) == 2
    assert _repeating_tool_period(history, second) is None


def test_same_polling_call_with_changing_result_is_progress():
    same = [_tc("status", '{"job": 1}')]
    history = [
        _observation(same, ["queued"]),
        _observation(same, ["running"]),
        _observation(same, ["75%"]),
    ]

    assert _repeating_tool_period(history) is None
    assert _repeating_tool_period(history, same) is None


def test_changing_multimodal_tool_content_is_progress():
    same = [_tc("screenshot", "{}")]
    history = [
        _observation(same, [[{"type": "image_url", "image_url": {"url": "frame-1"}}]]),
        _observation(same, [[{"type": "image_url", "image_url": {"url": "frame-2"}}]]),
    ]

    assert _repeating_tool_period(history) is None


def test_non_periodic_work_is_not_stopped():
    history = [
        _observation([_tc("q", '{"page": 1}')], ["one"]),
        _observation([_tc("q", '{"page": 2}')], ["two"]),
        _observation([_tc("q", '{"page": 3}')], ["three"]),
        _observation([_tc("q", '{"page": 4}')], ["four"]),
    ]

    assert _repeating_tool_period(history) is None


def test_missing_result_does_not_create_a_loop_observation():
    calls = [_tc("a", "{}"), _tc("b", "{}")]

    assert _tool_round_observation(calls, ["only one result"]) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["streaming", "background"])
@pytest.mark.parametrize("period", [1, 2, None], ids=["same", "alternating", "progress"])
async def test_real_loops_stop_repeated_side_effects_but_allow_progress(
    monkeypatch, entrypoint, period,
):
    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="web")
        db.add(session)
        await db.commit()
        session_id = str(session.id)

    script = [
        LLMResponse(
            content="",
            tool_calls=[{
                "id": f"call-{index}",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": str(index % (period or 1))}),
                },
            }],
            finish_reason="tool_calls",
        )
        for index in range(5)
    ]
    script.append(LLMResponse(content="finished", finish_reason="stop"))
    client = _ThrottleScriptClient(script)
    client.complete = client.stream
    _patch_call_llm_collaborators(monkeypatch, client)
    monkeypatch.setattr(caller, "get_agent_tools_for_llm", AsyncMock(return_value=[{
        "type": "function", "function": {"name": "read_file", "parameters": {}},
    }]))
    results = ["unchanged"] * 5 if period else [f"progress-{i}" for i in range(5)]
    execute = AsyncMock(side_effect=results)
    monkeypatch.setattr(caller, "execute_tool", execute)

    try:
        if entrypoint == "streaming":
            reply = await caller.call_llm(
                model=_FakeModel(), messages=[{"role": "user", "content": "read"}],
                agent_name="Reader", role_description="", agent_id=agent_id,
                user_id=user_id, session_id=session_id,
            )
        else:
            async with async_session() as db:
                reply = await caller.call_agent_llm_with_tools(
                    db, agent_id, "system", "read", max_rounds=6, session_id=session_id,
                )

        completed = 2 * period if period else 5
        assert execute.await_count == completed
        assert len(client.stream_calls) == completed + 1
        assert client.closed is True
        replay_results = [
            message.content for message in client.stream_calls[-1]["messages"]
            if message.role == "tool"
        ]
        assert replay_results == results[:completed]
        if period:
            assert isinstance(reply, LLMFailure)
            assert reply.code == f"tool_loop_period_{period}"
            assert reply.details == {"period": period, "round": completed + 1}
        else:
            assert reply == "finished"
        if entrypoint == "streaming":
            async with async_session() as db:
                rows = (await db.execute(select(ChatMessage).where(
                    ChatMessage.conversation_id == session_id,
                    ChatMessage.role == "tool_call",
                ))).scalars().all()
            events = [json.loads(row.content) for row in rows]
            assert len(events) == 2 * completed
            assert {(event["call_id"], event["status"]) for event in events} == {
                (f"call-{index}", status)
                for index in range(completed) for status in ("running", "done")
            }
            assert {
                event["call_id"]: event["result"] for event in events
                if event["status"] == "done"
            } == {
                f"call-{index}": result for index, result in enumerate(results[:completed])
            }
    finally:
        await engine.dispose()
