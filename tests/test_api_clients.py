"""Exercise tenant boundaries through the real FastAPI routes with mocked generation."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run():
    import app
    from media_storage import new_media_name, save_media

    def headers(key):
        return {"Authorization": f"Bearer {key}"}

    admin = headers("test-admin-key")
    gate = threading.Event()

    def generate(prompt, kind, timeout, *, on_acquired=None, **kwargs):
        if on_acquired:
            on_acquired()
        if not gate.wait(timeout=5):
            raise RuntimeError("Fixture didn't release generation")
        name = new_media_name(".mp4" if kind == "video" else ".png")
        path, size = save_media(app.CFG.media_dir, name, data=b"fixture bytes")
        return {"path": path, "filename": name, "size": size, "kind": kind}, "operator-fixture"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                 base_url="http://test") as http:
        async def issue(label, providers):
            response = await http.post("/admin/client-keys", headers=admin,
                                       json={"label": label, "providers": providers})
            assert response.status_code == 201, response.text
            return response.json()

        alice = await issue("Alice", ["muse"])
        bob = await issue("Bob", ["muse", "chatgpt"])
        al, bo = headers(alice["api_key"]), headers(bob["api_key"])
        stored = Path(app.store.clients_file).read_text()
        assert alice["api_key"] not in stored and bob["api_key"] not in stored
        assert app.Store(app.CFG).find_client(alice["api_key"])["id"] == alice["id"]
        listed = (await http.get("/admin/client-keys", headers=admin)).json()
        assert all("digest" not in row and "api_key" not in row for row in listed["clients"])
        assert (await http.get("/admin/accounts", headers=al)).status_code == 401
        assert (await http.get("/admin/apikey", headers=al)).status_code == 401
        assert (await http.get("/chatgpt/v1/models", headers=al)).status_code == 403
        denied = await http.post("/v1/chat/completions", headers=al,
                                 json={"model": "chatgpt/text", "prompt": "Wrong provider"})
        assert denied.status_code == 403
        with patch.object(app.web_providers, "port", 19211):
            model_list = (await http.get("/v1/models", headers=bo)).json()["data"]
            assert {item["owned_by"] for item in model_list} == {"muse", "chatgpt"}
        assert (await http.get("/v1/models", headers=al)).status_code == 200
        with patch.object(app.CFG, "ref_image_private_fetch", True), \
             patch.object(app.CFG, "ref_image_local_files", True):
            for ref in ("http://127.0.0.1:18610/admin", "/etc/hostname"):
                rejected = await http.post("/muse/v1/images/tasks", headers=al,
                                           json={"prompt": "Private input", "images": [ref]})
                assert rejected.status_code == 400, rejected.text

        endpoint = "/muse/v1/images/tasks"
        body = {"prompt": "A test from Alice"}
        try:
            with patch.object(app, "_run_generation", side_effect=generate):
                same_key = {"Idempotency-Key": "shared-ci-key"}
                created = await http.post(endpoint, headers={**al, **same_key}, json=body)
                assert created.status_code == 202, created.text
                alice_task = created.json()["id"]
                duplicate = await http.post(endpoint, headers={**al, **same_key}, json=body)
                assert duplicate.json()["id"] == alice_task
                created = await http.post(endpoint, headers={**bo, **same_key}, json=body)
                assert created.status_code == 202 and created.json()["id"] != alice_task
                created = await http.post("/muse/v1/videos", headers=al, json={"prompt": "A video"})
                assert created.status_code == 200
                video_task = created.json()["id"]
                assert (await http.post(endpoint, headers=al, json=body)).status_code == 429
                assert (await http.get(endpoint + "/" + alice_task, headers=bo)).status_code == 404
                assert (await http.get("/v1/videos/" + video_task, headers=bo)).status_code == 404
                assert (await http.delete("/admin/tasks/" + alice_task, headers=admin)).status_code == 409
                await http.post("/admin/tasks/clear", headers=admin, json={})
                assert (await http.get(endpoint + "/" + alice_task, headers=al)).status_code == 200
                gate.set()
                for _ in range(100):
                    result = (await http.get(endpoint + "/" + alice_task, headers=al)).json()
                    video = (await http.get("/muse/v1/videos/" + video_task, headers=al)).json()
                    if result["status"] == video["status"] == "completed":
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError((result, video))

                url = result["data"][0]["url"]
                raw_url = urlsplit(url).path
                assert "account" not in result and "api_prompt" not in result
                assert "client-" + alice["id"] in raw_url and "access=c." in url
                assert (await http.get(raw_url)).status_code == 404
                assert (await http.get(raw_url, headers=bo)).status_code == 404
                assert (await http.get(raw_url, headers=al)).content == b"fixture bytes"
                asset = await http.get(url)
                assert asset.content == b"fixture bytes" and asset.headers["cache-control"] == "private, no-store"
                assert (await http.get(url[:-1], headers=bo)).status_code == 404
                assert (await http.get(video["url"])).content == b"fixture bytes"
                assert (await http.get(urlsplit(video["url"]).path + "/thumbnail", headers=bo)).status_code == 404
                assert (await http.delete("/admin/tasks/" + alice_task, headers=admin)).status_code == 200
                assert (await http.get(raw_url)).status_code == 404  # Ownership survives history deletion.
                admin_items = (await http.get("/admin/media", headers=admin)).json()["media"]
                admin_url = next(item["url"] for item in admin_items if raw_url in item["url"])
                assert "access=a." in admin_url
                assert (await http.delete("/admin/client-keys/" + alice["id"], headers=admin)).status_code == 200
                assert (await http.get("/v1/models", headers=al)).status_code == 401
                assert (await http.get(url)).status_code == 404
                assert (await http.get(admin_url)).status_code == 200
        finally:
            gate.set()

        parallel = await issue("Concurrent requests", ["openai"])
        parallel_auth = headers(parallel["api_key"])
        start = asyncio.Queue()
        finish = asyncio.Event()

        async def slow_chat(*_args):
            start.put_nowait(True)
            await finish.wait()
            return {"ok": True}

        async def send_chat():
            return await http.post("/v1/chat/completions", headers=parallel_auth,
                                   json={"model": "openai/test-model", "prompt": "Fixture"})

        with patch.object(app.chat_providers, "chat", side_effect=slow_chat):
            pending = [asyncio.create_task(send_chat()) for _ in range(app.CLIENT_ACTIVE_POST_LIMIT)]
            try:
                for _ in pending:
                    await asyncio.wait_for(start.get(), timeout=3)
                limited = await asyncio.wait_for(send_chat(), timeout=3)
                assert limited.status_code == 429
            finally:
                finish.set()
                assert all(r.status_code == 200 for r in await asyncio.gather(*pending))
            assert (await send_chat()).status_code == 200

        restricted = await issue("Rate fixture", ["muse"])
        auth = headers(restricted["api_key"])
        with patch.object(app, "CLIENT_REQUESTS_PER_MINUTE", 1):
            first = await http.post("/v1/chat/completions", headers=auth,
                                    json={"model": "gemini-web/text", "prompt": "Fixture"})
            assert first.status_code == 403
            limited = await http.post("/v1/chat/completions", headers=auth,
                                      json={"model": "muse/text", "prompt": "Fixture"})
            assert limited.status_code == 429 and limited.headers["retry-after"]
            assert (await http.get("/v1/models", headers=auth)).status_code == 200
    print("PASS: provider permissions, scoped keys, admission limits, idempotency, task isolation, private media and revocation")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        "MUSE2API_HOME": tmp, "MUSE2API_PROFILE_ROOT": tmp, "MUSE2API_KEY": "test-admin-key",
    }):
        asyncio.run(run())
