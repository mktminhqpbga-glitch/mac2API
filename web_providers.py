"""Drive authenticated ChatGPT and Gemini tabs through a local DevTools port.

The browser is started and logged into by the operator. Only tabs created for
requests are closed here. Cookies and third-party API keys are never read.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit

import requests
from websocket import WebSocketException

from cdp import CDP, CDPError, CDPSession
from engine import _sniff_mime


@dataclass(frozen=True)
class WebProvider:
    url: str
    composer: str
    send: str
    reply: str
    stop: str
    finished: str


PROVIDERS = {
    "chatgpt": WebProvider(
        "https://chatgpt.com/",
        '#prompt-textarea',
        '[data-testid="send-button"], #composer-submit-button[data-testid="send-button"]',
        '[data-message-author-role="assistant"]',
        '[data-testid="stop-button"]',
        '[data-testid="copy-turn-action-button"]',
    ),
    "gemini-web": WebProvider(
        "https://gemini.google.com/app",
        'rich-textarea .ql-editor[contenteditable="true"], rich-textarea [role="textbox"][contenteditable="true"]',
        'button.send-button:not(.stop-button), button[aria-label="Send message"], button[aria-label="Gửi tin nhắn"]',
        'model-response, [data-test-id="model-response"]',
        'button.stop-button, button[aria-label="Stop response"], button[aria-label="Dừng phản hồi"]',
        'button[aria-label="Copy"], button[aria-label="Sao chép"], [data-test-id="copy-button"]',
    ),
}
DEFAULT_WEB_CDP_PORT = 19211
WEB_SETUP_MESSAGE = (
    "Chưa kết nối Chrome Web. Trong bảng quản trị, mở tab ChatGPT Web hoặc Gemini Web, "
    "bật gỡ lỗi từ xa trong Chrome đang dùng rồi bấm Kết nối Chrome."
)
WEB_TIMEOUT_MESSAGE = "Hết thời gian chờ Chrome xử lý tác vụ. Hãy kiểm tra phiên web trước khi gửi lại."
MAX_IMAGE_BYTES = 20 * 1024 * 1024
CHROME_ERRORS = (CDPError, OSError, WebSocketException, ValueError, KeyError, TypeError)


class WebProviderError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class WebPostSendError(WebProviderError):
    """The browser may already have submitted the request."""


class WebSessionUnavailable(WebProviderError):
    def __init__(self):
        super().__init__("Chưa xác nhận được tài khoản đã đăng nhập. Hãy mở hồ sơ, đăng nhập và kiểm tra lại.", 409)


def chrome_debug_file():
    root = Path.home()
    if sys.platform == "darwin":
        return root / "Library/Application Support/Google/Chrome/DevToolsActivePort"
    return root / ".config/google-chrome/DevToolsActivePort"


class ChromeProfileConnection:
    """Use one consented browser connection for the operator's chosen profiles."""

    def __init__(self, browser: CDP):
        self.browser = browser
        self.profiles: dict[str, str] = {}

    @classmethod
    def connect(cls, muse_port: int):
        try:
            with chrome_debug_file().open(encoding="ascii") as file:
                port_line, path = file.read(256).splitlines()
            port = int(port_line)
            if (not 0 < port < 65536 or port == muse_port
                    or not re.fullmatch(r"/devtools/browser(?:/[A-Za-z0-9_-]+)?", path)):
                raise ValueError("Invalid Chrome endpoint")
        except (OSError, ValueError) as exc:
            raise WebProviderError(
                "Chưa thấy cổng Chrome. Mở chrome://inspect/#remote-debugging trong Google Chrome, bật gỡ lỗi từ xa rồi thử lại.",
                503,
            ) from exc

        try:
            browser = CDP(f"ws://127.0.0.1:{port}{path}", timeout=40, suppress_origin=True)
        except CHROME_ERRORS as exc:
            raise WebProviderError(
                "Chrome chưa cho phép kết nối. Giữ Chrome mở, bấm Kết nối Chrome rồi chọn Cho phép trong hộp thoại Chrome.",
                503,
            ) from exc
        browser.timeout = 5
        browser.ws.settimeout(5)
        result = cls(browser)
        if not result.alive():
            browser.close()
            raise WebProviderError("Chrome vừa ngắt kết nối. Hãy kết nối lại.", 503)
        return result

    def alive(self):
        try:
            product = self.browser.send("Browser.getVersion")["result"]["product"]
            return isinstance(product, str) and product.startswith(("Chrome/", "Chromium/", "HeadlessChrome/"))
        except CHROME_ERRORS:
            return False

    @contextmanager
    def attach(self, target_id):
        result = self.browser.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = result["result"]["sessionId"]
        try:
            yield CDPSession(self.browser, session_id)
        finally:
            try:
                self.browser.send("Target.detachFromTarget", {"sessionId": session_id})
            except CHROME_ERRORS:
                pass

    @contextmanager
    def new_page(self, provider_id, provider):
        context_id = self.profiles.get(provider_id)
        if not context_id:
            raise WebProviderError(
                "Hãy mở tab đã đăng nhập và bấm đúng nút ChatGPT hoặc Gemini trong tiện ích.", 409)
        try:
            result = self.browser.send("Target.createTarget", {"url": provider.url, "browserContextId": context_id})
            target_id = result["result"]["targetId"]
        except CDPError as exc:
            raise WebProviderError(
                "Chrome không mở được tab trong hồ sơ đã xác minh. Hãy đưa cửa sổ của hồ sơ đó lên trước rồi thử lại.",
                409,
            ) from exc
        try:
            with self.attach(target_id) as page:
                yield page
        finally:
            try:
                self.browser.send("Target.closeTarget", {"targetId": target_id})
            except CHROME_ERRORS:
                pass


class WebProviders:
    def __init__(self, port: int, *, profile_preferred: bool = False):
        self.port = port
        self.profile_preferred = profile_preferred
        self.linked_at: dict[str, int] = {}
        self.linked_targets: dict[str, str] = {}
        self.profile_connection: ChromeProfileConnection | None = None
        self._connect_lock = threading.Lock()
        self._capability_snapshot: tuple[ChromeProfileConnection | int | None, bool, dict[str, str]] = (None, False, {})

    def ready(self, provider_id: str):
        profile = self.profile_connection
        if self.profile_preferred:
            return bool(profile and profile.browser.ws.connected and provider_id in profile.profiles)
        return bool(self.port)

    def capability_states(self, *, inspect=True):
        profile = self.profile_connection
        port = self.port
        profile_preferred = self.profile_preferred
        configured = bool(profile_preferred or port)
        connection = profile if profile else port if not profile_preferred else None
        bindings = profile.profiles if profile else self.linked_targets
        selected = bindings.copy()
        connected = False
        verified = {}
        if inspect:
            try:
                tabs = None
                if profile and profile.browser.ws.connected:
                    tabs = profile.browser.send("Target.getTargets", timeout=1)["result"]["targetInfos"]
                elif port and not profile_preferred:
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.get(f"http://127.0.0.1:{port}/json/list",
                                               timeout=1, allow_redirects=False)
                        if response.ok:
                            tabs = response.json()
                if isinstance(tabs, list):
                    connected = True
                    id_field = "browserContextId" if profile else "id"
                    verified = {name: selected[name] for name, provider in PROVIDERS.items()
                                if selected.get(name) and any(
                                    isinstance(tab, dict) and tab.get("type") == "page"
                                    and tab.get(id_field) == selected[name]
                                    and _origin(str(tab.get("url", ""))) == _origin(provider.url)
                                    for tab in tabs)}
            except (*CHROME_ERRORS, requests.RequestException):
                connected = False
            if selected == bindings:
                self._capability_snapshot = (connection, connected, verified)
        else:
            # A full generation queue prevents a fresh probe, not queue admission.
            previous_connection, was_connected, last_verified = self._capability_snapshot
            if (connection == previous_connection and was_connected
                    and (not profile or profile.browser.ws.connected)):
                connected, verified = True, last_verified
        states = {}
        for provider_id in PROVIDERS:
            binding = bindings.get(provider_id)
            ready = connected and bool(binding) and verified.get(provider_id) == binding
            reason = ("ready" if ready else "session_unverified" if connected else
                      "not_configured" if not configured else "busy" if not inspect else "browser_disconnected")
            states[provider_id] = {"configured": configured, "connected": connected,
                                   "ready": ready, "reason": reason}
        return states

    def _record_link(self, provider_id, connection, bindings, target_id):
        with self._connect_lock:
            current = self.profile_connection or (self.port if not self.profile_preferred else None)
            if connection != current:
                raise WebProviderError("Kết nối Chrome đã đổi. Hãy xác minh lại tab đang đăng nhập.", 409)
            bindings[provider_id] = target_id
            old_connection, connected, verified = self._capability_snapshot
            if connection != old_connection or not connected:
                verified = {}
            self._capability_snapshot = (connection, True, {**verified, provider_id: target_id})
            checked = self.linked_at[provider_id] = int(time.time())
            return checked

    def connect_profile(self, muse_port: int, persist):
        with self._connect_lock:
            if self.profile_connection and self.profile_connection.alive():
                return
            profile = ChromeProfileConnection.connect(muse_port)
            try:
                persist()
            except OSError as exc:
                profile.browser.close()
                raise WebProviderError("Không lưu được cấu hình kết nối Chrome.", 507) from exc
            if self.profile_connection:
                self.profile_connection.browser.close()
            self.linked_at.clear()
            self.linked_targets.clear()
            self._capability_snapshot = (None, False, {})
            self.profile_preferred = True
            self.profile_connection = profile

    def connect_port(self, port: int, persist):
        with self._connect_lock:
            if self.profile_connection and self.profile_connection.browser.ws.connected:
                raise WebProviderError("Hãy đóng kết nối của hồ sơ Chrome hiện tại trước khi đổi sang cổng cố định.", 409)
            self.probe_port(port)
            try:
                persist()
            except OSError as exc:
                raise WebProviderError("Không lưu được cấu hình kết nối Chrome.", 507) from exc
            if self.profile_connection:
                self.profile_connection.browser.close()
                self.profile_connection = None
            if self.profile_preferred or self.port != port:
                self.linked_at.clear()
                self.linked_targets.clear()
                self._capability_snapshot = (None, False, {})
            self.port = port
            self.profile_preferred = False

    def close(self):
        with self._connect_lock:
            if self.profile_connection:
                self.profile_connection.browser.close()
                self.profile_connection = None
            self.linked_at.clear()
            self.linked_targets.clear()
            self._capability_snapshot = (None, False, {})

    @staticmethod
    def probe_port(port: int):
        """Check the fixed loopback port before storing an enabled configuration."""
        try:
            with requests.Session() as session:
                session.trust_env = False
                url = f"http://127.0.0.1:{port}/json/version"
                response = session.get(url, timeout=3, allow_redirects=False)
                if not response.ok:
                    raise ValueError("DevTools endpoint unavailable")
                metadata = response.json()
            if not isinstance(metadata, dict) or not isinstance(metadata.get("Browser"), str):
                raise ValueError("Unexpected DevTools response")
            if not metadata["Browser"].startswith(("Chrome/", "HeadlessChrome/", "Chromium/")):
                raise ValueError("Unexpected browser")
            address = urlsplit(metadata.get("webSocketDebuggerUrl", ""))
            if (address.scheme != "ws" or address.hostname != "127.0.0.1" or address.port != port
                    or not address.path.startswith("/devtools/browser/")):
                raise ValueError("Unexpected DevTools socket")
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise WebProviderError(
                f"Chưa thấy Chrome DevTools ở cổng {port} trên máy chạy mac2API. "
                "Hãy kiểm tra cổng đã cấu hình hoặc dùng nút Kết nối Chrome.", 503
            ) from exc

    @contextmanager
    def _page(self, provider_id: str):
        provider = PROVIDERS[provider_id]
        if profile := self.profile_connection:
            try:
                with profile.new_page(provider_id, provider) as page:
                    yield page
            except TimeoutError as exc:
                raise WebProviderError(WEB_TIMEOUT_MESSAGE, 504) from exc
            except CHROME_ERRORS as exc:
                raise WebProviderError("Mất kết nối với Chrome. Hãy bấm Kết nối Chrome lại trong bảng quản trị.", 503) from exc
            return
        if self.profile_preferred or not self.port:
            raise WebProviderError(WEB_SETUP_MESSAGE, 503)
        base = f"http://127.0.0.1:{self.port}"
        target = None
        page = None
        with requests.Session() as session:
            session.trust_env = False
            try:
                response = session.put(f"{base}/json/new?{provider.url}", timeout=5, allow_redirects=False)
                response.raise_for_status()
                target = response.json()["id"]
                if not isinstance(target, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", target):
                    raise ValueError("Invalid browser target")
                page = CDP(f"ws://127.0.0.1:{self.port}/devtools/page/{target}", timeout=5, suppress_origin=True)
                yield page
            except WebProviderError:
                raise
            except TimeoutError as exc:
                raise WebProviderError(WEB_TIMEOUT_MESSAGE, 504) from exc
            except CHROME_ERRORS as exc:
                raise WebProviderError("Không kết nối được với Chrome Web hoặc phiên DevTools đã đóng.", 503) from exc
            finally:
                if page is not None:
                    page.close()
                if isinstance(target, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", target):
                    try:
                        session.get(f"{base}/json/close/{target}", timeout=3, allow_redirects=False)
                    except requests.RequestException:
                        pass

    def _wait_composer(self, page, provider, deadline):
        while time.monotonic() < deadline:
            ready = page.js(f"""(() => {{
                if (location.origin !== {json.dumps(_origin(provider.url))}) return false;
                const editor = document.querySelector({json.dumps(provider.composer)});
                return Boolean(editor?.getClientRects().length);
            }})()""", timeout=_remaining(deadline, 5))
            if ready is True:
                return
            time.sleep(0.3)
        raise WebProviderError("Không thấy ô chat. Hãy đăng nhập trong hồ sơ Chrome đã cấu hình và kiểm tra trang web.", 503)

    def check(self, provider_id: str):
        provider = PROVIDERS[provider_id]
        with self._page(provider_id) as page:
            self._wait_composer(page, provider, time.monotonic() + 15)

    def check_managed(self, provider_id, page_factory):
        with page_factory() as page:
            self._wait_managed_login(page, provider_id, time.monotonic() + 15)

    def _wait_managed_login(self, page, provider_id, deadline):
        provider = PROVIDERS[provider_id]
        selectors = {
            "chatgpt": '[data-testid="accounts-profile-button"], [data-testid="profile-button"]',
            "gemini-web": 'a[href^="https://accounts.google.com/SignOutOptions"], button[aria-label^="Google Account"], '
                          'button[aria-label^="Tài khoản Google"]',
        }
        while (remaining := deadline - time.monotonic()) > 0:
            present = page.js(f"""(() => {{
                if (location.origin !== {json.dumps(_origin(provider.url))}) return false;
                const editor = document.querySelector({json.dumps(provider.composer)});
                const account = document.querySelector({json.dumps(selectors[provider_id])});
                return Boolean(editor?.getClientRects().length && account?.getClientRects().length);
            }})()""", timeout=min(remaining, 3))
            if present is True:
                return
            time.sleep(0.3)
        raise WebSessionUnavailable()

    def _verify_marker(self, page, provider, origin, marker):
        matched = page.js(f"""(() => {{
            if (location.origin !== {json.dumps(origin)}) return false;
            const root = document.documentElement;
            if (root.dataset.mac2apiLink !== {json.dumps(marker)}) return false;
            delete root.dataset.mac2apiLink;
            return true;
        }})()""")
        if matched is not True:
            return False
        self._wait_composer(page, provider, time.monotonic() + 5)
        return True

    def link(self, provider_id: str, marker: str) -> int:
        """Verify that the extension's active tab belongs to this local browser."""
        provider = PROVIDERS[provider_id]
        origin = _origin(provider.url)
        if profile := self.profile_connection:
            try:
                targets = profile.browser.send("Target.getTargets")["result"]["targetInfos"]
                for tab in targets:
                    if (tab.get("type") != "page" or _origin(tab.get("url", "")) != origin
                            or not tab.get("browserContextId")):
                        continue
                    try:
                        with profile.attach(tab["targetId"]) as page:
                            if self._verify_marker(page, provider, origin, marker):
                                return self._record_link(provider_id, profile, profile.profiles, tab["browserContextId"])
                    except CDPError:
                        continue  # A tab may have been closed since Target.getTargets.
            except CHROME_ERRORS as exc:
                raise WebProviderError("Mất kết nối với Chrome. Hãy bấm Kết nối Chrome lại trong bảng quản trị.", 503) from exc
            raise WebProviderError("Hãy mở tiện ích ngay trong tab đã đăng nhập ở Chrome vừa cấp quyền.", 409)

        port = self.port
        if self.profile_preferred or not port:
            raise WebProviderError(WEB_SETUP_MESSAGE, 503)
        base = f"http://127.0.0.1:{port}"
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(base + "/json/list", timeout=5, allow_redirects=False)
                response.raise_for_status()
                tabs = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WebProviderError("Không kết nối được với Chrome trên máy chạy mac2API.", 503) from exc

        for tab in tabs:
            if tab.get("type") != "page" or _origin(tab.get("url", "")) != origin:
                continue
            target = tab.get("id")
            if not isinstance(target, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", target):
                continue
            try:
                page = CDP(f"ws://127.0.0.1:{port}/devtools/page/{target}", timeout=3, suppress_origin=True)
                try:
                    if self._verify_marker(page, provider, origin, marker):
                        return self._record_link(provider_id, port, self.linked_targets, target)
                finally:
                    page.close()
            except CHROME_ERRORS:
                continue
        raise WebProviderError("Tab này không thuộc Chrome mà mac2API đang kết nối. Hãy mở tiện ích trong đúng Chrome trên cùng máy.", 409)

    def generate(self, provider_id: str, prompt: str, *, image: bool, timeout: int, managed_page=None):
        provider = PROVIDERS[provider_id]
        deadline = time.monotonic() + timeout
        with (managed_page() if managed_page else self._page(provider_id)) as page:
            composer_deadline = min(deadline, time.monotonic() + 25)
            if managed_page:
                self._wait_managed_login(page, provider_id, composer_deadline)
            else:
                self._wait_composer(page, provider, composer_deadline)
            self._clean_composer(page, provider, min(deadline, time.monotonic() + 3))
            if image:
                prompt = "Hãy tạo một ảnh mới dựa trên yêu cầu sau và trả ảnh trong câu trả lời.\n" + prompt
            page.send("Input.insertText", {"text": prompt}, timeout=_remaining(deadline, 5))
            try:
                self._send(page, provider, min(deadline, time.monotonic() + 10))
                return self._wait_result(page, provider, deadline, image)
            except CHROME_ERRORS as exc:
                raise WebPostSendError("Mất xác nhận sau khi gửi yêu cầu. Hãy kiểm tra tác vụ gốc.", 503) from exc
            except WebProviderError as exc:
                if exc.status == 504:
                    raise WebPostSendError(str(exc), exc.status) from exc
                raise

    @staticmethod
    def _clean_composer(page, provider, deadline):
        payload = json.dumps(provider.composer)
        origin = json.dumps(_origin(provider.url))
        composer = rf"""
            const root = editor.closest('form,#composer-background,input-container') || editor.parentElement;
            const remove = 'button[data-testid*="remove-attachment"],button[data-test-id*="remove-attachment"]';
            const removeButtons = [...root.querySelectorAll('button[aria-label],button[title],' + remove)]
                .filter(button => button.matches(remove) || [button.getAttribute('aria-label'), button.title]
                    .some(label => /(?:remove|delete|gỡ|xóa|xoá)\s+(?:(?:an?|the)\s+)?(?:attachment|file|image|video|audio|tệp|ảnh)/iu.test(label || '')));
            const inputs = [...root.querySelectorAll('input[type=file]')];"""
        reset = f"""(() => {{
            if (location.origin !== {origin}) return false;
            const editor = document.querySelector({payload});
            if (!editor) return false;
            {composer}
            removeButtons.forEach(button => button.click());
            for (const input of inputs) if (input.files?.length) input.value = '';
            if (editor instanceof HTMLTextAreaElement) {{
                Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, '');
            }} else if (editor.isContentEditable && editor.innerText.trim()) {{
                editor.replaceChildren();
            }}
            editor.dispatchEvent(new Event('input', {{bubbles: true}}));
            return true;
        }})()"""
        if page.js(reset, timeout=_remaining(deadline, 5)) is not True:
            raise WebProviderError("Không thể dọn ô nhập của phiên web.")
        verify = f"""(() => {{
            if (location.origin !== {origin}) return false;
            const editor = document.querySelector({payload});
            if (!editor) return false;
            {composer}
            const cards = root.querySelectorAll('[data-testid*="attachment-preview"],[data-test-id*="attachment-preview"],'
                + '[data-testid*="attachment-card"],[data-test-id*="attachment-card"],[data-uploading="true"],'
                + 'img[src^="blob:"],video[src^="blob:"]');
            if ((editor.value ?? editor.innerText ?? '').trim() || inputs.some(input => input.files?.length)
                || [...removeButtons, ...cards].some(node => node.getClientRects().length)) return false;
            editor.focus();
            return document.activeElement === editor || editor.contains(document.activeElement);
        }})()"""
        empty_before = False
        while time.monotonic() < deadline:
            empty = page.js(verify, timeout=_remaining(deadline, 5)) is True
            if empty and empty_before:
                return
            empty_before = empty
            time.sleep(0.15)
        raise WebProviderError("Ô nhập phiên web còn nội dung cũ. Đã dừng trước khi gửi yêu cầu.")

    @staticmethod
    def _send(page, provider, deadline):
        while time.monotonic() < deadline:
            clicked = page.js(f"""(() => {{
                if (location.origin !== {json.dumps(_origin(provider.url))}) return false;
                const button = [...document.querySelectorAll({json.dumps(provider.send)})]
                    .find(e => e.getClientRects().length && !e.disabled && e.getAttribute('aria-disabled') !== 'true');
                if (!button) return false;
                button.click();
                return true;
            }})()""", timeout=_remaining(deadline, 5))
            if clicked is True:
                return
            time.sleep(0.2)
        raise WebProviderError("Không thấy nút gửi. Hãy kiểm tra phiên đăng nhập và giao diện trang web.")

    @staticmethod
    def _wait_result(page, provider, deadline, image):
        previous = None
        unchanged_since = time.monotonic()
        saw_stop = False
        while time.monotonic() < deadline:
            state = page.js(f"""(() => {{
                if (location.origin !== {json.dumps(_origin(provider.url))}) return null;
                const visible = e => e.getClientRects().length > 0;
                const reply = [...document.querySelectorAll({json.dumps(provider.reply)})].filter(visible).at(-1);
                const stopping = [...document.querySelectorAll({json.dumps(provider.stop)})].some(visible);
                const complete = reply && [...(reply.closest('article, model-response') || reply)
                    .querySelectorAll({json.dumps(provider.finished)})].some(visible);
                const body = reply?.querySelector('.markdown, .markdown-main-panel') || reply;
                const imgs = body ? [...body.querySelectorAll('img')].filter(
                    e => visible(e) && e.complete && e.naturalWidth >= 256 && e.naturalHeight >= 256) : [];
                return {{busy: stopping, complete: Boolean(complete), text: body?.innerText?.trim() || '',
                    image: imgs.at(-1)?.currentSrc || imgs.at(-1)?.src || ''}};
            }})()""", timeout=_remaining(deadline, 5))
            if not isinstance(state, dict) or "busy" not in state:
                raise WebProviderError("Trang đã rời phiên web đã cấu hình. Hãy kiểm tra đăng nhập.", 503)
            saw_stop |= state["busy"]
            current = state["image" if image else "text"]
            if current != previous or state["busy"]:
                previous = current
                unchanged_since = time.monotonic()
            if current and not state["busy"] and (state["complete"] or saw_stop):
                if time.monotonic() - unchanged_since >= 1:
                    return _read_image(page, current, timeout=_remaining(deadline, 15)) if image else current
            time.sleep(0.35)
        raise WebProviderError("Hết thời gian chờ kết quả từ phiên web. Kiểm tra đăng nhập, hạn mức và tính năng của tài khoản.", 504)


def _origin(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _remaining(deadline, limit):
    remaining = min(limit, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError(WEB_TIMEOUT_MESSAGE)
    return remaining


def _read_image(page, source: str, *, timeout=15):
    try:
        address = urlsplit(source)
        host = address.hostname or ""
        public_hosts = ("chatgpt.com", "oaiusercontent.com", "oaistatic.com",
                        "gemini.google.com", "googleusercontent.com", "gstatic.com",
                        "storage.googleapis.com")
        allowed_cdn = (address.scheme == "https" and not address.username and not address.password
                       and address.port in (None, 443)
                       and any(host == domain or host.endswith("." + domain) for domain in public_hosts))
    except ValueError:
        allowed_cdn = False
    if not allowed_cdn and not source.startswith(("blob:", "data:")):
        raise WebProviderError("Địa chỉ ảnh do trang trả về không được hỗ trợ.")
    encoded = page.js(f"""(async () => {{
        const response = await fetch({json.dumps(source)}, {{redirect: 'error'}});
        if (!response.ok) return null;
        const blob = await response.blob();
        if (blob.size > {MAX_IMAGE_BYTES}) return null;
        return new Promise(resolve => {{
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.onerror = () => resolve(null);
            reader.readAsDataURL(blob);
        }});
    }})()""", await_promise=True, timeout=timeout)
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise WebProviderError("Không đọc được ảnh hoặc ảnh vượt 20 MiB.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise WebProviderError("Dữ liệu ảnh từ trang web không hợp lệ.") from exc
    mime = _sniff_mime(data)
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    if mime not in extensions or len(data) > MAX_IMAGE_BYTES:
        raise WebProviderError("Trang web chưa trả ảnh PNG, JPEG hoặc WebP hợp lệ.")
    return data, extensions[mime]
