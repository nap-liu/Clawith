"""Tests for startup turn recovery scheduling."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.asyncio


async def _noop_async(*_args, **_kwargs):
    return None


@asynccontextmanager
async def _noop_async_context():
    yield


def _patch_lifespan_side_effects(monkeypatch):
    """Keep these tests focused on recovery scheduling, not full app bootstrap."""
    import app.main as main
    import app.services.audit_logger as audit_logger

    fake_mcp = SimpleNamespace(
        session_manager=SimpleNamespace(run=lambda: _noop_async_context())
    )

    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(main, "_log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(main, "_role_enabled", lambda *_roles: False)
    monkeypatch.setattr(main, "_start_ss_local", _noop_async)
    monkeypatch.setattr(main.realtime_router, "stop", _noop_async)
    monkeypatch.setattr(main, "close_redis", _noop_async)
    monkeypatch.setattr(audit_logger, "write_audit_log", _noop_async)
    monkeypatch.setitem(sys.modules, "app.mcp_server", SimpleNamespace(mcp=fake_mcp))
    return main


async def test_enabled_startup_turn_recovery_does_not_block_lifespan(monkeypatch):
    main = _patch_lifespan_side_effects(monkeypatch)
    monkeypatch.setenv("TURN_RECOVERY_ENABLED", "true")

    import app.services.turn_recovery as turn_recovery

    started = asyncio.Event()
    never_finish = asyncio.Event()

    async def fake_startup_turn_resume_once():
        started.set()
        await never_finish.wait()

    monkeypatch.setattr(turn_recovery, "startup_turn_resume_once", fake_startup_turn_resume_once)

    app = SimpleNamespace(state=SimpleNamespace())
    manager = main.lifespan(app)

    try:
        await asyncio.wait_for(manager.__aenter__(), timeout=0.2)
    except asyncio.TimeoutError:
        pytest.fail("startup turn recovery must not block FastAPI lifespan startup")

    try:
        await asyncio.wait_for(started.wait(), timeout=0.2)
        task = getattr(app.state, "turn_recovery_task", None)
        assert task is not None
        assert task.get_name() == "turn_recovery"
        assert not task.done()
    finally:
        await manager.__aexit__(None, None, None)

    assert app.state.turn_recovery_task.done()
    assert app.state.turn_recovery_task.cancelled()


async def test_disabled_startup_turn_recovery_creates_no_background_task(monkeypatch):
    main = _patch_lifespan_side_effects(monkeypatch)
    monkeypatch.setenv("TURN_RECOVERY_ENABLED", "false")

    import app.services.turn_recovery as turn_recovery

    async def fake_startup_turn_resume_once():
        raise AssertionError("disabled turn recovery should not be started")

    monkeypatch.setattr(turn_recovery, "startup_turn_resume_once", fake_startup_turn_resume_once)

    app = SimpleNamespace(state=SimpleNamespace())
    async with main.lifespan(app):
        assert getattr(app.state, "turn_recovery_task", None) is None
