"""Exercise key management, the official API settings and all dashboard layouts in Chromium."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cdp import CDP


def wait(predicate):
    end = time.monotonic() + 12
    while time.monotonic() < end:
        try:
            if value := predicate():
                return value
        except (httpx.HTTPError, StopIteration):
            pass
        time.sleep(0.05)
    raise AssertionError("Condition did not become true")


def check_dashboard(app, page, url):
    page.send("Page.enable")
    page.send("Page.addScriptToEvaluateOnNewDocument", {"source":
        "window.__errors=[];window.addEventListener('error',e=>__errors.push(e.message));window.confirm=()=>true;"})
    page.send("Page.navigate", {"url": url + "/admin"})
    wait(lambda: page.js("typeof goTab === 'function' && authOK === false"))
    page.js("$('key').value='ui-fixture-only';saveKey()")
    wait(lambda: page.js("$('overviewProviders').childElementCount === 5"))
    assert page.js("$('overviewBase').value.endsWith('/v1')")
    assert page.js("document.querySelector('nav [aria-current=page]').dataset.t === 'overview'")

    for width, height in ((1600, 1000), (390, 844), (320, 760)):
        page.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        if width < 760:
            page.js("$('mobileNav').value='clients';$('mobileNav').dispatchEvent(new Event('change'))")
            assert page.js("$('tab-clients').classList.contains('active')")
        for tab in ("overview", "clients", "usage", "pool", "openai", "gemini", "official",
                    "cookie", "tasks", "media", "play", "docs"):
            page.js(f"goTab('{tab}')")
            wait(lambda: page.js(f"$('tab-{tab}').classList.contains('active')"))
            assert page.js("document.documentElement.scrollWidth <= innerWidth + 1"), (width, tab)
    page.send("Emulation.setDeviceMetricsOverride",
              {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})

    def screenshot(name):
        output = os.getenv("MAC2API_DASHBOARD_SCREENSHOTS")
        if output:
            page.js("$('toasts').replaceChildren()")
            time.sleep(0.25)
            Path(output).mkdir(parents=True, exist_ok=True)
            response = page.send("Page.captureScreenshot", {"format": "png"})
            (Path(output) / (name + ".png")).write_bytes(base64.b64decode(response["result"]["data"]))

    page.js("goTab('official')")
    wait(lambda: page.js("$('officialPanels').childElementCount === 2"))
    page.js("""(() => {
      const form = $('officialPanels').firstElementChild;
      form.elements.model.value = 'fixture-text';
      form.elements.api_key.value = 'upstream-fixture-only';
      form.requestSubmit();
    })()""")
    wait(lambda: page.js("$('officialPanels').textContent.includes('openai/fixture-text')"))
    assert page.js("$('officialPanels').firstElementChild.elements.api_key.value === ''")
    page.js("$('officialPanels').querySelector('[data-official-test]').click()")
    wait(lambda: page.js("$('officialPanels').textContent.includes('Kết nối dùng được')"))
    screenshot("official")

    page.js("goTab('clients')")
    wait(lambda: page.js("!$('newClientKey').disabled"))
    page.js("""(() => {
      $('newClientKey').click();
      $('clientKeyLabel').value = 'Giao diện thử';
      const block = $('clientRules').querySelector('[data-provider="muse"]');
      block.querySelector('.client-provider-choice').click();
      $('clientSave').click();
    })()""")
    assert page.js("$('clientFormStatus').textContent.includes('chọn ít nhất một tài khoản')")
    assert page.js("!$('client-accounts-muse').hidden && $('client-accounts-chatgpt').hidden")
    assert page.js("$('clientSettingsFields').hidden && !$('clientRules').querySelector('[data-model]')")
    page.send("Emulation.setDeviceMetricsOverride",
              {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False})
    assert page.js("$('clientKeyDialog').scrollWidth <= $('clientKeyDialog').clientWidth + 1")
    screenshot("create-key-mobile")
    page.send("Emulation.setDeviceMetricsOverride",
              {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
    screenshot("create-key")
    page.js("$('client-accounts-muse').querySelector('.client-account').click();$('clientSave').click()")
    wait(lambda: page.js("$('clientKeyResult').value.startsWith('m2c_')"))
    key = page.js("$('clientKeyResult').value")
    client = app.store.find_client(key)
    assert set(client["rules"]) == {"muse/text", "muse/image", "muse/video"}
    assert all(rule["accounts"] == [app.store.accounts[0]["id"]] for rule in client["rules"].values())
    page.js("$('closeClientSecret').click()")
    wait(lambda: page.js("!$('clientKeyResult').value && $('clientKeyList').textContent.includes('Giao diện thử')"))

    page.js("""Array.from($('clientKeyList').querySelectorAll('button')).find(b=>b.textContent==='Cài đặt khóa').click()""")
    page.js("""(() => {
      $('clientHistoryDays').value = 7;
      const block = $('clientModelSettings').querySelector('[data-model="muse/image"]');
      block.open = true;
      block.querySelector('.client-limit').value = '5';
      block.querySelector('[data-field=charge][data-unit=image]').value = '42';
      $('clientSave').click();
    })()""")
    wait(lambda: page.js("!$('clientKeyDialog').open"))
    client = app.store.find_client(key)
    assert client["rules"]["muse/image"]["charge"]["image"] == 42
    assert client["history_days"] == 7

    job = app.client_usage.begin("use_ui_fixture", client, "muse/image", "image", "/muse/v1/images/tasks",
                                 prompt="Ảnh từ giao diện")
    job.record("started")
    account = app.store.accounts[0]
    job.record("attempt", account["id"], account["label"])
    job.record("finish", "completed", usage={"images": 1}, output="Kết quả mô phỏng")

    page.js("""$('clientSearch').value='Giao dien';$('clientSearch').dispatchEvent(new Event('input'))""")
    assert page.js("$('clientKeyList').textContent.includes('Giao diện thử')")
    page.js("""Array.from($('clientKeyList').querySelectorAll('button')).find(b=>b.textContent==='Chỉnh sửa').click()""")
    assert page.js("$('clientKeyDialog').open && $('clientKeyLabel').value === 'Giao diện thử'")
    page.js("$('clientKeyLabel').value='Đã sửa khóa';$('clientSave').click()")
    wait(lambda: page.js("!$('clientKeyDialog').open"))
    wait(lambda: page.js("$('clientKeyList').textContent.includes('Không có khóa khớp')"))
    page.js("$('clientSearch').value='';$('clientSearch').dispatchEvent(new Event('input'))")
    wait(lambda: page.js("$('clientKeyList').textContent.includes('Đã sửa khóa')"))
    screenshot("keys")
    page.js("""Array.from($('clientKeyList').querySelectorAll('button')).find(b=>b.textContent==='Xem lịch sử').click()""")
    wait(lambda: page.js("$('tab-usage').classList.contains('active') && $('usageList').textContent.includes('42')"))
    page.js("$('usageList').querySelector('button').click()")
    wait(lambda: page.js("$('usageDialog').open"))
    assert page.js("$('usageDialogTitle').textContent === 'Xem kết quả'")
    assert page.js("$('usageDetails').textContent.includes('Kết quả mô phỏng')")
    assert page.js("$('usageDetails').textContent.includes('Ảnh từ giao diện')")
    page.js("$('usageDialog').close()")

    page.js("""goTab('play');$('playAuthMode').value='client';$('playAuthMode').dispatchEvent(new Event('change'));
      $('playKey').value='m2c_invalid';$('playDiscover').click()""")
    wait(lambda: page.js("$('playStatus').dataset.error === 'true'"))
    assert page.js("authOK === true && $('imgBtn').disabled")
    page.js(f"$('playKey').value={json.dumps(key)};$('playDiscover').click()")
    wait(lambda: page.js("!$('imgBtn').disabled"))
    assert page.js("$('playImageModel').value === 'muse/image' && !$('vidBtn').disabled && !$('playTextSend').disabled")
    page.js("goTab('overview')")
    wait(lambda: page.js("$('overviewKeys').textContent === '1' && $('overviewRequests').textContent === '1'"))
    assert page.js("$('playKey').value === ''")
    wait(lambda: page.js("$('overviewUsers').textContent.includes('1/5')"))
    assert page.js("$('overviewAccounts').textContent.includes('Muse trình duyệt')")
    assert page.js("$('overviewByKind').textContent.includes('1 ảnh')")
    page.js("$('overviewDays').value='7';$('overviewDays').dispatchEvent(new Event('change'))")
    wait(lambda: page.js("overviewReport.days === 7"))
    page.js("$('overviewRecent').querySelector('button').click()")
    wait(lambda: page.js("$('usageDialog').open && $('usageDetails').textContent.includes('Kết quả mô phỏng')"))
    page.js("$('usageDialog').close()")
    page.js("$('overviewUsers').querySelector('button').click()")
    wait(lambda: page.js("$('usageOwner').value && $('usageList').textContent.includes('42')"))
    page.js("goTab('overview')")
    screenshot("overview")
    page.send("Emulation.setDeviceMetricsOverride",
              {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False})
    screenshot("mobile")
    assert not page.js("window.__errors"), page.js("window.__errors")
    assert httpx.get(url + "/v1/usage", headers={"Authorization": "Bearer " + key}).status_code == 200

    old_account = app.store.accounts[0]["id"]
    other = app.store.add_account({"hatch_sess": "second-fixture-cookie"}, "Tài khoản thứ hai")["id"]
    original = {"muse/text": {"accounts": [old_account]},
                "muse/image": {"accounts": [other], "daily_limit": 3, "charge": {"image": 20}}}
    headers = {"Authorization": "Bearer ui-fixture-only"}
    old = httpx.post(url + "/admin/client-keys", headers=headers,
                     json={"label": "Khóa cũ", "rules": original})
    assert old.status_code == 201, old.text
    old = old.json()
    page.js("goTab('clients')")
    wait(lambda: page.js("$('clientKeyList').textContent.includes('Khóa cũ')"))
    open_old = """Array.from($('clientKeyList').rows).find(row=>row.cells[0].textContent.startsWith('Khóa cũ'))
        .querySelector('button').click()"""
    page.js(open_old)
    assert page.js("$('clientRules').querySelector('[data-provider=muse] .client-provider-choice').indeterminate")
    page.js("$('clientKeyLabel').value='Khóa cũ đã đổi tên';$('clientSave').click()")
    wait(lambda: page.js("!$('clientKeyDialog').open && $('clientKeyList').textContent.includes('Khóa cũ đã đổi tên')"))
    assert app.store.get_client(old["id"])["rules"] == old["rules"]
    page.js(open_old)
    page.js("$('clientRules').querySelector('[data-provider=muse] .client-provider-choice').click();$('clientSave').click()")
    wait(lambda: page.js("!$('clientKeyDialog').open"))
    expanded = app.store.get_client(old["id"])["rules"]
    assert expanded["muse/text"] == old["rules"]["muse/text"]
    assert expanded["muse/image"] == old["rules"]["muse/image"]
    assert set(expanded["muse/video"]["accounts"]) == {old_account, other}
    wait(lambda: page.js("clientKeys.find(key=>key.label==='Khóa cũ đã đổi tên')?.rules['muse/video'] !== undefined"))
    page.js(open_old)
    concurrent = httpx.patch(url + "/admin/client-keys/" + old["id"], headers=headers,
                             json={"rules": original})
    assert concurrent.status_code == 200, concurrent.text
    page.js("$('clientKeyLabel').value='Lưu từ cửa sổ cũ';$('clientSave').click()")
    wait(lambda: page.js("$('clientFormStatus').textContent.includes('Khóa đã được cập nhật')"))
    assert app.store.get_client(old["id"])["rules"] == old["rules"]
    page.js("$('clientDismiss').click()")

    queued = app.store.create_task("image", "Ảnh còn chờ")
    page.js("goTab('tasks')")
    wait(lambda: page.js("Boolean($('taskBody').querySelector('[data-task-action=cancel]'))"))
    page.js("$('taskBody').querySelector('[data-task-action=cancel]').click()")
    wait(lambda: app.store.get_task(queued["id"])["status"] == "cancelled")
    wait(lambda: page.js("$('taskBody').textContent.includes('Đã hủy')"))

    async def workflow_fixture(run, item, _step, _checkpoint):
        return {"kind": "text", "text": 'Kết quả ' + run["items"][item]["input"]}

    with patch.object(app, "_execute_workflow_step", side_effect=workflow_fixture):
        page.js("goTab('workflows')")
        wait(lambda: page.js("$('workflowSteps').childElementCount === 1"))
        page.js("""$('workflowInput').value='[một] [hai]';$('workflowInput').dispatchEvent(new Event('input'));
            $('workflowStart').click()""")
        wait(lambda: page.js("$('workflowResults').textContent.includes('Kết quả hai')"))
        assert page.js("$('workflowProgress').value === 2 && $('workflowItems').children.length === 2")
        screenshot("workflow-mobile")
        page.send("Emulation.setDeviceMetricsOverride",
                  {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        assert page.js("$('tab-workflows').closest('.workspace-content') && $('workflowForm').clientWidth > 350")
        screenshot("workflow")
        opened_at = page.js("performance.timeOrigin")
        page.send("Page.reload")
        wait(lambda: page.js(f"performance.timeOrigin !== {opened_at} && "
                             "document.getElementById('workflowResults')?.textContent.includes('Kết quả hai') && "
                             "document.getElementById('workflowInput').value === '[một] [hai]'"))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
        "MUSE2API_HOME": root, "MUSE2API_PROFILE_ROOT": root, "MUSE2API_KEY": "ui-fixture-only",
        "OPENAI_API_KEY": "", "GEMINI_API_KEY": "",
    }):
        import app

        app.store.add_account({"hatch_sess": "ui-fixture-cookie"}, "Muse trình duyệt")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app.app, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        chrome_path = Path(root) / "browser"
        chrome = shutil.which("chromium") or shutil.which("google-chrome")
        assert chrome, "Chromium is required for this check"
        with patch.object(app.chat_providers, "test", new=AsyncMock(return_value={"ok": True})), \
             patch.object(app, "_check_update_sync", return_value={"local_version": "fixture"}):
            thread.start()
            process = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                                        "--remote-allow-origins=*", "--remote-debugging-port=0",
                                        f"--user-data-dir={chrome_path}", "about:blank"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            page = None
            try:
                wait(lambda: server.started)
                port_file = chrome_path / "DevToolsActivePort"
                wait(port_file.exists)
                port = port_file.read_text().splitlines()[0]
                target = wait(lambda: next(item for item in httpx.get(f"http://127.0.0.1:{port}/json/list").json()
                                           if item["type"] == "page"))
                page = CDP(target["webSocketDebuggerUrl"])
                check_dashboard(app, page, url)
                print("PASS: all dashboard sections, API key creation and editing, scoped test access and official settings")
            except Exception:
                if page:
                    print(page.js("[location.href, typeof goTab, typeof authOK, typeof authOK !== 'undefined' && authOK, window.__errors, document.body.innerText.slice(0,450)]"))
                raise
            finally:
                if page:
                    try:
                        page.send("Browser.close")
                    except Exception:
                        pass
                    finally:
                        page.close()
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
                server.should_exit = True
                thread.join(timeout=7)
