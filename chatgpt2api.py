"""Connect mac2API to the operator's ChatGPT2API server."""
from __future__ import annotations

import json
import os
import re
from threading import Lock
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr

from chat_providers import MODEL_ID, _check_response, _connection_error, compatible_chat
from store import _read, _write
from web_providers import MAX_IMAGE_BYTES, WebProviderError, decode_web_image

POOL_ID = "chatgpt2api:pool"
POOL_LABEL = "ChatGPT2API"


class ChatGPT2APISettings(BaseModel):
    base_url: str = Field(default="", max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    text_model: str = Field(default="auto", pattern=MODEL_ID)
    image_model: str = Field(default="gpt-image-2.6", pattern=MODEL_ID)


class ChatGPT2API:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "chatgpt2api.json")
        self.lock = Lock()

    def _read(self):
        saved = _read(self.path, {})
        return ChatGPT2APISettings().model_dump() | (saved if isinstance(saved, dict) else {})

    def status(self):
        saved = self._read()
        return {k: saved[k] for k in ("base_url", "text_model", "image_model")} | {
            "has_key": bool(saved["api_key"]), "ready": bool(saved.get("verified")),
            "configured": bool(saved["base_url"] and saved["api_key"]),
        }

    def save(self, settings):
        base_url = _base_url(settings.base_url)
        with self.lock:
            saved = self._read()
            updated = settings.model_dump(exclude={"api_key"}) | {"base_url": base_url}
            key = saved["api_key"] if base_url and base_url == saved["base_url"] else ""
            if "api_key" in settings.model_fields_set:
                key = settings.api_key.get_secret_value().strip() if settings.api_key and base_url else ""
                if key and not re.fullmatch(r"[!-~]+", key):
                    raise HTTPException(400, "Khóa ChatGPT2API không được chứa khoảng trắng hoặc ký tự đặc biệt ngoài ASCII.")
            updated["api_key"] = key
            updated["verified"] = bool(saved.get("verified")) and all(saved[k] == v for k, v in updated.items())
            _write(self.path, updated)
            return self.status()

    def _credentials(self, *, verified=True):
        saved = self._read()
        if not (saved["base_url"] and saved["api_key"]):
            raise HTTPException(503, "Chưa có địa chỉ hoặc khóa ChatGPT2API. Mở tab ChatGPT trong mac2API để thêm.")
        if verified and not saved.get("verified"):
            raise HTTPException(503, "Hãy bấm Kiểm tra kết nối ở tab ChatGPT trước khi gửi yêu cầu.")
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
                raise HTTPException(400, "ChatGPT2API chưa có mô hình đã chọn: " + ", ".join(missing))
        except httpx.RequestError as exc:
            raise _connection_error(exc) from None
        except ValueError:
            raise HTTPException(502, "Địa chỉ chưa trả về danh sách mô hình ChatGPT2API hợp lệ.") from None
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
                                     timeout, public_model="chatgpt/text")

    def image(self, prompt, timeout, *, before_send):
        saved = self._credentials()
        payload = {"model": saved["image_model"], "prompt": prompt, "n": 1, "response_format": "b64_json"}
        limit = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 65536
        before_send()
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10), trust_env=False) as client:
                with client.stream("POST", saved["base_url"] + "/images/generations",
                                   json=payload, headers=_headers(saved)) as response:
                    _check_response(response, submitted=True)
                    body = bytearray()
                    for chunk in response.iter_bytes(64 * 1024):
                        if len(body) + len(chunk) > limit:
                            raise _image_unknown("Ảnh do ChatGPT2API trả về vượt quá 20 MiB.")
                        body.extend(chunk)
            data = json.loads(body)
            return decode_web_image(data["data"][0]["b64_json"])
        except httpx.RequestError as exc:
            raise _connection_error(exc, submitted=True) from None
        except (ValueError, KeyError, IndexError, TypeError, WebProviderError):
            raise _image_unknown("ChatGPT2API đã phản hồi nhưng không đọc được tệp ảnh.") from None


def _image_unknown(message):
    return HTTPException(502, {"code": "DELIVERY_UNKNOWN", "message": message + " Cần đối chiếu kết quả."})


def _headers(settings):
    return {"Authorization": f"Bearer {settings['api_key']}"}


def _base_url(value):
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
    raise HTTPException(400, "Nhập địa chỉ ChatGPT2API bắt đầu bằng http:// hoặc https://.")
