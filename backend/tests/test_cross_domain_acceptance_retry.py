"""Retry behavior tests for the cross-domain acceptance runner."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.test_cross_domain_acceptance_runner import (
    AcceptanceRunner,
    RUNNER_MODULE,
    Scenario,
    _run,
    _runner_for_project,
)


def test_unrecovered_failed_runs_accepts_later_success_for_same_a2a_session() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        run_input={"session_id": "session-1"},
    )
    recovered = _run(
        "recovered",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        run_input={"session_id": "session-1"},
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, recovered]) == []


def test_unrecovered_failed_runs_rejects_unrelated_later_success() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        run_input={"session_id": "session-1"},
    )
    unrelated = _run(
        "unrelated",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        run_input={"session_id": "session-2"},
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, unrelated]) == ["failed"]


def test_unrecovered_failed_runs_accepts_explicit_retry() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        trigger_type="leader_reply_batch",
        run_input={"group_session_id": "group-1"},
    )
    retry = _run(
        "retry",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        trigger_type="retry",
        run_input={"retry_of_run_id": "failed"},
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, retry]) == []


def test_unrecovered_failed_runs_matches_work_item_before_trigger_type() -> None:
    failed = _run(
        "failed",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        trigger_type="a2a",
        work_item_id="work-1",
    )
    recovered = _run(
        "recovered",
        status="succeeded",
        created_at="2026-08-21T10:01:00Z",
        trigger_type="retry",
        agent_id="agent-2",
        work_item_id="work-1",
    )

    assert AcceptanceRunner._unrecovered_failed_runs([failed, recovered]) == []


def test_retry_failed_run_uses_public_api_and_does_not_invent_work_item() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run(
        "failed-1",
        status="failed",
        created_at="2026-08-21T10:00:00Z",
        trigger_type="a2a",
        agent_id="agent-9",
        run_input={"dispatch": {"task": "Review the exact campaign evidence"}},
    )
    failed["title"] = "Campaign evidence review"
    requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        requests.append((method, path, kwargs))
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        if method == "GET" and path == "/projects/project-1":
            return {"id": "project-1", "status": "running"}
        if method == "POST" and path.endswith("/runs"):
            return {"id": "retry-1", **kwargs["json"], "status": "queued"}
        raise AssertionError((method, path, kwargs))

    runner.request = request
    result = asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))

    assert result["id"] == "retry-1"
    post_payload = next(kwargs["json"] for method, _, kwargs in requests if method == "POST")
    assert post_payload == {
        "work_item_id": None,
        "agent_id": "agent-9",
        "trigger_type": "retry",
        "input": {
            "retry_of_run_id": "failed-1",
            "title": "Campaign evidence review",
            "objective": "Review the exact campaign evidence",
        },
    }
    assert runner.state["projects"][scenario.key]["retries"] == {"failed-1": "retry-1"}


def test_retry_failed_run_is_idempotent_when_public_api_already_has_retry() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run("failed-1", status="failed", created_at="2026-08-21T10:00:00Z")
    existing = _run(
        "retry-1",
        status="running",
        created_at="2026-08-21T10:01:00Z",
        trigger_type="retry",
        run_input={"retry_of_run_id": "failed-1"},
    )
    methods: list[str] = []

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del path, kwargs
        methods.append(method)
        return [failed, existing]

    runner.request = request
    result = asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))

    assert result == existing
    assert methods == ["GET"]


def test_retry_failed_run_preserves_exact_work_item_and_agent() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run(
        "failed-1",
        status="cancelled",
        created_at="2026-08-21T10:00:00Z",
        agent_id="agent-2",
        work_item_id="work-7",
        run_input={"objective": "Complete the assigned deliverable"},
    )
    posted: dict[str, Any] = {}

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        if method == "GET":
            return {"status": "running"}
        posted.update(kwargs["json"])
        return {"id": "retry-2", "status": "queued"}

    runner.request = request
    asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))

    assert posted["work_item_id"] == "work-7"
    assert posted["agent_id"] == "agent-2"
    assert posted["input"]["retry_of_run_id"] == "failed-1"


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "succeeded"])
def test_retry_failed_run_rejects_non_terminal_failure_states(status: str) -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    source = _run("run-1", status=status, created_at="2026-08-21T10:00:00Z")

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del method, path, kwargs
        return [source]

    runner.request = request
    with pytest.raises(RuntimeError, match="only failed or cancelled"):
        asyncio.run(runner.retry_failed_run((scenario,), "run-1"))


def test_retry_failed_run_rejects_completed_project() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run("failed-1", status="failed", created_at="2026-08-21T10:00:00Z")

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del kwargs
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        return {"status": "completed"}

    runner.request = request
    with pytest.raises(RuntimeError, match="public execution retries require a running project"):
        asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))


def test_retry_failed_run_rejects_run_without_responsible_agent() -> None:
    scenario = RUNNER_MODULE.SCENARIOS[0]
    runner = _runner_for_project(scenario)
    failed = _run("failed-1", status="failed", created_at="2026-08-21T10:00:00Z", agent_id="")

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        del kwargs
        if method == "GET" and path.endswith("/runs"):
            return [failed]
        return {"status": "running"}

    runner.request = request
    with pytest.raises(RuntimeError, match="no responsible Agent"):
        asyncio.run(runner.retry_failed_run((scenario,), "failed-1"))
