"""Real-Chromium acceptance test for the published-page SDK.

Run this script in the backend Docker image, which contains Chromium. The test
serves the production SDK unchanged and a frame-compatible local OAuth provider.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

from chromium_browser import ChromiumSession

SDK_SOURCE = (Path(__file__).resolve().parent.parent / "public/sdk/clawith.js").read_bytes()


class BrowserFixtureHandler(BaseHTTPRequestHandler):
    exchange_attempts = 0
    exchange_attempts_by_code: ClassVar[dict[str, int]] = {}
    hook_payloads: ClassVar[list[dict]] = []
    request_paths: ClassVar[list[str]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).request_paths.append(f"GET {self.path}")
        parsed = urlparse(self.path)
        if parsed.path == "/sdk/clawith.js":
            self._send(200, SDK_SOURCE, "application/javascript")
            return
        if parsed.path == "/embed":
            port = self.server.server_port
            body = f"""<!doctype html><meta charset=utf-8>
<iframe id=report src="http://127.0.0.1:{port}/p/sdk-browser"></iframe>
<pre id=result>waiting</pre>
<script>
addEventListener('message', event => {{
  if (event.source === document.querySelector('#report').contentWindow
      && event.data && event.data.type === 'published-page:sdk-auth-start') {{
    const report = document.querySelector('#report');
    const reportUrl = new URL(report.src);
    report.src = reportUrl.origin + '/api/sdk/auth/start?return_to=' +
      encodeURIComponent(reportUrl.href);
    return;
  }}
  if (event.data && event.data.type === 'sdk-browser-result') {{
    document.querySelector('#result').textContent = JSON.stringify(event.data.result);
    document.body.dataset.complete = '1';
  }}
}});
</script>""".encode()
            self._send(200, body, "text/html")
            return
        if parsed.path in {"/p/sdk-browser", "/p/sdk-retry"}:
            retry_scenario = parsed.path.endswith("sdk-retry")
            watermark_attribute = "" if retry_scenario else " data-watermark"
            body_template = """<!doctype html><meta charset=utf-8><title>SDK Browser Report</title>
<script src=/sdk/clawith.js data-hook=browser-hook__WATERMARK_ATTRIBUTE__></script>
<pre id=result>waiting</pre>
<script>
(async () => {
  let firstError = '';
  try { await Clawith.ready(); } catch (error) { firstError = String(error); }
  const user = await Clawith.ready();
  let onReadyUser = null;
  await Clawith.onReady(value => { onReadyUser = value; });
  localStorage.setItem('sdk-browser-storage', 'available');
  document.cookie = 'sdk_browser_cookie=available; Path=/';
  const automaticWatermark = !!document.querySelector('[data-clawith-wm]');
  if (__RETRY_SCENARIO__) await Clawith.watermark();
  const authenticatedWatermark = !!document.querySelector('[data-clawith-wm]');
  Clawith.watermark({ text: 'Manual Browser Watermark' });
  const watermark = document.querySelector('[data-clawith-wm]');
  const hook = await Clawith.triggerHook(Clawith.hook, { answer: 42 });
  const result = {
    user,
    globalUser: Clawith.user,
    onReadyUser,
    firstError,
    cleanSearch: location.search === '',
    hook,
    automaticWatermark,
    authenticatedWatermark,
    manualWatermark: watermark.style.backgroundImage.includes('Manual%20Browser%20Watermark'),
    localStorage: localStorage.getItem('sdk-browser-storage'),
    cookie: document.cookie.includes('sdk_browser_cookie=available'),
    topLevel: window === top,
    origin: location.origin,
  };
  document.querySelector('#result').textContent = JSON.stringify(result);
  document.body.dataset.complete = '1';
  if (parent !== window) parent.postMessage({ type: 'sdk-browser-result', result }, '*');
})().catch(error => {
  document.querySelector('#result').textContent = JSON.stringify({ fatal: String(error) });
  document.body.dataset.complete = 'error';
});
</script>"""
            body = (
                body_template.replace("__WATERMARK_ATTRIBUTE__", watermark_attribute)
                .replace("__RETRY_SCENARIO__", str(retry_scenario).lower())
                .encode()
            )
            self._send(200, body, "text/html")
            return
        if parsed.path == "/api/sdk/auth/start":
            return_to = parse_qs(parsed.query)["return_to"][0]
            separator = "&" if "?" in return_to else "?"
            code = "RETRY_CODE" if return_to.endswith("/p/sdk-retry") else "BROWSER_CODE"
            callback = f"{return_to}{separator}{urlencode({'code': code, 'state': 'BROWSER_STATE'})}"
            self.send_response(302)
            self.send_header("Location", callback)
            self.end_headers()
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        type(self).request_paths.append(f"POST {self.path}")
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/sdk/auth/exchange":
            type(self).exchange_attempts += 1
            code = str(payload.get("code", ""))
            code_attempts = type(self).exchange_attempts_by_code.get(code, 0) + 1
            type(self).exchange_attempts_by_code[code] = code_attempts
            if code == "RETRY_CODE" and code_attempts == 1:
                self._send(502, b'{"error":"retry once"}', "application/json")
                return
            self._send(
                200,
                json.dumps(
                    {
                        "userId": "browser-user",
                        "userName": "Browser SDK User",
                        "mobile": "13800001234",
                    }
                ).encode(),
                "application/json",
            )
            return
        if self.path == "/api/webhooks/t/browser-hook":
            type(self).hook_payloads.append(payload)
            self._send(200, b'{"accepted":true}', "application/json")
            return
        self._send(404, b"not found", "text/plain")


def assert_sdk_result(result: dict, *, top_level: bool, retry: bool) -> None:
    expected_user = {
        "userId": "browser-user",
        "userName": "Browser SDK User",
        "mobile": "13800001234",
    }
    assert result.get("fatal") is None, result
    assert result["user"] == expected_user
    assert result["globalUser"] == expected_user
    assert result["onReadyUser"] == expected_user
    if retry:
        assert "exchange failed 502" in result["firstError"]
    else:
        assert result["firstError"] == ""
    assert result["cleanSearch"] is True
    assert result["hook"] == {"accepted": True}
    assert result["automaticWatermark"] is (not retry)
    assert result["authenticatedWatermark"] is True
    assert result["manualWatermark"] is True
    assert result["localStorage"] == "available"
    if top_level:
        assert result["cookie"] is True
    assert result["topLevel"] is top_level
    assert result["origin"].startswith("http://127.0.0.1:")


def main() -> None:
    BrowserFixtureHandler.exchange_attempts = 0
    BrowserFixtureHandler.exchange_attempts_by_code = {}
    BrowserFixtureHandler.hook_payloads = []
    BrowserFixtureHandler.request_paths = []
    server = ThreadingHTTPServer(("0.0.0.0", 0), BrowserFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = ChromiumSession()
    try:
        port = server.server_port
        embedded = browser.report_result(f"http://localhost:{port}/embed")
        assert_sdk_result(embedded, top_level=False, retry=False)

        top_level_result = browser.report_result(f"http://127.0.0.1:{port}/p/sdk-retry")
        assert_sdk_result(top_level_result, top_level=True, retry=True)

        assert len(BrowserFixtureHandler.hook_payloads) == 2
        assert BrowserFixtureHandler.hook_payloads == [
            {"answer": 42, "report": {"short_id": "sdk-browser", "title": "SDK Browser Report"}},
            {"answer": 42, "report": {"short_id": "sdk-retry", "title": "SDK Browser Report"}},
        ]
        assert BrowserFixtureHandler.exchange_attempts == 3
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("published-page SDK Chromium tests passed")


if __name__ == "__main__":
    main()
