"""Persistent, sequential batch workflows for the mac2API dashboard."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from store import _read, _write

log = logging.getLogger("muse2api")
WorkflowKind = Literal["text", "image", "video"]
MAX_ITEMS = 25


class WorkflowStep(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: WorkflowKind
    instruction: str = Field(default="", max_length=8000)
    model: str = Field(default="", max_length=160)
    aspect_ratio: Literal["", "1:1", "9:16", "16:9", "3:4", "4:3"] = ""
    duration: int | None = Field(default=None, ge=1, le=600)
    input_image: bool = False


class WorkflowRequest(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(default="Luồng mới", max_length=100)
    input: str = Field(min_length=1, max_length=40000)
    context: str = Field(default="", max_length=16000)
    split: bool = True
    steps: list[WorkflowStep] = Field(min_length=1, max_length=10)


class SavedStep(BaseModel):
    status: Literal["queued", "running", "completed", "failed"]
    attempt: int = Field(ge=1)
    task_id: str | None = None
    error: str | None = None
    result: dict | None = None
    reuse_task: bool = False
    check_before_retry: bool = False


class SavedItem(BaseModel):
    input: str
    steps: list[SavedStep]


class SavedWorkflow(BaseModel):
    id: str
    config: WorkflowRequest
    state: Literal["running", "pausing", "paused", "errors", "completed"]
    context: str
    items: list[SavedItem] = Field(min_length=1, max_length=MAX_ITEMS)
    updated_at: float = Field(allow_inf_nan=False)


class WorkflowStepError(RuntimeError):
    """A displayable failure for one step."""

    def __init__(self, message, *, reuse_task=False, check_before_retry=False):
        super().__init__(message)
        self.reuse_task = reuse_task
        self.check_before_retry = check_before_retry


class WorkflowStorageError(HTTPException):
    def __init__(self):
        super().__init__(507, "Không lưu được tiến độ. Kiểm tra dung lượng ổ đĩa rồi thử lại.")


def split_workflow_input(value, split):
    value = value.strip()
    if not value:
        raise ValueError("Hãy nhập nội dung cần xử lý.")
    if not split or "[" not in value:
        return [value], ""
    items, shared = [], []
    depth = start = outside = 0
    for index, char in enumerate(value):
        if char == "[":
            if depth == 0:
                shared.append(value[outside:index])
                start = index + 1
            depth += 1
        elif char == "]":
            if not depth:
                raise ValueError("Có dấu ] chưa có dấu [ tương ứng. Hãy sửa dấu ngoặc hoặc tắt tách mục.")
            depth -= 1
            if depth == 0:
                item = value[start:index].strip()
                if not item:
                    raise ValueError("Có mục [ ] đang để trống.")
                items.append(item)
                outside = index + 1
    if depth:
        raise ValueError("Có mục [ ... ] chưa đóng. Hãy thêm dấu ] hoặc tắt tách mục.")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"Mỗi luồng hỗ trợ tối đa {MAX_ITEMS} mục. Hãy chia thành các đợt nhỏ hơn.")
    shared.append(value[outside:])
    return items, "\n".join(part.strip() for part in shared if part.strip())


def workflow_prompt(run, item, index):
    config = run["config"]
    parts = []
    for title, content in (("Ngữ cảnh chung", run["context"]), ("Nội dung của mục", item["input"])):
        if content:
            parts.append(f"{title}:\n{content}")
    for step, state in zip(config["steps"][:index], item["steps"][:index]):
        text = (state.get("result") or {}).get("text")
        if text:
            parts.append(f"Kết quả bước {step['name']}:\n{text}")
    instruction = config["steps"][index]["instruction"].strip()
    if instruction:
        parts.append(f"Yêu cầu cho bước hiện tại:\n{instruction}")
    return "\n\n".join(parts)


class Workflows:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.active = {}
        self.errors = {}

    @staticmethod
    def _load(path):
        try:
            saved = SavedWorkflow.model_validate(_read(str(path), None), strict=True)
        except ValidationError:
            return None
        if saved.id != path.stem or saved.config.id != saved.id or any(
            len(item.steps) != len(saved.config.steps) for item in saved.items
        ):
            return None
        run = saved.model_dump(exclude_unset=True)
        run["config"] = saved.config.model_dump()
        return run

    def get(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise HTTPException(404, "Không tìm thấy luồng.")
        path = self.directory / (run_id + ".json")
        run = self._load(path)
        if run is None:
            if path.is_file():
                raise HTTPException(409, "Tệp tiến độ của luồng bị lỗi. Hãy khôi phục tệp từ bản sao lưu.")
            raise HTTPException(404, "Không tìm thấy luồng đã lưu.")
        if run_id in self.errors:
            run.update(state="paused", error=self.errors[run_id])
        return run

    def list(self):
        if not self.directory.exists():
            return []
        files = []
        for path in self.directory.glob("*.json"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
        runs = []
        for _, path in sorted(files, reverse=True):
            run = self._load(path)
            if run is None:
                continue
            runs.append({"id": run["id"], "title": run["config"].get("title") or "Luồng đã lưu",
                         "state": "paused" if run["id"] in self.errors else run["state"],
                         "updated_at": run["updated_at"]})
            if len(runs) == 30:
                break
        return runs

    def save(self, run):
        run["updated_at"] = time.time()
        try:
            _write(str(self.directory / (run["id"] + ".json")), run)
        except OSError as exc:
            raise WorkflowStorageError() from exc
        self.errors.pop(run["id"], None)

    def create(self, request, execute):
        config = request.model_dump()
        path = self.directory / (request.id + ".json")
        if path.exists():
            run = self.get(request.id)
            if run["config"] != config:
                raise HTTPException(409, "Mã luồng này đã dùng cho cấu hình khác.")
            return run
        try:
            items, shared = split_workflow_input(request.input, request.split)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        previous_image = False
        for step in request.steps:
            if not step.name.strip():
                raise HTTPException(422, "Hãy đặt tên cho từng bước.")
            if step.input_image and (step.kind == "text" or (step.model and not step.model.startswith("muse"))):
                raise HTTPException(422, "Chỉ bước ảnh hoặc video Muse nhận ảnh từ bước trước.")
            if step.input_image and not previous_image:
                raise HTTPException(422, f"Bước {step.name} cần đặt sau một bước tạo ảnh.")
            previous_image |= step.kind == "image"
        run = {"id": request.id, "config": config, "state": "running",
               "context": "\n\n".join(part for part in (request.context.strip(), shared) if part),
               "items": [{"input": item, "steps": [{"status": "queued", "attempt": 1}
                                                  for _ in request.steps]} for item in items]}
        self.save(run)
        self.schedule(request.id, execute)
        return run

    def schedule(self, run_id, execute):
        previous = self.active.get(run_id)
        if not previous or previous.done():
            task = asyncio.create_task(self.drive(run_id, execute))
            self.active[run_id] = task
            task.add_done_callback(lambda done: self._finished(run_id, done))

    def _finished(self, run_id, task):
        if self.active.get(run_id) is task:
            del self.active[run_id]
        if not task.cancelled() and (error := task.exception()) is not None:
            if run_id not in self.active:
                self.errors[run_id] = ("Không lưu được tiến độ. Kiểm tra dung lượng đĩa rồi bấm Tiếp tục."
                                       if isinstance(error, WorkflowStorageError) else
                                       "Luồng đã dừng bất ngờ. Bấm Tiếp tục để kiểm tra tiến độ đã lưu.")
            log.error("Luồng %s dừng bất ngờ.", run_id, exc_info=error)

    def control(self, run_id, action, execute, item=None, step=None):
        run = self.get(run_id)
        activity = self.active.get(run_id)
        if action == "pause":
            if run["state"] == "running":
                run["state"] = "pausing" if activity and not activity.done() else "paused"
        elif action == "resume":
            if run["state"] in ("paused", "pausing", "running"):
                run.pop("error", None)
                run["state"] = "running"
        elif action == "retry":
            try:
                if item is None or item < 0 or step is None or step < 0:
                    raise IndexError
                state = run["items"][item]["steps"][step]
            except IndexError as exc:
                raise HTTPException(404, "Không tìm thấy bước này.") from exc
            if state["status"] != "failed":
                raise HTTPException(409, "Chỉ có thể chạy lại bước đang báo lỗi.")
            if state.get("reuse_task"):
                next_state = {"status": "queued", "attempt": state["attempt"]}
                if state.get("task_id"):
                    next_state["task_id"] = state["task_id"]
            else:
                next_state = {"status": "queued", "attempt": state["attempt"] + 1}
            run["items"][item]["steps"][step] = next_state
            run["state"] = "running"
        self.save(run)
        if run["state"] == "running":
            self.schedule(run_id, execute)
        return run

    def update_step(self, run_id, item, step, **changes):
        run = self.get(run_id)
        state = run["items"][item]["steps"][step]
        state.update(changes)
        self.save(run)

    async def drive(self, run_id, execute):
        while True:
            run = self.get(run_id)
            if run["state"] != "running":
                if run["state"] == "pausing":
                    run["state"] = "paused"
                    self.save(run)
                return
            pending = None
            has_errors = False
            for item_index, item in enumerate(run["items"]):
                if any(state["status"] == "failed" for state in item["steps"]):
                    has_errors = True
                    continue
                for index, state in enumerate(item["steps"]):
                    if state["status"] in ("queued", "running"):
                        pending = (item_index, index)
                        break
                if pending:
                    break
            if pending is None:
                run["state"] = "errors" if has_errors else "completed"
                self.save(run)
                return
            item_index, index = pending
            state = run["items"][item_index]["steps"][index]
            if state["status"] == "running" and run["config"]["steps"][index]["kind"] == "text":
                self.update_step(run_id, item_index, index, status="failed",
                                 error="Bước văn bản đã bị gián đoạn. Kiểm tra trước khi chạy lại.",
                                 check_before_retry=True)
                continue
            self.update_step(run_id, item_index, index, status="running")

            def checkpoint(task_id):
                self.update_step(run_id, item_index, index, task_id=task_id)

            try:
                result = await execute(run, item_index, index, checkpoint)
            except WorkflowStepError as exc:
                self.update_step(run_id, item_index, index, status="failed", error=str(exc),
                                 reuse_task=exc.reuse_task, check_before_retry=exc.check_before_retry)
            except WorkflowStorageError:
                # Persistence failed while remembering a newly accepted task. The
                # saved running step can recover with its original receipt key.
                raise
            except Exception:
                log.exception("Không xử lý được bước %s của luồng %s.", index + 1, run_id)
                self.update_step(run_id, item_index, index, status="failed",
                                 error="Không xử lý được bước này. Kiểm tra dịch vụ AI rồi thử lại.")
            else:
                self.update_step(run_id, item_index, index, status="completed", result=result)

    def recover(self):
        if not self.directory.exists():
            return
        for path in self.directory.glob("*.json"):
            run = self._load(path)
            if run is None:
                log.warning("Không đọc được tệp tiến độ luồng %s. Tệp gốc được giữ lại.", path.name)
                continue
            if run["state"] not in ("running", "pausing"):
                continue
            for item in run["items"]:
                for index, state in enumerate(item["steps"]):
                    if state["status"] != "running":
                        continue
                    if run["config"]["steps"][index]["kind"] == "text":
                        state.update(status="failed", check_before_retry=True,
                                     error="mac2API đã khởi động lại giữa bước. Kiểm tra trước khi chạy lại.")
                    else:
                        state["status"] = "queued"
            run["state"] = "paused"
            try:
                self.save(run)
            except HTTPException as exc:
                self.errors[run["id"]] = str(exc.detail)
                log.warning("Không khôi phục được tiến độ luồng %s.", run["id"])

    async def close(self):
        running = list(self.active.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
