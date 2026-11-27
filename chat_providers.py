"""Official chat providers and self-hosted web gateways."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Lock
import time
from typing import Literal
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from starlette.background import BackgroundTask

from store import _read, _write
from web_providers import MAX_IMAGE_BYTES, WebProviderError, decode_web_image


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
        return await compatible_chat(PROVIDERS[provider][4], key, model, payload, timeout)


async def compatible_chat(url, key, model, payload, timeout, *, public_model=None):
    body = {**payload, "model": model}
    body.pop("timeout", None)
    prompt = body.pop("prompt", None)
    if not body.get("messages"):
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, "Hãy nhập ít nhất một tin nhắn.")
        body["messages"] = [{"role": "user", "content": prompt.strip()}]

    client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), trust_env=public_model is None)
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
            "POST", url, json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
        response = await client.send(request, stream=True)
        _check_response(response, submitted=public_model is not None)
        if body.get("stream"):
            if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                raise HTTPException(502, "Nhà cung cấp không trả về luồng SSE.")

            async def chunks():
                try:
                    source = _gateway_chunks(response, public_model) if public_model else response.aiter_bytes()
                    async for chunk in source:
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
        if public_model:
            data["model"] = public_model
        return JSONResponse(data)
    except httpx.RequestError as exc:
        raise _connection_error(exc, submitted=True) from None
    except ValueError:
        raise HTTPException(502, "Phản hồi chat từ nhà cung cấp không hợp lệ.") from None
    finally:
        if not streaming:
            await close()


async def _gateway_chunks(response, model):
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            yield b"data: [DONE]\n\n"
            return
        try:
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError
        except ValueError:
            break
        if "error" in event:
            break
        event["model"] = model
        yield ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
    yield (b'data: {"error":{"message":"Web gateway stream interrupted",'
           b'"type":"server_error","code":"DELIVERY_UNKNOWN"}}\n\n')


def _check_response(response: httpx.Response, *, submitted=False):
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
    if submitted and (status == 408 or status >= 500):
        raise HTTPException(502, {"code": "DELIVERY_UNKNOWN",
                                  "message": "Dịch vụ web gặp lỗi sau khi nhận yêu cầu. Cần đối chiếu kết quả."})
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


class WebGatewaySettings(BaseModel):
    base_url: str = Field(default="", max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    text_model: str = Field(pattern=MODEL_ID)
    image_model: str = Field(pattern=MODEL_ID)


class ChatGPT2APISettings(WebGatewaySettings):
    text_model: str = Field(default="auto", pattern=MODEL_ID)
    image_model: str = Field(default="gpt-image-2.6", pattern=MODEL_ID)


class GeminiWeb2APISettings(WebGatewaySettings):
    text_model: str = Field(default="gemini-4-hydrogen", pattern=MODEL_ID)
    image_model: str = Field(default="gemini-3.1-flash-image", pattern=MODEL_ID)
    prefer: bool = False


class WebGateway:
    def __init__(self, data_dir, *, name, label, provider, settings):
        self.path = os.path.join(data_dir, f"{name}.json")
        self.label = label
        self.provider = provider
        self.account_id = f"{name}:pool"
        self.defaults = settings().model_dump()
        self.lock = Lock()

    def _read(self):
        saved = _read(self.path, {})
        return self.defaults | (saved if isinstance(saved, dict) else {})

    def status(self):
        saved = self._read()
        return {k: saved[k] for k in self.defaults if k != "api_key"} | {
            "has_key": bool(saved["api_key"]), "ready": bool(saved.get("verified")),
            "configured": bool(saved["base_url"] and saved["api_key"]),
        }

    def save(self, settings):
        base_url = _base_url(settings.base_url, self.label)
        with self.lock:
            saved = self._read()
            updated = settings.model_dump(exclude={"api_key"}) | {"base_url": base_url}
            key = saved["api_key"] if base_url and base_url == saved["base_url"] else ""
            if "api_key" in settings.model_fields_set:
                key = settings.api_key.get_secret_value().strip() if settings.api_key and base_url else ""
                if key and not re.fullmatch(r"[!-~]+", key):
                    raise HTTPException(400, f"Khóa {self.label} phải dùng ký tự ASCII và không chứa khoảng trắng.")
            updated["api_key"] = key
            updated["verified"] = bool(saved.get("verified")) and all(
                saved[k] == updated[k] for k in ("base_url", "api_key", "text_model", "image_model")
            )
            _write(self.path, updated)
            return self.status()

    def _credentials(self, *, verified=True):
        saved = self._read()
        if not (saved["base_url"] and saved["api_key"]):
            raise HTTPException(503, f"Chưa có địa chỉ hoặc khóa {self.label}. Hãy thêm trong bảng quản trị mac2API.")
        if verified and not saved.get("verified"):
            raise HTTPException(503, f"Hãy kiểm tra kết nối {self.label} trong mac2API trước khi gửi yêu cầu.")
        return saved

    async def test(self):
        saved = self._credentials(verified=False)
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.get(saved["base_url"] + "/models", headers=_headers(saved))
                _check_response(response)
                data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError
            models = {item.get("id") for item in data["data"]
                      if isinstance(item, dict) and isinstance(item.get("id"), str)}
            missing = [saved[k] for k in ("text_model", "image_model") if saved[k] not in models]
            if missing:
                raise HTTPException(400, f"{self.label} chưa có mô hình đã chọn: " + ", ".join(missing))
        except httpx.RequestError as exc:
            raise _connection_error(exc) from None
        except ValueError:
            raise HTTPException(502, f"Địa chỉ chưa trả về danh sách mô hình {self.label} hợp lệ.") from None
        with self.lock:
            if saved != self._read():
                raise HTTPException(409, "Cấu hình đã thay đổi. Hãy kiểm tra lại.")
            _write(self.path, saved | {"verified": True})
            return self.status()

    async def chat(self, messages, prompt, stream, timeout, *, before_send):
        saved = self._credentials()
        before_send()
        return await compatible_chat(saved["base_url"] + "/chat/completions", saved["api_key"],
                                     saved["text_model"], {"messages": messages, "prompt": prompt, "stream": stream},
                                     timeout, public_model=f"{self.provider}/text")

    def image(self, prompt, timeout, *, before_send):
        saved = self._credentials()
        payload = {"model": saved["image_model"], "prompt": prompt, "n": 1, "response_format": "b64_json"}
        limit = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 65536
        before_send()
        deadline = time.monotonic() + timeout
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10), trust_env=False) as client:
                with client.stream("POST", saved["base_url"] + "/images/generations",
                                   json=payload, headers=_headers(saved)) as response:
                    _check_response(response, submitted=True)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise _image_unknown(f"Hết thời gian chờ ảnh từ {self.label}.", status=504)
                        if len(body) + len(chunk) > limit:
                            raise _image_unknown(f"Ảnh do {self.label} trả về vượt quá 20 MiB.")
                        body.extend(chunk)
            data = json.loads(body)
            return decode_web_image(data["data"][0]["b64_json"])
        except httpx.RequestError as exc:
            raise _connection_error(exc, submitted=True) from None
        except (ValueError, KeyError, IndexError, TypeError, WebProviderError):
            raise _image_unknown(f"{self.label} đã phản hồi nhưng không đọc được tệp ảnh.") from None


def _image_unknown(message, *, status=502):
    return HTTPException(status, {"code": "DELIVERY_UNKNOWN", "message": message + " Cần đối chiếu kết quả."})


def _headers(settings):
    return {"Authorization": f"Bearer {settings['api_key']}"}


def _base_url(value, label):
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if (parts.scheme in {"http", "https"} and parts.hostname and parts.port != 0
                and parts.username is None and parts.password is None and not parts.query and not parts.fragment
                and re.fullmatch(r"[^\s\\?#]+", value)):
            return value if parts.path.endswith("/v1") else value + "/v1"
    except ValueError:
        pass
    raise HTTPException(400, f"Nhập địa chỉ {label} bắt đầu bằng http:// hoặc https://.")
