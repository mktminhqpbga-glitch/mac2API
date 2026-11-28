"""Exercise key management, dashboard layouts and workflow recovery in Chromium."""
import asyncio
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
            value = predicate()
            missing_value = isinstance(value, dict) and (
                "exceptionDetails" in value or value.get("result", {}).get("type") == "undefined")
            if value and not missing_value:
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
    assert page.js("!$('overviewBase').getClientRects().length")
    page.js("$('overviewBase').closest('details').querySelector('summary').click()")
    assert page.js("Boolean($('overviewBase').getClientRects().length)")
    page.js("$('overviewBase').closest('details').querySelector('summary').click()")
    assert page.js("document.querySelector('nav [aria-current=page]').dataset.t === 'overview'")
    page.js("""localStorage.setItem('mac2apiWorkflow:draft', JSON.stringify({
        input: 'Bản nháp còn dùng được', title: 123, steps: [null, {kind: 'video', name: 'Bước cũ', duration: 10}]
    }))""")

    for width, height in ((1600, 1000), (390, 844), (320, 760)):
        page.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        if width < 760:
            page.js("$('mobileNav').value='clients';$('mobileNav').dispatchEvent(new Event('change'))")
            assert page.js("$('tab-clients').classList.contains('active')")
        for tab in ("overview", "clients", "usage", "pool", "openai", "gemini", "official",
                    "tasks", "media", "workflows", "play", "docs"):
            page.js(f"goTab('{tab}')")
            wait(lambda: page.js(f"$('tab-{tab}').classList.contains('active')"))
            assert page.js("document.documentElement.scrollWidth <= innerWidth + 1"), (width, tab)
    wait(lambda: page.js("$('workflowSteps').querySelectorAll('.workflow-step').length === 1"))
    assert page.js("""$('workflowInput').value === 'Bản nháp còn dùng được'
        && $('workflowTitle').value === '' && $('workflowSteps').querySelector('input[type=number]').value === '10'""")
    page.js("location.hash = 'cookie'")
    wait(lambda: page.js("$('tab-pool').classList.contains('active') && $('museExtension').open"))
    assert page.js("Boolean($('museExtension').querySelector('a[href=\"/admin/extension\"]').getClientRects().length)")
    page.send("Emulation.setDeviceMetricsOverride",
              {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})

    scoped_tabs = (("pool", "muse", "/muse/v1"), ("openai", "chatgpt", "/chatgpt/v1"),
                   ("gemini", "gemini-web", "/gemini/v1"))
    with patch.object(app.CFG, "public_base", "https://client-api.example.invalid"):
        page.js("loadStatus()", await_promise=True)
        for _, provider, path in scoped_tabs:
            assert page.js(f'document.querySelector("[data-output-provider={provider}]").value') == (
                "https://client-api.example.invalid" + path)
    page.js("loadStatus()", await_promise=True)
    for tab, provider, path in scoped_tabs:
        page.js(f"goTab('{tab}');$('tab-{tab}').querySelector('[data-new-provider-key]').click()")
        wait(lambda: page.js("$('clientKeyDialog').open"))
        assert page.js(f"""(() => {{
          const options = [...$('clientRules').querySelectorAll('[data-provider]')];
          const choice = options[0]?.querySelector('.client-provider-choice');
          return options.length === 1 && options[0].dataset.provider === '{provider}'
            && choice.checked && choice.disabled
            && $('tab-{tab}').querySelector('[data-public-url]').value.endsWith('{path}');
        }})()""")
        page.js("$('clientDismiss').click()")
        wait(lambda: page.js("!$('clientKeyDialog').open"))

    for tab, provider in (("openai", "chatgpt"), ("gemini", "gemini-web")):
        aid = app.chrome_bridge.pair(provider, "c" * 32, "Hồ sơ thử proxy").split(".")[0]
        page.js(f"goTab('{tab}')")
        panel = f"$('tab-{tab}')"
        wait(lambda: page.js(f"""Boolean({panel}.querySelector('[data-web-extension] a[download]').getClientRects().length)"""))
        wait(lambda: page.js(f"{panel}.querySelector('[data-web-extension-accounts]').textContent.includes('Hồ sơ thử proxy')"))
        page.js(f"""[...{panel}.querySelectorAll('[data-web-extension-accounts] button')]
            .find(button => button.textContent === 'Proxy').click()""")
        assert page.js("$('proxyDialog').open && $('proxyMode').value === 'proxy' && $('proxyMode').disabled")
        page.js("""$('proxyServer').value = 'http://127.0.0.1:9876';
            $('proxyServer').dispatchEvent(new Event('input', {bubbles: true}));""")
        with patch.object(app, "_proxy_exit_ip", return_value="192.0.2.10"):
            page.js("$('proxyTest').click()")
            wait(lambda: page.js("$('proxyResult').textContent.includes('192.0.2.10')"))
        page.js("$('proxySave').click()")
        wait(lambda: page.js(f"!$('proxyDialog').open && {panel}.querySelector('[data-web-accounts]').textContent.includes('127.0.0.1:9876')"))
        assert app.managed_web.has_account(aid, provider)

    aid = app.chrome_bridge.pair("gemini-web", "d" * 32, "Hồ sơ thử ngắt").split(".")[0]
    page.js("loadChatProvider('gemini')", await_promise=True)
    page.js("""[...$('tab-gemini').querySelectorAll('[data-web-extension-accounts] button')]
        .find(button => button.textContent === 'Ngắt liên kết').click()""")
    wait(lambda: page.js("$('tab-gemini').querySelector('[data-web-accounts-status]').textContent.includes('Đã ngắt liên kết')"))
    assert not any(item["id"] == aid for item in app.chrome_bridge.list_accounts())

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
    page.js("$('clientDismiss').click();goTab('pool');$('museOutput').querySelector('[data-new-provider-key]').click()")
    wait(lambda: page.js("$('clientKeyDialog').open && $('clientRules').querySelectorAll('[data-provider]').length === 1"))
    page.js("""$('clientKeyLabel').value = 'Giao diện thử';
      $('client-accounts-muse').querySelector('.client-account').click();$('clientSave').click()""")
    wait(lambda: page.js("$('clientKeyResult').value.startsWith('m2c_')"))
    key = page.js("$('clientKeyResult').value")
    client = app.store.find_client(key)
    assert client["providers"] == ["muse"]
    assert set(client["rules"]) == {"muse/text", "muse/image", "muse/video"}
    assert all(rule["accounts"] == [app.store.accounts[0]["id"]] for rule in client["rules"].values())
    assert page.js("!$('clientScopedOutput').hidden && $('clientScopedUrl').value") == url + "/muse/v1"
    headers = {"Authorization": "Bearer " + key}
    models = httpx.get(url + "/muse/v1/models", headers=headers)
    assert models.status_code == 200
    catalog = models.json()["data"]
    assert {item["id"] for item in catalog} >= set(client["rules"])
    assert all(item["owned_by"] == "muse" for item in catalog)
    for scope in ("chatgpt", "gemini"):
        assert httpx.get(url + f"/{scope}/v1/models", headers=headers).status_code == 403
    assert httpx.post(url + "/v1/chat/completions", headers=headers,
                      json={"model": "chatgpt/text", "prompt": "Denied"}).status_code == 403
    page.js("$('closeClientSecret').click()")
    page.js("goTab('clients')")
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

    release = threading.Event()
    calls = []

    async def workflow_fixture(run, item, step, _checkpoint):
        calls.append((item, step))
        if (item, step) == (0, 0):
            while not release.is_set():
                await asyncio.sleep(0.05)
        if (item, step) == (0, 1) and calls.count((0, 1)) == 1:
            raise app.WorkflowStepError("Hết hạn mức, vui lòng kiểm tra tài khoản.")
        return {"kind": "text", "text": 'Kết quả ' + run["items"][item]["input"] + f' Bước {step + 1}'}

    with patch.object(app, "_execute_workflow_step", side_effect=workflow_fixture):
        page.js("goTab('workflows')")
        wait(lambda: page.js("$('workflowSteps').childElementCount === 1"))
        page.js("""$('workflowInput').value='[chưa đóng';$('workflowInput').dispatchEvent(new Event('input'));
            $('workflowStart').click()""")
        wait(lambda: page.js("$('workflowDraftStatus').textContent.includes('chưa đóng')"))
        assert not calls
        page.js("""$('workflowInput').value='[một] [hai]';$('workflowInput').dispatchEvent(new Event('input'));
            $('workflowAdd').click();
            $('workflowStart').click()""")
        wait(lambda: calls == [(0, 0)])
        wait(lambda: page.js("$('workflowRunState').textContent === 'Đang chạy'"))
        page.js("""window.__realFetch=window.fetch;
            window.fetch=(url,opts)=>String(url).startsWith('/admin/workflows/')
              ? Promise.reject(new TypeError('offline')) : __realFetch(url,opts)""")
        wait(lambda: page.js("$('workflowConnection').textContent.includes('Tạm mất kết nối')"))
        page.js("window.fetch=__realFetch;window.dispatchEvent(new Event('online'))")
        wait(lambda: page.js("$('workflowConnection').textContent === ''"))
        page.js("$('workflowPause').click()")
        wait(lambda: page.js("$('workflowRunState').textContent === 'Đang tạm dừng'"))
        release.set()
        wait(lambda: page.js("$('workflowRunState').textContent === 'Đã tạm dừng'"))
        assert calls == [(0, 0)]
        page.js("$('workflowResume').click()")
        wait(lambda: page.js("$('workflowRunState').textContent === 'Hoàn tất một phần'"))
        assert page.js("$('workflowItems').innerText.includes('Hết hạn mức')")
        assert page.js("$('workflowResults').textContent.includes('Kết quả hai Bước 2')")
        page.js("$('workflowItems').querySelector('button').click()")
        wait(lambda: page.js("$('workflowProgress').value === 4 && $('workflowItems').children.length === 2"))
        assert calls == [(0, 0), (0, 1), (1, 0), (1, 1), (0, 1)]
        page.js("""Object.defineProperty(navigator,'clipboard',{value:{writeText: async text=>window.__copied=text}});
            $('workflowResults').querySelector('button').click()""")
        wait(lambda: page.js("window.__copied === 'Kết quả một Bước 1'"))
        download = Path(app.CFG.data_dir) / "browser-downloads"
        page.send("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download)})
        page.js("$('workflowResults').querySelectorAll('button')[1].click()")
        saved = download / "mac2api-muc-1-buoc-1.txt"
        wait(saved.exists)
        assert saved.read_text() == "Kết quả một Bước 1"
        screenshot("workflow-mobile")
        page.send("Emulation.setDeviceMetricsOverride",
                  {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        assert page.js("$('tab-workflows').closest('.workspace-content') && $('workflowForm').clientWidth > 350")
        screenshot("workflow")
        page.js("window.__workflowBeforeReload = true")
        page.send("Page.reload")
        wait(lambda: page.js("window.__workflowBeforeReload === undefined && "
                             "typeof document.getElementById('workflowForm')?.onsubmit === 'function' && "
                             "document.getElementById('workflowResults')?.textContent.includes('Kết quả hai') && "
                             "document.getElementById('workflowInput').value === '[một] [hai]'"))

        page.js("""window.__realFetch=window.fetch;
            window.fetch=async (url,options)=>{
              const response=await __realFetch(url,options);
              if (url==='/admin/workflows' && options.method==='POST') throw new TypeError('offline');
              return response;
            };
            $('workflowInput').value='mục đã gửi';$('workflowInput').dispatchEvent(new Event('input'));
            $('workflowStart').click()""")
        wait(lambda: page.js("$('workflowDraftStatus').textContent.includes('Mất kết nối')"))
        assert page.js("Boolean(JSON.parse(localStorage.getItem('mac2apiWorkflow:pending')).id)")
        page.js("window.__workflowBeforeReload = true")
        page.send("Page.reload")
        wait(lambda: page.js("window.__workflowBeforeReload === undefined && "
                             "typeof document.getElementById('workflowForm')?.onsubmit === 'function' && "
                             "document.getElementById('workflowResults')?.textContent.includes('Kết quả mục đã gửi') && "
                             "localStorage.getItem('mac2apiWorkflow:pending') === 'null'"))
        assert calls.count((0, 0)) == 2
        cleared = page.js("(() => {$('key').value=''; saveKey(); return KEY === '' && authOK === false;})()")
        assert cleared is True, cleared
        wait(lambda: page.js("!$('workflowResults').querySelector('article') && $('workflowHistory').hidden"))
        assert page.js("$('workflowInput').value === 'mục đã gửi' && window.__errors.length === 0")
        assert page.js("![...document.querySelectorAll('[data-web-accounts], [data-web-extension-accounts]')].some(list => list.children.length)")

        page.send("Page.addScriptToEvaluateOnNewDocument", {"source": """
            if (location.search.includes('chan-luu')) {
              for (const name of ['localStorage','sessionStorage']) {
                Object.defineProperty(window,name,{get(){throw new DOMException('Restricted','SecurityError')}});
              }
            }
        """})
        page.send("Page.navigate", {"url": url + "/admin?chan-luu"})
        wait(lambda: page.js("location.search.includes('chan-luu') && typeof goTab === 'function' && authOK === false"))
        page.js("$('key').value='ui-fixture-only';saveKey()")
        wait(lambda: page.js("$('overviewProviders').childElementCount === 5"))
        assert page.js("window.__errors.length === 0")


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
                port = wait(lambda: port_file.exists() and next(iter(port_file.read_text().splitlines()), None))
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
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                server.should_exit = True
                thread.join(timeout=7)
