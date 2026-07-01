"""Read-only WebSocket monitor: any session a user may SEE should also update
live, but a viewer who does not own the session must never be able to send.

Pins three behaviors of the read-only monitor path added so that monitored
channel / other-user conversations stream into the web UI live (not only on
reload):

1. ``can_view_all_agent_chat_sessions`` — the single permission gate shared by
   the REST session APIs and this WS path (admins + the agent creator).
2. ``_resolve_chat_session`` — a viewer with that permission opening someone
   else's session is admitted as ``read_only``; anyone else is still rejected.
3. ``message_loop`` — a ``read_only`` connection that tries to send is refused
   server-side (the UI also disables the composer, but THIS is authoritative).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.websocket import WebSocketChatHandler
from app.core.permissions import can_view_all_agent_chat_sessions

# asyncio_mode = "auto" (pyproject) runs the async tests without an explicit
# marker; the pure permission tests below stay synchronous.


class _FakeWS:
    def __init__(self, incoming: list | None = None):
        self.sent: list[dict] = []
        self.closed_code: int | None = None
        self._incoming = list(incoming or [])

    async def send_json(self, payload: dict):
        self.sent.append(payload)

    async def close(self, code: int = 1000):
        self.closed_code = code

    async def receive_json(self):
        if self._incoming:
            return self._incoming.pop(0)
        raise WebSocketDisconnect(code=1000)


def _result(obj):
    return SimpleNamespace(scalar_one_or_none=lambda: obj)


def _db_returning(session_obj):
    async def _execute(*_a, **_k):
        return _result(session_obj)

    return SimpleNamespace(execute=_execute)


def _handler() -> WebSocketChatHandler:
    # Bypass __init__ — we set only the fields the code under test touches.
    return WebSocketChatHandler.__new__(WebSocketChatHandler)


# ── 1. permission gate ────────────────────────────────────────────────────────


def _agent(creator_id):
    return SimpleNamespace(id=uuid.uuid4(), creator_id=creator_id)


@pytest.mark.parametrize("role", ["platform_admin", "org_admin", "agent_admin"])
def test_admins_can_view_all_sessions(role):
    agent = _agent(creator_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), role=role)
    assert can_view_all_agent_chat_sessions(user, agent) is True


def test_agent_creator_can_view_all_sessions():
    creator = uuid.uuid4()
    agent = _agent(creator_id=creator)
    user = SimpleNamespace(id=creator, role="member")
    assert can_view_all_agent_chat_sessions(user, agent) is True


def test_regular_member_cannot_view_others_sessions():
    agent = _agent(creator_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), role="member")
    assert can_view_all_agent_chat_sessions(user, agent) is False


# ── 2. _resolve_chat_session read-only admission ──────────────────────────────


async def test_resolve_admits_privileged_viewer_as_read_only():
    owner_id = uuid.uuid4()
    viewer_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    other_session = SimpleNamespace(id=session_id, source_channel="dingtalk", user_id=owner_id)

    h = _handler()
    h.session_id_param = str(session_id)
    h.agent_id = agent.id
    h.agent = agent
    h.user = SimpleNamespace(id=viewer_id, role="org_admin")
    h.read_only = False
    h.websocket = _FakeWS()

    conv = await h._resolve_chat_session(_db_returning(other_session), viewer_id)

    assert conv == str(session_id), "a privileged viewer is admitted to the session"
    assert h.read_only is True, "and the connection is marked read-only"
    assert h.websocket.closed_code is None, "not closed"


async def test_resolve_rejects_unprivileged_viewer():
    owner_id = uuid.uuid4()
    viewer_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    other_session = SimpleNamespace(id=session_id, source_channel="dingtalk", user_id=owner_id)

    h = _handler()
    h.session_id_param = str(session_id)
    h.agent_id = agent.id
    h.agent = agent
    h.user = SimpleNamespace(id=viewer_id, role="member")
    h.read_only = False
    h.websocket = _FakeWS()

    conv = await h._resolve_chat_session(_db_returning(other_session), viewer_id)

    assert conv is None, "an unprivileged viewer is rejected"
    assert h.read_only is False
    assert h.websocket.closed_code == 4003


async def test_resolve_owner_is_writable_not_read_only():
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    own_session = SimpleNamespace(id=session_id, source_channel="dingtalk", user_id=owner_id)

    h = _handler()
    h.session_id_param = str(session_id)
    h.agent_id = agent.id
    h.agent = agent
    h.user = SimpleNamespace(id=owner_id, role="member")
    h.read_only = False
    h.websocket = _FakeWS()

    conv = await h._resolve_chat_session(_db_returning(own_session), owner_id)

    assert conv == str(session_id)
    assert h.read_only is False, "the owner keeps full read/write access"


# ── 3. message_loop blocks sends from a read-only monitor ─────────────────────


async def test_read_only_monitor_send_is_refused():
    h = _handler()
    h.read_only = True
    h.welcome_message = ""
    h.history_messages = [object()]  # non-empty → skip welcome push
    h.websocket = _FakeWS(incoming=[{"content": "试图以别人身份发送"}])

    with pytest.raises(WebSocketDisconnect):
        await h.message_loop()

    assert any(
        m.get("type") == "error" and "只读" in (m.get("content") or "") for m in h.websocket.sent
    ), "a read-only monitor that tries to send must get refused, never drive a turn"
