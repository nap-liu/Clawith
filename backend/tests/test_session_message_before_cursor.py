"""Observable pagination behavior for ``read_session_messages.before``."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from session_introspection_support import (  # noqa: F401
    _isolate_async_engine_between_tests,
    _seed_agent,
    _seed_message,
    _seed_session,
    _seed_tenant,
    _seed_user,
)

from app.services.tools.session_introspection.handlers import handle_read_session_messages


async def test_before_cursor_returns_strictly_older_page():
    tenant = await _seed_tenant()
    user = await _seed_user(role="member", tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id, access_mode="company")
    session = await _seed_session(agent.id, user.id, channel="web")
    base = datetime.now(UTC)
    for offset, content in enumerate(("oldest", "middle", "newest")):
        await _seed_message(
            agent.id,
            user.id,
            session.id,
            "user",
            content,
            created_at=base + timedelta(seconds=offset),
        )

    latest = await handle_read_session_messages(
        agent.id,
        user.id,
        str(session.id),
        {"session_id": str(session.id), "limit": 2},
    )
    assert "middle" in latest and "newest" in latest
    assert "oldest" not in latest
    cursor = re.search(r"before=([^\s]+)", latest).group(1).rstrip("。")

    older = await handle_read_session_messages(
        agent.id,
        user.id,
        str(session.id),
        {"session_id": str(session.id), "limit": 2, "before": cursor},
    )
    assert "oldest" in older
    assert "middle" not in older and "newest" not in older


async def test_invalid_before_cursor_is_not_silently_ignored():
    tenant = await _seed_tenant()
    user = await _seed_user(role="member", tenant_id=tenant.id)
    agent = await _seed_agent(user.id, tenant_id=tenant.id, access_mode="company")
    session = await _seed_session(agent.id, user.id, channel="web")

    output = await handle_read_session_messages(
        agent.id,
        user.id,
        str(session.id),
        {"session_id": str(session.id), "before": "not-a-cursor"},
    )
    assert output.startswith("❌ before 游标无效")
