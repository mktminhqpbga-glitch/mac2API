"""Smoke-test the module API with a real local Chromium and fixture pages."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import httpx
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAGE = """<!doctype html><form><textarea id="prompt-textarea">Old draft</textarea>
<button type="button" data-testid="add-attachment-button">Add attachment</button>
<div data-testid="attachment-card">old.pdf <button type="button" aria-label="Xoá tệp">Remove</button></div>
<button type="button" data-testid="send-button">Send</button></form>
<input-container><rich-textarea><div class="ql-editor" contenteditable="true">Old draft</div></rich-textarea>
<div data-test-id="attachment-card">old.mp3 <button type="button" title="Remove audio">Remove</button></div>
<button type="button" class="send-button">Send</button></input-container><main></main>
<script>
for (const card of document.querySelectorAll('[data-testid="attachment-card"],[data-test-id="attachment-card"]')) {
  card.querySelector('button').onclick = () => card.remove();
}
for (const button of document.querySelectorAll('[data-testid="send-button"], .send-button')) button.onclick = () => {
  const composer = button.parentNode;
  if (composer.querySelector('[data-testid="attachment-card"],[data-test-id="attachment-card"]')) return;
  const editor = composer.querySelector('textarea,.ql-editor');
  const text = editor.value ?? editor.innerText;
  if (!text.trim()) return;
  const article = document.createElement('article');
  article.innerHTML = '<div data-message-author-role="assistant"><div class="markdown"></div></div>'
    + '<button data-testid="copy-turn-action-button">Copy</button>'
    + '<model-response><div class="markdown-main-panel"></div><button aria-label="Copy">Copy</button></model-response>';
  document.querySelector('main').append(article);
  for (const result of article.querySelectorAll('.markdown,.markdown-main-panel')) {
    if (text.includes('tạo một ảnh mới')) {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 300;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = 'green';
      ctx.fillRect(0, 0, 300, 300);
      const image = document.createElement('img');
      image.src = canvas.toDataURL();
      result.append(image);
    } else {
      result.textContent = 'Test answer: ' + text + (localStorage.studioProfile || '');
    }
  }
}
</script>""".encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


async def check():
    import app
    import web_providers

    headers = {"Authorization": "Bearer fixture"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                 base_url="http://test") as client:
        port = app.web_providers.port
        app.web_providers.port = 0
        with patch.object(app, "DEFAULT_WEB_CDP_PORT", port):
            unauthorized = await client.post("/admin/web-providers/connect")
            assert unauthorized.status_code == 401
            with patch.dict(os.environ, {"RENDER": "true"}):
                remote = await client.post("/admin/web-providers/connect", headers=headers)
            assert remote.status_code == 409
            connected = await client.post("/admin/web-providers/connect", headers=headers)
        assert connected.status_code == 200, connected.text
        assert connected.json()["port"] == port and app.store.web_chrome_enabled()
        response = await client.get("/v1/models", headers=headers)
        assert set(app.MODULES) <= {m["id"] for m in response.json()["data"]}
        for prefix, owner in app.PROVIDER_PREFIXES.items():
            path = f"/{prefix}/v1"
            data = await client.get(path + "/models", headers=headers)
            assert data.status_code == 200 and data.json()["data"]
            assert all(item["owned_by"] == owner for item in data.json()["data"])
            assert (await client.get(path + "/models")).status_code == 401
            wrong = await client.post(path + "/chat/completions", headers=headers,
                                      json={"model": "muse/text" if owner != "muse" else "chatgpt/text",
                                            "prompt": "Wrong provider"})
            assert wrong.status_code == 400 and "error" in wrong.json()
        assert (await client.post("/gemini/v1/videos", headers=headers,
                                  json={"prompt": "Unsupported video"})).status_code == 404
        for provider in web_providers.PROVIDERS:
            web_url = web_providers.PROVIDERS[provider].url
            with requests.Session() as session:
                session.trust_env = False
                target = session.put(f"http://127.0.0.1:{app.web_providers.port}/json/new?{web_url}", timeout=3).json()["id"]
            page = web_providers.CDP(f"ws://127.0.0.1:{app.web_providers.port}/devtools/page/{target}",
                                     timeout=3, suppress_origin=True)
            try:
                definition = web_providers.PROVIDERS[provider]
                app.web_providers._wait_composer(page, definition, time.monotonic() + 5)
                draft = f"(() => {{const editor = document.querySelector({definition.composer!r}); return editor.value ?? editor.innerText;}})()"
                assert page.js(draft) == "Old draft"
                marker = "a" * 32
                page.js(f"document.documentElement.dataset.mac2apiLink = '{marker}'")
                endpoint = f"/admin/web-providers/{provider}/link"
                assert (await client.post(endpoint, json={"marker": marker})).status_code == 401
                linked = await client.post(endpoint, headers=headers, json={"marker": marker})
                assert linked.status_code == 200 and linked.json()["provider"] == provider, linked.text
                assert page.js(draft) == "Old draft"
                status = (await client.get("/admin/web-providers", headers=headers)).json()
                assert status["linked_at"][provider] == linked.json()["linked_at"]
                replay = await client.post(endpoint, headers=headers, json={"marker": marker})
                assert replay.status_code == 409, replay.text
            finally:
                page.close()
                with requests.Session() as session:
                    session.trust_env = False
                    session.get(f"http://127.0.0.1:{app.web_providers.port}/json/close/{target}", timeout=3)
            response = await client.post(f"/admin/web-providers/{provider}/test", headers=headers)
            assert response.status_code == 200, response.text
            prefix = "chatgpt" if provider == "chatgpt" else "gemini"
            base = f"/{prefix}/v1"
            response = await client.post(base + "/chat/completions", headers=headers, json={
                "prompt": "Hello", "timeout": 15,
            })
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == "Test answer: Hello"
            assert response.json()["model"] == f"{provider}/text"
            response = await client.post(base + "/images/tasks", headers=headers, json={
                "prompt": "A green square", "timeout": 15,
            })
            assert response.status_code == 202, response.text
            task = response.json()["id"]
            assert (await client.get(f"/muse/v1/images/tasks/{task}", headers=headers)).status_code == 404
            for _ in range(80):
                response = await client.get(f"{base}/images/tasks/{task}", headers=headers)
                result = response.json()
                if result["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.2)
            assert result["status"] == "completed", result
            assert (await client.get(f"/v1/images/tasks/{task}", headers=headers)).status_code == 200
            image = await client.get(result["data"][0]["url"])
            assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
        response = await client.post("/v1/videos", headers=headers, json={
            "model": "chatgpt/image", "prompt": "wrong endpoint",
        })
        assert response.status_code == 400
        response = await client.post("/v1/images/tasks", headers=headers, json={
            "model": "gemini-web/image", "prompt": "unsupported reference", "images": ["example"],
        })
        assert response.status_code == 400


async def check_existing_profile(port_file):
    import app
    import web_providers

    port, _ = port_file.read_text().splitlines()
    definition = web_providers.PROVIDERS["chatgpt"]
    with requests.Session() as session:
        session.trust_env = False
        target = session.put(f"http://127.0.0.1:{port}/json/new?{definition.url}", timeout=3).json()["id"]
    page = web_providers.CDP(f"ws://127.0.0.1:{port}/devtools/page/{target}", timeout=3, suppress_origin=True)
    headers = {"Authorization": "Bearer fixture"}
    try:
        app.web_providers._wait_composer(page, definition, time.monotonic() + 5)
        page.js("localStorage.studioProfile = ' (chosen profile)'")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
            endpoint = "/admin/web-providers/connect-profile"
            assert (await client.post(endpoint)).status_code == 401
            with patch.dict(os.environ, {"RENDER": "true"}):
                assert (await client.post(endpoint, headers=headers)).status_code == 409
            with patch.object(web_providers, "chrome_debug_file", return_value=port_file):
                connected = await client.post(endpoint, headers=headers)
            assert connected.status_code == 200, connected.text
            assert connected.json()["profile_connected"] and connected.json()["ready"] == []
            missing = await client.post("/admin/web-providers/chatgpt/test", headers=headers)
            assert missing.status_code == 409, missing.text

            marker = "b" * 32
            page.js(f"document.documentElement.dataset.mac2apiLink = '{marker}'")
            endpoint = "/admin/web-providers/chatgpt/link"
            linked = await client.post(endpoint, headers=headers, json={"marker": marker})
            assert linked.status_code == 200, linked.text
            assert (await client.post(endpoint, headers=headers, json={"marker": marker})).status_code == 409
            status = (await client.get("/admin/web-providers", headers=headers)).json()
            assert status["ready"] == ["chatgpt"]
            key = app.store.create_client("capability fixture", ["chatgpt", "gemini-web"])["api_key"]
            caps_headers = {"Authorization": f"Bearer {key}"}
            slot = app.ConcurrencyLock(1)
            assert slot.acquire(blocking=False)
            with patch.object(app, "GEN_LOCK", slot):
                try:
                    browser = app.web_providers.profile_connection.browser
                    with patch.object(browser, "send", side_effect=AssertionError("capabilities probed busy Chrome")):
                        busy = (await client.get("/v1/capabilities", headers=caps_headers)).json()
                    states = {provider["id"]: provider for provider in busy["providers"]}
                    assert states["chatgpt"]["ready"] and states["chatgpt"]["busy"]
                    assert states["chatgpt"]["reason"] == "busy" and not states["gemini-web"]["ready"]
                    queued = await client.post("/chatgpt/v1/images/tasks", headers=caps_headers,
                                               json={"prompt": "A green square", "timeout": 15})
                    assert queued.status_code == 202 and queued.json()["status"] == "queued", queued.text
                    task_path = f"/chatgpt/v1/images/tasks/{queued.json()['id']}"
                    waiting = await client.get(task_path, headers=caps_headers)
                    assert waiting.json()["status"] == "queued", waiting.text
                finally:
                    slot.release()
                for _ in range(80):
                    result = (await client.get(task_path, headers=caps_headers)).json()
                    if result["status"] in ("completed", "failed"):
                        break
                    await asyncio.sleep(0.2)
                assert result["status"] == "completed", result

            capabilities = (await client.get("/v1/capabilities", headers=caps_headers)).json()
            states = {provider["id"]: provider for provider in capabilities["providers"]}
            assert states["chatgpt"]["ready"] and states["chatgpt"]["connected"]
            assert not states["gemini-web"]["ready"]
            assert (await client.get("/gemini/v1/models", headers=headers)).json()["data"] == []

            response = await client.post("/chatgpt/v1/chat/completions", headers=headers, json={"prompt": "Hello"})
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == "Test answer: Hello (chosen profile)"
            assert page.js("document.querySelector('textarea').value") == "Old draft"
            browser = app.web_providers.profile_connection.browser
            targets = browser.send("Target.getTargets")["result"]["targetInfos"]
            matching_pages = [tab for tab in targets if tab["type"] == "page" and tab["url"] == definition.url]
            assert [tab["targetId"] for tab in matching_pages] == [target], matching_pages
            assert app.Store(app.CFG).web_chrome_profile_preferred()
            restarted = web_providers.WebProviders(int(port), profile_preferred=True)
            assert not restarted.ready("chatgpt")
            with patch.object(app, "web_providers", restarted):
                status = (await client.get("/admin/web-providers", headers=headers)).json()
                assert not status["configured"] and status["ready"] == []
                capabilities = (await client.get("/v1/capabilities", headers=caps_headers)).json()
                assert all(not provider["ready"] for provider in capabilities["providers"])
                blocked = await client.post("/chatgpt/v1/chat/completions", headers=headers, json={"prompt": "Hello"})
                assert blocked.status_code == 503, blocked.text
    finally:
        page.close()
        app.web_providers.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        chromium = shutil.which("chromium") or shutil.which("google-chrome")
        assert chromium, "Chromium is required for this test"
        profile = Path(tmp) / "web"
        profile.mkdir()
        proc = subprocess.Popen([chromium, "--headless", "--no-sandbox", "--no-proxy-server",
                                 "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = profile / "DevToolsActivePort"
            for _ in range(80):
                if port_file.exists():
                    break
                time.sleep(0.1)
            os.environ.update(MUSE2API_HOME=tmp, MUSE2API_PROFILE_ROOT=tmp,
                              MUSE2API_WEB_CDP_PORT=port_file.read_text().splitlines()[0],
                              MUSE2API_KEY="fixture", NO_PROXY="127.0.0.1,localhost")
            import web_providers
            fixtures = {name: replace(provider, url=f"http://127.0.0.1:{server.server_port}/")
                        for name, provider in web_providers.PROVIDERS.items()}
            with patch.dict(web_providers.PROVIDERS, fixtures):
                asyncio.run(check())
                asyncio.run(check_existing_profile(port_file))
            print("PASS: ChatGPT and Gemini module routes, real CDP transport, image polling, media and input validation")
            print("PASS: existing Chrome profile linkage, correct session storage, original tab retained")
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
