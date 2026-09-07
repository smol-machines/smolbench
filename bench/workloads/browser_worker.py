#!/usr/bin/env python3
"""A small stateful browser worker kept alive across Smol branches."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body { font-family: system-ui, sans-serif; margin: 0; background: #111827; color: #f9fafb; }
main { width: 720px; margin: 90px auto; padding: 42px; background: #1f2937; border-radius: 24px; }
h1 { font-size: 42px; margin: 0 0 12px; } p { color: #9ca3af; }
input { width: 70%; padding: 14px; font-size: 20px; border-radius: 10px; border: 0; }
button { padding: 14px 18px; font-size: 20px; margin-left: 8px; border: 0; border-radius: 10px; background: #ff5c35; color: white; }
#result { margin-top: 28px; font: 700 28px ui-monospace, monospace; color: #fbbf24; }
</style></head><body><main>
<h1>One browser, many branches.</h1>
<p>This page and its Chromium process were already running at checkpoint time.</p>
<input id="branch" value=""><button id="run">Run branch</button>
<div id="result">waiting</div>
<script>
window.actionCount = 0;
document.querySelector('#run').onclick = () => {
  window.actionCount += 1;
  const value = document.querySelector('#branch').value;
  document.querySelector('#result').textContent = `${value} · action ${window.actionCount}`;
};
</script></main></body></html>"""


PAGE = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.reply(200, {"ready": True})
            return
        if parsed.path != "/action":
            self.reply(404, {"error": "not found"})
            return
        branch_id = parse_qs(parsed.query).get("id", [""])[0]
        if not branch_id:
            self.reply(400, {"error": "missing id"})
            return
        if PAGE is None:
            self.reply(503, {"error": "browser is not ready"})
            return
        try:
            self.reply(200, perform_action(PAGE, branch_id))
        except Exception as error:
            self.reply(500, {"error": str(error)})

    def reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def launch_browser(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=["--disable-dev-shm-usage", "--no-sandbox"],
    )
    page = browser.new_page(viewport={"width": 1024, "height": 768})
    page.set_content(HTML, wait_until="load")
    return browser, page


def perform_action(page, branch_id: str) -> dict[str, object]:
    started = time.perf_counter()
    page.locator("#branch").fill(branch_id)
    page.locator("#run").click()
    text = page.locator("#result").inner_text()
    count = page.evaluate("window.actionCount")
    screenshot = page.screenshot(type="png")
    result: dict[str, object] = {
        "branch_id": branch_id,
        "action_count": count,
        "result": text,
        "screenshot_sha256": hashlib.sha256(screenshot).hexdigest(),
        "screenshot_base64": base64.b64encode(screenshot).decode(),
    }
    result["worker_action_seconds"] = time.perf_counter() - started
    return result


def serve() -> None:
    global PAGE
    with sync_playwright() as playwright:
        _browser, page = launch_browser(playwright)
        PAGE = page
        HTTPServer(("127.0.0.1", 8765), Handler).serve_forever(poll_interval=0.05)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--once":
        with sync_playwright() as playwright:
            started = time.perf_counter()
            _browser, page = launch_browser(playwright)
            launch_seconds = time.perf_counter() - started
            result = perform_action(page, sys.argv[2])
            result["browser_launch_seconds"] = launch_seconds
            print(json.dumps(result, separators=(",", ":")))
        return
    if len(sys.argv) != 1:
        raise SystemExit("usage: browser_worker.py [--once BRANCH_ID]")
    serve()


if __name__ == "__main__":
    main()
