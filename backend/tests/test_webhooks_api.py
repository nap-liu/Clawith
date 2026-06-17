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
    agent = SimpleNamespace(id=agent_id, webhook_rate_limit=5)

    session = FakeSession(triggers=[trigger], agent=agent)

    # Mock dependencies and DB session
    monkeypatch.setattr(webhooks_api, "async_session", FakeAsyncSessionFactory(session))

    # Mock redis rate limiting
    async def fake_record_and_count_hits(token):
        return 1

    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", fake_record_and_count_hits)

    # receive_webhook no longer calls enqueue_webhook_execution. The merged flow
    # stores the payload directly into the trigger config (legacy mode here writes
    # _webhook_pending/_webhook_payload via an UPDATE), and the trigger_daemon
    # later polls those flags. A successful receive just persists (commit) and
    # returns {"ok": True}.
    async with await client() as ac:
        response = await ac.post("/api/webhooks/t/valid_token", json={"event": "test"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert session.committed is True
