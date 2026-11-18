"""Exercise provider settings, routing and SSE against mock official APIs."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class EventStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"Xin chao"}}]}\n\n'
        yield b'data: [DONE]\n\n'


async def run():
    import app
    import chat_providers

    real_client = httpx.AsyncClient
    app.CFG.api_key = "test-gateway-key"
    auth = {"Authorization": "Bearer test-gateway-key"}
    calls = []

    async def upstream(request):
        calls.append(request)
        host = request.url.host
        is_openai = host == "api.openai.com"
        assert host in ("api.openai.com", "generativelanguage.googleapis.com")
        if request.method == "GET":
            if is_openai:
                assert request.headers["Authorization"] == "Bearer openai-test-key"
                assert request.url.path == "/v1/models/gpt-test"
                return httpx.Response(200, json={"id": "gpt-test"})
            assert request.headers["x-goog-api-key"] == "gemini-test-key"
            return httpx.Response(200, json={"name": "models/gemini-test"})
        body = json.loads(request.content)
        assert request.headers["Authorization"] == (
            "Bearer openai-test-key" if is_openai else "Bearer gemini-test-key"
        )
        expected_path = "/v1/chat/completions" if is_openai else "/v1beta/openai/chat/completions"
        assert request.url.path == expected_path
        assert body["messages"] == [{"role": "user", "content": "Xin chao"}]
        assert "timeout" not in body
        if body["model"] == "over-quota":
            return httpx.Response(429, content=b"secret upstream detail", headers={"Retry-After": "4"})
        if body["stream"]:
            assert body["model"] == "gemini-test"
            return httpx.Response(200, stream=EventStream(), headers={"Content-Type": "text/event-stream"})
        assert body["model"] == "gpt-test"
        assert body["max_completion_tokens"] == 42
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Xin chao"}}]})

    def mock_client(*args, **kwargs):
        return real_client(*args, **kwargs, transport=httpx.MockTransport(upstream))

    async with real_client(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        with patch.object(chat_providers.httpx, "AsyncClient", mock_client):
            response = await client.get("/admin/chat-providers")
            assert response.status_code == 401
            initial = await client.get("/v1/models", headers=auth)
            assert len(initial.json()["data"]) == 6
            legacy = await client.post("/v1/chat/completions", headers=auth, json={
                "model": "gpt-4o", "messages": [{"role": "user", "content": "Xin chao"}],
            })
            assert legacy.status_code == 400 and not calls
            for provider, model in (("openai", "gpt-test"), ("gemini", "gemini-test")):
                path = f"/admin/chat-providers/{provider}"
                response = await client.put(path, headers=auth, json={"model": model, "api_key": f"{provider}-test-key"})
                assert response.status_code == 200 and response.json()["configured"]
                assert f"{provider}-test-key" not in response.text
                response = await client.post(path + "/test", headers=auth)
                assert response.json() == {"ok": True, "model_id": f"{provider}/{model}"}
            assert app.chat_providers.path.stat().st_mode & 0o777 == 0o600
            models = await client.get("/v1/models", headers=auth)
            assert {m["id"] for m in models.json()["data"]} >= {"openai/gpt-test", "gemini/gemini-test"}
            url = "/v1/chat/completions"
            payload = {"model": "openai/gpt-test", "prompt": "Xin chao", "timeout": 10,
                       "max_completion_tokens": 42}
            response = await client.post(url, headers=auth, json=payload)
            assert response.json()["choices"][0]["message"]["content"] == "Xin chao"
            response = await client.post(url, headers=auth, json={
                "model": "gemini/gemini-test", "prompt": "Xin chao", "stream": True,
            })
            assert response.headers["Content-Type"].startswith("text/event-stream")
            assert '"content":"Xin chao"' in response.text and "data: [DONE]" in response.text
            response = await client.post(url, headers=auth, json={**payload, "model": "openai/over-quota"})
            assert response.status_code == 429 and response.headers["Retry-After"] == "4"
            assert "secret upstream detail" not in response.text
            response = await client.post("/v1/responses", headers=auth, json={"model": "openai/gpt-test", "input": "test"})
            assert response.status_code == 400
            response = await client.put("/admin/chat-providers/openai", headers=auth, json={"model": "gpt-test"})
            assert response.json()["configured"]
            response = await client.put("/admin/chat-providers/openai", headers=auth, json={"model": "gpt-test", "api_key": None})
            assert not response.json()["configured"]
            response = await client.post(url, headers=auth, json=payload)
            assert response.status_code == 503
            assert len(calls) == 5
    print("PASS: independent settings, authorization, model checks, legacy routing, chat, SSE and upstream errors")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as path:
        with patch.dict(os.environ, {"MUSE2API_HOME": path, "MUSE2API_PROFILE_ROOT": path,
                                     "OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}):
            asyncio.run(run())
