#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nhập cookie từ cửa sổ Chromium riêng vào Muse2API.

Chạy python get_muse_cookie.py --help để xem cách dùng.
Tập lệnh dùng thư viện chuẩn Python và Chrome DevTools Protocol.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SITE = "https://muse.ai/"
CDP_PORT = 9333
ESSENTIAL = ("hatch_sess",)
DOMAIN_HINT = "muse.ai"


# ---------------------------------------------------------------- 输出
def say(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:                     # 老 Windows 控制台
        print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def init_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")   # Python 3.7+
        except Exception:                          # noqa: BLE001
            pass


# ---------------------------------------------------------------- 极简 WebSocket
class WS:
    """只为 CDP 用的最小 WebSocket 客户端（RFC6455，文本帧）。"""

    def __init__(self, url: str, timeout: float = 15.0):
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Kết nối bị ngắt khi bắt tay WebSocket")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"Bắt tay WebSocket thất bại: {status!r}")
        self._id = 0

    # --- 发送 ---
    def _frame(self, opcode: int, payload: bytes):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def send_text(self, text: str):
        self._frame(0x1, text.encode("utf-8"))

    # --- 接收 ---
    def _read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("Kết nối đã đóng")
            out += chunk
        return out

    def recv_text(self) -> str:
        while True:
            h = self._read(2)
            opcode = h[0] & 0x0F
            masked = h[1] & 0x80
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mk = self._read(4) if masked else None
            data = self._read(ln) if ln else b""
            if mk:
                data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x9:                       # ping -> pong
                self._frame(0xA, data)
            elif opcode == 0x8:
                raise ConnectionError("Máy chủ đã đóng kết nối")
            # 其它（pong / 二进制 / 分片）直接忽略

    def call(self, method: str, params: dict | None = None,
             timeout: float = 20.0) -> dict:
        self._id += 1
        mid = self._id
        self.send_text(json.dumps({"id": mid, "method": method,
                                   "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(self.recv_text())
            except socket.timeout:
                continue
            except ConnectionError:
                raise
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} hết thời gian chờ")

    def close(self):
        try:
            self.sock.close()
        except Exception:                              # noqa: BLE001
            pass


# ---------------------------------------------------------------- 找浏览器
CHROME_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe")


def _win_registry_paths() -> list[str]:
    """从注册表 App Paths 找浏览器。

    不能靠 %ProgramFiles% 环境变量 —— 它在某些 shell / 精简环境下并不存在
    （本机实测 ProgramFiles 和 PROGRAMFILES 都是 None）。
    """
    out: list[str] = []
    try:
        import winreg
    except ImportError:
        return out
    sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for exe in CHROME_EXES:
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, sub + "\\" + exe, 0,
                                        winreg.KEY_READ | view) as k:
                        val = winreg.QueryValueEx(k, "")[0]
                        if val:
                            out.append(os.path.expandvars(val))
                except OSError:
                    continue
    return out


def _win_common_paths() -> list[str]:
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.environ.get("LOCALAPPDATA"), r"C:\Program Files",
             r"C:\Program Files (x86)"]
    rel = [
        r"Google\Chrome\Application\chrome.exe",
        r"Microsoft\Edge\Application\msedge.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    return [os.path.join(r, p) for r in roots if r for p in rel]


def find_chrome() -> str | None:
    if sys.platform == "win32":
        cands = _win_registry_paths() + _win_common_paths()
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        cands = ["google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge", "brave-browser"]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
        if not os.path.isabs(c):
            w = shutil.which(c)
            if w:
                return w
    return None


def wait_cdp(port: int, timeout: float = 30.0) -> str:
    """等 DevTools 端口起来，返回浏览器级 WebSocket 地址。"""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.load(r)["webSocketDebuggerUrl"]
        except Exception as exc:                       # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"Không mở được cổng DevTools ({port}): {last}")


# ---------------------------------------------------------------- 读 cookie
def read_cookies(ws: WS) -> dict[str, dict]:
    """用浏览器级 Storage.getCookies 拿全部 cookie，再筛 muse.ai。

    这是关键：CDP 能读到 httpOnly 的 cookie，网页 JS 读不到。
    """
    res = ws.call("Storage.getCookies", {}, timeout=20)
    out: dict[str, dict] = {}
    for c in res.get("cookies", []):
        domain = (c.get("domain") or "").lstrip(".")
        if DOMAIN_HINT not in domain:
            continue
        name = c.get("name")
        if not name:
            continue
        try:
            exp = int(float(c.get("expires", -1)))
        except (TypeError, ValueError):
            exp = -1
        out[name] = {"value": c.get("value", ""), "expires": exp,
                     "httpOnly": bool(c.get("httpOnly"))}
    return out


# ---------------------------------------------------------------- 上传
def upload(base: str, key: str, label: str, cookies: dict[str, dict]) -> dict:
    payload = {
        "label": label,
        "cookies": {k: v["value"] for k, v in cookies.items()},
        "expires": {k: v["expires"] for k, v in cookies.items() if v["expires"] > 0},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/admin/accounts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Gửi thất bại HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Không kết nối được {base}: {e.reason}") from None


def fmt_ts(ts: int) -> str:
    if ts <= 0:
        return "theo phiên"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- 主流程
def main() -> int:
    init_console()
    ap = argparse.ArgumentParser(
        description="Nhập cookie đăng nhập Muse.ai vào Muse2API")
    ap.add_argument("--base", default=os.environ.get("MUSE2API_BASE", ""),
                    help="Địa chỉ Muse2API, ví dụ http://your-server-ip:18610")
    ap.add_argument("--key", default=os.environ.get("MUSE2API_KEY", ""),
                    help="API Key (bắt đầu bằng m2a_)")
    ap.add_argument("--label", default="", help="Nhãn tài khoản, ví dụ acc-01")
    ap.add_argument("--timeout", type=int, default=300,
                    help="Số giây tối đa chờ đăng nhập, mặc định 300")
    ap.add_argument("--keep-open", action="store_true",
                    help="Giữ cửa sổ mở sau khi gửi thành công")
    ap.add_argument("--port", type=int, default=CDP_PORT,
                    help=f"Cổng DevTools, mặc định {CDP_PORT}")
    ap.add_argument("--chrome", default="", help="Đường dẫn tệp thực thi của trình duyệt")
    args = ap.parse_args()

    say("=" * 66)
    say("  Trợ lý nhập cookie Muse2API")
    say("=" * 66)
    say()

    if not args.base or not args.key:
        say("✗ Thiếu tham số. Chạy theo mẫu sau:")
        say("    python get_muse_cookie.py --base https://ten-mien-cua-ban --key m2a_xxx")
        say()
        say("  (Sao chép hai giá trị trong mục Thông tin kết nối của bảng quản trị.)")
        return 2
    if not args.label:
        args.label = "muse-" + time.strftime("%m%d-%H%M")

    chrome = args.chrome or find_chrome()
    if not chrome:
        say("✗ Không tìm thấy Chrome hoặc Edge.")
        say("  Cài Chrome hoặc dùng --chrome \"đường dẫn đầy đủ\" để chỉ định vị trí.")
        return 3
    say(f"Trình duyệt: {chrome}")

    profile = os.path.join(tempfile.gettempdir(), "muse2api-cookie-profile")
    shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile, exist_ok=True)

    args_cmd = [
        chrome,
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        # 受限环境（容器 / 沙箱 / CI）里 Chrome 自带的沙箱会初始化失败：
        #   "sandbox initialization failed: Operation not permitted"
        # → GPU/网络子进程崩掉 → Chrome 直接退出，调试端口从未监听起来，
        #   表现是 WebSocket 一握手就 "Kết nối đã đóng / Broken pipe"。
        # 加下面两个开关可绕过。日常桌面环境无副作用（只是不启用 Chrome 自带沙箱）。
        "--no-sandbox", "--disable-gpu",
        "--new-window", SITE,
    ]
    say("Đang mở một cửa sổ trình duyệt riêng…")
    proc = subprocess.Popen(args_cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    ws = None
    try:
        try:
            ws_url = wait_cdp(args.port, 30)
        except RuntimeError as exc:
            say(f"✗ {exc}")
            say("  Nếu trình duyệt đang mở, hãy đóng hẳn rồi thử lại.")
            say("  (Chrome đang chạy có thể tiếp quản cửa sổ mới và ngăn mở cổng DevTools.)")
            return 4
        ws = WS(ws_url)

        say()
        say("┌" + "─" * 64 + "┐")
        say("│  Hãy đăng nhập Muse.ai trong cửa sổ vừa mở.  │")
        say("│  Khi thấy giao diện chat, tập lệnh sẽ tự đọc cookie.    │")
        say("└" + "─" * 64 + "┘")
        say()

        deadline = time.time() + args.timeout
        got: dict[str, dict] = {}
        last_note = 0.0
        while time.time() < deadline:
            try:
                got = read_cookies(ws)
            except Exception as exc:                    # noqa: BLE001
                say(f"  Đọc cookie thất bại. Đang thử lại… ({exc})")
                time.sleep(2)
                continue

            have = [n for n in ESSENTIAL if n in got]
            if len(have) == len(ESSENTIAL):
                break

            now = time.time()
            if now - last_note > 5:
                last_note = now
                left = int(deadline - now)
                say(f"  Đang chờ đăng nhập… Đã đọc {len(have)}/{len(ESSENTIAL)} cookie chính"
                    f" ({', '.join(have) or 'chưa có'}). Còn {left} giây")
            time.sleep(2)
        else:
            say()
            say("✗ Hết thời gian chờ đăng nhập.")
            say("  Chạy lại tập lệnh và hoàn tất đăng nhập trong cửa sổ.")
            return 5

        say()
        say("✓ Đã đọc cookie phiên:")
        say()
        say(f"  {'Tên cookie':<30}{'httpOnly':<10}{'Hạn dùng'}")
        say("  " + "-" * 62)
        for name in ESSENTIAL:
            c = got.get(name, {})
            say(f"  {name:<30}{('Có' if c.get('httpOnly') else 'Không'):<10}"
                f"{fmt_ts(c.get('expires', -1))}")
        say()

        say("Đang gửi đến Muse2API…")
        try:
            r = upload(args.base, args.key, args.label, got)
        except RuntimeError as exc:
            say(f"✗ {exc}")
            say()
            say("  Đã đọc cookie nhưng gửi thất bại. Hãy sao chép dòng dưới đây,")
            say("  rồi dán vào biểu mẫu Nhập tài khoản trong bảng quản trị:")
            say()
            say("  " + "; ".join(f"{k}={v['value']}" for k, v in got.items()))
            return 6

        say()
        say("=" * 66)
        say(f"✓ Đã nhập thành công. Nhãn: {r['added'][0]['label']}")
        say(f"  ID tài khoản: {r['added'][0]['id']}")
        say(f"  Có {r['added'][0]['cookie_count']} cookie")
        if r.get("warning"):
            say(f"  ⚠ {r['warning']}")
        say()
        say(f"  Mở {args.base.rstrip('/')}/admin để xem tài khoản.")
        say("=" * 66)
        return 0

    finally:
        if ws:
            ws.close()
        if not args.keep_open:
            try:
                proc.terminate()
            except Exception:                           # noqa: BLE001
                pass
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("Đã hủy.")
        sys.exit(130)
