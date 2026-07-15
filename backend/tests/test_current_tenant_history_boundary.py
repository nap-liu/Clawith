"""Current-login tenant is authoritative for relationship and history APIs."""

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.api import websocket as websocket_api
from app.api.websocket import WebSocketChatHandler
from app.core.security import create_access_token
from app.database import async_session, engine
from app.main import app
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import Identity, User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_boundary_case():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        login_tenant = Tenant(name=f"Login {suffix}", slug=f"login-{suffix}")
        target_tenant = Tenant(name=f"Target {suffix}", slug=f"target-{suffix}")
        db.add_all([login_tenant, target_tenant])
        await db.flush()

        identity = Identity(
            username=f"boundary_{suffix}",
            email=f"boundary_{suffix}@test.local",
            password_hash="test",
        )
        db.add(identity)
        await db.flush()
        cross_admin = User(
            identity_id=identity.id,
            tenant_id=login_tenant.id,
            display_name="Cross admin",
            role="platform_admin",
            is_active=True,
        )
        switched_admin = User(
            identity_id=identity.id,
            tenant_id=target_tenant.id,
            display_name="Switched admin",
            role="platform_admin",
            is_active=True,
        )
        owner_identity = Identity(
            username=f"owner_{suffix}",
            email=f"owner_{suffix}@test.local",
            password_hash="test",
        )
        db.add(owner_identity)
        await db.flush()
        owner = User(
            identity_id=owner_identity.id,
            tenant_id=target_tenant.id,
            display_name="Owner",
            role="member",
            is_active=True,
        )
        ordinary_identity = Identity(
            username=f"ordinary_{suffix}",
            email=f"ordinary_{suffix}@test.local",
            password_hash="test",
        )
        db.add(ordinary_identity)
        await db.flush()
        ordinary = User(
            identity_id=ordinary_identity.id,
            tenant_id=target_tenant.id,
            display_name="Ordinary viewer",
            role="member",
            is_active=True,
        )
        db.add_all([cross_admin, switched_admin, owner, ordinary])
        await db.flush()

        agent = Agent(
            tenant_id=target_tenant.id,
            creator_id=owner.id,
            name=f"Boundary agent {suffix}",
            access_mode="company",
            status="idle",
        )
        other_agent = Agent(
            tenant_id=target_tenant.id,
            creator_id=owner.id,
            name=f"Other agent {suffix}",
            access_mode="company",
            status="idle",
        )
        peer_agent = Agent(
            tenant_id=target_tenant.id,
            creator_id=owner.id,
            name=f"Peer agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add_all([agent, other_agent, peer_agent])
        await db.flush()

        session = ChatSession(
            agent_id=agent.id,
            user_id=owner.id,
            title="Private history",
            source_channel="web",
        )
        unrelated_a2a = ChatSession(
            agent_id=other_agent.id,
            peer_agent_id=peer_agent.id,
            user_id=owner.id,
            title="Unrelated A2A",
            source_channel="agent",
        )
        db.add_all([session, unrelated_a2a])
        await db.flush()
        db.add_all(
            [
                ChatMessage(
                    agent_id=agent.id,
                    conversation_id=str(session.id),
                    user_id=owner.id,
                    sender_user_id=owner.id,
                    role="user",
                    content="tenant-private-message",
                ),
                ChatMessage(
                    agent_id=other_agent.id,
                    conversation_id=str(unrelated_a2a.id),
                    user_id=owner.id,
                    sender_agent_id=other_agent.id,
                    role="assistant",
                    content="wrong-agent-secret",
                ),
            ]
        )
        await db.commit()
        return SimpleNamespace(
            cross_admin=cross_admin,
            switched_admin=switched_admin,
            ordinary=ordinary,
            agent=agent,
            session=session,
            unrelated_a2a=unrelated_a2a,
        )


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("GET", "/sessions?scope=all", None),
        ("GET", "/sessions/{session_id}", None),
        ("GET", "/sessions/{session_id}/messages", None),
        ("POST", "/sessions", {"title": "forbidden"}),
        ("PATCH", "/sessions/{session_id}", {"title": "forbidden"}),
        ("DELETE", "/sessions/{session_id}", None),
        ("GET", "/chat-history/conversations", None),
        ("GET", "/chat-history/{session_id}", None),
    ],
)
async def test_unswitched_platform_admin_cannot_access_history(method, path_suffix, body):
    seeded = await _seed_boundary_case()
    token = create_access_token(str(seeded.cross_admin.id), "platform_admin")
    path = path_suffix.format(session_id=seeded.session.id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(
            method,
            f"/api/agents/{seeded.agent.id}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("GET", "/relationships/", None),
        ("GET", "/relationships/member-candidates", None),
        ("PUT", "/relationships/", {"relationships": []}),
        ("DELETE", "/relationships/{random_id}", None),
        ("GET", "/relationships/agent-candidates", None),
        ("GET", "/relationships/agents", None),
        ("GET", "/relationships/agents/candidates", None),
        ("PUT", "/relationships/agents", {"relationships": []}),
        ("DELETE", "/relationships/agents/{random_id}", None),
    ],
)
async def test_unswitched_platform_admin_cannot_access_any_relationship_route(method, path_suffix, body):
    seeded = await _seed_boundary_case()
    token = create_access_token(str(seeded.cross_admin.id), "platform_admin")
    path = path_suffix.format(random_id=uuid.uuid4())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(
            method,
            f"/api/agents/{seeded.agent.id}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403, response.text


async def test_switched_platform_admin_can_read_target_tenant_history():
    seeded = await _seed_boundary_case()
    token = create_access_token(str(seeded.switched_admin.id), "platform_admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/agents/{seeded.agent.id}/sessions/{seeded.session.id}/messages",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    assert any(item["content"] == "tenant-private-message" for item in response.json())


@pytest.mark.parametrize(
    "path_suffix",
    ["/chat-history/conversations", "/chat-history/{session_id}"],
)
async def test_regular_same_tenant_user_cannot_read_another_users_activity_history(
    path_suffix,
):
    seeded = await _seed_boundary_case()
    token = create_access_token(str(seeded.ordinary.id), "member")
    path = path_suffix.format(session_id=seeded.session.id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/agents/{seeded.agent.id}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403, response.text
    assert "tenant-private-message" not in response.text


async def test_chat_history_uuid_must_belong_to_path_agent():
    seeded = await _seed_boundary_case()
    token = create_access_token(str(seeded.switched_admin.id), "platform_admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/agents/{seeded.agent.id}/chat-history/{seeded.unrelated_a2a.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404, response.text
    assert "wrong-agent-secret" not in response.text


async def test_malformed_cross_tenant_human_session_is_hidden_and_unreadable():
    seeded = await _seed_boundary_case()
    malformed_id = uuid.uuid4()
    async with async_session() as db:
        await db.execute(
            text(
                "ALTER TABLE chat_sessions DISABLE TRIGGER "
                "trg_chat_session_agent_tenant"
            )
        )
        try:
            await db.execute(
                text(
                    """
                    INSERT INTO chat_sessions
                        (id, agent_id, user_id, title, source_channel,
                         is_group, is_primary, im_config)
                    VALUES
                        (:id, :agent_id, :user_id, 'malformed', 'web',
                         false, false, '{}'::jsonb)
                    """
                ),
                {
                    "id": malformed_id,
                    "agent_id": seeded.agent.id,
                    "user_id": seeded.cross_admin.id,
                },
            )
        finally:
            await db.execute(
                text(
                    "ALTER TABLE chat_sessions ENABLE TRIGGER "
                    "trg_chat_session_agent_tenant"
                )
            )
        await db.commit()

    token = create_access_token(str(seeded.switched_admin.id), "platform_admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            f"/api/agents/{seeded.agent.id}/sessions?scope=all",
            headers={"Authorization": f"Bearer {token}"},
        )
        detail = await client.get(
            f"/api/agents/{seeded.agent.id}/sessions/{malformed_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert listed.status_code == 200, listed.text
    assert str(malformed_id) not in listed.text
    assert detail.status_code == 404, detail.text


class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed_code = None

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.closed_code = code


async def test_websocket_setup_rejects_unswitched_platform_admin(monkeypatch):
    seeded = await _seed_boundary_case()
    token = create_access_token(str(seeded.cross_admin.id), "platform_admin")
    ws = _FakeWebSocket()
    handler = WebSocketChatHandler(
        websocket=ws,
        agent_id=seeded.agent.id,
        token=token,
        session_id=str(seeded.session.id),
        lang="zh",
        channel="web",
    )

    class _Result:
        def scalar_one_or_none(self):
            return seeded.cross_admin

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    class _SessionContext:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *_args):
            return False

    async def _cross_access(_db, _user, _agent_id):
        return seeded.agent, "manage"

    monkeypatch.setattr(websocket_api, "async_session", lambda: _SessionContext())
    monkeypatch.setattr(websocket_api, "check_agent_access", _cross_access)

    assert await handler.setup() is False
    assert ws.closed_code == 4002
    assert all(item.get("type") != "connected" for item in ws.sent)
