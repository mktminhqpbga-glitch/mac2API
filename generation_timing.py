"""Independent queue budget and generation task timing."""
from __future__ import annotations
import logging
from pathlib import Path
import re
import threading
import time
from pydantic import BaseModel, ConfigDict, Field

from store import _write

log = logging.getLogger("muse2api")

class QueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queue_timeout: int = Field(default=3600, ge=0, le=86400)

class QueuePolicy:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.value = QueueSettings()
        try:
            self.value = QueueSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("Không đọc được cài đặt hàng đợi (%s). Đang dùng thời gian chờ mặc định.", type(exc).__name__)

    def settings(self):
        with self.lock:
            return self.value.model_dump()

    def save(self, value):
        with self.lock:
            _write(str(self.path), value.model_dump())
            self.value = value.model_copy(deep=True)

class TaskNotQueued(Exception):
    """The worker must exit before sending content upstream."""


class PendingMedia(BaseModel):
    prompt: str
    model: str | None
    timeout: int
    references: list[str]


class PendingMediaStore:
    """Keep resumable input separate from public task records."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, task_id):
        if not re.fullmatch(r"task_[a-f0-9]{20}", task_id):
            raise ValueError("Invalid task ID")
        return self.directory / (task_id + ".json")

    def save(self, task_id, payload):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(str(self._path(task_id)), payload.model_dump())

    def exists(self, task_id):
        try:
            path = self._path(task_id)
            return path.is_file() and not path.is_symlink()
        except (OSError, ValueError):
            return False

    def load(self, task_id):
        return PendingMedia.model_validate_json(self._path(task_id).read_bytes())

    def discard(self, task_id):
        try:
            self._path(task_id).unlink(missing_ok=True)
        except (OSError, ValueError):
            log.warning("Không dọn được dữ liệu tạm của tác vụ %s.", task_id)

    def prune(self, queued_ids):
        try:
            for path in self.directory.iterdir():
                if path.suffix != ".json" or path.stem not in queued_ids:
                    try:
                        path.unlink()
                    except OSError:
                        log.warning("Không dọn được một tệp dữ liệu hàng chờ.")
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Không đọc được thư mục dữ liệu hàng chờ.")


class TaskTiming:
    """Store queue, generation and total durations with consistent meanings."""
    def __init__(self, store, task_id, already_queued=0.0):
        self.store, self.task_id = store, task_id
        self.created = time.monotonic() - already_queued
        self.queue_elapsed = already_queued
        self.started = False

    def queued(self, seconds):
        self.queue_elapsed = seconds
        if (self.store.get_task(self.task_id) or {}).get("status") in ("queued", "processing"):
            self.store.update_task(self.task_id, queue_elapsed=round(seconds, 1))

    def check_queued(self):
        if (self.store.get_task(self.task_id) or {}).get("status") != "queued":
            raise TaskNotQueued()

    def acquired(self):
        if not self.store.transition_task(
            self.task_id, "queued", status="processing", started_at=time.time()
        ):
            raise TaskNotQueued()
        self.started = True

    def account(self, aid, attempt):
        self.store.update_task(self.task_id, account=aid, attempt=attempt)

    def error(self, message):
        self.store.update_task(self.task_id, last_error=message)

    def finish(self):
        total = time.monotonic() - self.created
        return {"elapsed": round(max(0, total - self.queue_elapsed), 1) if self.started else 0,
                "queue_elapsed": round(self.queue_elapsed, 1), "total_elapsed": round(total, 1)}
