"""Verify both web gateways against a loopback OpenAI-compatible fixture."""
import asyncio
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")
KEY = "fixture-upstream-key"
ENDPOINT = "/admin/chatgpt2api"
SCOPED = "/chatgpt/v1"


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, body, status=200, kind="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            return self.reply({}, 401)
        if self.path in {"/v1/models", "/openai/v1/models"}:
            return self.reply({"data": [{"id": model} for model in (
                "auto", "gpt-image-2.6", "gemini-auto", "gemini-4-hydrogen", "gemini-3.1-flash-image"
            )]})
        self.reply({}, 404)

    def authorized(self):
        if self.path.startswith("/openai/v1/"):
            return self.headers.get("Authorization") is None
        return self.headers.get("Authorization") == "Bearer " + KEY

    def do_POST(self):
        if not self.authorized():
            return self.reply({}, 401)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, body))
        if self.path in {"/v1/chat/completions", "/openai/v1/chat/completions"}:
            if body["stream"]:
                if body["messages"][-1]["content"] == "broken stream":
                    return self.reply(b'data: {"error":{"message":"private upstream detail"}}\n\ndata: [DONE]\n\n',
                                      kind="text/event-stream")
                return self.reply(b'data: {"choices":[{"delta":{"content":"xin chao"},"finish_reason":"stop"}]}\n\n'
                                  b'data: [DONE]\n\n', kind="text/event-stream")
            if body["messages"][-1]["content"] == "limit":
                return self.reply({}, 429)
            return self.reply({"choices": [{"message": {"content": "xin chao", "role": "assistant"},
                                            "finish_reason": "stop"}]})
        if self.path in {"/v1/images/generations", "/openai/v1/images/generations"}:
            if "broken" in body["prompt"]:
                return self.reply({"data": []})
            return self.reply({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        self.reply({}, 404)


def headers(token):
    return {"Authorization": "Bearer " + token}


async def exercise(server):
    import app
    from chat_providers import ChatGPT2APISettings, WebGateway

    admin = headers("fixture-mac2api-key")
    legacy = app.store.create_client("Legacy", ["chatgpt"], rules={"chatgpt/text": {"accounts": ["old-profile"]}})
    legacy_headers = headers(legacy["api_key"])
    settings = {"base_url": f"http://127.0.0.1:{server.server_port}", "api_key": KEY}
    await verify(server, admin, settings, legacy, legacy_headers)
    restarted = WebGateway(app.CFG.data_dir, name="chatgpt2api", label="ChatGPT2API",
                           provider="chatgpt", settings=ChatGPT2APISettings)
    assert restarted.status()["ready"]
    assert (Path(app.CFG.data_dir) / "chatgpt2api.json").stat().st_mode & 0o777 == 0o600


async def verify(server, admin, settings, legacy, legacy_headers):
    import app
    pool_id = app.chatgpt2api.account_id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://127.0.0.1") as http:
        assert (await http.put(ENDPOINT, headers=legacy_headers, json=settings)).status_code == 401
        assert (await http.put(ENDPOINT, headers=admin, json={"base_url": "http://user:pass@localhost"})).status_code == 400
        assert (await http.put(ENDPOINT, headers=admin, json=settings | {"api_key": "wrong"})).status_code == 200
        assert (await http.post(ENDPOINT + "/test", headers=admin)).status_code == 502
        saved = await http.put(ENDPOINT, headers=admin, json=settings)
        assert saved.status_code == 200 and saved.json()["has_key"] and not saved.json()["ready"] and KEY not in saved.text
        tested = await http.post(ENDPOINT + "/test", headers=admin)
        assert tested.status_code == 200 and tested.json()["ready"], tested.text
        again = await http.put(ENDPOINT, headers=admin, json={"base_url": settings["base_url"] + "/v1/"})
        assert again.json()["ready"] and again.json()["has_key"] and KEY not in again.text
        configure = ENDPOINT + "/configure"
        assert (await http.post(configure, headers=legacy_headers, json=settings)).status_code == 401
        broken_draft = await http.post(configure, headers=admin, json=settings | {"api_key": "wrong"})
        assert broken_draft.status_code == 502
        saved = await http.get(ENDPOINT, headers=admin)
        assert saved.json()["ready"] and KEY not in saved.text
        catalog = (await http.get("/admin/client-keys", headers=admin)).json()["models"]
        assert next(m["accounts"] for m in catalog if m["id"] == "chatgpt/text")[0]["id"] == pool_id

        cap = (await http.get(SCOPED + "/capabilities", headers=legacy_headers)).json()
        assert not cap["models"][0]["ready"]
        denied = await http.post(SCOPED + "/chat/completions", headers=legacy_headers, json={"prompt": "denied"})
        assert denied.status_code == 403 and not server.calls
        grants = {f"chatgpt/{kind}": {"accounts": [pool_id]} for kind in ("text", "image")}
        updated = await http.patch("/admin/client-keys/" + legacy["id"], headers=admin, json={"rules": grants})
        assert updated.status_code == 200, updated.text
        cap = (await http.get(SCOPED + "/capabilities", headers=legacy_headers)).json()
        assert all(m["ready"] for m in cap["models"]) and len(cap["models"]) == 2
        text = SCOPED + "/chat/completions"
        messages = [{"role": "system", "content": "Vietnamese"}, {"role": "user", "content": "hello"}]
        result = await http.post(text, headers=legacy_headers, json={"messages": messages})
        assert result.status_code == 200 and result.json()["model"] == "chatgpt/text", result.text
        assert result.json()["choices"][0]["message"]["content"] == "xin chao"
        assert server.calls[-1] == ("/v1/chat/completions", {"model": "auto", "messages": messages, "stream": False})
        streamed = await http.post(text, headers=legacy_headers, json={"prompt": "hello", "stream": True})
        assert "xin chao" in streamed.text and "[DONE]" in streamed.text and "chatgpt/text" in streamed.text
        failed_stream = await http.post(text, headers=legacy_headers, json={"prompt": "broken stream", "stream": True})
        assert "DELIVERY_UNKNOWN" in failed_stream.text and "private upstream detail" not in failed_stream.text
        limited = await http.post(text, headers=legacy_headers, json={"prompt": "limit"})
        assert limited.status_code == 429, limited.text

        images = SCOPED + "/images/tasks"

        async def run_image(prompt, submission):
            request_headers = legacy_headers | {"Idempotency-Key": submission}
            accepted = await http.post(images, headers=request_headers, json={"prompt": prompt})
            assert accepted.status_code == 202, accepted.text
            replayed = await http.post(images, headers=request_headers, json={"prompt": prompt})
            assert accepted.json()["id"] == replayed.json()["id"]
            path = images + "/" + accepted.json()["id"]
            for _ in range(80):
                item = (await http.get(path, headers=legacy_headers)).json()
                if item["status"] not in {"queued", "processing"}:
                    return item
                await asyncio.sleep(0.05)
            raise AssertionError("Image task did not finish")

        image = await run_image("cat", "fixture-image")
        assert image["status"] == "completed", image
        result = await http.get(image["data"][0]["url"])
        assert result.content == PNG, result.text
        assert server.calls[-1][1]["response_format"] == "b64_json"
        assert sum("images" in path for path, _ in server.calls) == 1
        broken = await run_image("broken", "fixture-broken")
        assert broken["status"] == "interrupted" and broken["needs_reconcile"], broken


async def exercise_gemini(server):
    import app

    server.calls.clear()
    admin = headers("fixture-mac2api-key")
    path = "/admin/gemini-web2api"
    base = "/gemini/v1"
    text = base + "/chat/completions"
    host = f"http://127.0.0.1:{server.server_port}"
    settings = {"base_url": host + "/v1", "api_key": KEY,
                "text_model": "gemini-4-hydrogen", "image_model": "gemini-3.1-flash-image"}
    pool_id = app.gemini_web2api.account_id
    account = app.managed_web.create("gemini-web", "Fixture Chrome")
    app.managed_web.update(account, enabled=True, verified=True)
    chrome_id = account["id"]
    broad = headers(app.store.create_client("Gemini legacy", ["gemini-web"])["api_key"])

    def chrome(provider, prompt, *, image, allowed_accounts=None, **kwargs):
        assert provider == "gemini-web" and not image
        assert allowed_accounts() in (None, {chrome_id})
        return "via Chrome"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://127.0.0.1") as http:
        assert (await http.put(path, headers=broad, json=settings)).status_code == 401
        assert (await http.put(path, headers=admin, json=settings)).json()["prefer"] is False

        async def grant(label, accounts):
            rules = {f"gemini-web/{kind}": {"accounts": accounts} for kind in ("text", "image")}
            result = await http.post("/admin/client-keys", headers=admin, json={"label": label, "rules": rules})
            assert result.status_code == 201, result.text
            return headers(result.json()["api_key"])

        guest = await grant("Gemini gateway key", [pool_id])
        chrome_key = await grant("Chrome only", [chrome_id])
        mixed = await grant("Chrome and gateway", [pool_id, chrome_id])

        async def send(auth, **data):
            return await http.post(text, headers=auth, json={"prompt": "hello", **data})

        with patch.object(app, "_run_web_generation", side_effect=chrome) as browser:
            unavailable = await send(guest)
            assert unavailable.status_code == 503 and browser.call_count == 0 and not server.calls
            checked = await http.post(path + "/test", headers=admin)
            assert checked.status_code == 200 and checked.json()["ready"] and KEY not in checked.text, checked.text
            for auth in (broad, chrome_key):
                reply = await send(auth)
                assert reply.json()["choices"][0]["message"]["content"] == "via Chrome", reply.text
            reply = await send(mixed)
            assert reply.json()["model"] == "gemini-web/text" and browser.call_count == 2, reply.text

            preferred = await http.put(path, headers=admin, json=settings | {"prefer": True})
            assert preferred.json()["ready"] and preferred.json()["prefer"], preferred.text
            with patch.object(app.web_providers, "capability_states", side_effect=AssertionError("Probed Chrome")), \
                 patch.object(app.GEN_LOCK, "full", return_value=True):
                for auth in (guest, broad):
                    capabilities = await http.get(base + "/capabilities", headers=auth)
                    assert all(m["ready"] and not m["busy"] for m in capabilities.json()["models"])
            reply = await send(broad, stream=True)
            assert "xin chao" in reply.text and "gemini-web/text" in reply.text and "[DONE]" in reply.text
            assert server.calls[-1][1]["model"] == "gemini-4-hydrogen"
            reply = await send(chrome_key)
            assert reply.json()["choices"][0]["message"]["content"] == "via Chrome", reply.text

            result = await http.post(base + "/images/generations", headers=guest,
                                     json={"prompt": "cat", "response_format": "b64_json"})
            assert result.status_code == 200 and base64.b64decode(result.json()["data"][0]["b64_json"]) == PNG
            image_route, image_payload = server.calls[-1]
            assert image_route == "/v1/images/generations"
            assert image_payload["model"] == "gemini-3.1-flash-image"
            failed = await http.post(base + "/images/generations", headers=broad, json={"prompt": "broken"})
            assert failed.json()["error"]["code"] == "DELIVERY_UNKNOWN" and browser.call_count == 3

            removed = await http.put(path, headers=admin, json={})
            assert not removed.json()["configured"] and not removed.json()["prefer"]
            assert (await send(guest)).status_code == 503 and browser.call_count == 3
            reply = await send(broad)
            assert reply.json()["choices"][0]["message"]["content"] == "via Chrome", reply.text
            assert (await http.get(ENDPOINT, headers=admin)).json()["ready"]

            configured = await http.post(path + "/configure", headers=admin, json={"base_url": host})
            assert configured.status_code == 200, configured.text
            state = configured.json()
            assert state["ready"] and state["configured"] and not state["has_key"]
            assert state["base_url"] == host + "/openai/v1"
            assert state["text_model"] == state["image_model"] == "gemini-auto"
            selection = await http.put("/admin/connections/gemini-web", headers=admin, json={
                "mode": "external", "image_model": "gemini-2.5-flash-image",
            })
            assert selection.json()["mode"] == "external"
            assert (await send(broad)).json()["choices"][0]["message"]["content"] == "xin chao"
            assert server.calls[-1][0] == "/openai/v1/chat/completions"
            result = await http.post(base + "/images/generations", headers=guest,
                                     json={"prompt": "cat", "response_format": "b64_json"})
            assert result.status_code == 200 and base64.b64decode(result.json()["data"][0]["b64_json"]) == PNG
            assert server.calls[-1][0] == "/openai/v1/images/generations"
            assert browser.call_count == 4


async def exercise_official():
    import app
    import chat_providers

    admin = headers("fixture-mac2api-key")
    encoded = base64.b64encode(PNG).decode()
    requests = []
    real_async, real_sync = httpx.AsyncClient, httpx.Client

    def upstream(request):
        requests.append(request)
        path = request.url.path
        openai = request.url.host == "api.openai.com"
        assert openai or request.url.host == "generativelanguage.googleapis.com"
        if request.method == "GET":
            model = path.rsplit("/", 1)[1]
            authorized = (request.headers.get("Authorization") == "Bearer fixture-openai" if openai
                          else request.headers.get("x-goog-api-key") == "fixture-gemini")
            if not authorized:
                return httpx.Response(401)
            if model == "missing-image":
                return httpx.Response(404)
            return httpx.Response(200, json={"id": model} if openai else {"name": "models/" + model})
        body = json.loads(request.content)
        if path.endswith("/chat/completions"):
            assert request.headers["Authorization"] == ("Bearer fixture-openai" if openai else "Bearer fixture-gemini")
            assert body["model"] == ("gpt-fixture" if openai else "gemini-fixture")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "official"}}]})
        if openai:
            assert path == "/v1/images/generations" and body == {
                "model": "gpt-image-1.5", "prompt": "cat", "n": 1}
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        assert path == "/v1beta/models/gemini-2.5-flash-image:generateContent"
        assert request.headers["x-goog-api-key"] == "fixture-gemini"
        assert body["contents"][0]["parts"][0]["text"] == "cat"
        assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "cat"}, {"inlineData": {"mimeType": "image/png", "data": encoded}},
        ]}}]})

    transport = httpx.MockTransport(upstream)
    async with real_async(transport=httpx.ASGITransport(app=app.app), base_url="http://127.0.0.1") as http:
        with patch.object(chat_providers.httpx, "AsyncClient", lambda **kw: real_async(transport=transport, **kw)), \
             patch.object(chat_providers.httpx, "Client", lambda **kw: real_sync(transport=transport, **kw)), \
             patch.object(app, "_run_web_generation", side_effect=AssertionError("Unexpected Chrome dispatch")):
            for module, prefix, provider, image_model in (
                ("chatgpt", "chatgpt", "openai", "gpt-image-1.5"),
                ("gemini-web", "gemini", "gemini", "gemini-2.5-flash-image"),
            ):
                base = f"/{prefix}/v1"
                settings = f"/admin/connections/{module}"
                model = "gpt-fixture" if provider == "openai" else "gemini-fixture"
                configure = settings + "/official"
                config = {"model": model, "image_model": image_model, "api_key": f"fixture-{provider}"}
                previous = (await http.get(settings, headers=admin)).json()
                assert (await http.post(configure, json=config)).status_code == 401
                failed = await http.post(configure, headers=admin, json=config | {"api_key": "incorrect"})
                assert failed.status_code == 502
                assert (await http.get(settings, headers=admin)).json() == previous
                response = await http.post(configure, headers=admin, json=config)
                assert response.status_code == 200 and config["api_key"] not in response.text, response.text
                state = response.json()
                assert state["mode"] == "official" and state["official_configured"]
                assert state["official_text_model"] == model and state["image_model"] == image_model
                assert [x.url.path.rsplit("/", 1)[1] for x in requests[-2:]] == [model, image_model]
                failed = await http.post(configure, headers=admin, json={
                    "model": model, "image_model": "missing-image",
                })
                assert failed.status_code == 404
                assert (await http.get(settings, headers=admin)).json() == state
                env_name = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
                with patch.dict(os.environ, {env_name: config["api_key"]}):
                    assert (await http.post(configure, headers=admin, json=config)).status_code == 409
                    env = await http.post(configure, headers=admin, json={"model": model, "image_model": image_model})
                    assert env.status_code == 200 and env.json()["official_key_from_env"]
                catalog = (await http.get("/admin/client-keys", headers=admin)).json()["models"]
                text = next(entry for entry in catalog if entry["id"] == module + "/text")
                assert f"{provider}:api" in {item["id"] for item in text["accounts"]}
                new_key = await http.post("/admin/client-keys", headers=admin, json={
                    "label": "Official " + module,
                    "rules": {f"{module}/{kind}": {"accounts": [f"{provider}:api"]} for kind in ("text", "image")},
                })
                assert new_key.status_code == 201, new_key.text
                client = headers(new_key.json()["api_key"])
                assert (await http.get(settings, headers=client)).status_code == 401
                checked = await http.post(settings + "/test", headers=admin)
                assert checked.status_code == 200, checked.text
                assert [x.url.path.rsplit("/", 1)[1] for x in requests[-2:]] == [model, image_model]
                reply = await http.post(base + "/chat/completions", headers=client, json={"prompt": "hello"})
                assert reply.status_code == 200 and reply.json()["model"] == module + "/text", reply.text
                image = await http.post(base + "/images/generations", headers=client,
                                        json={"prompt": "cat", "response_format": "b64_json"})
                assert image.status_code == 200 and image.json()["data"][0]["b64_json"] == encoded, image.text

                changed = await http.put(settings, headers=admin, json={"mode": "official", "image_model": image_model})
                assert changed.json()["mode"] == "official" and f"fixture-{provider}" not in changed.text
                broad = headers(app.store.create_client(module + " default", [module])["api_key"])
                cap = (await http.get(base + "/capabilities", headers=broad)).json()
                assert len(cap["models"]) == 2 and all(entry["ready"] for entry in cap["models"])
                reply = await http.post(base + "/chat/completions", headers=broad, json={"prompt": "hello"})
                assert reply.status_code == 200, reply.text


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="web-gateway-fixture-") as root:
        os.environ.update(MUSE2API_HOME=root, MUSE2API_PROFILE_ROOT=root, MUSE2API_KEY="fixture-mac2api-key")
        import app

        with ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as server:
            server.calls = []
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                with patch.object(app, "_run_web_generation", side_effect=AssertionError("Unexpected Chrome path")):
                    asyncio.run(exercise(server))
                asyncio.run(exercise_gemini(server))
                asyncio.run(exercise_official())
            finally:
                server.shutdown()
    print("PASS: official ChatGPT and Gemini APIs, gateway compatibility, account isolation and media")
