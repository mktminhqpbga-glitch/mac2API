"""Dispatch mac2API jobs to explicitly paired local Chrome extensions."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable

from store import _read, _write
from web_providers import PROVIDERS, WebPostSendError, WebProviderError

EXTENSION_PROVIDERS = frozenset((*PROVIDERS, "chatgpt"))
HEARTBEAT_TTL = 45
CLEANUP_SECONDS = 8
ERRORS = {
    "session": "Hồ sơ Chrome chưa đăng nhập hoặc phiên đã hết hạn. Mở trang AI rồi kết nối lại trong tiện ích.",
    "page_unavailable": "Chrome chưa tải được trang AI. Tác vụ dừng trước khi gửi. Hãy kiểm tra mạng và mở thử trang AI trong hồ sơ đó.",
    "composer": "Không tìm thấy ô nhập sạch hoặc nút Gửi trong tab mới. Tiện ích đã dừng tác vụ.",
    "interrupted": "Mất kết nối với tab Chrome khi xử lý tác vụ.",
    "timeout": "Hết thời gian chờ Chrome xử lý tác vụ.",
    "media_missing": "Trang web chỉ trả văn bản hoặc chưa trả tệp ảnh hợp lệ.",
    "media_unreadable": "Chrome không đọc được ảnh kết quả hoặc ảnh vượt 20 MiB.",
    "close_failed": "Chrome chưa xác nhận đóng tab tác vụ. Hãy kiểm tra tab trước khi thử lại.",
}


@dataclass
class BrowserJob:
    id: str
    prompt: str
    image: bool
    deadline: float
    check_permission: Callable[[], None]
    claimed: bool = False
    authorized: bool = False
    finished: bool = False
    error: Exception | None = None
    result: str | tuple[bytes, str] | None = None


class ChromeBridge:
    def __init__(self, data_dir, *, is_managed=None):
        self.path = os.path.join(data_dir, "chrome-extensions.json")
        self.accounts = _read(self.path, {})
        self.condition = threading.Condition()
        self.seen = {}
        self.expired_sessions = set()
        self.last_used = {}
        self.reserved = set()
        self.jobs = {}
        self.is_managed = is_managed or (lambda aid: False)

    def pair(self, provider, profile, label):
        if provider not in EXTENSION_PROVIDERS:
            raise WebProviderError("Không tìm thấy nhà cung cấp web.", 404)
        aid = f"ext-{provider}-{profile}"
        with self.condition:
            if self.is_managed(aid):
                raise WebProviderError("Hồ sơ này dùng Chromium riêng. Hãy mở tài khoản trong mac2API để đăng nhập.", 409)
            if aid in self.reserved:
                raise WebProviderError("Hồ sơ Chrome đang chạy tác vụ. Hãy đợi tác vụ kết thúc.", 409)
            token = f"{aid}.{secrets.token_urlsafe(32)}"
            record = {"id": aid, "label": label, "provider": provider,
                      "enabled": self.accounts.get(aid, {}).get("enabled", True),
                      "digest": hashlib.sha256(token.encode()).hexdigest()}
            if provider == "chatgpt":
                record["direct_link"] = True
            accounts = {**self.accounts, aid: record}
            _write(self.path, accounts)
            self.accounts = accounts
            self.seen.pop(aid, None)
            self.expired_sessions.discard(aid)
        return token

    def revoke(self, provider, aid):
        with self.condition:
            account = self.accounts.get(aid)
            if not account or account["provider"] != provider:
                raise WebProviderError("Không tìm thấy hồ sơ tiện ích Chrome.", 404)
            if aid in self.reserved:
                raise WebProviderError("Hồ sơ Chrome đang chạy tác vụ. Hãy đợi tác vụ kết thúc.", 409)
            accounts = {name: record for name, record in self.accounts.items() if name != aid}
            _write(self.path, accounts)
            self.accounts = accounts
            self.seen.pop(aid, None)
            self.expired_sessions.discard(aid)
            self.last_used.pop(aid, None)
            self.condition.notify_all()

    def _authenticate(self, token):
        account = self.accounts.get(token.partition(".")[0])
        if not account or not self._supported(account) or self.is_managed(account["id"]) or not hmac.compare_digest(
            account["digest"], hashlib.sha256(token.encode()).hexdigest()
        ):
            raise WebProviderError("Kết nối tiện ích đã hết hiệu lực. Hãy liên kết lại.", 401)
        return account["id"]

    @staticmethod
    def _supported(account):
        provider = account["provider"]
        return provider in EXTENSION_PROVIDERS and (provider != "chatgpt" or account.get("direct_link") is True)

    def _connected(self, aid):
        return self.seen.get(aid, -HEARTBEAT_TTL) > time.monotonic() - HEARTBEAT_TTL

    @contextmanager
    def operation(self, provider, aid):
        with self.condition:
            account = self.accounts.get(aid)
            if not account or account["provider"] != provider or not self._supported(account) or self.is_managed(aid):
                raise WebProviderError("Không tìm thấy hồ sơ tiện ích Chrome.", 404)
            if aid in self.reserved:
                raise WebProviderError("Hồ sơ Chrome đang được dùng. Hãy đợi tác vụ kết thúc.", 409)
            self.reserved.add(aid)
        try:
            yield account
        finally:
            with self.condition:
                self.reserved.discard(aid)
                self.condition.notify_all()

    def update(self, account, changes):
        with self.condition:
            updated = {**self.accounts, account["id"]: {**account, **changes}}
            try:
                _write(self.path, updated)
            except OSError as exc:
                raise WebProviderError("Không lưu được cài đặt tài khoản Chrome.", 507) from exc
            self.accounts = updated

    def list_accounts(self):
        with self.condition:
            return [{key: item[key] for key in ("id", "label", "provider")}
                    | {"enabled": item.get("enabled", True), "verified": self._connected(aid) and aid not in self.expired_sessions,
                       "busy": aid in self.reserved}
                    for aid, item in self.accounts.items() if self._supported(item) and not self.is_managed(aid)]

    def state(self, provider, allowed_ids=None):
        accounts = [item for item in self.list_accounts() if item["provider"] == provider
                    and (allowed_ids is None or item["id"] in allowed_ids)]
        if not accounts:
            return None
        ready = [item for item in accounts if item["verified"] and item["enabled"]]
        busy = bool(ready) and all(item["busy"] for item in ready)
        return {"configured": True, "connected": bool(ready), "ready": bool(ready), "busy": busy,
                "reason": "busy" if busy else "ready" if ready else "session_unverified"}

    @contextmanager
    def reserve(self, provider, deadline, guard, allowed_accounts):
        account = None
        with self.condition:
            while True:
                if guard:
                    guard()
                allowed = allowed_accounts() if allowed_accounts else None
                ready = [item for aid, item in self.accounts.items() if item["provider"] == provider
                         and self._supported(item) and item.get("enabled", True) and not self.is_managed(aid)
                         and (allowed is None or aid in allowed) and self._connected(aid)
                         and aid not in self.expired_sessions]
                if not ready:
                    break
                account = min((item for item in ready if item["id"] not in self.reserved),
                              key=lambda item: self.last_used.get(item["id"], 0), default=None)
                if account:
                    self.reserved.add(account["id"])
                    self.last_used[account["id"]] = time.monotonic()
                    break
                remaining = deadline - time.monotonic() if deadline is not None else 0.25
                if remaining <= 0:
                    raise WebProviderError("Hết thời gian chờ hồ sơ tiện ích Chrome.", 429)
                self.condition.wait(min(remaining, 0.25))
        try:
            yield account
        finally:
            if account:
                with self.condition:
                    self.reserved.discard(account["id"])
                    self.condition.notify_all()

    def generate(self, aid, prompt, *, image, timeout, check_permission):
        if image:
            prompt = "Hãy tạo một ảnh mới dựa trên yêu cầu sau và trả ảnh trong câu trả lời.\n" + prompt
        job = BrowserJob(uuid.uuid4().hex, prompt, image, time.monotonic() + timeout, check_permission)
        with self.condition:
            if not self._connected(aid):
                raise WebProviderError("Tiện ích Chrome đã ngắt kết nối. Hãy mở lại Chrome.", 503)
            self.jobs[aid] = job
            try:
                while not job.finished:
                    deadline = job.deadline + (CLEANUP_SECONDS if job.claimed else 0)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._connected(aid):
                        error_type = WebPostSendError if job.authorized else WebProviderError
                        if job.error:
                            raise job.error
                        raise error_type("Hết thời gian chờ tiện ích Chrome. Kiểm tra tab tác vụ trước khi thử lại.", 504)
                    self.condition.wait(min(remaining, 1))
                if job.error:
                    raise job.error
                return job.result
            finally:
                del self.jobs[aid]

    def next_job(self, token):
        with self.condition:
            aid = self._authenticate(token)
            self.seen[aid] = time.monotonic()
            job = self.jobs.get(aid)
            if job and not job.claimed and job.deadline > time.monotonic():
                job.claimed = True
                return {"id": job.id, "prompt": job.prompt, "image": job.image,
                        "remaining_ms": max(1, int((job.deadline - time.monotonic()) * 1000))}

    def report_kind(self, token, job_id):
        with self.condition:
            job = self.jobs.get(self._authenticate(token))
            if job and job.id == job_id and job.claimed and not job.finished:
                return job.image

    def job_action(self, token, job_id, *, permit=False, heartbeat=False, result=None, error=None):
        with self.condition:
            aid = self._authenticate(token)
            self.seen[aid] = time.monotonic()
            job = self.jobs.get(aid)
            if not job or job.id != job_id or not job.claimed or job.finished:
                return False
            if heartbeat:
                return not job.error and time.monotonic() < job.deadline
            if permit and not job.authorized and not job.error and time.monotonic() < job.deadline:
                try:
                    job.check_permission()
                except Exception as exc:
                    # The worker still owns the tab. Release the slot when its cleanup arrives.
                    job.error = exc
                    return False
                job.authorized = True
                return True
            if permit:
                return False
            if error:
                if error == "session":
                    self.expired_sessions.add(aid)
                if error == "close_failed":
                    self.seen.pop(aid, None)
                error_type = WebPostSendError if job.authorized and error in (
                    "interrupted", "timeout", "close_failed"
                ) else WebProviderError
                status = {"timeout": 504, "session": 409, "page_unavailable": 503}.get(error, 502)
                job.error = job.error or error_type(ERRORS[error], status)
            elif job.authorized and result is not None and job.error is None:
                job.result = result
            else:
                return False
            job.finished = True
            self.condition.notify_all()
            return True
