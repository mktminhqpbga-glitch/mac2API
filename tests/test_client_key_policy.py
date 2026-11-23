"""Exercise model and account policy, durable accounting, and isolated usage through FastAPI."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch as mock_patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run():
    import app
    from client_usage import ClientUsage
    from media_storage import new_media_name, save_media

    from contextlib import ExitStack

    def headers(key):
        return {"Authorization": f"Bearer {key}"}

    admin = headers("test-admin-key")
    first = app.store.add_account({"hatch_sess": "fixture-first"}, "Muse 01")["id"]
    second = app.store.add_account({"hatch_sess": "fixture-second"}, "Muse 02")["id"]
    ignored = app.store.add_account({"hatch_sess": "fixture-ignored"}, "Muse 03")["id"]
    web = app.managed_web.create("chatgpt", "ChatGPT 01")
    app.managed_web.update(web, enabled=True, verified=True)
    seen = []
    pause = threading.Event()
    resume = threading.Event()

    def media(account_id=None, before_submit=None, **_):
        if pause.is_set():
            assert resume.wait(5)
        before_submit()
        seen.append(account_id)
        name = new_media_name(".png")
        path, size = save_media(app.CFG.media_dir, name, data=b"test bytes")
        return {"path": path, "filename": name, "size": size, "kind": "image"}

    def web_text(provider, prompt, *, before_submit, managed_page, **_):
        before_submit()
        return "Văn bản từ tài khoản được cấp quyền"

    async def official(*_):
        return app.JSONResponse({"choices": [{"message": {"content": "Chào bạn"}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 1000, "completion_tokens": 250}})

    def chat(cookies, prompt, expires, timeout, *, account_id, before_submit, **_):
        before_submit()
        seen.append(account_id)
        yield "Phản hồi"
        yield " Muse"

    def generate(cookies, prompt, **kwargs):
        return media(**kwargs)

    with ExitStack() as mocks:
        for method, stub in (("generate", generate), ("chat_stream", chat)):
            mocks.enter_context(mock_patch.object(app.engine, method, side_effect=stub))
        for method in ("start", "close_current_page"):
            mocks.enter_context(mock_patch.object(app.engine, method))
        for method in ("_bind_account_proxy", "_renew_and_persist", "_sync_cookies"):
            mocks.enter_context(mock_patch.object(app, method, return_value=None))
        mocks.enter_context(mock_patch.object(app.web_providers, "generate", side_effect=web_text))
        mocks.enter_context(mock_patch.object(app.chat_providers, "chat", side_effect=official))

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="https://test") as http:
            async def create(label, rules, **extra):
                response = await http.post("/admin/client-keys", headers=admin,
                                           json={"label": label, "rules": rules, **extra})
                assert response.status_code == 201, response.text
                return response.json()

            async def detail(tid, auth=admin):
                prefix = "/admin/api-usage/" if auth == admin else "/v1/usage/"
                response = await http.get(prefix + tid, headers=auth)
                assert response.status_code == 200, response.text
                return response.json()

            async def wait_for(tid):
                for _ in range(150):
                    item = await detail(tid)
                    if item["status"] not in ("queued", "processing"):
                        return item
                    await asyncio.sleep(0.02)
                raise AssertionError("usage did not finish")

            def image_rule(aid, price, **fields):
                return {"accounts": [aid], "charge": {"image": price}, **fields}

            alice = await create("Alice", {"muse/image": image_rule(first, 25, daily_limit=2)},
                                 history_days=1)
            bob = await create("Bob", {"muse/image": image_rule(ignored, 15)})
            a, b = headers(alice["api_key"]), headers(bob["api_key"])
            base = "/admin/client-keys/" + alice["id"]
            assert (await http.get("/v1/models", headers=a)).json()["data"][0]["id"] == "muse-image"
            catalog = (await http.get("/muse/v1/capabilities", headers=a)).json()
            assert {item["id"] for item in catalog["models"]} == {"muse/image", "muse-image"}
            assert (await http.post("/muse/v1/videos", headers=a, json={"prompt": "no"})).status_code == 403
            assert (await http.post("/v1/chat/completions", headers=a,
                                    json={"model": "muse/text", "prompt": "no"})).status_code == 403

            endpoint = "/muse/v1/images/tasks"
            body = {"prompt": "Ảnh mẫu và https://example.test/?secret=hidden"}
            keyed = {**a, "Idempotency-Key": "first-image"}
            assert app.GEN_LOCK.acquire(blocking=False)
            try:
                created = await http.post(endpoint, headers=keyed, json=body)
                tid = created.json()["id"]
                saved = await http.patch(base, headers=admin, json={
                    "label": "Alice renamed", "rules": {
                        "muse/image": image_rule(second, 50, daily_limit=2, upstream={"request": 8})}})
                assert saved.status_code == 200, saved.text
                assert "api_key" not in saved.json()
                repeated = await http.post(endpoint, headers=keyed, json=body)
                assert repeated.json()["id"] == tid
            finally:
                app.GEN_LOCK.release()
            item = await wait_for(tid)
            app.store.update_account(second, quota={"weekly_used_pct": 35, "checked_at": int(time.time())})
            overview = await http.get("/admin/overview?days=7", headers=admin)
            stats = overview.json()
            assert {provider["id"] for provider in stats["providers"]} == {
                "muse", "chatgpt", "gemini-web", "openai", "gemini",
            }
            assert stats["active_keys"] == 2 and stats["requests_today"] == 1
            report = stats["report"]
            assert report["days"] == 7 and report["totals"]["completed"] == 1 and report["kinds"][0]["images"] == 1
            assert report["recent"][0]["id"] == tid and report["recent"][0]["label"] == "Alice renamed"
            user = next(user for user in report["users"] if user["id"] == alice["id"])
            assert user["usage"]["charge"] == 25 and user["limits"] == [{"model": "muse/image", "used": 1, "limit": 2}]
            account = next(account for account in stats["accounts"] if account["id"] == second)
            assert account["quota"]["weekly_used_pct"] == 35
            assert not {"cookies", "cookies_exp", "proxy", "note"} & account.keys()
            assert (await http.get("/admin/overview?days=0", headers=admin)).status_code == 422
            assert (await http.get("/admin/overview", headers=b)).status_code == 401
            compact = (await http.get("/admin/status", headers=admin, params={"compact": 1})).json()
            assert "accounts" not in compact and "tasks" not in compact
            tasks = await http.get("/admin/tasks", headers=admin,
                                   params={"q": tid, "status": "completed", "provider": "muse", "limit": 1})
            assert tasks.json()["total"] == 1 and tasks.json()["tasks"][0]["id"] == tid
            assert item["status"] == "completed", item
            assert seen == [second]
            assert item["account_attempts"][0]["id"] == second
            assert item["charge"] == 25 and item["upstream"] is None
            assert item["input"] == "Ảnh mẫu và [đã ẩn]"
            assert body["prompt"] not in Path(app.CFG.tasks_file).read_text()
            assert "account_attempts" not in (await detail(tid, a))
            assert (await http.get("/v1/usage/" + tid, headers=b)).status_code == 404
            assert (await http.get("/admin/api-usage", headers=b)).status_code == 401
            filtered = await http.get("/admin/api-usage", headers=admin,
                                      params={"owner": alice["id"], "account": second, "model": "muse/image"})
            assert [row["id"] for row in filtered.json()["items"]] == [tid]
            assert "input" not in filtered.json()["items"][0]

            # Change the allowlist after leasing the account, while the provider is preparing its composer.
            pause.set()
            try:
                created = await http.post(endpoint, headers=a, json={"prompt": "quyền thay đổi giữa lúc chuẩn bị"})
                blocked = created.json()["id"]
                for _ in range(150):
                    if (await detail(blocked))["account_attempts"]:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("account never leased")
                overview = (await http.get("/admin/overview", headers=admin)).json()
                user = next(user for user in overview["report"]["users"] if user["id"] == alice["id"])
                assert user["processing"] == 1
                assert next(account for account in overview["accounts"] if account["id"] == second)["busy"]
                patch = await http.patch(base, headers=admin,
                                         json={"rules": {"muse/image": image_rule(first, 50, daily_limit=2)}})
                assert patch.status_code == 200, patch.text
            finally:
                resume.set()
                pause.clear()
            item2 = await wait_for(blocked)
            assert item2["status"] == "failed" and item2["error_code"] == "CLIENT_ACCOUNT_DENIED", item2
            assert item2["charge"] == 0 and seen == [second]
            limited = await http.post(endpoint, headers=a, json={"prompt": "quota"})
            assert limited.status_code == 429 and limited.json()["error"]["code"] == "CLIENT_DAILY_LIMIT"
            assert (await http.post(endpoint, headers=keyed, json=body)).json()["id"] == tid

            await http.patch(base, headers=admin, json={"enabled": False})
            assert (await http.get("/v1/models", headers=a)).status_code == 401
            await http.patch(base, headers=admin, json={"enabled": True})
            assert (await http.get("/v1/models", headers=a)).status_code == 200
            old_asset = (await detail(tid, a))["result_url"]
            assert (await http.get(old_asset)).status_code == 200
            rotated = await http.post(base + "/rotate", headers=admin)
            assert (await http.get("/v1/models", headers=a)).status_code == 401
            assert (await http.get(old_asset)).status_code == 404
            a = headers(rotated.json()["api_key"])
            assert (await http.get(endpoint + "/" + tid, headers=a)).status_code == 200
            await http.delete(base, headers=admin)
            assert any(record["id"] == alice["id"] and record["revoked_at"]
                       for record in (await http.get("/admin/client-keys", headers=admin)).json()["clients"])

            with mock_patch.dict(
                    os.environ, {"OPENAI_API_KEY": "test-official-fixture"}):
                model = next(row["model_id"] for row in app.chat_providers.status() if row["id"] == "openai")
                tenant = await create("Text tenant", {
                    "muse/text": {"accounts": [first]},
                    "chatgpt/text": {"accounts": [web["id"]]},
                    model: {"charge": {"input_1k_tokens": 30, "output_1k_tokens": 100}},
                }, history_days=1)
                t = headers(tenant["api_key"])
                stream = await http.post("/muse/v1/responses", headers=t,
                                         json={"input": "Kiểm thử", "stream": True})
                assert "response.completed" in stream.text, stream.text
                info = await detail(stream.headers["X-Mac2API-Usage-Id"])
                assert info["output"] == "Phản hồi Muse" and info["status"] == "completed"
                assert info["account_attempts"][0]["id"] == first and "input_tokens" not in info["usage"]

                response = await http.post("/chatgpt/v1/chat/completions", headers=t,
                                           json={"prompt": "Kiểm thử ChatGPT", "stream": True})
                info = await detail(response.headers["X-Mac2API-Usage-Id"])
                assert info["output"].startswith("Văn bản") and info["account_attempts"][0]["id"] == web["id"]
                app.managed_web.update(web, enabled=False)
                assert (await http.post("/chatgpt/v1/chat/completions", headers=t,
                                        json={"prompt": "Tài khoản đã tắt"})).status_code == 503

                response = await http.post("/v1/chat/completions", headers=t,
                                           json={"model": model, "prompt": "Kiểm thử chính thức"})
                info = await detail(response.headers["X-Mac2API-Usage-Id"])
                assert info["charge"] == 55 and info["usage"]["output_tokens"] == 250

            # Both older async image endpoints must reuse the receipt before normalizing references.
            for suffix in ("generations", "edits"):
                path = "/muse/v1/images/" + suffix
                retry_headers = {**b, "Idempotency-Key": suffix}
                request = {"prompt": "Ảnh dùng lại mã", "async": True}
                accepted = await http.post(path, headers=retry_headers, json=request)
                assert accepted.status_code == 202, accepted.text
                replay_id = accepted.json()["id"]
                with mock_patch.object(app, "_prepare_reference_images",
                                       side_effect=AssertionError("replay fetched input")):
                    replay = await http.post(path, headers=retry_headers, json=request)
                    assert replay.json()["id"] == replay_id
                    conflict = await http.post(path, headers=retry_headers, json={**request, "prompt": "khác"})
                    assert conflict.status_code == 409
                result = await wait_for(replay_id)
                assert result["charge"] == 15 and result["result_state"] == "available"
                assert (await http.get(result["result_url"])).status_code == 200
                assert "result_url" not in await detail(replay_id, b)

            # Keep an account busy and allow another tenant with a different account to proceed.
            holding = app.ACCOUNT_POOL.acquire(account_id=ignored)
            try:
                pending = await http.post(endpoint, headers=b, json={"prompt": "Đang đợi tài khoản"})
                assert pending.status_code == 202
                pending_id = pending.json()["id"]
                independent = await create("Independent", {"muse/image": image_rule(first, 16)})
                response = await http.post(endpoint, headers=headers(independent["api_key"]),
                                           json={"prompt": "Tài khoản khác đang rảnh"})
                assert response.status_code == 202
                assert (await wait_for(response.json()["id"]))["charge"] == 16
                assert (await detail(pending_id))["status"] == "queued"
            finally:
                app.ACCOUNT_POOL.release(holding["id"])
            assert (await wait_for(pending_id))["charge"] == 15

            reopened = ClientUsage(app.client_usage.path)
            assert reopened.detail(tid, admin=True)["charge"] == 25
            with reopened._database() as db:
                db.execute("UPDATE usage SET expires_at=0 WHERE id=?", (tid,))
            reopened.prune()
            assert "input" not in reopened.detail(tid, admin=True)

            ledger_owner = app.store.find_client(bob["api_key"])
            unsaved = reopened.begin("use_unsaved", ledger_owner, "muse/text", "text", "/muse/v1/chat/completions")
            unsaved.record("finish", "completed", output="no retention")
            assert (await detail(unsaved.id))["result_state"] == "not_saved"
            captured = reopened.begin("use_expired", {**ledger_owner, "history_days": 1}, "muse/text", "text", "/muse/v1/chat/completions")
            captured.record("finish", "completed", output="saved for a day")
            with reopened._database() as db:
                db.execute("UPDATE usage SET expires_at=? WHERE id=?", (time.time() - 1, captured.id))
            reopened.prune()
            expired = await detail(captured.id)
            assert expired["result_state"] == "expired" and "output" not in expired
            recovered = reopened.begin("task_recovered", ledger_owner, "muse/image", "image", endpoint)
            reopened.begin("use_still_running", ledger_owner, "muse/image", "image", endpoint)
            ended_at = int(time.time()) - 10
            reopened.reconcile({recovered.id: {"status": "completed", "kind": "image", "updated_at": ended_at}})
            invoice = reopened.detail(recovered.id, admin=True)
            assert invoice["charge"] == 15 and invoice["ended_at"] == ended_at
            assert reopened.detail("use_still_running", admin=True)["status"] == "queued"
            reopened.reconcile({}, restarting=True)
            assert reopened.detail("use_still_running", admin=True)["error_code"] == "SERVICE_RESTARTED"

    print("PASS: scoped model/account routing, pending edits and final dispatch, daily quota, snapshots, history isolation and chat usage")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, mock_patch.dict(os.environ, {
        "MUSE2API_HOME": tmp, "MUSE2API_PROFILE_ROOT": tmp, "MUSE2API_KEY": "test-admin-key",
        "MUSE2API_CONCURRENCY": "1",
    }):
        asyncio.run(run())
