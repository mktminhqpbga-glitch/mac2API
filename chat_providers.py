"""Official OpenAI and Gemini connections for the chat completions gateway."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Literal

import anyio
import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from starlette.background import BackgroundTask


ProviderName = Literal["openai", "gemini"]
MODEL_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
PROVIDERS = {
    "openai": ("OpenAI (ChatGPT)", "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4.1-mini",
               "https://api.openai.com/v1/chat/completions"),
    "gemini": ("Google Gemini", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-2.5-flash",
               "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
}


class ProviderSettings(BaseModel):
    model: str = Field(pattern=MODEL_ID)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class ChatProviders:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "chat-providers.json"
        self.lock = Lock()

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _settings(self, provider: ProviderName, saved):
        _, key_env, model_env, default_model, _ = PROVIDERS[provider]
        saved = saved.get(provider, {})
        key = os.environ.get(key_env, "").strip() or saved.get("api_key", "")
        model = saved.get("model") or os.environ.get(model_env) or default_model
        return key, model

    def status(self):
        saved = self._read()
        result = []
        for provider, (label, key_env, _, _, _) in PROVIDERS.items():
            key, model = self._settings(provider, saved)
            result.append({"id": provider, "label": label, "configured": bool(key),
                           "model": model, "model_id": f"{provider}/{model}",
                           "key_from_env": bool(os.environ.get(key_env, "").strip())})
        return result

    def save(self, provider: ProviderName, settings: ProviderSettings):
        with self.lock:
            saved = self._read()
            current = saved.setdefault(provider, {})
            current["model"] = settings.model
            if "api_key" in settings.model_fields_set:
                key = settings.api_key.get_secret_value().strip() if settings.api_key else ""
                if key and not re.fullmatch(r"[!-~]+", key):
                    raise HTTPException(400, "API Key phải gồm các ký tự ASCII và không chứa khoảng trắng.")
                if key:
                    current["api_key"] = key
                else:
                    current.pop("api_key", None)

            temporary = None
            try:
                with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                        prefix=".chat-providers-", delete=False) as file:
                    temporary = Path(file.name)
                    json.dump(saved, file)
                temporary.replace(self.path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return next(item for item in self.status() if item["id"] == provider)

    @staticmethod
    def route(model: str):
        provider, separator, model_id = model.partition("/")
        if not separator or provider not in PROVIDERS:
            return None
        if not re.fullmatch(MODEL_ID, model_id):
            raise HTTPException(400, "Model không hợp lệ. Dùng openai/<model> hoặc gemini/<model>.")
        return provider, model_id

    def _credentials(self, provider: ProviderName):
        key, model = self._settings(provider, self._read())
        if not key:
            raise HTTPException(503, f"Chưa cấu hình API Key cho {PROVIDERS[provider][0]}.")
        return key, model

    async def test(self, provider: ProviderName):
        key, model = self._credentials(provider)
        if not re.fullmatch(MODEL_ID, model):
            raise HTTPException(400, "Model mặc định không hợp lệ.")
        if provider == "openai":
            url = f"https://api.openai.com/v1/models/{model}"
            headers = {"Authorization": f"Bearer {key}"}
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            headers = {"x-goog-api-key": key}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, headers=headers)
                _check_response(response)
                data = response.json()
                identity = data.get("id" if provider == "openai" else "name") if isinstance(data, dict) else None
                expected = model if provider == "openai" else f"models/{model}"
                if identity != expected:
                    raise HTTPException(502, "Phản hồi kiểm tra model không hợp lệ.")
        except httpx.RequestError as exc:
            raise _connection_error(exc) from None
        except ValueError:
            raise HTTPException(502, "Phản hồi kiểm tra model không hợp lệ.") from None
        return {"ok": True, "model_id": f"{provider}/{model}"}

    async def chat(self, provider: ProviderName, model: str, payload: dict, timeout: int):
        key, _ = self._credentials(provider)
        body = {**payload, "model": model}
        body.pop("timeout", None)
        prompt = body.pop("prompt", None)
        if not body.get("messages"):
            if not isinstance(prompt, str) or not prompt.strip():
                raise HTTPException(400, "Hãy nhập ít nhất một tin nhắn.")
            body["messages"] = [{"role": "user", "content": prompt.strip()}]

        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10))
        response = None
        streaming = False

        async def close():
            with anyio.CancelScope(shield=True):
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await client.aclose()

        try:
            request = client.build_request(
                "POST", PROVIDERS[provider][4], json=body,
                headers={"Authorization": f"Bearer {key}"},
            )
            response = await client.send(request, stream=True)
            _check_response(response)
            if body.get("stream"):
                if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                    raise HTTPException(502, "Nhà cung cấp không trả về luồng SSE.")

                async def chunks():
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    except httpx.RequestError:
                        yield (b'\n\ndata: {"error":{"message":"Upstream stream interrupted",'
                               b'"type":"server_error","code":"DELIVERY_UNKNOWN"}}\n\n')
                    finally:
                        await close()
                stream = StreamingResponse(
                    chunks(), media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    background=BackgroundTask(close),
                )
                streaming = True
                return stream
            await response.aread()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
                raise HTTPException(502, "Phản hồi chat từ nhà cung cấp không hợp lệ.")
            return JSONResponse(data)
        except httpx.RequestError as exc:
            raise _connection_error(exc, submitted=True) from None
        except ValueError:
            raise HTTPException(502, "Phản hồi chat từ nhà cung cấp không hợp lệ.") from None
        finally:
            if not streaming:
                await close()


def _check_response(response: httpx.Response):
    if response.is_success:
        return
    status = response.status_code
    if status in (401, 403):
        raise HTTPException(502, f"Nhà cung cấp từ chối API Key hoặc quyền truy cập (HTTP {status}).")
    if status in (400, 404, 422):
        raise HTTPException(status, f"Nhà cung cấp từ chối yêu cầu hoặc model (HTTP {status}).")
    if status == 429:
        retry_after = response.headers.get("Retry-After", "")
        headers = {"Retry-After": retry_after} if retry_after.isascii() and retry_after.isdigit() else None
        raise HTTPException(status, "Đã đạt giới hạn yêu cầu hoặc hạn mức của nhà cung cấp.", headers=headers)
    raise HTTPException(502, f"Nhà cung cấp trả lỗi HTTP {status}.")


def _connection_error(exc: httpx.RequestError, *, submitted=False):
    if submitted and isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                                      httpx.WriteError, httpx.RemoteProtocolError)):
        return HTTPException(504 if isinstance(exc, httpx.TimeoutException) else 502,
                             {"code": "DELIVERY_UNKNOWN",
                              "message": "Mất kết nối với nhà cung cấp sau khi bắt đầu gửi. Cần đối chiếu kết quả."})
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, "Hết thời gian chờ nhà cung cấp.")
    return HTTPException(502, "Không kết nối được với nhà cung cấp. Hãy kiểm tra kết nối mạng của máy chủ.")
