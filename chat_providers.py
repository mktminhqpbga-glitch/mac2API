"""Official chat providers and self-hosted web gateways."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
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

    @staticmethod
    def _updated(saved, provider, settings):
        updated = {**saved.get(provider, {}), "model": settings.model}
        if "api_key" in settings.model_fields_set:
            key = settings.api_key.get_secret_value().strip() if settings.api_key else ""
            if key and not re.fullmatch(r"[!-~]+", key):
                raise HTTPException(400, "API Key phải gồm các ký tự ASCII và không chứa khoảng trắng.")
            if key:
                updated["api_key"] = key
            else:
                updated.pop("api_key", None)
        return updated

    def save(self, provider: ProviderName, settings: ProviderSettings):
        with self.lock:
            saved = self._read()
            saved[provider] = self._updated(saved, provider, settings)
            _write(str(self.path), saved)
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

    async def test(self, provider: ProviderName, *, model_override=None, trust_env=True):
        key, default_model = self._credentials(provider)
        model = model_override or default_model
        return await self._test_model(provider, key, model, trust_env=trust_env)

    @staticmethod
    async def _test_model(provider, key, model, *, trust_env=True):
        if not re.fullmatch(MODEL_ID, model):
            raise HTTPException(400, "Model mặc định không hợp lệ.")
        if provider == "openai":
            url = f"https://api.openai.com/v1/models/{model}"
            headers = {"Authorization": f"Bearer {key}"}
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            headers = {"x-goog-api-key": key}
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=trust_env) as client:
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


async def compatible_chat(url, key, model, payload, timeout, *, public_model=None, trust_env=True):
    body = {**payload, "model": model}
    body.pop("timeout", None)
    prompt = body.pop("prompt", None)
    if not body.get("messages"):
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, "Hãy nhập ít nhất một tin nhắn.")
        body["messages"] = [{"role": "user", "content": prompt.strip()}]

    client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), trust_env=trust_env)
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
            headers=_bearer_headers(key),
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
                                  "message": "Nhà cung cấp gặp lỗi sau khi nhận yêu cầu. Cần đối chiếu kết quả."})
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


class WebConnectionSettings(BaseModel):
    mode: Literal["browser", "official", "external"]
    image_model: str = Field(pattern=MODEL_ID)


class OfficialWebConnectionSettings(ProviderSettings):
    image_model: str = Field(pattern=MODEL_ID)


class OfficialWebConnection:
    def __init__(self, providers, provider, api_provider, image_model):
        self.providers = providers
        self.provider = provider
        self.api_provider = api_provider
        self.default_image_model = image_model
        self.label = "OpenAI API" if api_provider == "openai" else "Google Gemini API"
        self.account_id = f"{api_provider}:api"

    def settings(self):
        saved = self.providers._read().get("web_connections", {}).get(self.provider, {})
        return {"mode": saved.get("mode"), "image_model": saved.get("image_model", self.default_image_model)}

    def save(self, settings):
        with self.providers.lock:
            saved = self.providers._read()
            saved.setdefault("web_connections", {})[self.provider] = settings.model_dump()
            _write(str(self.providers.path), saved)

    def status(self):
        key, model = self.providers._settings(self.api_provider, self.providers._read())
        key_env = PROVIDERS[self.api_provider][1]
        return {"configured": bool(key), "ready": bool(key), "text_model": model,
                "key_from_env": bool(os.environ.get(key_env, "").strip())}

    async def configure(self, settings):
        previous = self.providers._read()
        previous_connection = previous.get("web_connections", {}).get(self.provider)
        provider = self.api_provider
        updated = self.providers._updated(previous, provider, settings)
        key_env = PROVIDERS[provider][1]
        env_key = os.environ.get(key_env, "").strip()
        if env_key and "api_key" in settings.model_fields_set:
            raise HTTPException(409, "Khóa đang được đặt trong cấu hình máy chủ. Hãy đổi khóa tại đó.")
        key = env_key or updated.get("api_key")
        if not key:
            raise HTTPException(400, f"Hãy nhập khóa của {self.label}.")
        for model in dict.fromkeys((settings.model, settings.image_model)):
            await self.providers._test_model(provider, key, model, trust_env=False)
        with self.providers.lock:
            saved = self.providers._read()
            if (saved.get(provider) != previous.get(provider)
                    or saved.get("web_connections", {}).get(self.provider) != previous_connection):
                raise HTTPException(409, "Cấu hình đã thay đổi. Hãy tải lại và kiểm tra lần nữa.")
            saved[provider] = updated
            saved.setdefault("web_connections", {})[self.provider] = {
                "mode": "official", "image_model": settings.image_model,
            }
            try:
                _write(str(self.providers.path), saved)
            except OSError as exc:
                raise HTTPException(507, "Không lưu được kết nối. Kiểm tra quyền ghi dữ liệu.") from exc

    async def test(self):
        await self.providers.test(self.api_provider, trust_env=False)
        await self.providers.test(self.api_provider, model_override=self.settings()["image_model"], trust_env=False)
        return {"ok": True}

    async def chat(self, messages, prompt, stream, timeout, *, before_send):
        key, model = self.providers._credentials(self.api_provider)
        before_send()
        return await compatible_chat(PROVIDERS[self.api_provider][4], key, model,
                                     {"messages": messages, "prompt": prompt, "stream": stream},
                                     timeout, public_model=f"{self.provider}/text", trust_env=False)

    def image(self, prompt, timeout, *, before_send):
        key, _ = self.providers._credentials(self.api_provider)
        model = self.settings()["image_model"]
        before_send()
        if self.api_provider == "openai":
            response = _post_image(OPENAI_IMAGES_URL, {"Authorization": f"Bearer {key}"},
                                   {"model": model, "prompt": prompt, "n": 1}, timeout, self.label, trust_env=False)
            return _decode_openai_image(response, self.label)
        response = _post_image(GEMINI_IMAGES_URL.format(model=model), {"x-goog-api-key": key}, {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }, timeout, self.label, trust_env=False)
        try:
            for candidate in response["candidates"]:
                for part in candidate["content"]["parts"]:
                    if inline := part.get("inlineData"):
                        return decode_web_image(inline["data"])
        except (KeyError, TypeError, AttributeError, WebProviderError):
            pass
        raise _image_unknown("Google Gemini đã phản hồi nhưng không trả ảnh hợp lệ.")


OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
GEMINI_IMAGES_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class ChatGPT2APISettings(WebGatewaySettings):
    text_model: str = Field(default="auto", pattern=MODEL_ID)
    image_model: str = Field(default="gpt-image-2.6", pattern=MODEL_ID)


class GeminiWeb2APISettings(WebGatewaySettings):
    text_model: str = Field(default="gemini-auto", pattern=MODEL_ID)
    image_model: str = Field(default="gemini-auto", pattern=MODEL_ID)
    prefer: bool = False


class WebGateway:
    def __init__(self, data_dir, *, name, label, provider, settings):
        self.path = os.path.join(data_dir, f"{name}.json")
        self.label = label
        self.provider = provider
        self.account_id = f"{name}:pool"
        self.key_required = provider == "chatgpt"
        self.root_path = "/openai/v1" if provider == "gemini-web" else "/v1"
        self.defaults = settings().model_dump()
        self.lock = Lock()

    def _read(self):
        saved = _read(self.path, {})
        return self.defaults | (saved if isinstance(saved, dict) else {})

    def status(self):
        saved = self._read()
        return {k: saved[k] for k in self.defaults if k != "api_key"} | {
            "has_key": bool(saved["api_key"]), "ready": bool(saved.get("verified")),
            "configured": self._configured(saved),
        }

    def _prepare(self, settings, saved):
        base_url = _base_url(settings.base_url, self.label, root_path=self.root_path)
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
        return updated

    def save(self, settings):
        with self.lock:
            updated = self._prepare(settings, self._read())
            _write(self.path, updated)
            return self.status()

    def _configured(self, settings):
        return bool(settings["base_url"] and (settings["api_key"] or not self.key_required))

    def _credentials(self):
        saved = self._read()
        if not self._configured(saved) or not saved.get("verified"):
            raise HTTPException(503, f"Hãy kiểm tra kết nối {self.label} trong mac2API trước khi gửi yêu cầu.")
        return saved

    async def test(self):
        saved = self._read()
        return await self._verify(saved, saved)

    async def configure(self, settings):
        saved = self._read()
        return await self._verify(saved, self._prepare(settings, saved))

    async def _verify(self, previous, candidate):
        if not candidate["base_url"]:
            raise HTTPException(400, f"Hãy nhập địa chỉ {self.label}.")
        if self.key_required and not candidate["api_key"]:
            raise HTTPException(400, f"Hãy nhập khóa của {self.label}.")
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.get(candidate["base_url"] + "/models",
                                            headers=_bearer_headers(candidate["api_key"]))
                _check_response(response)
                data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError
            models = {item.get("id") for item in data["data"]
                      if isinstance(item, dict) and isinstance(item.get("id"), str)}
            missing = [model for model in dict.fromkeys((candidate["text_model"], candidate["image_model"]))
                       if model not in models]
            if missing:
                raise HTTPException(400, f"{self.label} chưa có mô hình đã chọn: " + ", ".join(missing))
        except httpx.RequestError as exc:
            raise _connection_error(exc) from None
        except ValueError:
            raise HTTPException(502, f"Địa chỉ chưa trả về danh sách mô hình {self.label} hợp lệ.") from None
        with self.lock:
            if previous != self._read():
                raise HTTPException(409, "Cấu hình đã thay đổi. Hãy kiểm tra lại.")
            _write(self.path, candidate | {"verified": True})
            return self.status()

    async def chat(self, messages, prompt, stream, timeout, *, before_send):
        saved = self._credentials()
        before_send()
        return await compatible_chat(saved["base_url"] + "/chat/completions", saved["api_key"],
                                     saved["text_model"], {"messages": messages, "prompt": prompt, "stream": stream},
                                     timeout, public_model=f"{self.provider}/text", trust_env=False)

    def image(self, prompt, timeout, *, before_send):
        saved = self._credentials()
        payload = {"model": saved["image_model"], "prompt": prompt, "n": 1, "response_format": "b64_json"}
        before_send()
        response = _post_image(saved["base_url"] + "/images/generations", _bearer_headers(saved["api_key"]),
                               payload, timeout, self.label, trust_env=False)
        return _decode_openai_image(response, self.label)


def _post_image(url, headers, payload, timeout, label, *, trust_env=True):
    limit = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 65536
    deadline = time.monotonic() + timeout
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10), trust_env=trust_env) as client:
            with client.stream("POST", url, json=payload, headers=headers) as response:
                _check_response(response, submitted=True)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise _image_unknown(f"Hết thời gian chờ ảnh từ {label}.", status=504)
                    if len(body) + len(chunk) > limit:
                        raise _image_unknown(f"Ảnh do {label} trả về vượt quá 20 MiB.")
                    body.extend(chunk)
        return json.loads(body)
    except httpx.RequestError as exc:
        raise _connection_error(exc, submitted=True) from None
    except ValueError:
        raise _image_unknown(f"{label} đã phản hồi nhưng không đọc được kết quả.") from None


def _decode_openai_image(response, label):
    try:
        return decode_web_image(response["data"][0]["b64_json"])
    except (KeyError, IndexError, TypeError, WebProviderError):
        raise _image_unknown(f"{label} đã phản hồi nhưng không đọc được tệp ảnh.") from None


def _image_unknown(message, *, status=502):
    return HTTPException(status, {"code": "DELIVERY_UNKNOWN", "message": message + " Cần đối chiếu kết quả."})


def _bearer_headers(key):
    return {"Authorization": f"Bearer {key}"} if key else {}


def _base_url(value, label, *, root_path):
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if (parts.scheme in {"http", "https"} and parts.hostname and parts.port != 0
                and parts.username is None and parts.password is None and not parts.query and not parts.fragment
                and re.fullmatch(r"[^\s\\?#]+", value)):
            if not parts.path:
                return value + root_path
            return value if parts.path.endswith("/v1") else value + "/v1"
    except ValueError:
        pass
    raise HTTPException(400, f"Nhập địa chỉ {label} bắt đầu bằng http:// hoặc https://.")
