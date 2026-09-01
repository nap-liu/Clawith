from __future__ import annotations

import uuid

import pytest

from app.api import gateway as gateway_api
from app.database import async_session, engine
from app.models.agent import Agent
from gateway_workload_capacity_support import _native_background_args, _seed_native_pair


@pytest.fixture(autouse=True)
async def _isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_gateway_native_turn_uses_agent_imagination(monkeypatch):
    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair()
    async with async_session() as db:
        target = await db.get(Agent, target_id)
        assert target is not None
        target.temperature = 0
        await db.commit()

    observed: dict[str, object] = {}

    async def fake_failover(**kwargs):
        observed.update(kwargs)
        return "gateway imagination reply"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_failover)
    args = await _native_background_args(
        source_id,
        target_id,
        content="use the target runtime model",
        source_event_id=f"gateway-imagination-{uuid.uuid4()}",
    )

    assert await gateway_api._send_to_agent_background(*args) is None
    assert observed["primary_model"].temperature == 0
