"""极简 Chrome DevTools Protocol 客户端（仅依赖 websocket-client / requests）。"""
from __future__ import annotations

import json
import threading
import time

import requests
import websocket


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws_url: str, timeout: float = 90.0, max_size: int = 256 << 20, *, suppress_origin: bool = False):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size, suppress_origin=suppress_origin)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.RLock()

    def send(self, method: str, params: dict | None = None, timeout: float | None = None,
             *, session_id: str | None = None):
        budget = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        if budget <= 0 or not self._lock.acquire(timeout=budget):
            raise TimeoutError(f"CDP {method} timed out")
        original_timeout = self.ws.gettimeout()
        try:
            self._id += 1
            mid = self._id
            request = {"id": mid, "method": method, "params": params or {}}
            if session_id:
                request["sessionId"] = session_id
            payload = json.dumps(request)
            self.ws.settimeout(_time_left(deadline, method))
            try:
                self.ws.send(payload)
            except websocket.WebSocketTimeoutException as exc:
                raise TimeoutError(f"CDP {method} timed out") from exc
            while True:
                self.ws.settimeout(_time_left(deadline, method))
                try:
                    msg = json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict) and msg.get("id") == mid:
                    if "error" in msg:
                        raise CDPError(f"{method}: {msg['error']}")
                    return msg
        finally:
            try:
                self.ws.settimeout(original_timeout)
            finally:
                self._lock.release()

    def js(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout)
        res = r.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        return r.get("result")

    def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
        """在 seconds 秒内持续读取事件，交给回调处理。"""
        end = time.monotonic() + seconds
        if seconds <= 0 or not self._lock.acquire(timeout=seconds):
            return
        original_timeout = self.ws.gettimeout()
        try:
            while (remaining := end - time.monotonic()) > 0:
                self.ws.settimeout(min(sock_timeout, remaining))
                try:
                    ev = json.loads(self.ws.recv())
                except (websocket.WebSocketTimeoutException, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if on_event and isinstance(ev, dict) and "method" in ev:
                    on_event(ev)
        finally:
            try:
                self.ws.settimeout(original_timeout)
            finally:
                self._lock.release()

    def close(self):
        with self._lock:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass


class CDPSession:
    """Page commands over the browser's already-approved WebSocket."""

    def __init__(self, browser: CDP, session_id: str):
        self.browser = browser
        self.session_id = session_id

    def send(self, method, params=None, timeout=None):
        return self.browser.send(method, params, timeout, session_id=self.session_id)

    js = CDP.js


def _time_left(deadline, method):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"CDP {method} timed out")
    return remaining


def http_json(url: str, timeout: float = 5.0):
    return requests.get(url, timeout=timeout).json()
