"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import os
import tempfile
import threading
import time
import uuid

from proxy import public_proxy

_LOCK = threading.Lock()
log = logging.getLogger("muse2api")

# 登录会话存在性检查；可用性仍须经上游认证（与 engine 一致）
# hatch_vml 是可选工作区令牌，缺失不能据此拒绝会话导入
ESSENTIAL_COOKIES = ("hatch_sess",)


def _is_pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# hatch_vml 固定约 2 天，且不因使用而延长 —— 它就是决定账号寿命的那条 cookie。
# 实测对照：生成前后 synced_at 变了、也捞到新 cookie，但 4 条核心 cookie 的
# expires 一动不动。所以拿不到 hatch_vml 的 expires 时，按「导入时间 + 2 天」估算，
# 而不是退回到 hatch_native_auth_device 的 30 天（那个不决定生死，会误导用户）。
VML_TTL = 2 * 86400


def min_expiry(cookies_exp: dict | None) -> int | None:
    """核心 cookie 里最早过期的那个（通用兜底，不区分哪条决定寿命）。"""
    if not cookies_exp:
        return None
    vals = []
    for name in ESSENTIAL_COOKIES:
        v = cookies_exp.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        for v in cookies_exp.values():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    return min(vals) if vals else None


def account_expiry(cookies_exp: dict | None,
                   base_ts: int | None = None) -> int | None:
    """账号实际失效时间。

    优先级：
      1) hatch_vml 的真实 expires（最准）；
      2) 拿不到就用 base_ts(导入/同步时刻) + VML_TTL 估算 —— 因为寿命就是 2 天；
      3) 都没有再退回核心 cookie 最早过期。
    """
    ce = cookies_exp or {}
    vml = ce.get("hatch_vml")
    if _is_pos(vml):
        return int(vml)
    if base_ts:
        return int(base_ts) + VML_TTL
    return min_expiry(ce)


def _read(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write(path: str, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ClientRevisionConflict(RuntimeError):
    """The saved key changed after the admin opened its editor."""


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.accounts: list[dict] = _read(cfg.accounts_file, [])
        for a in self.accounts:
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                a["expires_at"] = int(ce["hatch_vml"])
        self.tasks: dict[str, dict] = _read(cfg.tasks_file, {})
        self.clients_file = os.path.join(cfg.data_dir, "api-clients.json")
        self.clients = _read(self.clients_file, {})
        self._client_requests: dict[str, list[float]] = {}
        self._client_active: dict[str, int] = {}

    def load_admin_key(self, configured: str) -> str:
        path = os.path.join(self.cfg.data_dir, "admin-key.json")
        saved = _read(path, {})
        source = hashlib.sha256(configured.encode()).hexdigest()
        if (isinstance(saved, dict) and saved.get("source") == source
                and isinstance(saved.get("key"), str) and saved["key"]):
            return saved["key"]
        key = configured or "m2a_" + secrets.token_hex(24)
        if saved or not configured:
            self.save_admin_key(configured, key)
        return key

    def save_admin_key(self, configured: str, key: str):
        source = hashlib.sha256(configured.encode()).hexdigest()
        with _LOCK:
            _write(os.path.join(self.cfg.data_dir, "admin-key.json"), {"source": source, "key": key})

    @staticmethod
    def _public_client(client: dict) -> dict:
        return {key: value for key, value in client.items() if key != "digest"}

    def list_clients(self):
        with _LOCK:
            return [self._public_client(client) for client in self.clients.values()]

    def create_client(self, label: str, providers: list[str], **settings):
        client_id = uuid.uuid4().hex[:16]
        token = f"m2c_{client_id}.{secrets.token_urlsafe(32)}"
        client = {**settings, "id": client_id, "label": label, "providers": providers,
                  "digest": hashlib.sha256(token.encode()).hexdigest(), "created_at": int(time.time()),
                  "revision": 0}
        with _LOCK:
            clients = {**self.clients, client_id: client}
            _write(self.clients_file, clients)
            self.clients = clients
        return {**self._public_client(client), "api_key": token}

    def get_client(self, client_id: str, *, active_only=False):
        client = self.clients.get(client_id)
        if client and (not active_only or (not client.get("revoked_at") and client.get("enabled", True))):
            return self._public_client(client)
        return None

    def update_client(self, client_id: str, *, rotate=False, revoke=False, expected_revision=None, **changes):
        with _LOCK:
            client = self.clients.get(client_id)
            if not client or client.get("revoked_at"):
                return None
            revision = client.get("revision", 0)
            if expected_revision is not None and revision != expected_revision:
                raise ClientRevisionConflict
            client = {**client, **changes, "updated_at": int(time.time()), "revision": revision + 1}
            token = None
            if rotate:
                token = f"m2c_{client_id}.{secrets.token_urlsafe(32)}"
                client.update(digest=hashlib.sha256(token.encode()).hexdigest(),
                              key_version=client.get("key_version", 0) + 1)
            if revoke:
                client.update(revoked_at=int(time.time()), enabled=False)
                client.pop("digest", None)
            clients = {**self.clients, client_id: client}
            _write(self.clients_file, clients)
            self.clients = clients
            return {**self._public_client(client), **({"api_key": token} if token else {})}

    def revoke_client(self, client_id: str) -> bool:
        return self.update_client(client_id, revoke=True) is not None

    def find_client(self, token: str) -> dict | None:
        match = re.fullmatch(r"m2c_([0-9a-f]{16})\.[A-Za-z0-9_-]{43}", token)
        if not match:
            return None
        client = self.clients.get(match[1])
        if (client and client.get("enabled", True) and not client.get("revoked_at")
                and hmac.compare_digest(client.get("digest", ""), hashlib.sha256(token.encode()).hexdigest())):
            return self._public_client(client)
        return None

    def acquire_client_request(self, client_id: str, rate_limit: int, active_limit: int) -> bool:
        now = time.monotonic()
        with _LOCK:
            if not self.get_client(client_id, active_only=True):
                return False
            recent = [seen for seen in self._client_requests.get(client_id, []) if seen > now - 60]
            active = self._client_active.get(client_id, 0)
            if len(recent) >= rate_limit or active >= active_limit:
                return False
            recent.append(now)
            self._client_requests[client_id] = recent
            self._client_active[client_id] = active + 1
            return True

    def release_client_request(self, client_id: str):
        with _LOCK:
            active = self._client_active.get(client_id, 0)
            if active > 1:
                self._client_active[client_id] = active - 1
            else:
                self._client_active.pop(client_id, None)

    def web_chrome_enabled(self):
        return _read(os.path.join(self.cfg.data_dir, "web-chrome.json"), False) is True

    def web_chrome_profile_preferred(self):
        return _read(os.path.join(self.cfg.data_dir, "web-chrome.json"), False) == "profile"

    def enable_web_chrome(self, *, profile=False):
        with _LOCK:
            _write(os.path.join(self.cfg.data_dir, "web-chrome.json"), "profile" if profile else True)

    def muse_prompt_language(self):
        settings = _read(os.path.join(self.cfg.data_dir, "muse-prompt.json"), {})
        language = settings.get("language") if isinstance(settings, dict) else None
        return language if language in ("vi", "en", "zh") else "vi"

    def save_muse_prompt_language(self, language):
        with _LOCK:
            _write(os.path.join(self.cfg.data_dir, "muse-prompt.json"), {"language": language})

    # ---------- 账号 ----------
    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None) -> dict:
        with _LOCK:
            aid = uuid.uuid4().hex[:12]
            exp = {k: int(v) for k, v in (cookies_exp or {}).items()
                   if _is_pos(v)}
            now = int(time.time())
            acc = {"id": aid, "label": label or aid, "cookies": cookies,
                   "cookies_exp": exp, "expires_at": account_expiry(exp, now),
                   "expiry_anchor": None,
                   "enabled": True, "created_at": now,
                   "last_used": None, "last_keepalive": None, "use_count": 0,
                   "ok": None, "note": "", "synced_at": None}
            self.accounts.append(acc)
            _write(self.cfg.accounts_file, self.accounts)
            return acc

    @staticmethod
    def public_account(account: dict) -> dict:
        item = {k: v for k, v in account.items() if k not in ("cookies", "proxy")}
        item["proxy"] = public_proxy(account.get("proxy"))
        item["cookie_count"] = len(account.get("cookies", {}))
        return item

    def list_accounts(self) -> list[dict]:
        out = []
        for a in self.accounts:
            item = self.public_account(a)
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                item["expires_at"] = int(ce["hatch_vml"])
            item["cookie_count"] = len(a.get("cookies", {}))
            item["essential_ok"] = all(
                a.get("cookies", {}).get(n) for n in ESSENTIAL_COOKIES)
            # 有效期是 hatch_vml 的实测 expires，还是按「2 天寿命」推算的
            item["expires_estimated"] = not _is_pos(ce.get("hatch_vml"))
            out.append(item)
        return out

    def delete_account(self, aid: str) -> bool:
        with _LOCK:
            n = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["id"] != aid]
            _write(self.cfg.accounts_file, self.accounts)
            return len(self.accounts) != n

    def pick_account(self, preferred_id: str | None = None, rotate: bool = True,
                     force_rotate: bool = False, exclude_id: str | None = None,
                     exclude_ids: set[str] | list[str] | None = None,
                     record_use: bool = True, allowed_ids: set[str] | None = None) -> dict | None:
        """选择可用账号：优先复用当前已预热的健康账号（减少切号重连耗时），每满 15 次或遇错自动轮转。
        exclude_ids: 需要排除的账号 ID 集合（例如正在被其他并发线程占用的账号）。"""
        with _LOCK:
            live = [a for a in self.accounts if a.get("enabled", True) and a.get("cookies")
                    and (allowed_ids is None or (a["id"] in allowed_ids and a.get("ok") is not False))]
            if not live:
                return None
            blocked = set(exclude_ids or [])
            if exclude_id:
                blocked.add(exclude_id)
            if blocked:
                available = [a for a in live if a["id"] not in blocked]
                if available:
                    live = available
                elif exclude_ids is not None:
                    # 并发模式下要求避开所有繁忙账号，但当前无剩余空闲账号
                    return None
                elif len(live) > 1:
                    live = [a for a in live if a["id"] != exclude_id] or live
            if preferred_id and not rotate and not force_rotate:
                for a in live:
                    if a["id"] == preferred_id and a.get("ok") is not False:
                        self._sticky_id = preferred_id
                        self._sticky_count = 1
                        if record_use:
                            a["last_used"] = time.time()
                            a["use_count"] = a.get("use_count", 0) + 1
                            _write(self.cfg.accounts_file, self.accounts)
                        return a
            healthy = [a for a in live if a.get("ok") is not False]
            candidates = healthy if healthy else live
            candidates.sort(key=lambda a: (a.get("last_used") or 0.0, a.get("use_count") or 0))
            acc = candidates[0]
            self._sticky_id = acc["id"]
            self._sticky_count = 1
            if record_use:
                acc["last_used"] = time.time()
                acc["use_count"] = acc.get("use_count", 0) + 1
                _write(self.cfg.accounts_file, self.accounts)
            return acc

    def touch_keepalive(self, aid: str, ok: bool | None = True, note: str = ""):
        """ok=None 仅记录未确认的检测；保留上次账号状态、成功保活时间和有效期。"""
        with _LOCK:
            now_ts = int(time.time())
            for a in self.accounts:
                if a["id"] == aid:
                    if ok is not None:
                        a["ok"] = ok
                    if ok is True:
                        a["last_keepalive"] = now_ts
                        # ponytail: 只信实际 Cookie 到期时间或原导入锚点，不凭检测成功虚推 48h。
                        a["expires_at"] = account_expiry(
                            a.get("cookies_exp"), a.get("expiry_anchor") or a.get("created_at"))
                    if note:
                        a["note"] = note[:300]
                    a["checked_at"] = now_ts
            _write(self.cfg.accounts_file, self.accounts)

    def mark(self, aid: str, ok: bool, note: str = ""):
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    a["ok"] = ok
                    a["note"] = note[:300]
                    a["checked_at"] = int(time.time())
            _write(self.cfg.accounts_file, self.accounts)

    def get_account(self, aid: str) -> dict | None:
        for a in self.accounts:
            if a["id"] == aid:
                return a
        return None

    def update_account(self, aid: str, **kw) -> dict | None:
        """更新标签 / 启用状态 / cookies / 有效期等；值为 None 的字段跳过。

        传了 cookies_exp 会自动重算 expires_at，不用调用方自己算。
        估算锚点 expiry_anchor 的维护规则（避免有效期被反复虚推）：
          - 读到了 hatch_vml 的真实 expires  -> 清空锚点，直接用真值；
          - 读不到且锚点为空                -> 把锚点钉在当下（只钉一次）；
          - 补入全新会话 cookie（重登）      -> 调用方显式传 expiry_anchor=now 重置。
        """
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    for k, v in kw.items():
                        if v is not None or k == "ok":
                            a[k] = v
                    if "cookies_exp" in kw and kw["cookies_exp"] is not None:
                        ce = a["cookies_exp"]
                        if _is_pos(ce.get("hatch_vml")):
                            a["expiry_anchor"] = None
                        elif not a.get("expiry_anchor"):
                            a["expiry_anchor"] = int(time.time())
                        a["expires_at"] = account_expiry(
                            ce, a.get("expiry_anchor") or a.get("created_at"))
                    _write(self.cfg.accounts_file, self.accounts)
                    return a
        return None

    def stats(self) -> dict:
        now = int(time.time())
        total = len(self.accounts)
        enabled = sum(1 for a in self.accounts if a.get("enabled", True))
        bad = sum(1 for a in self.accounts
                  if a.get("enabled", True) and a.get("ok") is False)
        healthy = sum(1 for a in self.accounts
                      if a.get("enabled", True) and a.get("ok") is True)
        expiring = sum(1 for a in self.accounts
                       if a.get("enabled", True)
                       and a.get("expires_at")
                       and a["expires_at"] - now < 3 * 86400)
        expired = sum(1 for a in self.accounts
                      if a.get("enabled", True)
                      and a.get("expires_at") and a["expires_at"] <= now)
        return {"total": total, "enabled": enabled, "disabled": total - enabled,
                "healthy": healthy, "error": bad,
                "expiring": expiring, "expired": expired}

    # ---------- 任务 ----------
    def create_task(self, kind: str, prompt: str, *, task_id=None, **metadata) -> dict:
        with _LOCK:
            tid = task_id or "task_" + uuid.uuid4().hex[:20]
            t = {"id": tid, "object": "task", "kind": kind, "prompt": prompt,
                 "status": "queued", "created_at": int(time.time()),
                 "updated_at": int(time.time()), "result": None, "error": None, **metadata}
            tasks = {**self.tasks, tid: t}
            _write(self.cfg.tasks_file, tasks)
            self.tasks = tasks
            return t

    def update_task(self, tid: str, *, expected_status=None, **kw):
        with _LOCK:
            t = self.tasks.get(tid)
            if not t or (expected_status and t.get("status") != expected_status):
                return None
            updated = {**t, **kw, "updated_at": int(time.time())}
            tasks = {**self.tasks, tid: updated}
            try:
                _write(self.cfg.tasks_file, tasks)
            except OSError as exc:
                # Generation may already have happened. Preserve the observed state for
                # polling and release its queue slot while the service is still running.
                log.warning("Không lưu được trạng thái tác vụ %s (%s).", tid, type(exc).__name__)
            self.tasks = tasks
            return updated

    def get_task(self, tid: str) -> dict | None:
        task = self.tasks.get(tid)
        return task if task and not task.get("receipt_only") else None

    def transition_task(self, tid: str, expected: str, **changes):
        with _LOCK:
            current = self.tasks.get(tid)
            if not current or current.get("status") != expected:
                return None
            task = {**current, **changes, "updated_at": int(time.time())}
            tasks = {**self.tasks, tid: task}
            _write(self.cfg.tasks_file, tasks)
            self.tasks = tasks
            return task

    def list_tasks(self, limit: int = 50) -> list[dict]:
        items = sorted((task for task in self.tasks.values() if not task.get("receipt_only")),
                       key=lambda t: t.get("created_at") or 0, reverse=True)
        return items[:limit]

    @staticmethod
    def _remove_task_history(tasks, task):
        task_id = task["id"]
        if task.get("request_key") or task.get("image_request_key"):
            fields = ("id", "kind", "provider", "owner", "status", "progress", "created_at",
                      "request_key", "request_hash", "image_request_key", "image_request_hash")
            tasks[task_id] = {**{key: task[key] for key in fields if key in task}, "receipt_only": True}
        else:
            del tasks[task_id]

    def delete_task(self, tid: str) -> bool:
        with _LOCK:
            task = self.get_task(tid)
            if task and task.get("status") in ("queued", "processing"):
                raise ValueError("Không thể xóa tác vụ đang chờ hoặc đang chạy.")
            if task:
                tasks = dict(self.tasks)
                self._remove_task_history(tasks, task)
                _write(self.cfg.tasks_file, tasks)
                self.tasks = tasks
                return True
            return False

    def clear_tasks(self, keep: int = 0) -> int:
        with _LOCK:
            items = sorted((task for task in self.tasks.values() if not task.get("receipt_only")),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
            keep_ids = {t["id"] for t in items[:keep]}
            keep_ids.update(t["id"] for t in items if t.get("status") in ("queued", "processing"))
            removed = len(items) - len(keep_ids)
            tasks = dict(self.tasks)
            for task in items:
                if task["id"] not in keep_ids:
                    self._remove_task_history(tasks, task)
            _write(self.cfg.tasks_file, tasks)
            self.tasks = tasks
            return max(0, removed)
