#!/bin/bash
set -eu

cd -P -- "$(dirname -- "$0")"

if [[ ! -f app.py || ! -f admin.html || ! -r admin.html || ! -s admin.html ]]; then
    printf 'Thiếu app.py hoặc admin.html không đọc được trong %s. Hãy giải nén toàn bộ gói.\n' "$PWD" >&2
    exit 1
fi

if [[ ! -x venv/bin/python ]]; then
    printf '%s\n' 'Hãy tạo venv và cài thư viện trong thư mục này theo phần Chạy trực tiếp trên Mac của README.md.' >&2
    exit 1
fi

if [[ "${1:-}" == "--copy-admin-key" ]]; then
    exec venv/bin/python - <<'PY'
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import subprocess

from config import CFG

key_file = Path(CFG.data_dir) / "admin-key.json"
try:
    key = json.loads(key_file.read_text(encoding="utf-8")).get("key") or CFG.api_key
except FileNotFoundError:
    key = CFG.api_key
except (OSError, ValueError, AttributeError):
    raise SystemExit(f"Không đọc được khóa quản trị tại {key_file}.") from None
if not isinstance(key, str) or not key:
    raise SystemExit(f"Chưa có khóa quản trị trong {key_file.parent}.")

connection = HTTPConnection("127.0.0.1", CFG.port, timeout=5)
try:
    connection.request("GET", "/admin/status?compact=1", headers={"Authorization": "Bearer " + key})
    status = connection.getresponse().status
except (OSError, HTTPException, ValueError):
    raise SystemExit(f"Chưa kiểm tra được mac2API ở cổng {CFG.port}. Hãy mở ứng dụng rồi thử lại.") from None
finally:
    connection.close()
if status == 401:
    raise SystemExit(f"mac2API đang chạy dùng khóa khác. Kiểm tra thư mục dữ liệu trong cửa sổ khởi động: {key_file.parent}")
if status != 200:
    raise SystemExit("mac2API chưa xác nhận khóa. Hãy kiểm tra cửa sổ ứng dụng rồi thử lại.")
try:
    subprocess.run(["pbcopy"], input=key, text=True, check=True)
except (OSError, subprocess.CalledProcessError):
    raise SystemExit("Khóa đúng nhưng chưa sao chép được vào bộ nhớ tạm của macOS.") from None
print("Đã xác minh và sao chép khóa. Dán vào ô Khóa quản trị rồi bấm Lưu.")
PY
fi

export MUSE2API_RESTART_ON_UPGRADE=1

run_mac2api() {
venv/bin/python - <<'PY'
import errno
import socket
from pathlib import Path

import uvicorn

from config import CFG

host = "127.0.0.1"
port = CFG.port
if not 1 <= port <= 65535:
    raise SystemExit("MUSE2API_PORT phải nằm trong khoảng 1 đến 65535.")

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        listener.listen()
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"Cổng {port} đang được tiến trình khác sử dụng. "
                "Hãy dừng bản mac2API cũ trước khi chạy lại.\n"
                f"Kiểm tra tiến trình bằng lsof -nP -iTCP:{port} -sTCP:LISTEN"
            ) from None
        raise SystemExit(f"Không mở được cổng {port}. {error}") from None

    print(f"Thư mục mã mac2API: {Path.cwd()}", flush=True)
    print(f"Thư mục dữ liệu mac2API: {Path(CFG.data_dir).resolve()}", flush=True)
    print(f"Bảng quản trị: http://{host}:{port}/admin", flush=True)
    server = uvicorn.Server(uvicorn.Config("app:app", host=host, port=port))
    server.run(sockets=[listener])
    if not server.started:
        raise SystemExit(1)
PY
}

while true; do
    if run_mac2api; then
        exit 0
    else
        status=$?
    fi
    if [[ "$status" -ne 75 ]]; then
        exit "$status"
    fi
    printf '%s\n' 'Đang mở lại mac2API sau khi cập nhật…'
done
