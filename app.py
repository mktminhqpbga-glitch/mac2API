"""muse2api — 把 Muse(muse.ai) 网页免费账号的对话/生图/生视频额度反代成 API。

OpenAI 兼容:
  POST /v1/chat/completions          （文本 / 代码对话，支持 stream，可接 Codex）
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

管理（前端账号池管理页面在 GET /）:
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            （单条 / 批量文本 / 批量数组）
  PATCH  /admin/accounts/{id}       （改标签、启用/禁用）
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  （真实打开 muse.ai 验证会话是否有效）
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               （任务记录）
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
  GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           （浏览器扩展 zip，用来取 cookie）
  GET    /admin/cookie-helper       （命令行取 cookie 脚本，进阶）
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

import asyncio
import copy
from contextlib import contextmanager
import base64
import collections
import binascii
import io
import ipaddress
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import zipfile

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import model_validator, BaseModel, Field, SecretStr, field_validator

from chat_providers import ChatProviders, ProviderName, ProviderSettings
from config import CFG
from engine import (ESSENTIAL_COOKIES, MAX_REFERENCE_IMAGE_BYTES,
                    MAX_REFERENCE_IMAGES, MAX_REFERENCE_TOTAL_BYTES,
                    REFERENCE_MIME_EXT, MuseAuthError, MuseEngine, MuseGenerationError,
                    MuseModelRefusalError, MusePostSendError, RefImageSafetyError, reference_kind)
from engine import MusePageUnavailable, MuseProxyError, MuseSessionUnavailable
from store import Store, account_expiry, min_expiry
from generation_timing import QueuePolicy, QueueSettings, TaskNotQueued, TaskTiming
from proxy import normalize_server, request_proxy_context, split_proxy_url
from media_storage import CAPACITY_BYTES, CLEANUP_BYTES, delete_media_file, media_entries, media_owner, new_media_name, save_media
from web_providers import DEFAULT_WEB_CDP_PORT, PROVIDERS as WEB_PROVIDERS, WebPostSendError, WebProviderError, WebProviders
from thumbnails import (VIDEO_FORMATS, ThumbnailUnavailable,
                        source_version, video_thumbnail)

import sys
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
log = logging.getLogger("muse2api")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

def _read_local_version() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "version.json"), encoding="utf-8") as file:
            metadata = json.load(file)
        if isinstance(metadata, dict):
            return metadata
    except (OSError, ValueError):
        pass
    return {"version": "1.5.0", "highlights": []}


CFG.ensure_dirs()
app = FastAPI(title="muse2api", version=str(_read_local_version().get("version") or "unknown"))

# Cookie 助手脚本从 muse.ai 页面发起导入请求，需要放行该来源；
# 浏览器扩展从 chrome-extension:// 发起，也一并放行。
#
_origins = [o.strip() for o in (CFG.cors_origins or "").split(",") if o.strip() not in ("", "*")]
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware,
                   allow_origins=_origins,
                   allow_origin_regex=r"chrome-extension://[a-p]{32}",
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)


@app.middleware("http")
async def protect_bearer_transport(request: Request, call_next):
    sensitive = "authorization" in request.headers or "access" in request.query_params
    if sensitive and request.url.scheme != "https":
        try:
            loopback = ipaddress.ip_address(request.client.host if request.client else "").is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return JSONResponse(
                {"error": {"code": "HTTPS_REQUIRED", "message": "Kết nối từ xa cần HTTPS."}},
                status_code=403, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    if request.url.path.startswith(("/admin/apikey", "/admin/client-keys", "/admin/api-clients")):
        response.headers["Cache-Control"] = "no-store"
    return response


class AccessLogFilter(logging.Filter):
    """Keep signed media query strings out of Uvicorn access logs."""

    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, protocol, status = record.args
            if isinstance(target, str) and "?" in target:
                record.args = client, method, target.split("?", 1)[0], protocol, status
        return True


logging.getLogger("uvicorn.access").addFilter(AccessLogFilter())

store = Store(CFG)
chat_providers = ChatProviders(CFG.data_dir)
if CFG.web_cdp_port and (not 1 <= CFG.web_cdp_port <= 65535 or CFG.web_cdp_port == CFG.cdp_port):
    raise ValueError("MUSE2API_WEB_CDP_PORT must be separate from the Muse CDP port")
web_port = CFG.web_cdp_port
if not web_port and CFG.cdp_port != DEFAULT_WEB_CDP_PORT and store.web_chrome_enabled():
    web_port = DEFAULT_WEB_CDP_PORT
web_providers = WebProviders(web_port, profile_preferred=store.web_chrome_profile_preferred())
MODULES = {f"muse/{kind}": ("muse", kind) for kind in ("text", "image", "video")}
MODULES.update({f"{provider}/{kind}": (provider, kind)
                for provider in WEB_PROVIDERS for kind in ("text", "image")})


PROVIDER_PREFIXES = {"muse": "muse", "chatgpt": "chatgpt", "gemini": "gemini-web"}
MUSE_MODELS_BY_KIND = {"text": "muse-spark", "image": "muse-image", "video": "muse-video"}


def _path_provider(request: Request | None) -> str | None:
    if request is None:
        return None
    scope = getattr(request, "scope", {})
    route = scope.get("route")
    path = route.path if route else scope.get("path", "")
    prefix, _, rest = path.lstrip("/").partition("/")
    return PROVIDER_PREFIXES.get(prefix) if rest.startswith("v1/") else None


def _scoped_model(request: Request | None, kind: str, model: str | None) -> str | None:
    provider = _path_provider(request)
    if provider is None:
        return model
    default = f"{provider}/{kind}"
    if model is None:
        return default
    if model == default or (provider == "muse" and model in (
        MUSE_MODELS_BY_KIND[kind], f"muse/{MUSE_MODELS_BY_KIND[kind]}"
    )):
        return model
    raise HTTPException(400, f"Base URL này chỉ nhận mô-đun {default} cho loại yêu cầu này.")


def _scope_request_model(request: Request | None, kind: str, req):
    if _path_provider(request) is None:
        return req
    model = req.model if "model" in req.model_fields_set else None
    return req.model_copy(update={"model": _scoped_model(request, kind, model)})


def _module_provider(model: str, kind: str) -> str | None:
    route = MODULES.get(model)
    if route is None:
        if model.partition("/")[0] in WEB_PROVIDERS and "/" in model:
            raise HTTPException(404, "Không tìm thấy mô-đun web đã chọn.")
        return None
    provider, expected = route
    if expected != kind:
        raise HTTPException(400, "Mô-đun không phù hợp với loại tác vụ ở endpoint này.")
    return provider if provider in WEB_PROVIDERS else None


def _web_call(fn, *args, on_acquired=None, on_queue=None, queue_guard=None, **kwargs):
    queued_at = time.monotonic()
    acquired = _wait_for_generation_slot(QUEUE_POLICY.settings()["queue_timeout"], queue_guard)
    try:
        if on_queue:
            on_queue(time.monotonic() - queued_at)
        if not acquired:
            raise _backpressure("QUEUE_TIMEOUT", "Hết thời gian chờ khe xử lý.", 1)
        if on_acquired:
            on_acquired()
        return fn(*args, **kwargs)
    except WebProviderError as error:
        raise HTTPException(error.status, str(error)) from error
    finally:
        if acquired:
            GEN_LOCK.release()


def _run_web_image(provider, prompt, timeout, **callbacks):
    data, ext = _web_call(web_providers.generate, provider, prompt, image=True,
                          timeout=timeout, **callbacks)
    name = new_media_name(ext)
    try:
        path, size = save_media(CFG.media_dir, name, data=data)
    except (OSError, ValueError) as exc:
        raise HTTPException(507, "Không lưu được ảnh vào kho media.") from exc
    return {"filename": name, "path": path, "size": size, "kind": "image"}


def _image_module(req):
    provider = _module_provider(req.model, "image")
    if provider and (req.n != 1 or _image_references(req)):
        raise HTTPException(400, "Mô-đun web hiện tạo một ảnh từ văn bản và chưa nhận ảnh tham chiếu.")
    return provider


def _web_image_prompt(req):
    lines = [req.prompt]
    if req.aspect_ratio or req.size:
        lines.append(f"Kích thước hoặc tỷ lệ mong muốn: {req.aspect_ratio or req.size}.")
    if req.extra:
        lines.append(req.extra)
    return "\n".join(lines)
engine = MuseEngine(CFG)
engine.proxy_resolver = lambda aid: (store.get_account(aid) or {}).get("proxy")


class ConcurrencyLock:
    """多账号并发调度锁。
    由 CFG.max_concurrency 限制最大并发槽位；
    同时提供与标准 Lock 兼容的 acquire / release / locked 接口，
    确保向下兼容历史单锁断言与管理端操作。"""
    def __init__(self, limit: int = 2):
        self.limit = max(1, limit)
        self._sem = threading.BoundedSemaphore(self.limit)
        self._count_lock = threading.Lock()
        self._active_count = 0
        self._idle_since = time.monotonic()

    def acquire(self, blocking: bool = True, timeout: float | None = -1) -> bool:
        if timeout is None:
            timeout = -1
        if timeout < 0:
            ok = self._sem.acquire(blocking=blocking)
        else:
            ok = self._sem.acquire(blocking=blocking, timeout=timeout)
        if ok:
            with self._count_lock:
                self._active_count += 1
        return ok

    def release(self, *, touch_idle=True):
        with self._count_lock:
            if not self._active_count:
                raise RuntimeError("Không có khe xử lý đang được giữ.")
            self._active_count -= 1
            if touch_idle:
                self._idle_since = time.monotonic()
        self._sem.release()

    def stop_browser_if_idle(self, browser, idle_seconds: int, has_queued_work) -> bool:
        # Holding the admission lock through stop prevents a new lease from using
        # Chromium while it is being shut down.
        with self._count_lock:
            if (not idle_seconds or self._active_count
                    or time.monotonic() - self._idle_since < idle_seconds
                    or browser.proc is None or has_queued_work()):
                return False
            browser.stop()
            return True

    def locked(self) -> bool:
        with self._count_lock:
            return self._active_count > 0

    def full(self) -> bool:
        with self._count_lock:
            return self._active_count >= self.limit

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountPoolManager:
    """账号并发冲突管理器：确保同一账号不会同时被两个并发任务操作，多账号则并行执行。"""
    def __init__(self, store_instance):
        self.store = store_instance
        self.busy_accounts: set[str] = set()
        self.cond = threading.Condition()

    def acquire(self, account_id: str | None = None, preferred_id: str | None = None,
                rotate: bool = True, force_rotate: bool = False,
                timeout: float | None = 60.0, record_use: bool = True, queue_guard=None) -> dict:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while True:
                if queue_guard:
                    queue_guard()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise MuseGenerationError("等待空闲账号超时，当前所有账号均处于繁忙中")

                if account_id:
                    acc = self.store.get_account(account_id)
                    if not acc or not acc.get("enabled", True):
                        raise MuseAuthError(f"指定账号 {account_id} 不存在或已禁用")
                    if account_id not in self.busy_accounts:
                        self.busy_accounts.add(account_id)
                        return acc
                else:
                    pref = preferred_id if preferred_id not in self.busy_accounts else None
                    acc = self.store.pick_account(
                        preferred_id=pref,
                        rotate=rotate,
                        force_rotate=force_rotate,
                        exclude_ids=self.busy_accounts,
                        record_use=record_use,
                    )
                    if acc:
                        self.busy_accounts.add(acc["id"])
                        return acc

                live = [a for a in self.store.accounts if a.get("enabled", True) and a.get("cookies")]
                if not live:
                    raise MuseAuthError("没有可用账号，请先在管理页面导入 cookie")

                self.cond.wait(timeout=0.5 if remaining is None else min(remaining, 0.5))

    def release(self, account_id: str | None):
        if not account_id:
            return
        with self.cond:
            self.busy_accounts.discard(account_id)
            self.cond.notify_all()


GEN_LOCK = ConcurrencyLock(limit=CFG.max_concurrency)
ACCOUNT_POOL = AccountPoolManager(store)
QUEUE_POLICY = QueuePolicy(os.path.join(CFG.data_dir, "generation-queue.json"))


def _wait_for_generation_slot(timeout, guard):
    if not guard:
        return GEN_LOCK.acquire(timeout=timeout or None)
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        guard()
        remaining = deadline - time.monotonic() if deadline is not None else 0.25
        if remaining <= 0:
            return False
        if GEN_LOCK.acquire(timeout=min(remaining, 0.25)):
            return True


def _bind_account_proxy(account: dict):
    """Pin the selected account's route for the whole operation, including retries."""
    if hasattr(engine, "_ensure_local"):
        engine._ensure_local().proxy_snapshot = copy.deepcopy(account.get("proxy")) or None


def _clear_account_proxy():
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            del local.proxy_snapshot


def _account_proxy(account: dict):
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            return local.proxy_snapshot
    return account.get("proxy")


@contextmanager
def _account_operation(aid: str, *, wait_seconds: float = 0):
    """Maintenance and settings share the same account lease as generation."""
    with ACCOUNT_POOL.cond:
        account = store.get_account(aid)
        if not account:
            raise HTTPException(404, "账号不存在")
        if not ACCOUNT_POOL.cond.wait_for(
            lambda: aid not in ACCOUNT_POOL.busy_accounts, timeout=wait_seconds
        ):
            raise HTTPException(409, "账号正在执行任务或检测，请稍后再试")
        account = store.get_account(aid)
        if not account:
            raise HTTPException(404, "账号不存在")
        ACCOUNT_POOL.busy_accounts.add(aid)
        account = copy.deepcopy(account)
    try:
        _bind_account_proxy(account)
        yield account
    finally:
        try:
            engine.close_current_page()
        finally:
            _clear_account_proxy()
            ACCOUNT_POOL.release(aid)


# ------------------------- OpenAI 风格的错误响应 -------------------------
# 各类智能体基本都按 OpenAI 的 {"error": {"message": ...}} 取错误信息；
# FastAPI 默认返回的是 {"detail": ...}，客户端会读不到原因、只显示"未知错误"。
# 所以 /v1/* 统一转成 OpenAI 格式，管理接口保持原样（前端依赖 detail）。
def _err_type(status: int) -> str:
    if status in (404, 410):
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/") or _path_provider(request):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        headers = dict(exc.headers or {})
        if exc.status_code == 429 and not any(key.lower() == "retry-after" for key in headers):
            headers["Retry-After"] = "2"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code, **detail}},
            headers=headers)
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    # Never echo user-supplied proxy credentials in validation errors.
    errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")}
              for error in exc.errors()]
    if request.url.path.startswith("/v1/") or _path_provider(request):
        return JSONResponse(status_code=422, content={"error": {
            "message": "请求参数校验失败：" + str(errors)[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": errors})

MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark —— 文本 / 代码对话（网页免费额度，支持流式）"},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image —— 文生图 / 图像编辑（网页免费额度）"},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video —— 文生视频 / 图生视频（网页免费额度）"},
]

# 下游（Codex / Cline / 各种客户端）习惯按 OpenAI、Anthropic 的名字传模型，
# 这里统一映射到 muse 的真实能力上。
#
# 说明：muse.ai 网页是自动路由的 agent，对外**没有可枚举的模型清单**，
# 能稳定调用的就是三条真实能力 —— Muse Spark（语言/代码）、
# Muse Image（生图）、Muse Video（生视频）。别名只是让下游不用改配置。
MODEL_ALIASES = {
    # ---- 文本 / 代码 → muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- 生图 → muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- 生视频 → muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, n or default)


# ------------------------- 鉴权 -------------------------
def _bearer_token(authorization: str | None):
    parts = authorization.split(None, 1) if authorization else []
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _is_admin(token: str) -> bool:
    return bool(CFG.api_key and token and hmac.compare_digest(token.encode(), CFG.api_key.encode()))


def auth(authorization: str | None = Header(default=None)):
    token = _bearer_token(authorization)
    if _is_admin(token) or (not CFG.api_key and not token):
        return True
    if not token:
        raise HTTPException(401, "缺少 Authorization: Bearer <key>")
    raise HTTPException(401, "API key 无效")


CLIENT_REQUESTS_PER_MINUTE = 20
CLIENT_ACTIVE_POST_LIMIT = 2
CLIENT_PENDING_MEDIA_LIMIT = 2
KIND_PENDING_MEDIA_LIMIT = 8
MEDIA_ADMISSION_LOCK = threading.Lock()
CLIENT_MEDIA_PATTERN = re.compile(r"^client-([0-9a-f]{16})--")
NO_STORE = {"Cache-Control": "no-store"}


def _backpressure(code, message, retry_after):
    return HTTPException(429, {"code": code, "message": message}, headers={"Retry-After": str(retry_after)})


def contract_auth(request: Request, authorization: str | None = Header(default=None)):
    token = _bearer_token(authorization)
    if _is_admin(token):
        raise HTTPException(403, "Endpoint này cần khóa người dùng m2c_.", headers=NO_STORE)
    client = store.find_client(token)
    if not client:
        raise HTTPException(401, "API Key không hợp lệ hoặc đã bị thu hồi.", headers=NO_STORE)
    scope = _path_provider(request)
    if scope and scope not in client["providers"]:
        raise HTTPException(403, "Khóa API không có quyền dùng Base URL này.", headers=NO_STORE)
    return client


async def api_auth(request: Request, authorization: str | None = Header(default=None)):
    """Return the restricted client, or None for the existing administrator workflow."""
    token = _bearer_token(authorization)
    if _is_admin(token) or (not CFG.api_key and not token):
        yield None
        return
    client = store.find_client(token)
    if not client:
        raise HTTPException(401, "API Key không hợp lệ hoặc đã bị thu hồi.")
    provider = _path_provider(request)
    if provider and provider not in client["providers"]:
        raise HTTPException(403, "Khóa API này không có quyền dùng nhà cung cấp ở Base URL này.")
    is_write = request.method == "POST"
    if is_write and not store.acquire_client_request(
        client["id"], CLIENT_REQUESTS_PER_MINUTE, CLIENT_ACTIVE_POST_LIMIT
    ):
        raise _backpressure("CLIENT_RATE_LIMIT", "Khóa API đã đạt giới hạn yêu cầu hoặc đang có yêu cầu xử lý.", 60)
    try:
        yield client
    finally:
        if is_write:
            store.release_client_request(client["id"])


def _authorize_model(client, model: str, kind: str):
    if not isinstance(client, dict):
        return
    module = MODULES.get(model)
    official = chat_providers.route(model)
    if module:
        provider = module[0]
    elif official and kind == "text":
        provider = official[0]
    elif model in (MUSE_MODELS_BY_KIND[kind], f"muse/{MUSE_MODELS_BY_KIND[kind]}"):
        provider = "muse"
    else:
        raise HTTPException(400, "Hãy chỉ định model có trong danh sách /models của khóa API này.")
    if provider not in client["providers"]:
        raise HTTPException(403, "Khóa API này không có quyền dùng mô-đun đã chọn.")


def _client_id(client):
    return client.get("id") if isinstance(client, dict) else None


def _generate_owned(owner, fn, *args, **kwargs):
    token = media_owner.set(owner)
    try:
        return fn(*args, **kwargs)
    finally:
        media_owner.reset(token)


def _require_task_owner(task, client):
    owner = _client_id(client)
    if owner and task.get("owner") != owner:
        raise HTTPException(404, "Không tìm thấy tác vụ.")


def _check_media_admission(tasks, kind, owner):
    active = [task for task in tasks if task.get("status") in ("queued", "processing")]
    if sum(task.get("kind") == kind for task in active) >= KIND_PENDING_MEDIA_LIMIT:
        raise _backpressure("QUEUE_FULL", "Hàng đợi tạo nội dung đã đầy.", 2)
    if owner and sum(task.get("owner") == owner for task in active) >= CLIENT_PENDING_MEDIA_LIMIT:
        raise _backpressure("CLIENT_PENDING_LIMIT", "Khóa API đang có tối đa 2 tác vụ media chờ hoặc đang chạy.", 2)


def _public_task(task, client):
    data = dict(task)
    if _client_id(client):
        allowed = {"id", "object", "kind", "provider", "prompt", "status", "progress", "created_at",
                   "updated_at", "error", "last_error", "elapsed", "queue_elapsed", "total_elapsed",
                   "result", "size", "response_format", "url", "video", "needs_reconcile", "error_code"}
        data = {key: value for key, value in data.items() if key in allowed}
    result = data.get("result")
    if isinstance(result, dict) and result.get("filename"):
        url = media_url(result["filename"], admin=not _client_id(client))
        data.update(result={**result, "url": url}, url=url)
        if data.get("kind") == "video":
            data["video"] = {"url": url}
    return data


# ------------------------- 请求模型 -------------------------

def _persist_session_cookies(acc_id: str, cookies: dict, cookies_exp: dict | None) -> None:
    """Merge cookies rotated by muse.ai into storage without confirming the session."""
    acc = store.get_account(acc_id)
    if not acc or not cookies:
        return
    merged = {**(acc.get("cookies") or {}), **{k: v for k, v in cookies.items() if v}}
    exps = {**(acc.get("cookies_exp") or {}),
            **{k: v for k, v in (cookies_exp or {}).items() if v}}
    if merged == (acc.get("cookies") or {}) and exps == (acc.get("cookies_exp") or {}):
        return
    # synced_at / last_keepalive mean "session confirmed"; rotation alone does not.
    store.update_account(acc_id, cookies=merged, cookies_exp=exps)


engine.cookie_sink = _persist_session_cookies


def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """调用 /api/session 续签账号 cookie 并写回 store，返回最新账号 dict。
    近期（10分钟内）已续签且状态正常的账号直接复用，避免每次请求阻塞 2~3 秒 HTTP 往返。"""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm,
                                        **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"会话正常 (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except MuseProxyError:
        raise
    except MuseSessionUnavailable as exc:
        _persist_session_cookies(acc_id, exc.cookies, exc.cookies_exp)
        # The page load right after must not renew again with the pre-rotation cookie.
        engine._last_http_renew[acc_id] = now
        log.warning("HTTP 预续签账号 %s 未分配工作区%s: %s", acc_id,
                    "（已保存轮换后的 cookie）" if exc.rotated else "", exc)
    except Exception as exc:
        log.warning("HTTP 预续签账号 %s 异常: %s", acc_id, type(exc).__name__)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):
    """Acquire a generation slot before the account lease; release both after cleanup."""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        leased_id = None
        slot_acquired = False
        try:
            slot_acquired = GEN_LOCK.acquire(timeout=timeout)
            if not slot_acquired:
                q.put(("error", MuseGenerationError("等待对话并发槽位超时")))
                return
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    if leased_id:
                        engine.close_current_page()
                        ACCOUNT_POOL.release(leased_id)
                        leased_id = None
                    try:
                        alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)
                    except Exception:
                        break
                    if not alt or alt["id"] == cur_id:
                        if alt:
                            leased_id = alt["id"]
                        break
                    cur_id = alt["id"]
                    leased_id = cur_id
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("【对话自动切号】切换到备用账号 %s (%s) 重试...", alt.get("label"), cur_id)
                elif cur_id:
                    ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)
                    leased_id = cur_id

                yielded = False
                try:
                    if cur_id:
                        _bind_account_proxy(store.get_account(cur_id) or {})
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    if stop_event.is_set():
                        return
                    engine.start()
                    for chunk in engine.chat_stream(
                        cur_cookies, prompt, cur_exp, timeout,
                        account_id=cur_id, stop_event=stop_event
                    ):
                        yielded = True
                        q.put(("data", chunk))
                        if stop_event.is_set():
                            return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MusePostSendError as exc:
                    last_exc = exc
                    break
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    engine.close_current_page()
                    if yielded:
                        break
                except MusePageUnavailable as exc:
                    last_exc = exc
                    if cur_id:
                        store.touch_keepalive(cur_id, None, f"Lỗi tác vụ: {exc}")
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                q.put(("error", last_exc))
        except Exception as exc:
            q.put(("error", exc))
        finally:
            try:
                engine.close_current_page()
            except Exception as exc:
                log.warning("对话清理失败: %s", type(exc).__name__)
            finally:
                try:
                    _clear_account_proxy()
                finally:
                    try:
                        if leased_id:
                            ACCOUNT_POOL.release(leased_id)
                    finally:
                        if slot_acquired:
                            GEN_LOCK.release()
                        q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    duration: int | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = None
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    image_url: Any = None
    reference_image: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions 请求。

    对第三方客户端要尽量宽松：不认识字段一律忽略（Pydantic 默认行为），
    只挑我们真正用得上的读 —— 否则各种智能体各传各的参数就会 422。
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # 兼容把 prompt 直接放顶层的客户端
    # 下面这些声明出来只是为了「能读到」，muse.ai 端不做对应处理
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API（新版 Codex 默认走这个）。"""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountProxy(BaseModel):
    server: str
    username: str = ""
    password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_proxy_url(cls, value):
        return split_proxy_url(value) if isinstance(value, dict) else value

    @field_validator("server")
    @classmethod
    def validate_server(cls, value):
        return normalize_server(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if ":" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Proxy username cannot contain a colon or control characters")
        try:
            value.encode("latin-1")
        except UnicodeError:
            raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        if value is not None:
            try:
                value.get_secret_value().encode("latin-1")
            except UnicodeError:
                raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    def configuration(self, previous=None):
        previous = previous or {}
        password = self.password.get_secret_value() if self.password is not None else None
        if password is None:
            if previous.get("server") == self.server and previous.get("username", "") == self.username:
                password = previous.get("password", "")
            elif self.username:
                raise HTTPException(400, "Enter the proxy password when changing its server or username")
            else:
                password = ""
        return {"server": self.server, "username": self.username, "password": password}


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # 多行文本，每行一个账号
    expires: dict[str, int] = Field(default_factory=dict)   # cookie 名 -> 过期时间戳
    proxy: AccountProxy | None = None


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    proxy: AccountProxy | None = None


class ProxyTestRequest(BaseModel):
    proxy: AccountProxy


# ------------------------- 提示词构造 -------------------------
def _image_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        candidate = (value.get("image_url") or value.get("video_url") or value.get("audio_url")
                     or value.get("file_url") or value.get("url") or value.get("b64_json")
                     or value.get("data"))
        if isinstance(candidate, dict):
            candidate = candidate.get("url")
        return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
    return None


def _reference_list(images: list | None, legacy: list[Any], field: str,
                    extra: dict[str, list | None] | None = None) -> list[str]:
    """Validate ordered references and make legacy precedence explicit.

    顺序：images（或旧版单图字段）在前，其后依次是 videos / audios / files。
    images 本身也可以混放视频、音频与文档，类型在预处理时按 MIME/文件头识别。
    """
    primary = next((ref for value in legacy if (ref := _image_value(value))), None)
    if primary and images:
        raise HTTPException(400, f"{field} 不能与旧版单图字段同时使用")
    groups: list[tuple[str, list]] = []
    if primary:
        groups.append((field, [primary]))
    elif images is not None:
        groups.append((field, images))
    for name, values in (extra or {}).items():
        if values is not None:
            if not isinstance(values, list):
                raise HTTPException(400, f"{name} 必须是数组")
            groups.append((name, values))
    total = sum(len(values) for _, values in groups)
    if total > MAX_REFERENCE_IMAGES:
        raise HTTPException(400, f"参考素材最多 {MAX_REFERENCE_IMAGES} 个（当前 {total} 个）")
    refs = []
    for name, values in groups:
        for index, value in enumerate(values):
            ref = _image_value(value)
            if not ref:
                raise HTTPException(400, f"{name}[{index}] 不是有效素材引用")
            refs.append(ref)
    return refs


def _extra_media(req) -> dict[str, list | None]:
    return {"videos": req.videos, "audios": req.audios, "files": req.files}


def _image_references(req: ImageRequest) -> list[str]:
    return _reference_list(req.images, [req.reference_image, req.image], "images",
                           _extra_media(req))


def _video_references(req: VideoRequest) -> list[str]:
    return _reference_list(
        req.images, [req.reference_image, req.image_url, req.image], "images",
        _extra_media(req))


def _reference_kinds(prepared: list[str]) -> list[str]:
    """预处理后的 data URL -> image/video/audio/document 列表（保持顺序）。"""
    return [reference_kind(ref[5:].split(";", 1)[0]) for ref in prepared]


MusePromptLanguage = Literal["vi", "en", "zh"]


class MusePromptSettings(BaseModel):
    language: MusePromptLanguage


_MUSE_PROMPT_LABELS = {
    "vi": {"image": "ảnh", "video": "video", "audio": "âm thanh", "document": "tài liệu"},
    "en": {"image": "image", "video": "video", "audio": "audio clip", "document": "document"},
}

_MUSE_MEDIA_DELIVERY = {
    "vi": "Dùng chức năng tạo {kind} của Muse ngay trong lượt này và đính kèm tệp {kind} kết quả. "
          "Khi mô tả đủ để thực hiện, hãy tạo trực tiếp, đừng trình phương án hoặc xin duyệt. "
          "Nếu có chú thích hoặc cần báo lý do chưa thể tạo, dùng tiếng Việt.",
    "en": "Use Muse's {kind} generation in this turn and attach the resulting {kind} file. "
          "When the description is sufficient, generate it now without a proposal or approval step. "
          "Use English for any caption or explanation if generation is unavailable.",
    "zh": "请在本轮使用 Muse 的生成能力直接生成并附上{kind}成品。"
          "描述充分时直接生成，无需提交方案或请求确认。若无法生成，请说明原因。",
}


def _media_prompt_localized(r, refs, kinds, language, *, video=False):
    """Build the translated media instructions around the caller's original description."""
    vi = language == "vi"
    labels = _MUSE_PROMPT_LABELS[language]
    parts = []
    if refs:
        if video:
            parts.append("Tạo video chỉ từ các tệp tham chiếu vừa tải lên và mô tả bên dưới."
                         if vi else "Create a video using only the newly uploaded references and the description below.")
        else:
            parts.append("Tạo hoặc chỉnh sửa một ảnh chỉ từ các tệp tham chiếu vừa tải lên và mô tả bên dưới."
                         if vi else "Generate or edit one image using only the newly uploaded references and the description below.")
        parts.append("Dùng từng tệp theo vai trò trong mô tả. Không dùng tệp từ cuộc trò chuyện trước."
                     if vi else "Use each file as specified in the description. Do not use files from earlier conversations.")
        seen = collections.Counter()
        for index, kind in enumerate(kinds, 1):
            seen[kind] += 1
            old_token = f"[{_KIND_TOKENS[kind]}{seen[kind]}]"
            if old_token in r.prompt:
                parts.append(f"{old_token} là tệp đính kèm số {index} ({labels[kind]})."
                             if vi else f"{old_token} is attachment {index} ({labels[kind]}).")
            else:
                parts.append(f"Tệp đính kèm số {index}: {labels[kind]}."
                             if vi else f"Attachment {index}: {labels[kind]}.")
        if video and kinds == ["image"]:
            parts.append("Dùng đúng ảnh vừa tải lên làm khung hình mở đầu."
                         if vi else "Use the newly uploaded image as the opening frame.")
        elif video and all(kind == "image" for kind in kinds):
            parts.append("Dùng ảnh đầu tiên làm khung hình mở đầu nếu mô tả không chỉ định khác. Dùng các ảnh còn lại để tham chiếu chủ thể, bối cảnh và phong cách."
                         if vi else "Use the first image as the opening frame unless specified otherwise. Use other images as subject, scene and style references.")
        if not video and "image" in kinds:
            parts.append("Ảnh kết quả không có chữ, hình mờ, chữ ký, nhãn góc hoặc logo từ ảnh tham chiếu."
                         if vi else "Return a clean image without text, watermarks, signatures, corner labels or logos from the references.")
    elif video:
        parts.append("Tạo một video mới từ mô tả văn bản. Không dùng ảnh hoặc ngữ cảnh cũ."
                     if vi else "Create a new video from the text description. Do not use historical images or context.")
    else:
        parts.append("Tạo một ảnh mới từ mô tả văn bản. Không tìm ảnh cũ hoặc yêu cầu ảnh gốc."
                     if vi else "Create one new image from the text description. Do not look up older images or ask for an original image.")

    size = (r.size or "").strip().lower()
    ratio = (r.aspect_ratio or "").strip().lower()
    if any(value in ratio or value in size for value in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920")):
        orientation = ("Khung hình dọc 9:16 phủ kín màn hình, cao hơn rộng, không nằm ngang."
                       if vi else "Use a full-screen vertical 9:16 frame, taller than wide, never landscape.")
        parts.append(orientation)
        if video:
            if (r.resolution or "").strip().lower() in ("", "720", "720p", "720x1280"):
                parts.append("Dùng khung 720x1280, không có viền đen." if vi else "Use a 720x1280 frame without black bars.")
            else:
                parts.append("Khung hình không có viền đen." if vi else "Use a frame without black bars.")
    elif any(value in ratio or value in size for value in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        parts.append("Dùng khung hình ngang 16:9." if vi else "Use a widescreen 16:9 landscape frame.")
    elif video and ratio:
        parts.append(f"Dùng tỷ lệ {r.aspect_ratio}." if vi else f"Use a {r.aspect_ratio} aspect ratio.")
    elif not video:
        if any(value in ratio or value in size for value in ("1:1", "square", "正方形", "1024x1024")):
            parts.append("Dùng khung hình vuông 1:1." if vi else "Use a square 1:1 frame.")
        elif any(value in ratio or value in size for value in ("4:3", "4/3")):
            parts.append("Dùng tỷ lệ 4:3." if vi else "Use a 4:3 aspect ratio.")
        elif any(value in ratio or value in size for value in ("3:4", "3/4")):
            parts.append("Dùng khung hình dọc 3:4." if vi else "Use a 3:4 portrait frame.")
        elif ratio or size:
            shape = (r.aspect_ratio or r.size)
            parts.append(f"Kích thước hoặc tỷ lệ: {shape}." if vi else f"Size or aspect ratio: {shape}.")
    if video:
        duration = r.duration or 6
        parts.append(f"Thời lượng chính xác {duration} giây." if vi else f"Duration must be exactly {duration} seconds.")
        if r.resolution:
            parts.append(f"Độ phân giải: {r.resolution}." if vi else f"Resolution: {r.resolution}.")
    parts.append(r.prompt)
    if r.extra:
        parts.append(r.extra)
    parts.append(_MUSE_MEDIA_DELIVERY[language].format(kind=labels["video" if video else "image"]))
    return "\n".join(parts)


_KIND_LABELS = {"image": ("张", "图片"), "video": ("段", "视频"),
                "audio": ("段", "音频"), "document": ("份", "文档")}


def _describe_kinds(kinds: list[str]) -> str:
    counts = collections.Counter(kinds)
    return "、".join(f"{counts[k]} {_KIND_LABELS[k][0]}{_KIND_LABELS[k][1]}"
                    for k in ("image", "video", "audio", "document") if counts[k])


# 工作台提示词用 [图N] / [视频N] / [音频N] 指代素材，按同类顺序编号（与上传顺序一致）。
# muse 不认识这些标记，所以把「标记 -> 第几个附件」写进提示词。
_KIND_TOKENS = {"image": "图", "video": "视频", "audio": "音频", "document": "文档"}
_REFERENCE_TOKEN = re.compile(r"\[(?:图|视频|音频|文档)\d+\]")


def _reference_legend(kinds: list[str]) -> str:
    seen: collections.Counter = collections.Counter()
    parts = []
    for index, kind in enumerate(kinds, 1):
        seen[kind] += 1
        parts.append(f"[{_KIND_TOKENS[kind]}{seen[kind]}] 是第 {index} 个附件（{_KIND_LABELS[kind][1]}）")
    return "附件对照：" + "，".join(parts)

def build_image_prompt(r: ImageRequest, kinds: list[str] | None = None, *, language: MusePromptLanguage = "zh") -> str:
    refs = _image_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if language != "zh":
        return _media_prompt_localized(r, refs, kinds, language)
    if has_ref and any(k != "image" for k in kinds):
        parts = [f"基于我本次上传附带的参考素材（{_describe_kinds(kinds)}；{_reference_legend(kinds)}；"
                 f"只使用本次上传的这些附件，各附件的用途以下面的描述为准）"
                 f"进行生图/编辑，输出一张图片：{r.prompt.strip()}"]
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(r.prompt):
        parts = [f"基于我本次上传附带的参考图片（{_reference_legend(kinds)}）进行生图/编辑：{r.prompt.strip()}"]
    elif has_ref:
        parts = [f"基于我本次上传附带的参考图片进行生图/编辑：{r.prompt.strip()}"]
    else:
        parts = [f"全新文生图创作（当前未提供任何参考图，请勿查找历史相册或向用户索要原图，直接根据文字描述从零绘制生成一张全新图片）：{r.prompt.strip()}"]
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()

    if any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920")):
        parts.append("【画面构图与比例要求】：严格 9:16 竖屏满屏画幅（9:16 vertical portrait aspect ratio，高大于宽的手机全屏竖版画面），绝对不要生成横屏，保持垂直构图")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        parts.append("【画面构图与比例要求】：16:9 宽屏横屏画幅（16:9 widescreen landscape aspect ratio）")
    elif any(k in ar or k in sz for k in ("1:1", "square", "正方形", "1024x1024")):
        parts.append("【画面构图与比例要求】：1:1 正方形画幅（1:1 square aspect ratio）")
    elif any(k in ar or k in sz for k in ("4:3", "4/3")):
        parts.append("【画面构图与比例要求】：4:3 比例画幅")
    elif any(k in ar or k in sz for k in ("3:4", "3/4")):
        parts.append("【画面构图与比例要求】：3:4 竖向画幅")
    elif r.aspect_ratio:
        parts.append(f"【画面构图与比例要求】：{r.aspect_ratio} 画面比例")
    elif r.size:
        parts.append(f"尺寸/比例：{r.size}")

    if has_ref and "image" in kinds:
        parts.append("【纯净画面要求】：彻底清除并去除参考图中的所有文字、水印、签名、角标及Logo标记（clean image without any watermark, text, or logo），输出绝对纯净无字画面")

    if r.extra:
        parts.append(r.extra)
    parts.append(_MUSE_MEDIA_DELIVERY["zh"].format(kind="图片"))
    return "，".join(parts)


def _content_text(content, language: MusePromptLanguage = "zh") -> str:
    """把 OpenAI 的 content 归一成纯文本（兼容多模态 list 形式）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url":
                    parts.append({"vi": "[Ảnh]", "en": "[Image]", "zh": "[图片]"}[language])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def build_chat_prompt(messages: list[ChatMessage], *, language: MusePromptLanguage = "zh") -> str:
    """把 messages 拼成发给 muse.ai 的一段提示词。

    muse.ai 网页本身是个带上下文的会话，但本 API 是无状态的（每次可能落到
    不同账号/页面），所以把历史拼进 prompt 最可控 —— 这正好匹配 Codex 这类
    「每轮都带全量历史」的客户端。
    """
    context, assistant, user, tool_result, tool_request = {
        "vi": ("Bối cảnh và yêu cầu", "Trợ lý", "Người dùng", "[Kết quả công cụ]", "[Yêu cầu gọi công cụ]"),
        "en": ("Context and task instructions", "Assistant", "User", "[Tool result]", "[Tool call requested]"),
        "zh": ("背景与任务设定", "助手", "用户", "【工具执行结果】", "【请求调用工具】"),
    }[language]
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content, language).strip()
        if role == "tool":
            # 工具执行结果 → 当成"用户提供的信息"发过去
            if text:
                turns.append(("user", tool_result + "\n" + text))
            continue
        if not text:
            # 部分客户端的 assistant 消息只带 tool_calls、没有正文
            if role == "assistant" and m.tool_calls:
                turns.append(("assistant", tool_request + json.dumps(
                    m.tool_calls, ensure_ascii=False)[:600]))
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # 单轮且无系统指令 → 直接发原文，最贴近自然对话
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    separator = "：" if language == "zh" else ":"
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"{context}{separator}\n{sys_text}")
    for role, text in turns:
        label = assistant if role == "assistant" else user
        parts.append(f"{label}{separator}\n{text}")
    return "\n\n".join(parts)


# ------------------------- 工具调用（function calling）适配 -------------------------
# muse.ai 的网页模型**不会**返回结构化的 tool_calls，所以这里做一层协议适配：
#   1) 请求带 tools 时，把工具定义翻译成提示词里的【工具调用协议】；
#   2) 模型按协议输出 ```json {"tool": "...", "arguments": {...}} ```；
#   3) 我们把这段解析回 OpenAI 的 tool_calls 交给下游 agent。
#
# 注意：这是"尽力适配"而非保证 —— 目标模型是通用对话模型，没有针对
# function calling 做微调，遵守协议的程度需要实测观察。
_TOOL_PROTOCOL_HEAD = """你可以根据需要调用以下工具来协助用户完成任务。
若需调用工具，请直接输出如下格式的 JSON 代码块（不要包含其他多余解释）：
```json
{"name": "<工具名>", "arguments": {<参数>}}
```
如果需要调用多个工具，请输出包含多个对象的 JSON 数组。
如果无需调用工具，请直接用自然语言回答。

可用工具列表：
"""


_TOOL_PROMPT_TEXT = {
    "zh": (_TOOL_PROTOCOL_HEAD, "参数：", "（无参数）", "必填", "可选"),
    "vi": (
        "Nếu cần gọi công cụ, chỉ trả khối mã JSON như ví dụ bên dưới. "
        "Với nhiều lượt gọi, trả một mảng JSON. Nếu không cần công cụ, trả lời tự nhiên.\n"
        '```json\n{"name": "<tên công cụ>", "arguments": {}}\n```\n\nCông cụ khả dụng:\n',
        "Tham số:", "(không có tham số)", "bắt buộc", "tùy chọn",
    ),
    "en": (
        "To call a tool, return only a JSON code block as shown below. "
        "Return a JSON array for multiple calls. If no tool is needed, answer naturally.\n"
        '```json\n{"name": "<tool name>", "arguments": {}}\n```\n\nAvailable tools:\n',
        "Parameters:", "(no parameters)", "required", "optional",
    ),
}


def _describe_params(params, language: MusePromptLanguage = "zh") -> str:
    """把 JSON Schema 的参数描述成易读的多行文本。"""
    _, _, empty, mandatory, optional = _TOOL_PROMPT_TEXT[language]
    if not isinstance(params, dict):
        return "      " + empty
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      " + empty
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            mandatory if name in required else optional,
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None, *, language: MusePromptLanguage = "zh") -> str:
    """把 tools 定义翻译成提示词片段（没有工具时返回空串）。

    同时兼容 Chat Completions 的 `{"type":"function","function":{...}}`
    和 Responses API 的 `{"type":"function","name":...,"parameters":...}`。
    """
    if not tools:
        return ""
    header, parameters, *_ = _TOOL_PROMPT_TEXT[language]
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   %s\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            parameters, _describe_params(fn.get("parameters"), language)))
    if not items:
        return ""
    return header + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """把解析出的 JSON 转成 OpenAI tool_calls；不像工具调用就返回 None。"""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """从模型输出里抽出工具调用。

    返回 (tool_calls, 剩余文本)；抽不到就返回 (None, 原文)。
    支持多种形态：
    1) ```json ... ``` 代码块；
    2) 裸 JSON（或带有前导 json/JSON 关键字）；
    3) 文本中内嵌的完整 JSON 对象或数组。
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest, kinds: list[str] | None = None, *, language: MusePromptLanguage = "zh") -> str:
    user_prompt = r.prompt.strip()
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()
    dur = r.duration or 6
    refs = _video_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if language != "zh":
        return _media_prompt_localized(r, refs, kinds, language, video=True)
    if any(k != "image" for k in kinds):
        # 不再默认「第一张图作起始画面」：实测会把模型拉回图片场景、忽略参考视频
        reference_instruction = (
            f"基于我本次上传的参考素材（{_describe_kinds(kinds)}）生成视频"
            f"（{_reference_legend(kinds)}；只使用本次上传的这些附件，严禁使用历史素材；"
            "各附件的用途以下面的描述为准）")
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(user_prompt):
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（{_reference_legend(kinds)}；"
            "各图片的用途以下面的描述为准，未说明时以第一张作为主要起始画面；严禁使用历史图片或其他图像）")
    elif len(refs) > 1:
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（以第一张作为主要起始画面，"
            "其余图片作为主体、场景与风格参考，严禁使用历史图片或其他图像）")
    else:
        reference_instruction = (
            "基于我本次上传的参考图片附件作为第一帧参考图（严禁使用历史图片或任何其他图像，"
            "必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）")

    gen_word = "图生视频" if all(k == "image" for k in kinds) else "视频"
    is_vertical = any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920"))

    parts = []
    if is_vertical:
        dimensions = (", 720x1280" if (r.resolution or "").strip().lower()
                      in ("", "720", "720p", "720x1280") else "")
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个严格9:16竖屏手机满屏的动态{gen_word}（9:16 vertical portrait video{dimensions}，高大于宽的手机全屏竖版画面，严禁生成横屏或黑边，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个严格9:16竖屏手机满屏视频（9:16 vertical portrait video{dimensions}，高大于宽的手机全屏竖版画面，严禁生成横屏或带有左右黑边，保持垂直构图，时长严格为 {dur} 秒）：{user_prompt}")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个16:9宽屏横屏{gen_word}（16:9 widescreen landscape video，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个16:9横屏宽屏视频（16:9 widescreen landscape video，时长严格为 {dur} 秒）：{user_prompt}")
    else:
        if has_ref:
            parts.append(f"{reference_instruction}：生成动态{gen_word}（时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个视频（时长严格为 {dur} 秒）：{user_prompt}")
        if r.aspect_ratio:
            parts.append(f"画面比例：{r.aspect_ratio}")

    if r.resolution:
        parts.append(f"画质规格：{r.resolution}")
    if r.extra:
        parts.append(r.extra)
    parts.append(_MUSE_MEDIA_DELIVERY["zh"].format(kind="视频"))
    return "，".join(parts)


def _media_signature(name: str, mode: str, expires: int):
    payload = f"{mode}:{expires}:{name}".encode()
    return hmac.new(CFG.api_key.encode(), payload, hashlib.sha256).hexdigest()


MEDIA_URL_TTL_SECONDS = 7 * 86400


def _media_access_query(name: str, *, admin: bool):
    if not CFG.api_key:
        return ""
    mode = "a" if admin or not CLIENT_MEDIA_PATTERN.match(name) else "c"
    expires = int(time.time()) + MEDIA_URL_TTL_SECONDS
    return f"access={mode}.{expires}.{_media_signature(name, mode, expires)}"


def media_url(name: str, *, admin: bool = False) -> str:
    from urllib.parse import quote
    url = f"{_public_base()}/v1/media/{quote(name, safe='')}"
    query = _media_access_query(name, admin=admin)
    return f"{url}?{query}" if query else url


def _check_media_access(name: str, access: str, authorization: str | None):
    match = CLIENT_MEDIA_PATTERN.match(name)
    owner = match[1] if match else None
    token = _bearer_token(authorization)
    client = store.find_client(token)
    if _is_admin(token) or (owner and client and client["id"] == owner):
        return True
    signature = re.fullmatch(r"([ac])\.([0-9]{1,12})\.([0-9a-f]{64})", access)
    if signature and CFG.api_key:
        mode, timestamp, mac = signature.groups()
        now = int(time.time())
        if (now <= int(timestamp) <= now + MEDIA_URL_TTL_SECONDS and (mode == "a" or owner in store.clients)
                and hmac.compare_digest(mac, _media_signature(name, mode, int(timestamp)))):
            return True
    if not owner and CFG.allow_legacy_public_media and not access:
        return False
    raise HTTPException(404, "Không tìm thấy tệp.")


# ------------------------- cookie 解析 -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """把 `a=1; b=2` / JSON / Set-Cookie 行解析成 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """批量导入：每行 `标签 | cookie串`，标签可省略。"""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # `|` 出现在 cookie 里时不算分隔符
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out


# ------------------------- 核心生成 -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """生成完从浏览器读回 cookie 并写回账号池。

    实测结论：**核心 cookie 的 expires 不会被使用行为续期**（hatch_vml 固定
    约 2 天就到期），这里同步回来的主要是那些每次访问都会重新下发的非核心
    cookie（_fbp / wd / dpr 等）以及可能新增的条目，避免账号信息比实际更旧。

    注意：这个函数**绝不能把生成结果搞失败** —— 生成已经成功了，
    同步只是锦上添花，出任何问题都只记一条日志。
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # 有效期要「合并」而不是「覆盖」：会话 cookie 读回的 expires 是 -1，
        # 若整体覆盖会把原有有效期记录清空（实测踩过这个坑）。
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("cookie 同步失败（不影响本次生成）: %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _generation_callback(callback, *args):
    if callback:
        try:
            callback(*args)
        except Exception as exc:
            log.warning("Task metadata callback failed: %s", type(exc).__name__)


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    on_acquired=None, on_account=None, on_queue=None, on_error=None,
                    queue_guard=None) -> tuple[dict, str | None]:
    if queue_guard:
        queue_guard()
    if not any(a.get("enabled", True) and a.get("cookies") for a in store.accounts):
        raise MuseGenerationError("没有可用的生成账号，请检查账号池")
    queue_timeout = QUEUE_POLICY.settings()["queue_timeout"]
    t0 = time.monotonic()
    acquired = _wait_for_generation_slot(queue_timeout, queue_guard)
    queued = time.monotonic() - t0
    _generation_callback(on_queue, queued)
    if not acquired:
        raise MuseGenerationError("排队时限已到，尚未开始生成；请稍后重试或调整独立排队时限")
    try:
        return _run_generation_locked(prompt, kind, timeout, account_id,
                                      on_progress, reference_image, reference_images,
                                      on_acquired=on_acquired, on_account=on_account,
                                      on_queue=on_queue, on_error=on_error,
                                      queued=queued, queue_timeout=queue_timeout, queue_guard=queue_guard)
    finally:
        GEN_LOCK.release()


def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    deadline=None, on_acquired=None, on_account=None,
                    on_queue=None, on_error=None, queued=0.0, queue_timeout=None,
                    queue_guard=None) -> tuple[dict, str | None]:
    if queue_timeout is None:
        queue_timeout = QUEUE_POLICY.settings()["queue_timeout"]

    def lease(**kwargs):
        nonlocal queued, deadline
        remaining = max(0.001, queue_timeout - queued) if queue_timeout else None
        t0 = time.monotonic()
        try:
            return ACCOUNT_POOL.acquire(timeout=remaining, **kwargs)
        finally:
            waited = time.monotonic() - t0
            queued += waited
            # Retry account waits also use the queue budget, never the generation budget.
            if deadline is not None:
                deadline += waited
            _generation_callback(on_queue, queued)

    cur_acc = lease(account_id=account_id, preferred_id=None, rotate=True,
                    **({"queue_guard": queue_guard} if queue_guard else {}))
    if deadline is None:
        deadline = time.monotonic() + max(1, timeout)
    last_exc = None
    try:
        if on_acquired:
            on_acquired()
        for attempt in range(2):
            # Preserve the original failure, including a refusal at the deadline boundary.
            if isinstance(last_exc, (MuseModelRefusalError, MusePostSendError)) or time.monotonic() >= deadline:
                if last_exc is not None:
                    raise last_exc
                raise MuseGenerationError("生成时限已到（不含排队时间），已停止等待")
            if attempt > 0:
                old_id = cur_acc["id"] if cur_acc else None
                cur_acc = None
                try:
                    engine.close_current_page()
                finally:
                    ACCOUNT_POOL.release(old_id)
                try:
                    alt = lease(rotate=True, force_rotate=True)
                except (MuseAuthError, MuseGenerationError):
                    raise last_exc
                cur_acc = alt
                if not alt or (old_id and alt["id"] == old_id):
                    break
            _generation_callback(on_account, cur_acc["id"], attempt + 1)
            try:
                _bind_account_proxy(cur_acc)
                refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
                if refreshed:
                    cur_acc = refreshed
                engine.start()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MuseGenerationError("生成准备超时（不含排队时间），尚未发送本次提示词")
                refs = list(reference_images or [])
                engine_kwargs = {}
                if len(refs) == 1 and not reference_image:
                    reference_image = refs[0]
                elif len(refs) > 1:
                    engine_kwargs["reference_images"] = refs
                res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                      timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                      account_id=cur_acc["id"], on_progress=on_progress,
                                      reference_image=reference_image, **engine_kwargs)
                store.mark(cur_acc["id"], True, "")
                _sync_cookies(cur_acc["id"])
                return res, cur_acc["id"]
            except MuseAuthError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], False, str(exc))
                engine.close_current_page()
            except MuseProxyError as exc:
                last_exc = exc
                store.touch_keepalive(cur_acc["id"], None, f"代理异常: {str(exc)[:160]}")
                engine.close_current_page()
            except MusePageUnavailable as exc:
                last_exc = exc
                store.touch_keepalive(cur_acc["id"], None, f"Lỗi tác vụ: {exc}")
            except MuseGenerationError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], True, f"任务异常: {str(exc)[:200]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
            except Exception as exc:
                engine.close_current_page()
                last_exc = MuseGenerationError(f"生成失败: {exc}")
            if on_error:
                on_error(str(last_exc))
        raise last_exc
    finally:
        try:
            engine.close_current_page()
        finally:
            _clear_account_proxy()
            if cur_acc:
                ACCOUNT_POOL.release(cur_acc.get("id"))


# ------------------------- 基础接口 -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@app.get("/muse/v1/models")
@app.get("/chatgpt/v1/models")
@app.get("/gemini/v1/models")
@app.get("/v1/models")
def models(_=Depends(api_auth), request: Request = None):
    provider = _path_provider(request)
    external = [] if provider else [
        {"id": item["model_id"], "object": "model", "owned_by": item["id"]}
        for item in chat_providers.status() if item["configured"]
    ]
    modules = [
        {"id": model, "object": "model", "owned_by": provider, "type": kind}
        for model, (provider, kind) in MODULES.items()
        if provider == "muse" or web_providers.ready(provider)
    ]
    data = [*MODELS, *modules, *external]
    if provider:
        data = [model for model in data if model["owned_by"] == provider]
    if isinstance(_, dict):
        data = [model for model in data if model["owned_by"] in _["providers"]]
    return {"object": "list", "data": data}


def _muse_capability_state():
    now = time.time()
    accounts = [account for account in store.accounts if account.get("enabled", True)
                and all((account.get("cookies") or {}).get(key) for key in ESSENTIAL_COOKIES)]
    verified = [account for account in accounts if account.get("ok") is True
                and (not account.get("expires_at") or account["expires_at"] > now)]
    connected = bool(engine.proc and engine.proc.poll() is None)
    browser_available = connected or bool(shutil.which(CFG.chromium))
    ready = bool(verified and browser_available)
    with ACCOUNT_POOL.cond:
        accounts_busy = bool(verified) and all(account["id"] in ACCOUNT_POOL.busy_accounts
                                               for account in verified)
    busy = accounts_busy or GEN_LOCK.full()
    reason = ("not_configured" if not accounts else "browser_unavailable" if not browser_available
              else "session_unverified" if not verified else "busy" if busy
              else "ready" if connected else "browser_idle")
    return {"configured": bool(accounts), "connected": connected, "ready": ready,
            "busy": busy, "reason": reason}


@app.get("/muse/v1/capabilities")
@app.get("/chatgpt/v1/capabilities")
@app.get("/gemini/v1/capabilities")
@app.get("/v1/capabilities")
def capabilities(request: Request, client=Depends(contract_auth)):
    scope = _path_provider(request)
    allowed = {scope} if scope else set(client["providers"])
    states = {}
    if "muse" in allowed:
        states["muse"] = _muse_capability_state()
    if allowed.intersection(WEB_PROVIDERS):
        lease = GEN_LOCK.acquire(blocking=False)
        try:
            web_states = web_providers.capability_states(inspect=lease)
        finally:
            if lease:
                GEN_LOCK.release(touch_idle=False)
        for provider, state in web_states.items():
            if provider in allowed:
                busy = GEN_LOCK.full()
                states[provider] = {**state, "busy": busy,
                                    "reason": "busy" if busy and state["ready"] else state["reason"]}
    model_routes = {**MODULES, "muse-spark": ("muse", "text"),
                    "muse-image": ("muse", "image"), "muse-video": ("muse", "video")}
    for provider in chat_providers.status() if allowed.intersection(("openai", "gemini")) else ():
        provider_id = provider["id"]
        if provider_id in allowed:
            configured = provider["configured"]
            states[provider_id] = {"configured": configured, "connected": False, "ready": False, "busy": False,
                                   "reason": "credentials_unverified" if configured else "not_configured"}
            if configured:
                model_routes[provider["model_id"]] = (provider_id, "text")
    models = []
    for model_id, (provider, kind) in model_routes.items():
        if provider not in states:
            continue
        references = provider == "muse" and kind in ("image", "video")
        modes = ["text", "singleImage"] if references else ["text"]
        if references and kind == "image":
            modes.append("multiReference")
        models.append({"id": model_id, "provider": provider, "type": kind, **states[provider],
                       "modes": modes, "maxReferences": (1 if kind == "video" else MAX_REFERENCE_IMAGES) if references else 0,
                       "allowedMimeTypes": [mime for mime in REFERENCE_MIME_EXT if mime.startswith("image/")] if references else [],
                       "maxReferenceBytes": MAX_REFERENCE_IMAGE_BYTES if references else 0})
    return JSONResponse({
        "contractVersion": 1,
        "serverVersion": _read_local_version()["version"],
        "providers": [{"id": provider, **state} for provider, state in states.items()],
        "models": models,
        "limits": {"perClientPending": CLIENT_PENDING_MEDIA_LIMIT, "perKindPending": KIND_PENDING_MEDIA_LIMIT},
        "features": {"idempotency": True, "lookupByRequestKey": True, "cancel": True},
    }, headers=NO_STORE)


# ------------------------- 生图 -------------------------
def _image_response(req: ImageRequest, res: dict, *, admin: bool = False) -> dict:
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"], admin=admin),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        try:
            with open(fpath, "rb") as f:
                item["b64_json"] = base64.b64encode(f.read()).decode()
        except FileNotFoundError as exc:
            raise HTTPException(410, "Tệp kết quả đã được dọn khỏi kho media.") from exc
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _generic_upload_mime(mime: str) -> bool:
    """multipart 上传常见的笼统类型（交给文件头识别）以及别名写法。"""
    return mime in ("application/octet-stream", "binary/octet-stream") or mime in (
        "image/jpg", "audio/mp3", "audio/x-wav", "audio/wave", "audio/x-m4a",
        "audio/m4a", "audio/x-flac", "audio/x-aac", "video/x-m4v")


def _prepare_reference_images(images: list[str], *, public_only: bool = False) -> list[str]:
    """Validate and snapshot all sources before a task is accepted."""
    prepared = []
    total_bytes = 0
    for index, image in enumerate(images):
        try:
            b64, mime = engine._normalize_image(image, public_only=True) if public_only else engine._normalize_image(image)
            data = base64.b64decode(b64, validate=True)
        except (RefImageSafetyError, MuseGenerationError, binascii.Error, ValueError) as exc:
            raise HTTPException(400, f"images[{index}]: {exc}") from exc
        total_bytes += len(data)
        if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
            raise HTTPException(
                400, f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB 限制")
        prepared.append(f"data:{mime};base64,{b64}")
    return prepared


def _media_submission(req, key):
    if not key:
        return {}
    key_hash = _request_key_hash(key)
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False)
    for chunk in encoder.iterencode(req.model_dump(by_alias=True)):
        digest.update(chunk.encode())
    uuid_key = bool(re.fullmatch(r"[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}", key, re.I))
    return {"request_key": key_hash, "request_hash": digest.hexdigest(), "request_v1": uuid_key}


def _request_key_hash(key):
    if not key or not key.strip() or len(key) > 256:
        raise HTTPException(400, "Cần Idempotency-Key hợp lệ, tối đa 256 ký tự.", headers=NO_STORE)
    return hashlib.sha256(key.encode()).hexdigest()


def _replayed_media_task(tasks, kind, provider, owner, submission, *, legacy=None):
    if not submission:
        return None
    for task in tasks:
        if task.get("owner") != owner:
            continue
        same_route = task.get("kind") == kind and (task.get("provider") or "muse") == provider
        if not same_route and not submission.get("request_v1"):
            continue  # Older callers used opaque keys with route-local scope.
        if task.get("request_key") == submission["request_key"]:
            matches = same_route and task.get("request_hash") == submission["request_hash"]
        elif same_route and legacy is not None and task.get("image_request_key") == submission["request_key"]:
            req, references = legacy
            payload = {"request": req.model_dump(by_alias=True), "references": references}
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            matches = task.get("image_request_hash") == digest
        else:
            continue
        if not matches:
            raise HTTPException(409, {"code": "IDEMPOTENCY_CONFLICT",
                                      "message": "Idempotency-Key này đã được dùng với nội dung khác."})
        return task
    return None


def _task_receipt(task):
    return {"id": task["id"], "task_id": task["id"], "object": f"{task['kind']}.task",
            "kind": task["kind"], "status": task["status"], "progress": task.get("progress", 0),
            "created_at": task["created_at"]}


@app.get("/muse/v1/tasks/by-request")
@app.get("/chatgpt/v1/tasks/by-request")
@app.get("/gemini/v1/tasks/by-request")
@app.get("/v1/tasks/by-request")
def lookup_task_by_request(request: Request, idempotency_key: Annotated[str | None, Header()] = None,
                           client=Depends(contract_auth)):
    digest = _request_key_hash(idempotency_key)
    scope = _path_provider(request)
    matches = []
    for task in store.tasks.values():
        provider = task.get("provider") or "muse"
        if (task.get("owner") == client["id"] and provider in client["providers"]
                and (not scope or provider == scope)
                and digest in (task.get("request_key"), task.get("image_request_key"))):
            matches.append(task)
    if len(matches) != 1:
        raise HTTPException(404, "Không tìm thấy biên nhận duy nhất.", headers=NO_STORE)
    receipt = _task_receipt(matches[0])
    return JSONResponse({key: receipt[key] for key in ("id", "task_id", "status", "kind", "progress")},
                        headers=NO_STORE)


@app.post("/muse/v1/images/tasks/{task_id}/cancel")
@app.post("/chatgpt/v1/images/tasks/{task_id}/cancel")
@app.post("/gemini/v1/images/tasks/{task_id}/cancel")
@app.post("/v1/images/tasks/{task_id}/cancel")
@app.post("/muse/v1/videos/{task_id}/cancel")
@app.post("/v1/videos/{task_id}/cancel")
def cancel_media_task(task_id: str, request: Request, client=Depends(contract_auth)):
    scope = _path_provider(request)
    kind = "image" if "/images/tasks/" in request.url.path else "video"
    task = store.get_task(task_id)
    provider = (task or {}).get("provider") or "muse"
    if (not task or task.get("kind") != kind or task.get("owner") != client["id"]
            or provider not in client["providers"] or (scope and scope != provider)):
        raise HTTPException(404, "Không tìm thấy tác vụ.", headers=NO_STORE)
    try:
        cancelled = store.transition_task(task_id, "queued", status="cancelled")
    except OSError as exc:
        raise HTTPException(507, "Không lưu được trạng thái hủy.", headers=NO_STORE) from exc
    if cancelled:
        return JSONResponse({"task_id": task_id, "status": "cancelled", "cancelled": True},
                            headers=NO_STORE)
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy tác vụ.", headers=NO_STORE)
    processing = task["status"] == "processing"
    return JSONResponse({"code": "CANNOT_CANCEL_RUNNING" if processing else "ALREADY_TERMINAL",
                         "status": task["status"],
                         "message": "Không thể dừng tác vụ đang chạy an toàn." if processing
                         else "Tác vụ đã kết thúc."}, status_code=409, headers=NO_STORE)


def _create_media_task(kind, prompt, **metadata):
    try:
        return store.create_task(kind, prompt, **metadata)
    except OSError as exc:
        raise HTTPException(507, "Không lưu được tác vụ mới. Hãy kiểm tra dung lượng và quyền ghi thư mục dữ liệu.") from exc


def _start_media_worker(task, owner, worker):
    try:
        threading.Thread(target=_generate_owned, args=(owner, worker), daemon=True).start()
    except (OSError, RuntimeError):
        return _fail_media_task(TaskTiming(store, task["id"]), RuntimeError("Không thể khởi động tác vụ trên máy chủ.")) or task
    return task


def _redact_task_error(message):
    return re.sub(r"(?i)\b(?:https?|socks5h?)://[^\s]+|\bm2[ac]_[\w.-]+|\bhatch_\w+=[^\s;]+",
                  "[đã ẩn]", message)[:600]


def _fail_media_task(timing, exc):
    expected = "processing" if timing.started else "queued"
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    error = str(detail.get("message") or "") if isinstance(detail, dict) else detail
    uncertain = isinstance(exc, MusePostSendError) or isinstance(getattr(exc, "__cause__", None), WebPostSendError)
    changes = {"status": "interrupted" if uncertain else "failed", "needs_reconcile": uncertain,
               "error": _redact_task_error(error), **timing.finish()}
    if isinstance(detail, dict) and detail.get("code"):
        changes["error_code"] = detail["code"]
    try:
        store.transition_task(timing.task_id, expected, **changes)
    except OSError:
        log.warning("Không lưu được lỗi tác vụ %s.", timing.task_id)
        store.update_task(timing.task_id, expected_status=expected, **changes)
    return store.get_task(timing.task_id)


def _queue_image(req: ImageRequest, prompt: str, reference_images: list[str], *, owner=None, submission=None):
    web_provider = _image_module(req)
    provider = web_provider or "muse"
    with MEDIA_ADMISSION_LOCK:
        tasks = store.tasks.values()
        existing = _replayed_media_task(tasks, "image", provider, owner, submission, legacy=(req, reference_images))
        if existing:
            return JSONResponse(status_code=202, content=_task_receipt(existing))
        _check_media_admission(tasks, "image", owner)
        task = _create_media_task(
            "image", req.prompt, api_prompt=prompt, size=req.size, provider=provider, owner=owner,
            response_format=req.response_format, progress=0, queue_elapsed=0.0,
            **(submission or {}))
        tid = task["id"]

    def worker():
        timing = TaskTiming(store, tid)
        def _on_acquired():
            timing.acquired()
            store.update_task(tid, progress=5)
        try:
            timing.check_queued()
            if web_provider:
                res = _run_web_image(web_provider, prompt, req.timeout or CFG.image_timeout,
                                     on_acquired=_on_acquired, on_queue=timing.queued,
                                     queue_guard=timing.check_queued)
                acc_id = None
            else:
                res, acc_id = _run_generation(
                    prompt, "image", req.timeout or CFG.image_timeout,
                    reference_images=reference_images,
                    on_progress=lambda p: store.update_task(tid, progress=p),
                    on_acquired=_on_acquired, on_account=timing.account,
                    on_queue=timing.queued, on_error=lambda message: timing.error(_redact_task_error(message)),
                    queue_guard=timing.check_queued)
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              **timing.finish(),
                              result={k: res[k] for k in ("filename", "size", "kind")})
        except TaskNotQueued:
            return
        except Exception as exc:  # noqa: BLE001
            _fail_media_task(timing, exc)

    return JSONResponse(status_code=202, content=_task_receipt(_start_media_worker(task, owner, worker)))


@app.post("/muse/v1/images/tasks")
@app.post("/chatgpt/v1/images/tasks")
@app.post("/gemini/v1/images/tasks")
@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: Annotated[str | None, Header()] = None, _=Depends(api_auth),
                      request: Request = None):
    req = _scope_request_model(request, "image", req)
    _authorize_model(_, req.model, "image")
    owner = _client_id(_)
    provider = _image_module(req)
    submission = _media_submission(req, idempotency_key)
    existing = _replayed_media_task(store.tasks.values(), "image", provider or "muse", owner, submission)
    if existing:
        return JSONResponse(status_code=202, content=_task_receipt(existing))
    _check_media_admission(store.tasks.values(), "image", owner)
    options = {"owner": owner, "submission": submission}
    if provider:
        return _queue_image(req, _web_image_prompt(req), [], **options)
    refs = _prepare_reference_images(_image_references(req), **({"public_only": True} if owner else {}))
    prompt = build_image_prompt(req, _reference_kinds(refs), language=store.muse_prompt_language())
    return _queue_image(req, prompt, refs, **options)


@app.get("/muse/v1/images/tasks/{task_id}")
@app.get("/chatgpt/v1/images/tasks/{task_id}")
@app.get("/gemini/v1/images/tasks/{task_id}")
@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(api_auth), request: Request = None):
    task = store.get_task(task_id)
    scope = _path_provider(request)
    if (not task or task.get("kind") != "image"
            or (scope and scope != (task.get("provider") or "muse"))):
        raise HTTPException(404, "image task 不存在")
    _require_task_owner(task, _)
    out = _public_task(task, _)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"], admin=not _client_id(_)))
    return out


@app.post("/muse/v1/images/generations")
@app.post("/chatgpt/v1/images/generations")
@app.post("/gemini/v1/images/generations")
@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(api_auth), request: Request = None):
    req = _scope_request_model(request, "image", req)
    _authorize_model(_, req.model, "image")
    owner = _client_id(_)
    options = {"owner": owner} if owner else {}
    provider = _image_module(req)
    if provider:
        prompt = _web_image_prompt(req)
        if req.async_:
            return _queue_image(req, prompt, [], **options)
        res = await asyncio.to_thread(_generate_owned, owner, _run_web_image, provider, prompt, req.timeout or CFG.image_timeout)
        return _image_response(req, res)
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _image_references(req), **({"public_only": True} if owner else {}))

    prompt = build_image_prompt(req, _reference_kinds(ref_images), language=store.muse_prompt_language())
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_images, **options)
    try:
        res, _acc = await asyncio.to_thread(
            _generate_owned, owner, _run_generation, prompt, "image", timeout, reference_images=ref_images)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@app.post("/muse/v1/images/edits")
@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(api_auth)):
    """OpenAI 兼容的图生图/图像编辑接口，兼容 multipart/form-data 与 application/json。"""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data: list[str] = []
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form(
            max_files=MAX_REFERENCE_IMAGES, max_fields=20)
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        # image 可重复提交；video / audio / file 同样可重复，按字段顺序排在 image 之后
        img_fields = [(name, value) for name in ("image", "image[]", "video", "audio", "file")
                      for value in form.getlist(name)]
        if len(img_fields) > MAX_REFERENCE_IMAGES:
            raise HTTPException(400, f"参考素材最多 {MAX_REFERENCE_IMAGES} 个（当前 {len(img_fields)} 个）")
        total_upload_bytes = 0
        mib = 1024 * 1024
        for index, (name, img_field) in enumerate(img_fields):
            if img_field and hasattr(img_field, "read"):
                ref_mime = (getattr(img_field, "content_type", "") or "").split(";", 1)[0].strip().lower()
                if ref_mime and ref_mime not in REFERENCE_MIME_EXT and not _generic_upload_mime(ref_mime):
                    raise HTTPException(400, f"{name}[{index}] 文件类型不受支持（{ref_mime}）")
                content = await img_field.read(MAX_REFERENCE_IMAGE_BYTES + 1)
                if len(content) > MAX_REFERENCE_IMAGE_BYTES:
                    raise HTTPException(400, f"{name}[{index}] 超过 {MAX_REFERENCE_IMAGE_BYTES // mib} MiB 限制")
                total_upload_bytes += len(content)
                if total_upload_bytes > MAX_REFERENCE_TOTAL_BYTES:
                    raise HTTPException(400, f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // mib} MiB 限制")
                ref_mime = ref_mime or "application/octet-stream"
                ref_image_data.append(
                    f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}")
            elif isinstance(img_field, str) and img_field.strip():
                ref_image_data.append(img_field.strip())
            else:
                raise HTTPException(400, f"{name}[{index}] 不是有效素材引用")
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        raw_images = body.get("images")
        if raw_images is not None and not isinstance(raw_images, list):
            raise HTTPException(400, "images 必须是数组")
        legacy = [body.get("reference_image"), body.get("image_url"), body.get("image")]
        ref_image_data = _reference_list(raw_images, legacy, "images", {
            name: body.get(name) for name in ("videos", "audios", "files")})

    if not prompt:
        prompt = "参考此图片并进行生图创作"

    req_obj = ImageRequest(
        prompt=prompt,
        model=_scoped_model(request, "image", model),
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        images=ref_image_data,
        **{"async": async_mode}
    )
    _authorize_model(_, req_obj.model, "image")
    owner = _client_id(_)
    if _module_provider(req_obj.model, "image"):
        raise HTTPException(400, "Mô-đun web chưa hỗ trợ chỉnh sửa ảnh.")
    ref_image_data = await asyncio.to_thread(
        _prepare_reference_images, ref_image_data, **({"public_only": True} if owner else {}))
    full_prompt = build_image_prompt(req_obj, _reference_kinds(ref_image_data), language=store.muse_prompt_language())
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data, **({"owner": owner} if owner else {}))
    try:
        res, _acc = await asyncio.to_thread(
            _generate_owned, owner, _run_generation, full_prompt, "image", gen_timeout,
            reference_images=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- 生视频（异步任务） -------------------------
@app.post("/muse/v1/videos")
@app.post("/v1/videos")
@app.post("/muse/v1/videos/generations")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest, _=Depends(api_auth), request: Request = None,
                       idempotency_key: Annotated[str | None, Header()] = None):
    req = _scope_request_model(request, "video", req)
    _authorize_model(_, req.model, "video")
    owner = _client_id(_)
    _module_provider(req.model, "video")
    submission = await asyncio.to_thread(_media_submission, req, idempotency_key) if idempotency_key else {}
    existing = _replayed_media_task(store.tasks.values(), "video", "muse", owner, submission)
    if existing:
        return _task_receipt(existing)
    if owner:
        ratio = (req.aspect_ratio or "").strip().lower()
        resolution = (req.resolution or "").strip().lower()
        if ((req.duration is not None and req.duration <= 0)
                or (ratio and not re.fullmatch(r"[1-9]\d*[:/][1-9]\d*|portrait|landscape|auto", ratio))
                or (resolution and not re.fullmatch(
                    r"[1-9]\d{2,3}p?|[1-9]\d?k|[1-9]\d{2,4}x[1-9]\d{2,4}|auto|default", resolution))):
            raise HTTPException(400, {"code": "UNSUPPORTED_MEDIA_OPTIONS",
                                      "message": "Thời lượng, tỷ lệ hoặc độ phân giải video không được hỗ trợ."})
    _check_media_admission(store.tasks.values(), "video", owner)
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _video_references(req), **({"public_only": True} if owner else {}))

    prompt = build_video_prompt(req, _reference_kinds(ref_images), language=store.muse_prompt_language())
    timeout = req.timeout or CFG.video_timeout
    with MEDIA_ADMISSION_LOCK:
        tasks = store.tasks.values()
        existing = _replayed_media_task(tasks, "video", "muse", owner, submission)
        if existing:
            return _task_receipt(existing)
        _check_media_admission(tasks, "video", owner)
        task = _create_media_task("video", req.prompt, owner=owner, provider="muse",
                                  api_prompt=prompt, queue_elapsed=0.0, progress=10, **submission)

    def worker():
        timing = TaskTiming(store, task["id"])
        def _on_acquired():
            timing.acquired()
            store.update_task(task["id"], progress=15)
        try:
            timing.check_queued()
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb,
                                          reference_images=ref_images, on_acquired=_on_acquired, on_account=timing.account,
                on_queue=timing.queued, on_error=lambda message: timing.error(_redact_task_error(message)),
                queue_guard=timing.check_queued)
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              **timing.finish(),
                              result={"filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"]})
        except TaskNotQueued:
            return
        except Exception as exc:  # noqa: BLE001
            _fail_media_task(timing, exc)

    return _task_receipt(_start_media_worker(task, owner, worker))


@app.get("/muse/v1/videos/{task_id}")
@app.get("/v1/videos/{task_id}")
@app.get("/muse/v1/videos/generations/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(api_auth)):
    t = store.get_task(task_id)
    if not t or t.get("kind") != "video":
        raise HTTPException(404, "task 不存在")
    _require_task_owner(t, _)
    out = _public_task(t, _)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        # Progress reports observed milestones only; Muse exposes no percentage.
        out["progress"] = max(out.get("progress", 0) or 0, 15)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out


# ------------------------- 对话（OpenAI 兼容） -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _want_usage(stream_options) -> bool:
    """下游（LangChain、部分 SDK / 智能体框架）会带
    `stream_options.include_usage=true`，要求在 [DONE] 之前补一个
    `choices: []` + `usage` 的分片。不发的话少数框架会一直等 usage 而卡住。"""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """把大块文本平滑切分为仿原生 LLM 打字机的微流式 Token。
    若传入文本本身微小（<= 3 字），直接秒级放行，零延迟；
    若传入文本是大块（如 DOM 批量刷新），按每 chunk_size 字间隔 delay 平滑输出。
    """
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/muse/v1/chat/completions")
@app.post("/chatgpt/v1/chat/completions")
@app.post("/gemini/v1/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request, _=Depends(api_auth)):
    """OpenAI 兼容的对话接口 —— 各类智能体客户端都能接。

    说明：muse.ai 网页是**自动路由**的 agent，界面上没有模型选择器，所有请求
    都会落到同一个网页助手（自称 Koda，底层是 Muse 系列语言模型）。所以
    `model` 字段只用于兼容下游，不影响路由结果。

    请求带 `tools` 时，会注入【工具调用协议】并把模型输出的 JSON 解析回
    `tool_calls`（muse.ai 没有原生 function calling，这层属于协议适配）。

    对话与生图/生视频共用同一个浏览器实例，靠 `GEN_LOCK` 串行。
    """
    req = _scope_request_model(request, "text", req)
    _authorize_model(_, req.model, "text")
    web_provider = _module_provider(req.model, "text")
    if web_provider:
        if req.tools or any(isinstance(msg.content, list) and any(
            isinstance(part, dict) and part.get("type") not in (None, "text", "input_text", "output_text")
            for part in msg.content
        ) for msg in req.messages):
            raise HTTPException(400, "Mô-đun web chỉ nhận văn bản và chưa hỗ trợ gọi công cụ.")
        prompt = build_chat_prompt(req.messages, language="vi") or (req.prompt or "").strip()
        if not prompt:
            raise HTTPException(400, "Hãy nhập nội dung chat.")
        timeout = max(1, min(req.timeout or CFG.chat_timeout, 600))
        text = await asyncio.to_thread(_web_call, web_providers.generate, web_provider, prompt,
                                       image=False, timeout=timeout)
        cid, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
        if req.stream:
            return StreamingResponse(iter((
                _sse(_chat_chunk(cid, created, req.model, {"role": "assistant", "content": text})),
                _sse(_chat_chunk(cid, created, req.model, {}, finish="stop")),
                "data: [DONE]\n\n",
            )), media_type="text/event-stream", headers=_SSE_HEADERS)
        return {"id": cid, "object": "chat.completion", "created": created, "model": req.model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}]}

    provider_route = chat_providers.route(req.model)
    if provider_route:
        provider, model_id = provider_route
        payload = await request.json()
        payload["stream"] = req.stream
        timeout = max(1, min(req.timeout or CFG.chat_timeout, 600))
        return await chat_providers.chat(provider, model_id, payload, timeout)

    language = store.muse_prompt_language()
    prompt = build_chat_prompt(req.messages, language=language) or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages 为空")

    # 工具调用协议默认不注入 —— 实测 muse.ai 的助手会明确拒绝输出"伪工具调用"，
    # 注入反而污染正常回答；详见 config.py 的 tool_protocol 注释。
    tool_note = build_tools_prompt(req.tools, language=language) if CFG.tool_protocol else ""
    if tool_note:
        # 放在末尾：开头是 agent 自己的 system 提示，夹在中间容易被忽略；
        # 紧贴用户消息之前，模型对末尾指令的遵守度明显更高。
        prompt = prompt + "\n\n" + tool_note

    model = "muse/text" if req.model == "muse/text" else resolve_model(req.model.removeprefix("muse/"), default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """按 OpenAI 习惯分两片发：先 id/name，再 arguments 全文。"""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # 握手建立瞬间立即发送 role: assistant 首包，让下游客户端秒级捕获光标
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                if not tool_note:
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                yield "data: [DONE]\n\n"
                store.mark(acc_id, True, "")
            except MuseAuthError as exc:
                store.mark(acc_id, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                store.mark(acc_id, True, f"助手超时: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"内部错误: {exc}", "type": "server_error", "code": 500}})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """把 Responses API 的 instructions / input 归一成 messages。"""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message 形态
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="【工具执行结果】\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="【请求调用工具】"
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/muse/v1/responses")
@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(api_auth), request: Request = None):
    """OpenAI Responses API —— 新版 Codex 默认走这个端点。

    只实现 Codex 实际用到的子集：文本进 → 文本出（含流式事件）。
    muse.ai 不会返回结构化的 tool_calls，关于工具调用的边界见 README。
    """
    req = _scope_request_model(request, "text", req)
    _authorize_model(_, req.model, "text")
    if _module_provider(req.model, "text") or chat_providers.route(req.model):
        raise HTTPException(400, "Hãy dùng /v1/chat/completions cho OpenAI hoặc Gemini.")
    prompt = build_chat_prompt(_responses_messages(req), language=store.muse_prompt_language())
    if not prompt:
        raise HTTPException(400, "input 为空")

    model = resolve_model(req.model.removeprefix("muse/"), default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id, cookies, expires = acc["id"], acc["cookies"], acc.get("cookies_exp")

    def envelope(status: str, text: str = "") -> dict:
        done = status == "completed"
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model,
                "output": [{"type": "message", "id": mid, "role": "assistant",
                            "status": "completed" if done else "in_progress",
                            "content": ([{"type": "output_text", "text": text,
                                          "annotations": []}] if text else [])}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []}})
                yield _sse_event("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": mid,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "",
                             "annotations": []}})
                full = ""
                for chunk in safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id):
                    full += chunk
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": mid,
                        "output_index": 0, "content_index": 0, "delta": chunk})
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": mid,
                    "output_index": 0, "content_index": 0, "text": full})
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full,
                                          "annotations": []}]}})
                yield _sse_event("response.completed", {
                    "type": "response.completed",
                    "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("responses 流式失败: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    return envelope("completed", text)


@app.get("/v1/media/{name}")
def get_media(name: str, access: str = "", authorization: str | None = Header(default=None)):
    if name.startswith(".") or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    private = _check_media_access(name, access, authorization)
    p = os.path.join(CFG.media_dir, name)
    if os.path.islink(p) or not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    headers = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
    if private:
        headers["Cache-Control"] = "private, no-store"
    mime = mimetypes.guess_type(name)[0] or ""
    if mime.startswith(("image/", "video/", "audio/")) and mime != "image/svg+xml":
        return FileResponse(p, media_type=mime, headers=headers)
    return FileResponse(p, media_type="application/octet-stream", filename=name, headers=headers)


@app.get("/admin/muse-prompt")
def get_muse_prompt_settings(_=Depends(auth)):
    return {"language": store.muse_prompt_language()}


@app.put("/admin/muse-prompt")
def update_muse_prompt_settings(settings: MusePromptSettings, _=Depends(auth)):
    store.save_muse_prompt_language(settings.language)
    return settings


@app.get("/admin/generation-queue")
def get_generation_queue(_=Depends(auth)):
    return QUEUE_POLICY.settings()

@app.put("/admin/generation-queue")
def save_generation_queue(req: QueueSettings, _=Depends(auth)):
    QUEUE_POLICY.save(req)
    return QUEUE_POLICY.settings()

@app.get("/v1/media/{name}/thumbnail")
def get_media_thumbnail(name: str, access: str = "", authorization: str | None = Header(default=None)):
    private = _check_media_access(name, access, authorization)
    try:
        path = video_thumbnail(CFG.media_dir, os.path.join(CFG.data_dir, "thumbnails"), name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, "文件不存在")
    except ThumbnailUnavailable:
        raise HTTPException(503, "视频缩略图暂不可用")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, no-store" if private else "public, max-age=86400"})


# ------------------------- 管理：总览 -------------------------
@app.get("/admin/web-providers")
def list_web_providers(_=Depends(auth)):
    profile = web_providers.profile_connection
    profile_connected = bool(profile and profile.browser.ws.connected)
    return {"configured": profile_connected if web_providers.profile_preferred else bool(web_providers.port),
            "port": web_providers.port,
            "profile_connected": profile_connected,
            "ready": [provider for provider in WEB_PROVIDERS if web_providers.ready(provider)],
            "linked_at": dict(web_providers.linked_at),
            "local_setup": not (os.environ.get("RENDER", "").lower() == "true"
                                or os.environ.get("RENDER_GIT_COMMIT"))}


@app.post("/admin/web-providers/connect")
def connect_web_chrome(_=Depends(auth)):
    if not list_web_providers()["local_setup"]:
        raise HTTPException(409, "Render không thể dùng Chrome trên máy Mac của bạn.")
    port = web_providers.port or DEFAULT_WEB_CDP_PORT
    if port == CFG.cdp_port:
        raise HTTPException(409, "Cổng Chrome Web trùng cổng Muse. Hãy cấu hình hai cổng riêng.")
    try:
        web_providers.connect_port(port, store.enable_web_chrome)
    except WebProviderError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return list_web_providers()


@app.post("/admin/web-providers/connect-profile")
async def connect_web_chrome_profile(_=Depends(auth)):
    if not list_web_providers()["local_setup"]:
        raise HTTPException(409, "Render không thể dùng Chrome trên máy Mac của bạn.")
    await asyncio.to_thread(_web_call, web_providers.connect_profile, CFG.cdp_port,
                            lambda: store.enable_web_chrome(profile=True))
    return list_web_providers()


class WebProviderLink(BaseModel):
    marker: str = Field(pattern=r"^[a-f0-9]{32}$")


@app.post("/admin/web-providers/{provider}/link")
async def link_web_provider(provider: str, req: WebProviderLink, _=Depends(auth)):
    if provider not in WEB_PROVIDERS:
        raise HTTPException(404, "Không tìm thấy nhà cung cấp web.")
    linked_at = await asyncio.to_thread(_web_call, web_providers.link, provider, req.marker)
    return {"ok": True, "provider": provider, "linked_at": linked_at}


@app.post("/admin/web-providers/{provider}/test")
async def test_web_provider(provider: str, _=Depends(auth)):
    if provider not in WEB_PROVIDERS:
        raise HTTPException(404, "Không tìm thấy nhà cung cấp web.")
    await asyncio.to_thread(_web_call, web_providers.check, provider)
    return {"ok": True}


@app.get("/admin/chat-providers")
def list_chat_providers(_=Depends(auth)):
    return {"providers": chat_providers.status()}


@app.put("/admin/chat-providers/{provider}")
def save_chat_provider(provider: ProviderName, settings: ProviderSettings, _=Depends(auth)):
    return chat_providers.save(provider, settings)


@app.post("/admin/chat-providers/{provider}/test")
async def test_chat_provider(provider: ProviderName, _=Depends(auth)):
    return await chat_providers.test(provider)


class ClientKeyRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    providers: list[Literal["muse", "chatgpt", "gemini-web", "openai", "gemini"]] = Field(min_length=1, max_length=5)


@app.get("/admin/client-keys")
def list_client_keys(_=Depends(auth)):
    return {"clients": store.list_clients()}


@app.post("/admin/client-keys", status_code=201)
def create_client_key(req: ClientKeyRequest, _=Depends(auth)):
    label = req.label.strip()
    if not label:
        raise HTTPException(400, "Hãy nhập tên người dùng.")
    client = store.create_client(label, list(dict.fromkeys(req.providers)))
    return JSONResponse(status_code=201, content=client, headers={"Cache-Control": "no-store"})


@app.delete("/admin/client-keys/{client_id}")
def revoke_client_key(client_id: str, _=Depends(auth)):
    if not store.revoke_client(client_id):
        raise HTTPException(404, "Không tìm thấy khóa người dùng.")
    return {"revoked": True}


@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "generation_queue": QUEUE_POLICY.settings(),
        "tasks": store.list_tasks(20),
        "media_count": len(media_entries(CFG.media_dir)) if os.path.isdir(CFG.media_dir) else 0,
        "muse_prompt_language": store.muse_prompt_language(),
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- 管理：账号池 -------------------------
@app.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": store.list_accounts(), "stats": store.stats()}


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    proxy = req.proxy.configuration() if req.proxy else None
    added: list[dict] = []
    seen: list[dict] = []          # 本次导入的所有 cookie，用来检查核心项是否齐全

    if req.batch:
        for label, cookies in parse_batch(req.batch):
            acc = store.add_account(cookies, label)
            seen.append(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at")})

    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=req.expires)
        seen.append(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at")})

    if not added:
        raise HTTPException(400, "未解析到任何 cookie，请检查格式")

    if proxy:
        for account in added:
            store.update_account(account["id"], proxy=proxy)

    # 只要有一个账号把核心 cookie 凑齐就算通过（批量时按整体判断）
    missing = [n for n in ESSENTIAL_COOKIES
               if not any(n in c for c in seen)]
    return {"added": added, "count": len(added),
            "essential_missing": missing,
            "warning": (f"缺少核心 cookie：{', '.join(missing)}，该账号可能无法生成"
                        if missing else "")}


# One short-lived receipt per account; credentials are represented only by a hash.
_PROXY_TEST_RECEIPTS: dict[str, dict] = {}
_PROXY_TEST_RECEIPT_TTL = 300


def _proxy_test_identity(proxy: dict, cookies: dict | None) -> str:
    value = json.dumps({"proxy": proxy, "cookies": cookies or {}},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    with ACCOUNT_POOL.cond:
        existing = store.get_account(aid)
        if not existing:
            raise HTTPException(404, "账号不存在")
        changes = {"label": req.label, "enabled": req.enabled}
        if "proxy" in req.model_fields_set:
            if aid in ACCOUNT_POOL.busy_accounts:
                raise HTTPException(409, "账号正在执行任务或检测，请稍后再修改代理")
            proxy = req.proxy.configuration(existing.get("proxy")) if req.proxy else {}
            if proxy != (existing.get("proxy") or {}):
                changes.update(proxy=proxy, synced_at=0, last_keepalive=0,
                               checked_at=0, proxy_status={}, ok=None,
                               note="代理已修改，请重新测试")
                receipt = _PROXY_TEST_RECEIPTS.get(aid)
                if (proxy and receipt and 0 <= time.monotonic() - receipt["at"] <= _PROXY_TEST_RECEIPT_TTL
                        and receipt["identity"] == _proxy_test_identity(proxy, existing.get("cookies"))):
                    changes["proxy_status"] = dict(receipt["result"])
                    changes["note"] = "代理配置已保存，账号浏览器状态待测试"
                engine._last_http_renew.pop(aid, None)
            _PROXY_TEST_RECEIPTS.pop(aid, None)
        acc = store.update_account(aid, **changes)
        return store.public_account(acc)


@app.post("/admin/accounts/{aid}/proxy/test")
def test_account_proxy(aid: str, req: ProxyTestRequest | None = None, _=Depends(auth)):
    import ipaddress
    import requests
    with _account_operation(aid) as acc:
        proxy = req.proxy.configuration(acc.get("proxy")) if req is not None else _account_proxy(acc)
        if not proxy:
            raise HTTPException(400, "请输入代理链接或先保存该账号的代理")
        identity = _proxy_test_identity(proxy, acc.get("cookies"))
        result = {"ok": False, "proxy_configured": True, "proxy_reachable": False,
                  "exit_ip": None, "session_ok": None, "workspace_status": None}
        authentication_failed = "Xác thực proxy thất bại (HTTP 407). Hãy kiểm tra tên đăng nhập và mật khẩu proxy."
        try:
            # Fixed HTTPS endpoint receives no Muse cookies or proxy credentials.
            with request_proxy_context(proxy) as proxy_options, requests.Session() as session:
                session.trust_env = False
                response = session.get("https://api.ipify.org?format=json",
                                       timeout=(5, 10), allow_redirects=False,
                                       **proxy_options)
            if response.status_code == 407:
                raise MuseProxyError(authentication_failed)
            if response.status_code != 200:
                raise MuseProxyError("代理出口检测未成功，请检查代理连接或认证")
            result["exit_ip"] = str(ipaddress.ip_address(response.json()["ip"]))
            result["proxy_reachable"] = True
            checked = engine.renew_session_http(acc.get("cookies") or {}, acc.get("cookies_exp"),
                                                wake_vm=False, proxy=proxy)
            result.update(ok=True, session_ok=True, workspace_status=checked.get("status"),
                          message="代理出口与 Muse 会话检查通过；生成能力请使用账号测试和实际任务验证")
        except MuseAuthError:
            result.update(session_ok=False, message="代理可连接，但 Muse 会话认证失败，请重新导入 Cookie")
        except MuseProxyError as exc:
            result["message"] = str(exc)
        except MuseSessionUnavailable as exc:
            _persist_session_cookies(aid, exc.cookies, exc.cookies_exp)
            result.update(session_ok=False, message=str(exc))
        except MuseGenerationError as exc:
            result.update(session_ok=False, message=str(exc))
        except requests.exceptions.ProxyError as exc:
            if re.search(r"\bTunnel connection failed: 407\b", str(exc), re.IGNORECASE):
                result["message"] = authentication_failed
            else:
                result["message"] = "Không kết nối được qua proxy. Hãy kiểm tra địa chỉ, cổng và kết nối mạng."
        except (requests.RequestException, ValueError, KeyError, TypeError):
            result["message"] = "代理出口检测失败，请检查代理地址、认证和网络"
        recorded = {**result, "checked_at": int(time.time())}
        tested_saved = False
        with ACCOUNT_POOL.cond:
            current = store.get_account(aid)
            if current and identity == _proxy_test_identity(proxy, current.get("cookies")):
                # Unsaved routes stay only in memory; saving the exact configuration may adopt this proof.
                _PROXY_TEST_RECEIPTS[aid] = {"identity": identity, "at": time.monotonic(),
                                           "result": dict(recorded)}
                tested_saved = proxy == _account_proxy(current)
                if tested_saved:
                    store.update_account(aid, proxy_status=dict(recorded))
        result["tested_saved_proxy"] = tested_saved
        return result


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "账号不存在")
    with ACCOUNT_POOL.cond:
        _PROXY_TEST_RECEIPTS.pop(aid, None)
    return {"deleted": True, "id": aid}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """真实打开 muse.ai 验证该账号 cookie 是否仍可登录。"""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK, _account_operation(aid) as acc:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
                synced = _sync_cookies(aid)
                quota = None
                quota_error = None
                try:  # Session validity and quota readability are independent results.
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"), account_id=aid)
                    if quota.get("found"):
                        quota["checked_at"] = int(time.time())
                        store.update_account(aid, quota=quota)
                    else:
                        quota_error = "额度面板未读到有效数据；旧缓存未覆盖"
                        quota = None
                except Exception as exc:  # noqa: BLE001
                    quota_error = f"{type(exc).__name__}；额度缓存未更新"
                store.mark(aid, True, "会话有效" + ("；额度读取失败" if quota_error else ""))
                return {"ok": True, "message": "会话有效，可正常生成",
                        "synced": synced, "quota": quota,
                        "quota_error": quota_error}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"测试未确认（保留账号状态）: {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """更新某个账号的 cookie（用于会话过期后补新 cookie）。"""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "未解析到 cookie")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # 补入的是「全新会话」的 cookie，有效期估算锚点必须重置到当下，
    # 否则会沿用旧会话的锚点，把剩余天数算少。
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="已更新 cookie",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    try:
        with GEN_LOCK, _account_operation(acc["id"]) as selected:
            engine.start()
            engine.refresh(selected["cookies"], selected.get("cookies_exp"), account_id=selected["id"])
            return {"ok": True, "account": selected["id"]}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- 管理：额度 -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """打开 muse.ai 的 Settings 面板读该账号的额度（实时），并缓存到账号记录。

    返回例：{"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK, _account_operation(aid, wait_seconds=60) as acc:
            engine.start()
            q = engine.quota(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
            if not q.get("found"):
                raise MuseGenerationError("额度面板未读到有效数据；旧缓存未覆盖")
            q["checked_at"] = int(time.time())
            store.update_account(aid, quota=q)
            return q

    try:
        return await asyncio.to_thread(_probe)
    except HTTPException:
        raise
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"额度查询失败: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """用当前最久未用的可用账号查一次额度（同池账号共享同一 muse.ai 计划的
    通常只有一人使用时够用；多账号时建议按账号查）。"""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    return await query_quota(acc["id"], _)


# ------------------------- 管理：接入信息 / API Key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """生成新的 API Key，写入 .env 并立即生效（不用重启）。"""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    _persist_env("MUSE2API_KEY", new_key)
    CFG.api_key = new_key
    return {"ok": True, "api_key": new_key,
            "message": "已生成新 Key 并立即生效；旧 Key 已失效，请更新下游项目"}


def _persist_env(key: str, value: str):
    """把配置写回 .env（保留其它行，原子替换）。"""
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp, path)


# ------------------------- 管理：Cookie 获取 -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """返回本机取 cookie 的助手脚本（进阶方式，需要装 Python）。"""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "助手脚本缺失")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """把浏览器扩展打包成 zip 返回（推荐方式，零命令行）。

    用户下载后解压 → chrome://extensions 开发者模式加载 → 点一下就导入 cookie。
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, names in os.walk(src):
            dirs.sort()
            for name in sorted(names):
                p = os.path.join(root, name)
                relative_path = os.path.relpath(p, src).replace(os.sep, "/")
                z.write(p, f"muse2api-extension/{relative_path}")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


@app.get("/admin/extension/files")
def extension_files():
    """列出扩展目录内容（前端展示用）。"""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    files = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for name in sorted(names):
            p = os.path.join(root, name)
            files.append(os.path.relpath(p, src).replace(os.sep, "/"))
    return {"files": sorted(files)}


# ------------------------- 管理：任务 / 媒体 -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": [_public_task(task, None) for task in store.list_tasks(limit)]}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    try:
        removed = store.delete_task(tid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not removed:
        raise HTTPException(404, "任务不存在")
    return {"deleted": True}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@app.get("/admin/media")
def admin_media(_=Depends(auth)):
    items = []
    entries = media_entries(CFG.media_dir)
    for path, stat in reversed(entries):
        name = path.name
        is_video = path.suffix.lower() in VIDEO_FORMATS
        item = {"name": name, "url": media_url(name, admin=True), "bytes": stat.st_size,
                "mtime": int(stat.st_mtime), "kind": "video" if is_video else "image"}
        if is_video:
            from urllib.parse import quote
            item["thumbnail_url"] = f"/v1/media/{quote(name, safe='')}/thumbnail?v={source_version(stat)}"
            query = _media_access_query(name, admin=True)
            if query:
                item["thumbnail_url"] += "&" + query
        items.append(item)
    return {"media": items, "count": len(items),
            "used_bytes": sum(info.st_size for _, info in entries),
            "capacity_bytes": CAPACITY_BYTES, "cleanup_bytes": CLEANUP_BYTES}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names 必须是文件名数组")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or name.startswith(".") or "/" in name or "\\" in name or ".." in name:
            errors.append(f"非法文件名: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                delete_media_file(p)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if name.startswith(".") or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    try:
        delete_media_file(p)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")


# ------------------------- 前端页面 -------------------------
def _admin_page_response() -> HTMLResponse:
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
    path = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(path, encoding="utf-8") as file:
            page = file.read()
        if not page.strip():
            raise ValueError("admin.html trống")
    except (OSError, ValueError) as error:
        log.error("Không đọc được bảng quản trị %s: %s", path, error)
        return HTMLResponse(
            '<!doctype html><html lang="vi"><meta charset="utf-8"><title>mac2API</title>'
            '<body style="background:#0b0f19;color:#e6e8ee;font-family:system-ui;padding:40px">'
            '<h1>Không mở được bảng quản trị mac2API</h1>'
            '<p>Máy chủ không đọc được <code>admin.html</code> cạnh <code>app.py</code>.</p>'
            '<p>Hãy giải nén cả gói. Trên Mac, chạy <code>start-mac2api.command</code> '
            'trong thư mục vừa giải nén rồi mở địa chỉ mà Terminal in ra.</p>'
            '<p>Nhật ký máy chủ có đường dẫn và nguyên nhân chi tiết.</p></body></html>',
            status_code=503, headers=headers,
        )
    return HTMLResponse(page, headers=headers)


@app.get("/", response_class=HTMLResponse)
def index():
    return _admin_page_response()


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return _admin_page_response()



# ------------------------- 账号自动保活与静默续期 -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """Verify the upstream session through this account's saved route."""
    try:
        with _account_operation(aid) as acc:
            if not acc.get("cookies"):
                return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": "账号无有效 cookie"}
            res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True,
                                            **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
            store.update_account(
                aid,
                cookies=res["cookies"],
                cookies_exp=res["cookies_exp"],
                ok=True if res.get("ok") else False,
                synced_at=int(time.time()),
            )
            vm_state = res.get("vm_state") or "RUNNING"
            store.touch_keepalive(aid, True, f"会话有效 · 自动保活 (VM: {vm_state})")
            quota = acc.get("quota")
            if check_quota and GEN_LOCK.acquire(blocking=False):
                try:
                    engine.start()
                    quota = engine.quota(res["cookies"], res["cookies_exp"],
                                         account_id=aid)
                    if not quota.get("found"):
                        raise MuseGenerationError("额度面板未读到有效数据；旧缓存未覆盖")
                    quota["checked_at"] = int(time.time())
                    store.update_account(aid, quota=quota)
                except Exception as qe:  # noqa: BLE001
                    log.warning("读取账号 %s 额度失败: %s", aid, qe)
                finally:
                    GEN_LOCK.release()
            updated = store.get_account(aid) or {}
            return {
                "ok": True,
                "id": aid,
                "label": acc.get("label", aid),
                "vm_id": res.get("vm_id"),
                "vm_state": vm_state,
                "wake_ok": res.get("wake_ok"),
                "expires_at": updated.get("expires_at"),
                "quota": quota,
            }
    except HTTPException as exc:
        return {"ok": False, "id": aid, "busy": exc.status_code == 409, "error": str(exc.detail)}
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"保活认证失败: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}
    except MuseSessionUnavailable as exc:
        _persist_session_cookies(aid, exc.cookies, exc.cookies_exp)
        store.touch_keepalive(aid, None, f"保活未确认（保留账号状态）: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc), "cookies_rotated": exc.rotated}
    except Exception as exc:
        store.touch_keepalive(aid, None, f"保活未确认（保留账号状态）: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}


async def run_keepalive_all(force: bool = False) -> dict:
    """执行账号保活：force=True 时强制刷新所有启用账号，否则刷新临期/未检账号并保持 VM 热备。"""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            for a in accounts:
                aid = a["id"]
                label = a.get("label", aid)
                exp_at = a.get("expires_at")
                last_ka = a.get("last_keepalive") or 0

                needs_run = (
                    force
                    or not last_ka
                    or ((now - last_ka) > 6 * 3600 and
                        (not exp_at or exp_at - now < 36 * 3600 or
                         a.get("ok") is not True))
                )
                if not needs_run:
                    skipped.append({"id": aid, "label": label, "reason": "会话充足且近期已保活"})
                    continue

                log.info("【自动保活】正在为账号 %s (%s) 执行静默续期与 VM 唤醒...", label, aid)
                res = await asyncio.to_thread(_probe_account_sync, aid, False)
                results.append(res)
                log.info("【自动保活】账号 %s 执行结果: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """后台静默预热浏览器与首个可用账号的 WebSocket 隧道，使重启后首条请求也秒回。"""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    acc = None
    try:
        acc = ACCOUNT_POOL.acquire(rotate=False, timeout=0.1, record_use=False)
        log.info("【浏览器预热】正在后台预热账号 %s (%s) 的热备标签页...", acc.get("label"), acc["id"])
        _bind_account_proxy(acc)
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("【浏览器预热】账号 %s (%s) 热备标签页与 WebSocket 已就绪", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("【浏览器预热】预热异常: %s", exc)
    finally:
        # Warmup runs in a worker thread; do not retain its account reservation or page.
        engine.close_current_page()
        _clear_account_proxy()
        if acc:
            ACCOUNT_POOL.release(acc["id"])
        GEN_LOCK.release()


async def _keepalive_loop():
    """后台常驻守护任务：每 15 分钟轮询一次账号健康状态并保持 VM 热备，同时静默同步仓库更新。"""
    log.info("【自动保活守护进程】已启动，检测周期: 15 分钟")
    await asyncio.sleep(1)
    try:
        await asyncio.to_thread(_check_update_sync, True)
    except Exception:
        pass
    if not CFG.chromium_idle_seconds:
        try:
            await asyncio.to_thread(_warmup_browser_sync)
        except Exception as e:  # noqa: BLE001
            log.warning("【浏览器预热】异常: %s", e)
    while True:
        try:
            await asyncio.to_thread(_check_update_sync, True)
        except Exception:
            pass
        try:
            await run_keepalive_all(force=False)
        except Exception as e:  # noqa: BLE001
            log.error("【自动保活守护进程】轮询异常: %s", e)
        await asyncio.sleep(900)


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """管理员手动触发一次全账号保活续期。"""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "保活任务正在执行中，请稍候"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """手动针对单个账号执行保活续期。"""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """获取保活守护协程状态。"""
    return KEEPALIVE_STATE


# ------------------------- 仓库实时更新检测、通知与一键在线升级 -------------------------
REPO_SLUG = os.environ.get("MUSE2API_REPOSITORY", "mktminhqpbga-glitch/mac2API")
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPO_SLUG):
    raise ValueError("Invalid MUSE2API_REPOSITORY")
REPO_REF = os.environ.get("MUSE2API_REPO_REF", "main")
REPO_URL = f"https://github.com/{REPO_SLUG}"
MANAGED_DEPLOYMENT = (
    os.environ.get("MUSE2API_MANAGED_DEPLOYMENT") == "1"
    or os.environ.get("RENDER", "").lower() == "true"
    or bool(os.environ.get("RENDER_GIT_COMMIT"))
)
TRACKED_REPO_PATHS = [
    "app.py", "engine.py", "store.py", "cdp.py", "config.py", "proxy.py",
    "generation_timing.py", "thumbnails.py", "media_storage.py", "chat_providers.py", "web_providers.py", "GENERATION_QUEUE.md", "VERSION_REPORTING.md",
    "admin.html", "start-mac2api.command", "README.md", "AGENTS.md", "CHANGELOG.md", "version.json", "requirements.txt",
    "MAC2API_API_CONTRACT_TESTS.md",
    "Dockerfile", "docker-compose.yml", ".env.example", ".gitignore",
    "LICENSE", "extension", "deploy", "tools", "adapters",
]
_UPDATE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _read_env_key(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                s = ln.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _installed_sha_file() -> str:
    return os.path.join(CFG.data_dir, ".installed_sha")


def _git(args: list[str], timeout: int = 30):
    import subprocess
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", f"safe.directory={BASE_DIR}", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_git_repo(token: str = ""):
    """Initialize Git for the configured update source."""
    git_dir = os.path.join(BASE_DIR, ".git")
    remote_url = f"https://x-access-token:{token}@github.com/{REPO_SLUG}.git" if token else f"{REPO_URL}.git"
    if not os.path.isdir(git_dir):
        _git(["init", "-b", "main"])
        _git(["remote", "add", "origin", remote_url])
    else:
        _git(["remote", "set-url", "origin", remote_url])
    _git(["config", "user.name", "czg86389-hub"])
    _git(["config", "user.email", "czg86389-hub@users.noreply.github.com"])


def _compare_update_commits(local: str, remote: str, has_git: bool, headers: dict) -> str:
    """Describe remote relative to local, without fetching or modifying Git."""
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (local, remote)):
        return "unknown"
    if local == remote:
        return "identical"
    if has_git:
        base = _git(["merge-base", local, remote])
        if base.returncode == 0:
            ancestor = base.stdout.strip()
            if ancestor == local:
                return "ahead"
            if ancestor == remote:
                return "behind"
    import requests
    response = requests.get(
        f"https://api.github.com/repos/{REPO_SLUG}/compare/{local}...{remote}",
        params={"per_page": 1}, headers=headers, timeout=6,
    )
    response.raise_for_status()
    comparison = response.json().get("status")
    return comparison if comparison in ("ahead", "behind", "diverged") else "unknown"


def _check_update_sync(force: bool = False, _auto_upgrade: bool = False) -> dict:
    """A check is read-only. Report installed files and remote state separately."""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and now - _UPDATE_CACHE["ts"] < 90:
        return _UPDATE_CACHE["data"]
    import requests
    from urllib.parse import quote
    local = _read_local_version()
    local_version = str(local.get("version") or "unknown")
    local_sha = (os.environ.get("MUSE2API_BUILD_SHA") or os.environ.get("RENDER_GIT_COMMIT", "")).strip()
    has_git = os.path.exists(os.path.join(BASE_DIR, ".git"))
    if not local_sha and has_git:
        result = _git(["rev-parse", "HEAD"])
        if result.returncode == 0:
            local_sha = result.stdout.strip()
    if not local_sha and os.path.isfile(_installed_sha_file()):
        with open(_installed_sha_file()) as f:
            local_sha = f.read().strip()
    local_dirty = False
    if has_git:
        changed = _git(["status", "--porcelain", "--", *TRACKED_REPO_PATHS])
        local_dirty = changed.returncode == 0 and bool(changed.stdout.strip())
    remote_version, remote_sha, remote_commit, remote_msg, remote_time = "", "", "", "", ""
    commits, highlights, highlights_en, highlights_vi, errors = [], [], [], [], []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "muse2api-updater"}
    token = _read_env_key("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(
            f"https://api.github.com/repos/{REPO_SLUG}/commits",
            params={"sha": REPO_REF, "per_page": 5}, headers=headers, timeout=6)
        response.raise_for_status()
        raw_commits = response.json()
        remote_commit = raw_commits[0]["sha"] if raw_commits else ""
        for commit in raw_commits:
            info = commit.get("commit") or {}
            commits.append({"sha": commit["sha"][:7], "message": info.get("message", "").split("\n")[0],
                            "date": (info.get("committer") or {}).get("date", ""),
                            "url": commit.get("html_url") or REPO_URL})
        if commits:
            remote_sha, remote_msg, remote_time = (commits[0][k] for k in ("sha", "message", "date"))
    except Exception:
        errors.append("远端提交读取失败")
    # Pin release notes and version to the same commit as the reported remote SHA.
    try:
        response = requests.get(
            f"https://raw.githubusercontent.com/{REPO_SLUG}/{quote(remote_commit or REPO_REF, safe='')}/version.json",
            timeout=6)
        response.raise_for_status()
        remote = response.json()
        remote_version = str(remote.get("version") or "")
        highlights = list(remote.get("highlights") or [])
        highlights_en = list(remote.get("highlights_en") or [])
        highlights_vi = list(remote.get("highlights_vi") or [])
    except Exception:
        errors.append("远端版本文件读取失败")
    relation = "unknown"
    try:
        relation = _compare_update_commits(local_sha, remote_commit, has_git, headers)
    except Exception:
        errors.append("Không xác minh được quan hệ giữa hai commit.")
    verified = relation != "unknown" and bool(remote_version)
    has_update = verified and relation == "ahead"
    data = {"repo_url": REPO_URL, "repo_ref": REPO_REF, "has_git": has_git,
            "local_version": local_version, "local_sha": local_sha[:7], "remote_relation": relation,
            "remote_version": remote_version, "remote_sha": remote_sha, "remote_commit": remote_commit,
            "remote_msg": remote_msg, "remote_time": remote_time,
            "has_update": has_update, "up_to_date": (relation in ("identical", "behind") and not local_dirty) if verified else None,
            "local_dirty": local_dirty,
            "check_error": "; ".join(errors), "managed_deployment": MANAGED_DEPLOYMENT,
            "highlights": highlights, "highlights_en": highlights_en, "highlights_vi": highlights_vi,
            "recent_commits": commits, "checked_at": int(now)}
    _UPDATE_CACHE.update(ts=now, data=data)
    return data


def _upgrade_from_github_sync() -> dict:
    """Explicit upgrades pin one verified remote commit and preserve deployment data."""
    if MANAGED_DEPLOYMENT:
        raise HTTPException(409, "此实例通过镜像部署，请从配置的仓库分支构建并更新容器")
    if GEN_LOCK.locked() or any(t.get("status") in ("queued", "processing") for t in store.tasks.values()):
        raise HTTPException(409, "仍有任务排队或运行，请等待任务完成后更新")
    import requests
    import tarfile
    comparison = _check_update_sync(force=True, _auto_upgrade=False)
    if comparison.get("remote_relation") != "ahead" or not comparison.get("has_update"):
        raise HTTPException(409, "Chỉ cập nhật khi xác minh được commit từ xa nối tiếp bản đang chạy.")
    if comparison.get("local_dirty"):
        raise HTTPException(409, "Mã nguồn có thay đổi cục bộ. Hãy lưu thay đổi trước khi cập nhật.")
    target = comparison.get("remote_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise HTTPException(502, "无法确认远端目标提交，未执行更新")
    upgraded_via = ""
    try:
        _ensure_git_repo(_read_env_key("GITHUB_TOKEN"))
        for command in (["fetch", "origin", REPO_REF],
                        ["checkout", "-f", target, "--", "."],
                        ["reset", "--mixed", target]):
            result = _git(command, timeout=45)
            result.check_returncode()
        upgraded_via = "git"
    except Exception as exc:
        log.warning("Git 更新未完成，尝试固定提交归档 (%s)", type(exc).__name__)
    if not upgraded_via:
        response = requests.get(f"https://codeload.github.com/{REPO_SLUG}/tar.gz/{target}", timeout=60)
        if response.status_code != 200:
            raise HTTPException(502, f"下载更新包失败 (HTTP {response.status_code})")
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1].replace("\\", "/")
                if rel.startswith("/") or ".." in rel.split("/") or rel == ".env" or rel.startswith("data/") or rel.startswith(".git/"):
                    continue
                path = os.path.join(BASE_DIR, rel)
                if member.isdir():
                    os.makedirs(path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with archive.extractfile(member) as source, open(path, "wb") as out:
                        out.write(source.read())
        upgraded_via = "tarball"
    # Record what was installed, never a newer remote HEAD observed after the download.
    with open(_installed_sha_file(), "w") as f:
        f.write(target)
    _UPDATE_CACHE["ts"] = 0.0
    status = _check_update_sync(force=True, _auto_upgrade=False)
    return {"ok": True, "via": upgraded_via, "installed_sha": target,
            "message": f"已安装版本 {_read_local_version().get('version')} ({target[:7]})", "status": status}


@app.get("/admin/update/check")
@app.get("/admin/repo/status")
async def admin_check_update(force: bool = False):
    """供所有已部署节点实时检测 GitHub 官方仓库是否有新版本更新。"""
    return await asyncio.to_thread(_check_update_sync, force)


@app.post("/admin/update/upgrade")
@app.post("/admin/repo/pull")
async def admin_upgrade_now(payload: dict = Body(default={}), _=Depends(auth)):
    """一键从 GitHub 官方仓库拉取最新更新并自动平滑重启服务。"""
    res = await asyncio.to_thread(_upgrade_from_github_sync)
    restart = payload.get("restart", True) if isinstance(payload, dict) else True
    if restart:
        def _delayed_restart():
            time.sleep(0.5)
            try:
                engine.stop()
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_delayed_restart, daemon=True).start()
    return res


@app.post("/admin/repo/push")
async def admin_repo_push(payload: dict = Body(default={}), _=Depends(auth)):
    """维护者专用：将当前节点核心代码推送到 GitHub 仓库（自动过滤 .env 与 data 目录）。"""
    msg = (payload.get("message") or "").strip() or f"chore: sync update ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    new_token = (payload.get("github_token") or "").strip()
    if new_token:
        _persist_env("GITHUB_TOKEN", new_token)
        os.environ["GITHUB_TOKEN"] = new_token
    token = new_token or _read_env_key("GITHUB_TOKEN")

    def _do_push():
        _ensure_git_repo(token)
        existing_paths = [p for p in TRACKED_REPO_PATHS if os.path.exists(os.path.join(BASE_DIR, p))]
        _git(["add", "--", *existing_paths])
        st = _git(["status", "--porcelain", "--", *existing_paths])
        committed = False
        if st.stdout.strip():
            c_res = _git(["commit", "-m", msg])
            if c_res.returncode != 0:
                raise HTTPException(500, f"Git commit 失败: {c_res.stderr or c_res.stdout}")
            committed = True
        p_res = _git(["push", "origin", "HEAD:main"], timeout=60)
        if p_res.returncode != 0:
            err = (p_res.stderr or p_res.stdout or "").strip()
            raise HTTPException(500, f"Git push 失败: {err[:300]}")
        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True)
        return {
            "ok": True,
            "committed": committed,
            "message": "已成功提交并推送到 GitHub 仓库",
            "status": status,
        }

    return await asyncio.to_thread(_do_push)


def _has_queued_media_work():
    return any(task.get("status") in ("queued", "processing")
               for task in list(store.tasks.values()))


async def _chromium_idle_loop():
    idle_seconds = CFG.chromium_idle_seconds
    while True:
        await asyncio.sleep(min(idle_seconds, 2))
        try:
            stopped = await asyncio.to_thread(
                GEN_LOCK.stop_browser_if_idle, engine, idle_seconds, _has_queued_media_work
            )
            if stopped:
                log.info("Chromium đã tắt sau %s giây không có tác vụ Muse.", idle_seconds)
        except Exception:
            log.exception("Không thể tắt Chromium khi rảnh")


@app.on_event("startup")
async def _startup():
    for task in list(store.tasks.values()):
        if task.get("kind") in ("image", "video") and task.get("status") in ("queued", "processing"):
            store.transition_task(task["id"], task["status"], status="interrupted", needs_reconcile=True,
                                  error="Dịch vụ đã khởi động lại. Hãy đối chiếu kết quả trước khi gửi yêu cầu mới.")
    if not CFG.api_key:
        import secrets
        new_key = "m2a_" + secrets.token_hex(24)
        _persist_env("MUSE2API_KEY", new_key)
        CFG.api_key = new_key
        log.info("Đã tạo khóa quản trị trong tệp .env của dịch vụ.")
    app.state.keepalive_task = asyncio.create_task(_keepalive_loop())
    if CFG.chromium_idle_seconds:
        app.state.chromium_idle_task = asyncio.create_task(_chromium_idle_loop())


@app.on_event("shutdown")
async def _shutdown():
    loops = [task for name in ("chromium_idle_task", "keepalive_task")
             if (task := getattr(app.state, name, None)) is not None]
    for loop in loops:
        loop.cancel()
    await asyncio.gather(*loops, return_exceptions=True)
    await asyncio.to_thread(web_providers.close)
    await asyncio.to_thread(engine.stop)
