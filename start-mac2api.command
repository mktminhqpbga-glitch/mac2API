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

exec venv/bin/python - <<'PY'
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
    print(f"Bảng quản trị: http://{host}:{port}/admin", flush=True)
    server = uvicorn.Server(uvicorn.Config("app:app", host=host, port=port))
    server.run(sockets=[listener])
    if not server.started:
        raise SystemExit(1)
PY
