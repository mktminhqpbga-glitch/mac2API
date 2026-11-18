"""Drive authenticated ChatGPT and Gemini tabs through a local DevTools port.

The browser is started and logged into by the operator. Only tabs created for
requests are closed here. Cookies and third-party API keys are never read.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
import time
from urllib.parse import urlsplit

import requests

from cdp import CDP
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
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class WebProviderError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class WebProviders:
    def __init__(self, port: int):
        self.port = port

    @contextmanager
    def _page(self, provider: WebProvider):
        if not self.port:
            raise WebProviderError("Chưa cấu hình cổng Chrome Web trên máy chạy mac2API.", 503)
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
            except (requests.RequestException, ValueError, KeyError, OSError) as exc:
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
            }})()""")
            if ready is True:
                return
            time.sleep(0.3)
        raise WebProviderError("Không thấy ô chat. Hãy đăng nhập trong hồ sơ Chrome đã cấu hình và kiểm tra trang web.", 503)

    def check(self, provider_id: str):
        provider = PROVIDERS[provider_id]
        with self._page(provider) as page:
            self._wait_composer(page, provider, time.monotonic() + 15)

    def generate(self, provider_id: str, prompt: str, *, image: bool, timeout: int):
        provider = PROVIDERS[provider_id]
        deadline = time.monotonic() + timeout
        with self._page(provider) as page:
            self._wait_composer(page, provider, min(deadline, time.monotonic() + 25))
            focused = page.js(f"""(() => {{
                if (location.origin !== {json.dumps(_origin(provider.url))}) return false;
                const editor = document.querySelector({json.dumps(provider.composer)});
                if (!editor) return false;
                editor.focus();
                return document.activeElement === editor || editor.contains(document.activeElement);
            }})()""")
            if focused is not True:
                raise WebProviderError("Không thể nhập nội dung vào ô chat.")
            if image:
                prompt = "Hãy tạo một ảnh mới dựa trên yêu cầu sau và trả ảnh trong câu trả lời.\n" + prompt
            page.send("Input.insertText", {"text": prompt})
            self._send(page, provider, min(deadline, time.monotonic() + 10))
            return self._wait_result(page, provider, deadline, image)

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
            }})()""")
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
            }})()""")
            if not isinstance(state, dict) or "busy" not in state:
                raise WebProviderError("Trang đã rời phiên web đã cấu hình. Hãy kiểm tra đăng nhập.", 503)
            saw_stop |= state["busy"]
            current = state["image" if image else "text"]
            if current != previous or state["busy"]:
                previous = current
                unchanged_since = time.monotonic()
            if current and not state["busy"] and (state["complete"] or saw_stop):
                if time.monotonic() - unchanged_since >= 1:
                    return _read_image(page, current) if image else current
            time.sleep(0.35)
        raise WebProviderError("Hết thời gian chờ kết quả từ phiên web. Kiểm tra đăng nhập, hạn mức và tính năng của tài khoản.", 504)


def _origin(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _read_image(page, source: str):
    if urlsplit(source).scheme not in ("https", "blob", "data"):
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
    }})()""", await_promise=True, timeout=15)
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
