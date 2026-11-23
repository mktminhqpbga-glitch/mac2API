"""Local, persistent browser profiles for accounts signed in by the operator."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from cdp import CDP
from proxy import HTTPProxyRelay, public_proxy
from store import _read, _write
from web_providers import CHROME_ERRORS, ChromeProfileConnection, WebProviderError


def visible_chrome_available():
    if os.environ.get("RENDER", "").lower() == "true" or os.environ.get("RENDER_GIT_COMMIT"):
        return False
    return sys.platform in ("darwin", "win32") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class ManagedBrowser:
    def __init__(self, cfg, account, *, visible, url="about:blank"):
        directory = Path(cfg.profile_root) / "web-accounts" / account["id"]
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.proc = None
        self.browser = None
        self.relay = None
        self.visible = visible
        args = [cfg.chromium, f"--user-data-dir={directory}", "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
                "--disable-dev-shm-usage"]
        if not visible:
            args.append("--headless=new")
        if getattr(os, "geteuid", lambda: None)() == 0:
            args.append("--no-sandbox")
        marker = directory / "DevToolsActivePort"
        try:
            old_mtime = marker.stat().st_mtime_ns if marker.exists() else None
            if account.get("proxy"):
                self.relay = HTTPProxyRelay(account["proxy"])
                args += [f"--proxy-server={self.relay.url}", "--proxy-bypass-list=<-loopback>",
                         "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
            self.proc = subprocess.Popen([*args, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 12
            while self.proc.poll() is None and time.monotonic() < deadline:
                try:
                    if marker.stat().st_mtime_ns != old_mtime:
                        port, path = marker.read_text(encoding="ascii").splitlines()
                        if 0 < int(port) < 65536 and re.fullmatch(r"/devtools/browser/[\w-]+", path, re.ASCII):
                            self.browser = CDP(f"ws://127.0.0.1:{int(port)}{path}", timeout=2, suppress_origin=True)
                            return
                except CHROME_ERRORS:
                    pass
                time.sleep(0.15)
        except (OSError, ValueError):
            pass
        self.close()
        raise WebProviderError("Không khởi động được hồ sơ Chromium riêng. Hãy kiểm tra Chromium và cấu hình mạng.", 503)

    def close(self):
        try:
            if self.browser is not None:
                try:
                    self.browser.send("Browser.close", timeout=1)
                except CHROME_ERRORS:
                    pass
                self.browser.close()
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=3)
        finally:
            if self.relay:
                self.relay.close()

    @contextmanager
    def page(self, url):
        target = self.browser.send("Target.createTarget", {"url": url})["result"]["targetId"]
        try:
            with ChromeProfileConnection(self.browser).attach(target) as page:
                yield page
        finally:
            try:
                self.browser.send("Target.closeTarget", {"targetId": target})
            except CHROME_ERRORS:
                pass


class ManagedWebAccounts:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = os.path.join(cfg.data_dir, "web-accounts.json")
        saved = _read(self.path, {})
        self.accounts = saved if isinstance(saved, dict) else {}
        self.condition = threading.Condition()
        self.in_use = set()
        self.login_windows = {}
        self.last_used = {}

    def _save(self, account):
        updated = {**self.accounts, account["id"]: account}
        try:
            _write(self.path, updated)
        except OSError as error:
            raise WebProviderError("Không lưu được thông tin hồ sơ web.", 507) from error
        self.accounts = updated
        self.condition.notify_all()

    def create(self, provider, label):
        account = {"id": uuid.uuid4().hex, "provider": provider, "label": label,
                   "proxy": None, "enabled": False, "verified": False}
        with self.condition:
            self._save(account)
        return account

    def update(self, account, **changes):
        with self.condition:
            changed = {**self.accounts[account["id"]], **changes}
            self._save(changed)
            return changed

    def public(self, account):
        return {**account, "proxy": public_proxy(account.get("proxy")), "busy": account["id"] in self.in_use}

    def list_accounts(self):
        with self.condition:
            return [self.public(account) for account in self.accounts.values()]

    def enabled(self, provider):
        return [account for account in self.accounts.values()
                if account["provider"] == provider and account["enabled"]]

    def state(self, provider, allowed_ids=None):
        with self.condition:
            accounts = [account for account in self.enabled(provider)
                        if allowed_ids is None or account["id"] in allowed_ids]
            if not accounts:
                if allowed_ids is not None:
                    return {"configured": False, "connected": False, "ready": False,
                            "busy": False, "reason": "not_configured"}
                return None
            verified = [account["id"] for account in accounts if account["verified"]]
            busy = bool(verified) and all(aid in self.in_use for aid in verified)
            return {"configured": True, "connected": any(
                        aid in self.login_windows and self.login_windows[aid].proc.poll() is None for aid in verified),
                    "ready": bool(verified), "busy": busy,
                    "reason": "busy" if busy else "ready" if verified else "session_unverified"}

    @contextmanager
    def operation(self, provider, aid):
        with self.condition:
            account = self.accounts.get(aid)
            if not account or account["provider"] != provider:
                raise WebProviderError("Không tìm thấy tài khoản web.", 404)
            if aid in self.in_use:
                raise WebProviderError("Tài khoản đang được sử dụng. Hãy thử lại khi tác vụ kết thúc.", 409)
            self.in_use.add(aid)
        try:
            yield account
        finally:
            self._release(aid)

    @contextmanager
    def reserve(self, provider, deadline, guard, allowed_accounts=None):
        with self.condition:
            account = None
            while True:
                if guard:
                    guard()
                enabled = self.enabled(provider)
                allowed_ids = allowed_accounts() if allowed_accounts else None
                if allowed_ids is not None:
                    enabled = [item for item in enabled if item["id"] in allowed_ids]
                    if not enabled:
                        raise WebProviderError("Không có tài khoản web được cấp quyền đang bật.", 503)
                if not enabled:
                    break
                verified = [item for item in enabled if item["verified"]]
                if not verified:
                    raise WebProviderError("Hãy kiểm tra phiên đăng nhập của tài khoản web đã bật.", 503)
                available = [item for item in verified if item["id"] not in self.in_use]
                if available:
                    account = min(available, key=lambda item: self.last_used.get(item["id"], 0))
                    self.in_use.add(account["id"])
                    self.last_used[account["id"]] = time.monotonic()
                    break
                remaining = deadline - time.monotonic() if deadline is not None else 0.25
                if remaining <= 0:
                    raise WebProviderError("Hết thời gian chờ tài khoản web khả dụng.", 429)
                self.condition.wait(min(remaining, 0.25))
        try:
            yield account
        finally:
            if account:
                self._release(account["id"])

    def _release(self, aid):
        with self.condition:
            self.in_use.discard(aid)
            self.condition.notify_all()

    def close_login(self, aid):
        with self.condition:
            browser = self.login_windows.pop(aid, None)
        if browser:
            browser.close()

    def login(self, account, url):
        if not visible_chrome_available():
            raise WebProviderError("Cửa sổ đăng nhập cần chạy mac2API trong phiên máy tính có giao diện.", 409)
        self.close_login(account["id"])
        browser = ManagedBrowser(self.cfg, account, visible=True, url=url)
        with self.condition:
            self.login_windows[account["id"]] = browser

    @contextmanager
    def page(self, account, url):
        login = self.login_windows.get(account["id"])
        browser = login if login and login.proc.poll() is None else None
        temporary = browser is None
        try:
            if temporary:
                self.close_login(account["id"])
                browser = ManagedBrowser(self.cfg, account, visible=False)
            with browser.page(url) as page:
                yield page
        except CHROME_ERRORS as error:
            raise WebProviderError("Mất kết nối với hồ sơ Chromium của tài khoản.", 503) from error
        finally:
            if temporary and browser:
                browser.close()

    def close(self):
        with self.condition:
            accounts = list(self.login_windows)
        for aid in accounts:
            self.close_login(aid)
