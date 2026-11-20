"""Offline contract fixtures. Run directly with Python; no AI upstream calls."""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
RUNTIME = tempfile.TemporaryDirectory(prefix="mac2api-contract-tests-")
os.environ.update(MUSE2API_HOME=RUNTIME.name, MUSE2API_PROFILE_ROOT=RUNTIME.name,
                  MUSE2API_KEY="fixture-admin-key")
import app
from web_providers import WebProviders


def bearer(token):
    return {"Authorization": "Bearer " + token}


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app.store.tasks = {}
        app.store.accounts = []
        app.store.clients = {}
        self.alice = app.store.create_client("Alice", ["muse"])
        self.bob = app.store.create_client("Bob", ["muse", "chatgpt", "gemini-web"])
        self.a = bearer(self.alice["api_key"])
        self.b = bearer(self.bob["api_key"])
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                      base_url="http://127.0.0.1")
        self.addAsyncCleanup(self.http.aclose)

    async def test_capabilities_readiness_scope_and_https(self):
        endpoint = "/v1/capabilities"
        for headers, status in (({}, 401), (bearer("fixture-admin-key"), 403)):
            response = await self.http.get(endpoint, headers=headers)
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["cache-control"], "no-store")
        result = await self.http.get(endpoint, headers=self.b)
        data = result.json()
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(set(data), {"contractVersion", "serverVersion", "providers", "models", "limits", "features"})
        self.assertEqual(data["serverVersion"], app._read_local_version()["version"])
        self.assertEqual(data["contractVersion"], 1)
        self.assertEqual(data["limits"], {"perClientPending": 2, "perKindPending": 8})
        self.assertEqual({item["id"] for item in data["providers"]}, {"muse", "chatgpt", "gemini-web"})
        self.assertTrue(all(not item["ready"] for item in data["providers"]))
        self.assertFalse(any(item["type"] == "video" and item["provider"] != "muse" for item in data["models"]))
        self.assertEqual((await self.http.get("/chatgpt/v1/capabilities", headers=self.a)).status_code, 403)
        account = app.store.add_account({"hatch_sess": "fixture"}, "fixture")
        app.store.update_account(account["id"], ok=True)
        with patch.object(app.shutil, "which", return_value="/fixture/chromium"), \
             patch.object(app.engine, "start", side_effect=AssertionError("capabilities started Chromium")):
            data = (await self.http.get(endpoint, headers=self.a)).json()
        self.assertEqual(data["providers"][0], {"id": "muse", "configured": True, "connected": False,
                                               "ready": True, "busy": False, "reason": "browser_idle"})
        if os.environ.get("MAC2API_TEST_PRINT_SAMPLE") == "1":
            print("CAPABILITIES_FIXTURE", json.dumps(data, ensure_ascii=False))
        client = ("198.51.100.23", 1234)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app, client=client)) as remote:
            insecure = await remote.get("http://127.0.0.1" + endpoint, headers=self.a)
            self.assertEqual(insecure.status_code, 403)
            self.assertEqual(insecure.json()["error"]["code"], "HTTPS_REQUIRED")
            self.assertEqual((await remote.get("https://api.example.invalid" + endpoint,
                                               headers=self.a)).status_code, 200)
        for origin, expected in (("http://127.0.0.1:3000", 200), ("https://untrusted.invalid", 400)):
            preflight = await self.http.options(endpoint, headers={
                "Origin": origin, "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization"})
            self.assertEqual(preflight.status_code, expected)

    async def test_lookup_idempotency_and_queued_cancellation(self):
        path = "/v1/images/tasks"
        key = str(uuid4())
        headers = {**self.a, "Idempotency-Key": key}
        app.store.add_account({"hatch_sess": "fixture"}, "fixture")
        slot = app.ConcurrencyLock(1)
        self.assertTrue(slot.acquire())
        try:
            with patch.object(app, "GEN_LOCK", slot), \
                 patch.object(app.engine, "start", side_effect=AssertionError("cancelled worker started Chrome")) as start:
                submissions = await asyncio.gather(*(self.http.post(path, headers=headers, json={
                    "model": "muse/image", "prompt": "fixture"
                }) for _ in range(2)))
                self.assertEqual([r.status_code for r in submissions], [202, 202])
                task_id = submissions[0].json()["task_id"]
                self.assertEqual(task_id, submissions[1].json()["task_id"])
                self.assertEqual(len(app.store.tasks), 1)
                lookup = "/v1/tasks/by-request"
                self.assertEqual((await self.http.get(lookup, headers=self.a)).status_code, 400)
                self.assertEqual((await self.http.get(lookup, headers={**self.b, "Idempotency-Key": key})).status_code, 404)
                receipt = await self.http.get(lookup, headers=headers)
                self.assertEqual(receipt.json(), {"id": task_id, "task_id": task_id, "status": "queued",
                                                  "kind": "image", "progress": 0})
                if os.environ.get("MAC2API_TEST_PRINT_SAMPLE") == "1":
                    print("LOOKUP_FIXTURE", json.dumps(receipt.json()))
                for route, body in ((path, {"prompt": "changed"}), ("/v1/videos", {"prompt": "fixture"})):
                    response = await self.http.post(route, json=body, headers=headers)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
                cancel = f"{path}/{task_id}/cancel"
                self.assertEqual((await self.http.post(cancel, headers=self.b)).status_code, 404)
                cancelled = await self.http.post(cancel, headers=self.a)
                self.assertEqual(cancelled.json(), {"task_id": task_id, "status": "cancelled", "cancelled": True})
                self.assertEqual((await self.http.get(lookup, headers=headers)).json()["status"], "cancelled")
                terminal = await self.http.post(cancel, headers=self.a)
                self.assertEqual(terminal.status_code, 409)
                self.assertEqual(terminal.json()["code"], "ALREADY_TERMINAL")
                self.assertEqual((await self.http.delete("/admin/tasks/" + task_id,
                                                        headers=bearer("fixture-admin-key"))).status_code, 200)
                self.assertIsNone(app.store.get_task(task_id))
                self.assertEqual((await self.http.get(lookup, headers=headers)).json()["id"], task_id)
                replay = await self.http.post(path, headers=headers, json={"model": "muse/image", "prompt": "fixture"})
                self.assertEqual(replay.json()["id"], task_id)
                self.assertNotIn("prompt", app.store.tasks[task_id])
                self.assertNotIn(key, Path(app.CFG.tasks_file).read_text())
                slot.release()
                await asyncio.sleep(0.30)
                start.assert_not_called()
                self.assertEqual(app.store.tasks[task_id]["status"], "cancelled")
                self.assertTrue(slot.acquire(blocking=False))
        finally:
            slot.release()

    async def test_processing_cancel_does_not_release_generation_lease(self):
        app.store.add_account({"hatch_sess": "fixture"}, "fixture")
        started, release = threading.Event(), threading.Event()
        slot = app.ConcurrencyLock(1)
        accounts = app.AccountPoolManager(app.store)

        def fake_generation(*args, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("fixture deadline")
            return {"filename": "fixture.mp4", "size": 7, "kind": "video"}

        with patch.object(app, "GEN_LOCK", slot), patch.object(app, "ACCOUNT_POOL", accounts), \
             patch.object(app, "_renew_and_persist", return_value=None), patch.object(app, "_sync_cookies"), \
             patch.object(app.engine, "start"), patch.object(app.engine, "close_current_page"), \
             patch.object(app.engine, "generate", side_effect=fake_generation):
            response = await self.http.post("/v1/videos", headers=self.a, json={"prompt": "test"})
            task_id = response.json()["task_id"]
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                cancellation = await self.http.post(f"/v1/videos/{task_id}/cancel", headers=self.a)
                self.assertEqual(cancellation.status_code, 409)
                self.assertEqual(cancellation.json()["code"], "CANNOT_CANCEL_RUNNING")
                self.assertEqual(cancellation.json()["status"], "processing")
                self.assertTrue(slot.full())
                self.assertTrue(accounts.busy_accounts)
            finally:
                release.set()
            for _ in range(60):
                if app.store.get_task(task_id)["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(slot.locked())
            self.assertFalse(accounts.busy_accounts)
            self.assertEqual(app.store.get_task(task_id)["status"], "completed")

    async def test_missing_references_are_rejected_before_browser_admission(self):
        with patch.object(app, "_start_media_worker", side_effect=AssertionError("invalid input started a worker")):
            for endpoint, prompt in (
                ("/v1/images/tasks", "将输入图像中人物的服装替换为参考图像中的服装"),
                ("/v1/images/generations", '{"task":"reference_image_guided_photo"}'),
                ("/v1/images/edits", "Đổi trang phục theo [图1]"),
                ("/v1/videos", "Mở đầu theo [图1]"),
            ):
                response = await self.http.post(endpoint, headers=self.a, json={"prompt": prompt})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "MUSE_REFERENCES_MISSING")
        self.assertFalse(app.store.tasks)

    async def test_legacy_task_errors_get_diagnostics_without_mutating_history(self):
        for reply, code, stage in (
            ("Hết thời gian tải Muse.ai vì ô chat chưa xuất hiện.", "MUSE_PAGE_NOT_READY", "before_send"),
            ("Muse chỉ trả văn bản và chưa gửi tệp kết quả. Phản hồi của Muse: Lượt này không có ảnh nào đính kèm.",
             "MUSE_REFERENCES_NOT_SEEN", "muse_reply"),
            ("Muse chỉ trả văn bản và chưa gửi tệp kết quả. Phản hồi của Muse: "
             "Tính năng tạo video không khả dụng cho tài khoản này ở quốc gia/khu vực hiện tại.",
             "MUSE_VIDEO_UNAVAILABLE", "muse_reply"),
        ):
            kind = "video" if code == "MUSE_VIDEO_UNAVAILABLE" else "image"
            task = app.store.create_task(kind, "fixture", owner=self.alice["id"])
            app.store.update_task(task["id"], status="failed", error=reply)
            prefix = "/v1/videos/" if kind == "video" else "/v1/images/tasks/"
            result = (await self.http.get(prefix + task["id"], headers=self.a)).json()
            self.assertEqual(result["error_code"], code)
            self.assertEqual(result["error_stage"], stage)
            raw = app.store.get_task(task["id"])
            self.assertNotIn("error_code", raw)
            self.assertEqual(raw["error"], reply)

    async def test_muse_confirmation_reply_stops_after_one_send(self):
        for label in ("first", "second"):
            app.store.add_account({"hatch_sess": label}, label)
        reply = "Em tạo 1 ảnh meme mèo 1:1 thay thế nhé?\nTạo ảnh meme mèo 1:1 thay thế"
        with patch.object(app, "_renew_and_persist", return_value=None), \
             patch.object(app.engine, "start"), patch.object(app.engine, "reset_thread"), \
             patch.object(app.engine, "close_current_page"), \
             patch.object(app.engine, "generate", side_effect=app.engine._media_text_error(reply)) as generate:
            response = await self.http.post("/v1/images/tasks", headers=self.a,
                                            json={"model": "muse/image", "prompt": "Ảnh meme con mèo"})
            self.assertEqual(response.status_code, 202)
            endpoint = "/v1/images/tasks/" + response.json()["task_id"]
            for _ in range(60):
                task = (await self.http.get(endpoint, headers=self.a)).json()
                if task["status"] == "failed":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["error_code"], "MUSE_CONFIRMATION_REQUIRED")
            self.assertEqual(task["error_stage"], "muse_reply")
            self.assertEqual(task["reference_count"], 0)
            self.assertIn("phương án chờ duyệt", task["error"])
            self.assertIn("meme mèo 1:1 thay thế", task["error"])
            self.assertIsNone(task["result"])
            generate.assert_called_once()

    async def test_backpressure_and_restart(self):
        owner = self.alice["id"]
        req = app.ImageRequest(prompt="fixture", model="muse/image")
        key = str(uuid4())
        first = app.store.create_task("image", "fixture", owner=owner, **app._media_submission(req, key))
        app.store.transition_task(first["id"], "queued", status="processing")
        second = app.store.create_task("video", "fixture", owner=owner)
        endpoint = "/v1/images/tasks"
        rejected = await self.http.post(endpoint, headers=self.a, json={"prompt": "test", "model": "muse/image"})
        self.assertEqual((rejected.status_code, rejected.json()["error"]["code"]), (429, "CLIENT_PENDING_LIMIT"))
        for _ in range(7):
            app.store.create_task("image", "fixture", owner=uuid4().hex[:16])
        rejected = await self.http.post(endpoint, headers=self.b, json={"prompt": "test", "model": "muse/image"})
        self.assertEqual((rejected.status_code, rejected.json()["error"]["code"]), (429, "QUEUE_FULL"))
        self.assertTrue(rejected.headers["Retry-After"].isdigit())
        with patch.object(app, "store", app.Store(app.CFG)), \
             patch.object(app, "_start_media_worker", side_effect=AssertionError("startup replayed")) as worker:
            await app._startup()
            try:
                worker.assert_not_called()
                for task in (first, second):
                    restored = app.store.get_task(task["id"])
                    self.assertEqual(restored["status"], "interrupted")
                    self.assertTrue(restored["needs_reconcile"])
                receipt = await self.http.get("/v1/tasks/by-request", headers={**self.a, "Idempotency-Key": key})
                self.assertEqual(receipt.json()["status"], "interrupted")
            finally:
                await app._shutdown()

    async def test_signed_admin_media_and_scope(self):
        name = "fixture-admin.png"
        path = Path(app.CFG.media_dir) / name
        path.write_bytes(b"fixture")
        self.addCleanup(path.unlink, missing_ok=True)
        signed = app.media_url(name, admin=True)
        self.assertEqual((await self.http.get(urlsplit(signed).path)).status_code, 404)
        self.assertEqual((await self.http.get(urlsplit(signed).path, headers=self.a)).status_code, 404)
        response = await self.http.get(signed)
        self.assertEqual(response.content, b"fixture")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["content-length"], "7")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        with patch.object(app.time, "time", return_value=time.time() + app.MEDIA_URL_TTL_SECONDS + 1):
            self.assertEqual((await self.http.get(signed)).status_code, 404)
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, "%s %s %s %s %s",
                                   ("client", "GET", signed, "1.1", 200), None)
        self.assertTrue(app.AccessLogFilter().filter(record))
        self.assertNotIn("access=", record.getMessage())

    def test_web_connection_is_separate_from_verified_tab(self):
        providers = WebProviders(19211)
        tabs = [{"id": "tab1", "type": "page", "url": "https://chatgpt.com/"}]
        with patch("web_providers.requests.Session") as session:
            reply = session.return_value.__enter__.return_value.get.return_value
            reply.ok = True
            reply.json.return_value = tabs
            states = providers.capability_states()
            self.assertTrue(states["chatgpt"]["connected"])
            self.assertFalse(states["chatgpt"]["ready"])
            providers.linked_targets["chatgpt"] = "tab1"
            self.assertTrue(providers.capability_states()["chatgpt"]["ready"])
            reply.json.return_value = [{**tabs[0], "url": "https://other.invalid/"}]
            self.assertFalse(providers.capability_states()["chatgpt"]["ready"])
            reply.ok = False
            self.assertFalse(providers.capability_states()["chatgpt"]["connected"])
            reply.ok = True
            reply.json.return_value = tabs
            self.assertTrue(providers.capability_states()["chatgpt"]["ready"])

    def test_web_image_fetch_rejects_internal_urls(self):
        from unittest.mock import Mock
        from web_providers import WebProviderError, _read_image
        page = Mock()
        for url in ("https://127.0.0.1/abc", "https://local.test/a",
                    "https://oaiusercontent.com.evil.test/a", "https://user@chatgpt.com/a"):
            with self.assertRaises(WebProviderError):
                _read_image(page, url)
        page.js.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
