import uuid
import pytest
from types import SimpleNamespace
import httpx

from app.api import webhooks as webhooks_api
from app.main import app


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if len(self._value) == 1 else None
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


class FakeSession:
    def __init__(self, triggers=None, agent=None):
        self.triggers = triggers or []
        self.agent = agent
        self.added = []
        self.committed = False
        self.expunged = []

    async def execute(self, statement):
        stmt_str = str(statement)
        if "agent_triggers" in stmt_str:
            return FakeScalarResult(self.triggers)
        elif "agents" in stmt_str:
            return FakeScalarResult(self.agent)
        return FakeScalarResult(None)

    def add(self, value):
        self.added.append(value)

    def expunge(self, value):
        self.expunged.append(value)

    async def commit(self):
        self.committed = True


class FakeAsyncSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)

    async def _build():
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


@pytest.mark.asyncio
async def test_receive_webhook_success(monkeypatch, client):
    # Setup test trigger and agent
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test-trigger",
        type="webhook",
        config={"token": "valid_token"},
        is_enabled=True,
    )
    agent = SimpleNamespace(id=agent_id, webhook_rate_limit=5, webhook_queue_max=1000)

    session = FakeSession(triggers=[trigger], agent=agent)

    # Mock dependencies and DB session
    monkeypatch.setattr(webhooks_api, "async_session", FakeAsyncSessionFactory(session))

    # Mock redis rate limiting
    async def fake_record_and_count_hits(token):
        return 1

    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", fake_record_and_count_hits)

    async def fake_allocate_event_id(_db):
        return 41

    async def fake_persist_payload(staged, **_kwargs):
        return {
            "kind": "webhook_inbox_event_v1",
            "event_id": 41,
            "received_at_ms": staged.received_at_ms,
            "event_key": f"{staged.received_at_ms}_00000000000000000041",
            "path": "webhook/test/20260825/41/payload.json",
            "size": staged.size,
            "sha256": staged.sha256,
            "content_type": "application/json",
        }

    monkeypatch.setattr(webhooks_api, "allocate_webhook_event_id", fake_allocate_event_id)
    monkeypatch.setattr(webhooks_api, "persist_webhook_payload", fake_persist_payload)

    async with await client() as ac:
        response = await ac.post("/api/webhooks/t/valid_token", json={"event": "test"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert session.committed is True
    assert trigger.config["_webhook_event"]["event_id"] == 41
    assert trigger.config["_webhook_payload"] is None
