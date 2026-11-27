"""Exercise the browser bridge API and extension DOM actions in Chromium fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp import CDP
from test_admin_dashboard_browser import wait

PAGE = """<!doctype html><meta charset="utf-8">
<button data-testid="accounts-profile-button" aria-label="Google Account">Signed in</button>
<form>
  <div id="prompt-textarea" contenteditable="true" style="min-height:30px;width:400px"></div>
  <rich-textarea><div class="ql-editor" contenteditable="true" style="min-height:30px;width:400px"></div></rich-textarea>
  <button data-testid="send-button" class="send-button" type="button" disabled>Send</button>
</form>
<script>
const chatgpt = location.pathname === '/';
const editor = document.querySelector(chatgpt ? '#prompt-textarea' : '.ql-editor');
const button = document.querySelector('.send-button');
if (location.search) editor.innerText = 'ORIGINAL DRAFT';
editor.oninput = () => {button.disabled = !editor.innerText.trim()};
button.onclick = () => {
  fetch('/fixture-click', {method:'POST'});
  const reply = document.createElement(chatgpt ? 'div' : 'model-response');
  reply.dataset.messageAuthorRole = 'assistant';
  const body = document.createElement('div');
  body.className = 'markdown';
  reply.append(body);
  if (editor.innerText.includes('tạo một ảnh mới')) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 300;
    canvas.getContext('2d').fillRect(0, 0, 300, 300);
    body.append(Object.assign(new Image(), {src: canvas.toDataURL()}));
  } else {
    body.textContent = editor.innerText;
  }
  const copy = reply.appendChild(document.createElement('button'));
  copy.setAttribute('aria-label', 'Copy');
  copy.dataset.testid = 'copy-turn-action-button';
  copy.textContent = 'Copy';
  document.body.append(reply);
};
</script>""".encode()


class ProviderFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        self.server.submissions.append(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):
        pass


def pair(api, provider, profile, admin):
    path = f"/admin/web-providers/{provider}/extension"
    config = {"profile": profile, "label": f"Fixture {provider}"}
    assert api.post(path, json=config).status_code == 401
    response = api.post(path, json=config, headers=admin)
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}, token.split(".")[0]


def serve_job(api, browser, origin, pool, app, auth, bridge, prefix, provider, kind):
    path = "/" if provider == "chatgpt" else "/app"
    routes = {"text": "chat/completions", "image": "images/generations"}
    request = {"model": f"{provider}/{kind}", "prompt": "Fixture line 1\nFixture line 2", "timeout": 12}
    if kind == "image":
        request["response_format"] = "b64_json"
    pending = pool.submit(api.post, f"/{prefix}/v1/{routes[kind]}", headers=auth, json=request)
    job = api.get("/browser-bridge/next", headers=bridge).json()
    endpoint = f"/browser-bridge/jobs/{job['id']}"
    assert job["image"] == (kind == "image") and app.GEN_LOCK.locked()
    assert api.post(endpoint + "/permit", headers=auth).status_code == 401
    result_tab = browser.put("/json/new?about:blank").json()
    try:
        with closing(CDP(result_tab["webSocketDebuggerUrl"], suppress_origin=True)) as page:
            page.send("Page.navigate", {"url": origin + path})
            wait(lambda: page.js("Boolean(document.querySelector('form'))"))
            script = (ROOT / "extension/bridge-site.js").read_text()
            page.js(script)
            page.js(f"const site = {{...bridgeSites[{json.dumps(provider)}], origin: location.origin}}")
            call = "bridgeSiteAction(site, '{}', " + json.dumps(job["prompt"]) + ")"
            page.js("document.querySelector(site.account).hidden = true")
            verified = page.js(call.format("verify"), await_promise=True)
            assert verified["signedIn"] and verified["composerReady"]
            page.js("document.querySelector(site.composer).innerText = 'previous draft'")
            assert page.js(call.format("prepare"), await_promise=True)["dirty"]
            page.js("document.querySelector(site.composer).innerText = ''")
            page.js("document.querySelector('form').insertAdjacentHTML('beforeend', "
                    "'<button type=button aria-label=\"Remove attachment\">old file</button>')")
            assert page.js(call.format("prepare"), await_promise=True)["dirty"]
            page.js("document.querySelector('[aria-label=\"Remove attachment\"]').remove()")
            wait(lambda: page.js(call.format("prepare"), await_promise=True)["ready"])
            assert api.post(endpoint + "/permit", headers=bridge).json()["ok"]
            assert not api.post(endpoint + "/permit", headers=bridge).json()["ok"]
            assert page.js(call.format("send"), await_promise=True)["sent"]
            state = wait(lambda: value if (value := page.js("bridgeSiteAction(site, 'read')",
                         await_promise=True))["image" if kind == "image" else "text"] else None)
            if kind == "image":
                image = page.js(f"readBridgeImage({json.dumps(state['image'])}, location.origin, 2000)",
                                await_promise=True)
                result = {"image": image}
            else:
                result = {"text": state["text"]}
    finally:
        browser.get("/json/close/" + result_tab["id"]).raise_for_status()
    assert app.GEN_LOCK.locked()
    assert api.post(endpoint + "/finish", headers=bridge, json=result).json()["ok"]
    response = pending.result(timeout=8)
    assert response.status_code == 200, response.text
    assert not api.post(endpoint + "/finish", headers=bridge, json=result).json()["ok"]
    if kind == "image":
        assert response.json()["data"][0]["b64_json"].startswith("iVBORw0")
    else:
        assert "Fixture line 2" in response.json()["choices"][0]["message"]["content"]


def exercise(api, browser, fixture, pool, app):
    admin = {"Authorization": "Bearer fixture-bridge-key"}
    chat, chat_id = pair(api, "chatgpt", "a" * 32, admin)
    gemini, gemini_id = pair(api, "gemini-web", "b" * 32, admin)
    for bridge in (chat, gemini):
        assert api.post("/browser-bridge/jobs/unknown/heartbeat", headers=bridge).status_code == 200
    wait(lambda: len(api.get("/admin/web-providers", headers=admin).json()["ready"]) == 2)

    grant = {"label": "Fixture customer", "rules": {
        "chatgpt/text": {"accounts": [chat_id]}, "chatgpt/image": {"accounts": [chat_id]}, "gemini-web/image": {"accounts": [gemini_id]}}}
    client_key = api.post("/admin/client-keys", json=grant, headers=admin)
    assert client_key.status_code == 201, client_key.text
    customer = {"Authorization": "Bearer " + client_key.json()["api_key"]}
    origin = f"http://127.0.0.1:{fixture.server_port}"
    assert app.engine.proc is None
    cap = api.get("/v1/capabilities", headers=customer).json()
    assert all(provider["ready"] for provider in cap["providers"])
    serve_job(api, browser, origin, pool, app, customer, chat, "chatgpt", "chatgpt", "text")
    serve_job(api, browser, origin, pool, app, customer, gemini, "gemini", "gemini-web", "image")
    serve_job(api, browser, origin, pool, app, customer, chat, "chatgpt", "chatgpt", "image")
    assert len(fixture.submissions) == 3
    assert api.post("/gemini/v1/chat/completions", headers=customer,
                    json={"model": "gemini-web/text", "prompt": "Denied"}).status_code == 403

    pending = pool.submit(api.post, "/chatgpt/v1/chat/completions", headers=customer,
                          json={"model": "chatgpt/text", "prompt": "Before revoke", "timeout": 12})
    job = api.get("/browser-bridge/next", headers=chat).json()
    endpoint = f"/browser-bridge/jobs/{job['id']}"
    chat_path = f"/admin/web-providers/chatgpt/extension/{chat_id}"
    proxy = {"proxy": {"server": "http://127.0.0.1:9876", "username": "fixture", "password": "fixture-secret"}}
    assert api.post(chat_path + "/proxy", headers=admin, json=proxy).status_code == 409
    assert not api.post(endpoint + "/permit", headers=gemini).json()["ok"]
    key_path = "/admin/client-keys/" + client_key.json()["id"]
    assert api.patch(key_path, headers=admin, json={"enabled": False}).status_code == 200
    assert not api.post(endpoint + "/permit", headers=chat).json()["ok"]
    assert app.GEN_LOCK.locked() and not pending.done()
    assert api.post(endpoint + "/finish", headers=chat, json={"error": "interrupted"}).json()["ok"]
    assert pending.result(timeout=8).status_code == 403
    assert api.delete(chat_path, headers=admin).status_code == 204
    assert api.get("/browser-bridge/next", headers=chat).status_code == 401
    assert api.get("/browser-bridge/next", headers=admin).status_code == 401
    assert api.post(endpoint + "/permit", headers={
        **admin, "X-Forwarded-For": "127.0.0.1"}).status_code == 403

    chat, chat_id = pair(api, "chatgpt", "a" * 32, admin)
    assert api.post("/browser-bridge/jobs/unknown/heartbeat", headers=chat).status_code == 200
    assert api.patch(chat_path, headers=admin, json={"enabled": False, "label": "Proxy test"}).status_code == 200
    assert "chatgpt" not in api.get("/admin/web-providers", headers=admin).json()["ready"]
    pair_path = "/admin/web-providers/chatgpt/extension"
    chat, _ = pair(api, "chatgpt", "a" * 32, admin)
    assert api.post("/browser-bridge/jobs/unknown/heartbeat", headers=chat).status_code == 200
    assert "chatgpt" not in api.get("/admin/web-providers", headers=admin).json()["ready"]
    assert api.patch(chat_path, headers=admin, json={"enabled": True, "label": "Proxy test"}).status_code == 200
    migrated = api.post(chat_path + "/proxy", json=proxy, headers=admin)
    assert migrated.status_code == 201 and migrated.json()["id"] == chat_id, migrated.text
    assert migrated.json()["label"] == "Proxy test"
    assert "fixture-secret" not in migrated.text and migrated.json()["proxy"]["has_password"]
    assert not migrated.json()["verified"] and not migrated.json()["enabled"]
    assert api.get("/browser-bridge/next", headers=chat).status_code == 401
    assert api.post(pair_path, json={"profile": "a" * 32}, headers=admin).status_code == 409
    assert api.patch(key_path, headers=admin, json={"enabled": True}).status_code == 200
    unavailable = api.post("/chatgpt/v1/chat/completions", headers=customer,
                           json={"model": "chatgpt/text", "prompt": "Before checking the migrated account"})
    assert unavailable.status_code == 503 and len(fixture.submissions) == 3


def main():
    chromium = shutil.which("chromium") or shutil.which("google-chrome")
    if not chromium:
        print("SKIP: Chromium is required for the Chrome bridge browser test")
        return
    with tempfile.TemporaryDirectory(prefix="bridge-test-") as data:
        os.environ.update(MUSE2API_HOME=data, MUSE2API_PROFILE_ROOT=data, MUSE2API_KEY="fixture-bridge-key")
        import app

        with ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture) as fixture, socket.socket() as sock:
            fixture.submissions = []
            sock.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app.app, log_level="warning", lifespan="off"))
            profile = Path(data) / "chrome"
            chrome = subprocess.Popen([
                chromium, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-background-networking", "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                f"--user-data-dir={profile}", "--remote-debugging-port=0", "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with ThreadPoolExecutor(max_workers=6) as pool:
                pool.submit(fixture.serve_forever)
                pool.submit(server.run, sockets=[sock])
                try:
                    debug_file = profile / "DevToolsActivePort"
                    debug_port = wait(lambda: debug_file.exists() and debug_file.read_text().splitlines()[0])
                    with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=25) as api, \
                         httpx.Client(base_url=f"http://127.0.0.1:{debug_port}") as browser:
                        exercise(api, browser, fixture, pool, app)
                finally:
                    chrome.terminate()
                    chrome.wait(timeout=10)
                    server.should_exit = True
                    fixture.shutdown()
    print("PASS: Chrome bridge text and image, permissions before send, and idle startup")


if __name__ == "__main__":
    main()
