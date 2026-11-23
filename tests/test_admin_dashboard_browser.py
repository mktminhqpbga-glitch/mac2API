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
      $('clientHistoryDays').value = 7;
      const block = document.querySelector('[data-model="muse/image"]');
      block.querySelector('.client-grant').click();
      block.querySelector('.client-account').click();
      block.querySelector('.client-limit').value = '5';
      block.querySelector('[data-field=charge][data-unit=image]').value = '42';
      $('clientSave').click();
    })()""")
    wait(lambda: page.js("$('clientKeyResult').value.startsWith('m2c_')"))
    key = page.js("$('clientKeyResult').value")
    client = app.store.find_client(key)
    assert set(client["rules"]) == {"muse/image"}
    assert client["rules"]["muse/image"]["charge"]["image"] == 42
    assert client["history_days"] == 7
    page.js("$('closeClientSecret').click()")
    wait(lambda: page.js("!$('clientKeyResult').value && $('clientKeyList').textContent.includes('Giao diện thử')"))

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
    assert page.js("$('playImageModel').value === 'muse/image' && $('vidBtn').disabled && $('playTextSend').disabled")
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
