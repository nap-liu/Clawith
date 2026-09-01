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
from app.services.conversation_turn_lifecycle import ConversationTurnSnapshot
from session_introspection_support import (
    _isolate_async_engine_between_tests,
    _seed_agent,
    _seed_session,
    _seed_tenant,
    _seed_user,
)

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
    handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
    handler.pending_initial_assistant = None
    handler.project_session_access = None
    return handler


async def test_project_subagent_message_loop_stops_before_generic_web_llm():
    """A handled durable child input must never reach the direct WS caller."""

    h = _handler()
    h.websocket = _FakeWS([{"content": "continue", "message_id": "client-1"}])
    h.welcome_message = ""
    h.history_messages = []
    h.onboarding_required = False
    h.project_session_access = "edit"
    h.read_only = False
    h.source_channel = "subagent"

    routed: list[dict] = []

    async def _still_writable():
        return True

    async def _enqueue(**kwargs):
        routed.append(kwargs)
        return True

    async def _generic_path_must_not_run(*_args, **_kwargs):
        raise AssertionError("project child input fell through to the generic WS LLM path")

    h._project_session_still_writable = _still_writable
    h._enqueue_project_subagent_message = _enqueue
    h._load_scene_manifest = _generic_path_must_not_run

    with pytest.raises(WebSocketDisconnect):
        await h.message_loop()

    assert routed == [
        {
            "content": "continue",
            "display_content": "",
            "file_name": "",
            "client_message_id": "client-1",
            "attachments": None,
        }
    ]


# ── 1. permission gate ────────────────────────────────────────────────────────


def _agent(creator_id):
    return SimpleNamespace(id=uuid.uuid4(), creator_id=creator_id)


@pytest.mark.parametrize(
    ("role", "access"),
    [("platform_admin", None), ("org_admin", None), ("agent_admin", "manage")],
)
def test_admins_can_view_all_sessions(role, access):
    agent = _agent(creator_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), role=role)
    assert can_view_all_agent_chat_sessions(user, agent, access) is True


def test_agent_admin_requires_manage_access_for_this_agent():
    agent = _agent(creator_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), role="agent_admin")
    assert can_view_all_agent_chat_sessions(user, agent, "use") is False


def test_platform_admin_identity_flag_uses_the_same_governance_path():
    agent = _agent(creator_id=uuid.uuid4())
    user = SimpleNamespace(
        id=uuid.uuid4(),
        role="member",
        identity=SimpleNamespace(is_platform_admin=True),
    )
    assert can_view_all_agent_chat_sessions(user, agent, "manage") is True


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
    other_session = SimpleNamespace(
        id=session_id,
        source_channel="dingtalk",
        user_id=owner_id,
        im_config={},
    )

    h = _handler()
    h.session_id_param = str(session_id)
    h.agent_id = agent.id
    viewer = SimpleNamespace(id=viewer_id, role="org_admin")
    h.read_only = False
    h.websocket = _FakeWS()

    conv = await h._resolve_chat_session(
        _db_returning(other_session),
        viewer_id,
        viewer=viewer,
        agent=agent,
    )

    assert conv == str(session_id), "a privileged viewer is admitted to the session"
    assert h.read_only is True, "and the connection is marked read-only"
    assert h.websocket.closed_code is None, "not closed"


async def test_resolve_rejects_unprivileged_viewer():
    owner_id = uuid.uuid4()
    viewer_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    other_session = SimpleNamespace(
        id=session_id,
        source_channel="dingtalk",
        user_id=owner_id,
        im_config={},
    )

    h = _handler()
    h.session_id_param = str(session_id)
    h.agent_id = agent.id
    viewer = SimpleNamespace(id=viewer_id, role="member")
    h.read_only = False
    h.websocket = _FakeWS()

    conv = await h._resolve_chat_session(
        _db_returning(other_session),
        viewer_id,
        viewer=viewer,
        agent=agent,
    )

    assert conv is None, "an unprivileged viewer is rejected"
    assert h.read_only is False
    assert h.websocket.closed_code == 4003


async def test_resolve_owner_is_writable_not_read_only():
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    own_session = SimpleNamespace(
        id=session_id,
        source_channel="dingtalk",
        user_id=owner_id,
        im_config={},
    )

    h = _handler()
    h.session_id_param = str(session_id)
    h.agent_id = agent.id
    viewer = SimpleNamespace(id=owner_id, role="member")
    h.read_only = False
    h.websocket = _FakeWS()

    conv = await h._resolve_chat_session(
        _db_returning(own_session),
        owner_id,
        viewer=viewer,
        agent=agent,
    )

    assert conv == str(session_id)
    assert h.read_only is False, "the owner keeps full read/write access"


async def test_resolve_honors_session_level_read_only_for_owner():
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=owner_id)
    planning_session = SimpleNamespace(
        id=session_id,
        source_channel="web",
        user_id=owner_id,
        im_config={"read_only": True, "planning_transport": "project_group"},
    )

    h = _handler()
    h.session_id_param = str(session_id)
    h.agent_id = agent.id
    h.read_only = False
    h.websocket = _FakeWS()

    conv = await h._resolve_chat_session(
        _db_returning(planning_session),
        owner_id,
        viewer=SimpleNamespace(id=owner_id, role="member"),
        agent=agent,
    )

    assert conv == str(session_id)
    assert h.read_only is True


async def test_resolve_accepts_a2a_session_from_peer_side():
    """The peer Agent opens the normalized A2A session instead of a fallback web session."""
    from app.database import async_session

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    source = await _seed_agent(owner.id, tenant_id=tenant.id, name="Source")
    peer = await _seed_agent(owner.id, tenant_id=tenant.id, name="Peer")
    session = await _seed_session(
        source.id,
        None,
        channel="agent",
        peer=peer.id,
    )

    h = _handler()
    h.session_id_param = str(session.id)
    h.agent_id = peer.id
    h.source_channel = "web"
    h.read_only = False
    h.websocket = _FakeWS()

    async with async_session() as db:
        conv = await h._resolve_chat_session(
            db,
            owner.id,
            viewer=owner,
            agent=peer,
            agent_access="manage",
        )

    assert conv == str(session.id)
    assert h.source_channel == "agent"
    assert h.websocket.closed_code is None


# ── 3. message_loop blocks sends from a read-only monitor ─────────────────────


async def test_read_only_monitor_send_is_refused():
    h = _handler()
    h.agent_id = uuid.uuid4()
    h.conv_id = str(uuid.uuid4())
    h.current_client_message_id = "readonly-attempt"
    h.read_only = True
    h.welcome_message = ""
    h.history_messages = [object()]  # non-empty → skip welcome push
    h.websocket = _FakeWS(incoming=[{
        "content": "试图以别人身份发送",
        "message_id": "readonly-attempt",
    }])

    async def _current_snapshot(*_args, **_kwargs):
        return ConversationTurnSnapshot(
            anchor_id=uuid.uuid4(),
            generation=7,
            revision=11,
            status="active",
        )

    async def _must_not_broadcast(_payload):
        raise AssertionError("a local rejection must not be broadcast to other viewers")

    h._load_turn_snapshot = _current_snapshot
    h._safe_send = _must_not_broadcast

    with pytest.raises(WebSocketDisconnect):
        await h.message_loop()

    assert any(m.get("type") == "error" and "只读" in (m.get("content") or "") for m in h.websocket.sent), (
        "a read-only monitor that tries to send must get refused, never drive a turn"
    )
    rejection = next(m for m in h.websocket.sent if m.get("type") == "error")
    assert rejection["rejected_message_id"] == "readonly-attempt"
    assert rejection["turn"]["generation"] == 7
    assert rejection["turn"]["revision"] == 11
    assert rejection["turn"]["status"] == "active"
