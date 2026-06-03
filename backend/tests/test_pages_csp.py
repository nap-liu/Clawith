import uuid
import pathlib
import pytest
import httpx

from app.main import app
from app.config import get_settings
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.agent import Agent
from app.models.published_page import PublishedPage

pytestmark = pytest.mark.asyncio
settings = get_settings()


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_page(html: str, short_id: str):
    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.flush()
        page = PublishedPage(short_id=short_id, agent_id=agent.id, user_id=user.id,
                             source_path="out/r.html", title="T")
        db.add(page)
        await db.commit()
        agent_id = agent.id
    base = pathlib.Path(settings.AGENT_DATA_DIR) / str(agent_id) / "out"
    base.mkdir(parents=True, exist_ok=True)
    (base / "r.html").write_text(html, encoding="utf-8")


async def test_csp_unchanged_without_sdk():
    sid = f"x{uuid.uuid4().hex[:6]}"
    await _make_page("<h1>no sdk</h1>", sid)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/p/{sid}")
    assert resp.status_code == 200
    csp = resp.headers["Content-Security-Policy"]
    assert "allow-top-navigation" not in csp


async def test_csp_relaxed_with_sdk():
    sid = f"y{uuid.uuid4().hex[:6]}"
    await _make_page('<script src="/sdk/clawith.js"></script>', sid)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/p/{sid}")
    assert resp.status_code == 200
    assert "allow-top-navigation" in resp.headers["Content-Security-Policy"]
