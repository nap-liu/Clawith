"""Seed one public report for Docker-only real-browser acceptance.

This helper is intentionally not collected by pytest. Run it only against an
isolated test database whose name satisfies the repository test DB safety gate.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, text

from app.database import async_session
from app.models.agent import Agent
from app.models.published_page import PublishedPage
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.storage import get_storage_backend
from tests.conftest import _is_safe_test_database_url

SHORT_ID = "browserdirect"
SOURCE_PATH = "out/browser-direct.html"

REPORT_HTML = rb"""<!doctype html>
<html><head><meta charset="utf-8"><title>Direct Browser Report</title>
<style>body{font-family:sans-serif}#capability-marker{color:rgb(1,2,3)}</style>
<script src="/sdk/clawith.js"></script></head>
<body><h1>Direct Browser Report</h1><div id="capability-marker">ready</div><pre id="result">waiting</pre>
<script type="module">
(async () => {
  const errors = [];
  addEventListener('error', event => errors.push(String(event.message || event.error)));
  localStorage.setItem('published-direct-storage', 'available');
  document.cookie = 'published_direct_cookie=available; Path=/';
  const sdkAsset = await fetch('/sdk/clawith.js').then(response => response.ok);
  const appShell = await fetch('/').then(response => response.text());
  const cssPath = (appShell.match(/href="([^"]+\.css)"/) || [])[1];
  const cssAsset = !!cssPath && await fetch(cssPath).then(response => response.ok);
  const imported = await import('data:text/javascript,export default 7');
  const workerValue = await new Promise((resolve, reject) => {
    const source = URL.createObjectURL(new Blob(['postMessage(11)'], {type: 'text/javascript'}));
    const worker = new Worker(source);
    worker.onmessage = event => { worker.terminate(); URL.revokeObjectURL(source); resolve(event.data); };
    worker.onerror = reject;
  });
  await document.fonts.ready;
  const font = document.fonts.check('12px sans-serif');
  const form = document.createElement('form');
  const formInput = document.createElement('input');
  formInput.name = 'browser-capability';
  formInput.value = 'available';
  form.appendChild(formInput);
  let formSubmitted = false;
  form.addEventListener('submit', event => { event.preventDefault(); formSubmitted = true; });
  document.body.appendChild(form);
  form.requestSubmit();
  const downloadLink = document.createElement('a');
  downloadLink.download = 'published-report.txt';
  downloadLink.href = 'data:text/plain,published-report';
  document.body.appendChild(downloadLink);
  const initialUrl = new URL(location.href);
  history.pushState(null, '', '#published-navigation-check');
  const navigation = location.hash === '#published-navigation-check';
  history.replaceState(null, '', initialUrl.pathname + initialUrl.search + initialUrl.hash);
  const health = await fetch('/api/health').then(response => response.ok);
  await Clawith.watermark({text: 'Direct Browser Watermark'});
  const result = {
    initialHash: initialUrl.hash,
    topLevel: window === top,
    origin: location.origin,
    localStorage: localStorage.getItem('published-direct-storage'),
    cookie: document.cookie.includes('published_direct_cookie=available'),
    sdkAsset,
    cssAsset,
    dynamicImport: imported.default,
    workerValue,
    font,
    form: formSubmitted && new FormData(form).get('browser-capability') === 'available',
    navigation,
    unsandboxed: !frameElement || !frameElement.hasAttribute('sandbox'),
    health,
    watermark: !!document.querySelector('[data-clawith-wm]'),
    computedColor: getComputedStyle(document.querySelector('#capability-marker')).color,
    errors,
  };
  document.querySelector('#result').textContent = JSON.stringify(result);
  document.body.dataset.complete = '1';
  if (parent !== window) parent.postMessage({type: 'published-direct-result', result}, '*');
})().catch(error => {
  document.querySelector('#result').textContent = JSON.stringify({fatal: String(error)});
  document.body.dataset.complete = 'error';
});
</script></body></html>"""


async def main() -> None:
    async with async_session() as db:
        url = db.bind.url
        if url.get_backend_name() != "postgresql" or not _is_safe_test_database_url(url):
            raise RuntimeError("Published browser fixtures require an isolated PostgreSQL test database")
        actual_database = await db.scalar(text("SELECT current_database()"))
        if actual_database != url.database:
            raise RuntimeError("Connected database differs from the verified test database")
        await db.execute(delete(PublishedPage).where(PublishedPage.short_id == SHORT_ID))
        tenant = Tenant(
            name="Published Browser Test",
            slug=f"published-browser-{uuid.uuid4().hex[:8]}",
            im_provider="web_only",
        )
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"published_browser_{uuid.uuid4().hex[:8]}",
            email=f"published-browser-{uuid.uuid4().hex[:8]}@test.local",
            password_hash="test-only",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Published Browser Owner",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name="Published Browser Agent",
            role_description="",
            creator_id=user.id,
            tenant_id=tenant.id,
            agent_type="native",
        )
        db.add(agent)
        await db.flush()
        db.add(
            PublishedPage(
                short_id=SHORT_ID,
                agent_id=agent.id,
                user_id=user.id,
                last_published_by_user_id=user.id,
                tenant_id=tenant.id,
                source_path=SOURCE_PATH,
                title="Direct Browser Report",
                access_mode="public",
            )
        )
        await get_storage_backend().write_bytes(f"{agent.id}/{SOURCE_PATH}", REPORT_HTML)
        await db.commit()
    print(SHORT_ID)


if __name__ == "__main__":
    asyncio.run(main())
