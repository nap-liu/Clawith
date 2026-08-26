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


async def _make_page(html: str | bytes, short_id: str):
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
    content = html.encode("utf-8") if isinstance(html, str) else html
    (base / "r.html").write_bytes(content)


async def test_viewer_shell_and_embedded_report_keep_platform_boundary_without_runtime_restrictions():
    sid = f"x{uuid.uuid4().hex[:6]}"
    html = b'<meta charset="windows-1252"><h1>raw \x80 report</h1>'
    await _make_page(html, sid)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        viewer = await client.get(f"/p/{sid}")
        report = await client.get(f"/p/{sid}?__report_embed=1")

    assert viewer.status_code == 200
    assert viewer.headers["x-accel-redirect"] == "/__published_page_viewer"
    assert viewer.headers["cache-control"] == "no-store"

    assert report.status_code == 200
    assert report.content == html
    assert report.headers["content-type"] == "text/html"
    assert report.headers["cache-control"] == "no-store"
    for header in (
        "content-security-policy",
        "x-frame-options",
        "cross-origin-opener-policy",
        "cross-origin-embedder-policy",
        "cross-origin-resource-policy",
        "permissions-policy",
        "x-content-type-options",
    ):
        assert header not in report.headers


@pytest.mark.parametrize(
    "html",
    [
        "<h1>no sdk</h1>",
        '<script src="/sdk/clawith.js"></script>',
    ],
)
async def test_legacy_embed_query_does_not_change_direct_report_response(html: str):
    sid = f"y{uuid.uuid4().hex[:6]}"
    await _make_page(html, sid)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/p/{sid}?__report_embed=1",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_1_1 like Mac OS X) "
                    "AppleWebKit/605.1.15 Version/15.1 Mobile/15E148 Safari/604.1"
                ),
            },
        )
    assert resp.status_code == 200
    assert resp.text == html
    assert resp.headers["content-type"] == "text/html"
    assert "Content-Security-Policy" not in resp.headers
    assert "X-Frame-Options" not in resp.headers
    assert "X-Content-Type-Options" not in resp.headers
    assert resp.headers["Cache-Control"] == "no-store"
