"""Verify batch order, retries, saved progress, and the existing generation routes."""
import asyncio
import base64
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def check(data_dir):
    os.environ.update(MUSE2API_HOME=data_dir, MUSE2API_PROFILE_ROOT=data_dir, MUSE2API_KEY="workflow-fixture")
    import app
    from workflows import split_workflow_input

    assert split_workflow_input("Chung [một [con]] giữa [hai] cuối", True) == (
        ["một [con]", "hai"], "Chung\ngiữa\ncuối")
    assert split_workflow_input("không tách [văn bản]", False) == (["không tách [văn bản]"], "")
    headers = {"Authorization": "Bearer workflow-fixture"}
    seen = []
    attempts = 0

    async def text(req, *_args):
        nonlocal attempts
        prompt = req.messages[0].content
        seen.append(prompt)
        if "mục lỗi" in prompt and "chỉ dẫn hai" in prompt:
            attempts += 1
            if attempts == 1:
                raise app.HTTPException(502, "Nhà cung cấp tạm thời chưa trả lời.")
        return {"choices": [{"message": {"content": "Kết quả bước một" if "chỉ dẫn một" in prompt else "Kết quả bước hai"}}]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://127.0.0.1") as client:
        async def await_state(run_id, want):
            for _ in range(320):
                response = await client.get("/admin/workflows/" + run_id, headers=headers)
                assert response.status_code == 200, response.text
                run = response.json()
                if run["state"] == want:
                    return run
                await asyncio.sleep(0.025)
            raise AssertionError(run)

        run_id = "a" * 32
        request = {"id": run_id, "input": "đầu vào chung [mục lỗi] [mục tốt]", "context": "ngữ cảnh chung",
                   "steps": [{"name": "Một", "kind": "text", "instruction": "chỉ dẫn một"},
                             {"name": "Hai", "kind": "text", "instruction": "chỉ dẫn hai"}]}
        with patch.object(app, "_chat_completions", side_effect=text):
            assert (await client.post("/admin/workflows", json=request)).status_code == 401
            invalid = await client.post("/admin/workflows", headers=headers, json={**request, "input": "[chưa đóng"})
            assert invalid.status_code == 422 and "chưa đóng" in invalid.json()["detail"]
            response = await client.post("/admin/workflows", headers=headers, json=request)
            assert response.status_code == 201, response.text
            partial = await await_state(run_id, "errors")
            assert [item["steps"][-1]["status"] for item in partial["items"]] == ["failed", "completed"]
            assert all("đầu vào chung" in prompt and "ngữ cảnh chung" in prompt for prompt in seen)
            assert "mục tốt" not in seen[1] and "Kết quả bước một" in seen[1]
            assert "tạm thời" in partial["items"][0]["steps"][1]["error"]
            duplicate = await client.post("/admin/workflows", headers=headers, json=request)
            assert duplicate.json()["id"] == run_id and len(seen) == 4
            retry = await client.post(f"/admin/workflows/{run_id}/retry", headers=headers, json={"item": 0, "step": 1})
            assert retry.status_code == 200, retry.text
            done = await await_state(run_id, "completed")
            assert len(seen) == 5 and done["items"][0]["steps"][1]["attempt"] == 2
            assert done["items"][1]["steps"][1]["attempt"] == 1 and "Kết quả bước một" in seen[-1]

        pending = asyncio.Event()
        release = asyncio.Event()

        async def fake_step(_run, _item, index, _checkpoint):
            if index == 0:
                pending.set()
                await release.wait()
            return {"kind": "text", "text": str(index)}

        run_id = "b" * 32
        with patch.object(app, "_execute_workflow_step", side_effect=fake_step):
            response = await client.post("/admin/workflows", headers=headers,
                                         json={**request, "id": run_id, "input": "tạm dừng"})
            assert response.status_code == 201, response.text
            await asyncio.wait_for(pending.wait(), 1)
            assert (await client.post(f"/admin/workflows/{run_id}/pause", headers=headers)).json()["state"] == "pausing"
            release.set()
            paused = await await_state(run_id, "paused")
            assert [step["status"] for step in paused["items"][0]["steps"]] == ["completed", "queued"]
            assert (await client.post(f"/admin/workflows/{run_id}/resume", headers=headers)).status_code == 200
            await await_state(run_id, "completed")

        image = Path(app.CFG.media_dir) / "workflow-fixture.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture-image")
        app.store.add_account({"hatch_sess": "workflow-fixture"}, "fixture")
        run_id = "c" * 32
        with patch.object(app, "_run_generation",
                          return_value=({"filename": image.name, "size": image.stat().st_size,
                                         "kind": "image", "path": str(image)}, "fixture")) as generate:
            response = await client.post("/admin/workflows", headers=headers, json={
                "id": run_id, "input": "tạo ảnh", "steps": [{"name": "Tạo ảnh", "kind": "image"}]})
            assert response.status_code == 201, response.text
            done = await await_state(run_id, "completed")
            result = done["items"][0]["steps"][0]
            assert result["task_id"].startswith("task_") and "access=a." in result["result"]["url"]
            preview = await client.get(result["result"]["url"])
            download = await client.get(result["result"]["download_url"])
            assert preview.content == download.content == image.read_bytes()
            assert "content-disposition" not in preview.headers
            assert download.headers["content-disposition"].startswith("attachment;")
            assert generate.call_count == 1
            interrupted = app.workflows.get(run_id)
            interrupted["state"] = result["status"] = "running"
            interrupted["items"][0]["steps"][0] = result
            result["result"].pop("url", None)
            app.workflows.save(interrupted)
            app.workflows.recover()
            paused = await await_state(run_id, "paused")
            assert paused["items"][0]["steps"][0]["task_id"] == result["task_id"]
            assert (await client.post(f"/admin/workflows/{run_id}/resume", headers=headers)).status_code == 200
            await await_state(run_id, "completed")
            assert generate.call_count == 1, "recovery should read the original task, never create another image"

        original_request = httpx.ASGITransport.handle_async_request
        poll_failures = 0

        async def interrupted_poll(transport, req):
            nonlocal poll_failures
            if req.method == "GET" and req.url.path.startswith("/v1/images/tasks/") and poll_failures < 3:
                poll_failures += 1
                return httpx.Response(503)
            return await original_request(transport, req)

        run_id = "e" * 32
        with patch.object(app, "_run_generation", return_value=(
                {"filename": image.name, "size": image.stat().st_size, "kind": "image",
                 "path": str(image)}, "fixture")) as generate, \
             patch.object(httpx.ASGITransport, "handle_async_request", new=interrupted_poll):
            response = await client.post("/admin/workflows", headers=headers, json={
                "id": run_id, "input": "giữ biên nhận", "steps": [{"name": "Ảnh", "kind": "image"}]})
            assert response.status_code == 201, response.text
            failed = (await await_state(run_id, "errors"))["items"][0]["steps"][0]
            assert failed["reuse_task"] and failed["task_id"] and generate.call_count == 1
            response = await client.post(f"/admin/workflows/{run_id}/retry", headers=headers, json={"item": 0, "step": 0})
            assert response.status_code == 200
            done = (await await_state(run_id, "completed"))["items"][0]["steps"][0]
            assert done["task_id"] == failed["task_id"] and done["attempt"] == failed["attempt"]
            assert generate.call_count == 1

        video = Path(app.CFG.media_dir) / "workflow-fixture.mp4"
        video.write_bytes(b"fixture-video")

        def media(prompt, kind, _timeout, **kwargs):
            path = image if kind == "image" else video
            if kind == "video":
                assert "ngữ cảnh chung" in prompt
                expected = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
                assert kwargs["reference_images"] == [expected]
            return {"filename": path.name, "size": path.stat().st_size, "kind": kind,
                    "path": str(path)}, "fixture"

        run_id = "d" * 32
        with patch.object(app, "_run_generation", side_effect=media) as generate, \
             patch.object(app, "_asset_usage", return_value={}):
            response = await client.post("/admin/workflows", headers=headers, json={
                **request, "id": run_id, "input": "chuỗi ảnh rồi video",
                "steps": [{"name": "Ảnh", "kind": "image"},
                          {"name": "Video", "kind": "video", "input_image": True, "duration": 10}]})
            assert response.status_code == 201, response.text
            done = await await_state(run_id, "completed")
            assert generate.call_count == 2 and done["items"][0]["steps"][1]["result"]["filename"] == video.name
            # An already accepted video can be recovered after its earlier reference is removed.
            image.unlink()
            interrupted = app.workflows.get(run_id)
            interrupted["state"] = interrupted["items"][0]["steps"][1]["status"] = "running"
            app.workflows.save(interrupted)
            app.workflows.recover()
            assert (await client.post(f"/admin/workflows/{run_id}/resume", headers=headers)).status_code == 200
            await await_state(run_id, "completed")
            assert generate.call_count == 2


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="mac2api-workflow-test-") as root:
        asyncio.run(check(root))
    print("PASS: ordered workflow, isolated errors, retry, pause, and media receipt recovery")
