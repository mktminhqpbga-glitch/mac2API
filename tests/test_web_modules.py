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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAGE = """<!doctype html><textarea id="prompt-textarea"></textarea>
<rich-textarea><div class="ql-editor" contenteditable="true"></div></rich-textarea>
<button data-testid="send-button" class="send-button">Send</button><main></main>
<script>
document.querySelector('button').onclick = () => {
  const text = document.querySelector('textarea').value || document.querySelector('.ql-editor').innerText;
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
      result.textContent = 'Test answer: ' + text;
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
        response = await client.get("/v1/models", headers=headers)
        assert set(app.MODULES) <= {m["id"] for m in response.json()["data"]}
        for provider in web_providers.PROVIDERS:
            response = await client.post(f"/admin/web-providers/{provider}/test", headers=headers)
            assert response.status_code == 200, response.text
            response = await client.post("/v1/chat/completions", headers=headers, json={
                "model": f"{provider}/text", "prompt": "Hello", "timeout": 15,
            })
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == "Test answer: Hello"
            response = await client.post("/v1/images/tasks", headers=headers, json={
                "model": f"{provider}/image", "prompt": "A green square", "timeout": 15,
            })
            assert response.status_code == 202, response.text
            task = response.json()["id"]
            for _ in range(80):
                response = await client.get(f"/v1/images/tasks/{task}", headers=headers)
                result = response.json()
                if result["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.2)
            assert result["status"] == "completed", result
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
            print("PASS: ChatGPT and Gemini module routes, real CDP transport, image polling, media and input validation")
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
