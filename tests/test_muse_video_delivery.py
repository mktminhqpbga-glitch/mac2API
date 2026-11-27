"""Offline regressions for MUSE page readiness and video delivery waits."""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("muse_engine_candidate", ROOT / "engine.py")
engine_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine_module)

expression = engine_module.MuseEngine._COMPOSER_READY_JS

for name, state, host, connecting, hydrated, disabled, expected in (
        ("empty_connected", "complete", "muse.ai", False, True, False, "ready"),
        ("slow_subresources", "interactive", "muse.ai", False, True, False, "ready"),
        ("unparsed_dom", "loading", "muse.ai", False, True, False, "loading"),
        ("foreign_page", "complete", "example.test", False, True, False, "wrong-site"),
        ("connecting", "complete", "muse.ai", True, True, False, "connecting"),
        ("hydrating", "complete", "muse.ai", False, False, False, "hydrating"),
        ("disabled_input", "complete", "muse.ai", False, True, True, "input-disabled")):
    script = """const c=%s;
const ta={matches:s=>s==='textarea'||(s===':disabled'&&c.disabled),getClientRects:()=>[{}],closest:()=>null};
const location={hostname:c.host};const getComputedStyle=()=>({visibility:'visible'});
const document={readyState:c.state,body:{innerText:c.connecting?'Connecting...':''},querySelectorAll:()=>[ta],
    querySelector:s=>s==='textarea'?ta:s==='[data-hatch-shell-hydration-state]'?{getAttribute:()=>c.hydrated?'hydrated':'server'}:null};
console.log(%s);""" % (
        json.dumps(dict(state=state, host=host, connecting=connecting, hydrated=hydrated, disabled=disabled)), expression)
    result = subprocess.check_output(["node", "-e", script], text=True).strip()
    assert result == expected, (name, result, expected)

page = Mock()
page.send.side_effect = [{"result": {"errorText": "net::ERR_CONNECTION_RESET"}}, {"result": {"frameId": "ok"}}]
assert engine_module.MuseEngine._check_muse_page(page, 10, {"server": "http://proxy.test"}) > 0
first, second = page.send.call_args_list
assert first.args == second.args == ("Page.navigate", {"url": "https://muse.ai/thread/new"})
assert 0 < second.kwargs["timeout"] <= first.kwargs["timeout"] <= 10
page.send.reset_mock(side_effect=True)
page.send.return_value = {"result": {"errorText": "net::ERR_TUNNEL_CONNECTION_FAILED"}}
try:
    engine_module.MuseEngine._check_muse_page(page, 10, {"server": "http://proxy.test"})
except engine_module.MuseProxyError:
    page.send.assert_called_once()
else:
    raise AssertionError("proxy navigation failure was not returned immediately")

with tempfile.TemporaryDirectory() as profile:
    engine = engine_module.MuseEngine(SimpleNamespace(profile_dir=profile, login_wait=2))
    clock = [0.0]
    engine_module.time = SimpleNamespace(time=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n))
    engine.page = SimpleNamespace(js=lambda _: json.dumps({
        "cnt": 0, "txt": "", "stop": True, "tail": "Still sending"}))
    engine.attachments = lambda: []
    try:
        engine._wait_attachment("", 60, "video")
    except engine_module.MuseGenerationError as exc:
        assert "chưa xác nhận đã nhận yêu cầu" in str(exc)
        assert clock[0] < 32, clock[0]
    else:
        raise AssertionError("undelivered video request waited for the full timeout")

print("PASS: composer readiness and undelivered video fast-fail")

for body, composer, second_ready, expected_load, error_type in (
    ("", False, True, True, None),
    ("", False, False, True, engine_module.MusePageUnavailable),
    ("", True, True, True, None),
    ("", True, False, True, engine_module.MusePageUnavailable),
    ("Đang kết nối…", True, True, False, None),
    ("Connecting...", True, False, False, engine_module.MusePageUnavailable),
    ("Proxy authentication required", True, False, False, engine_module.MuseProxyError),
):
    clock = SimpleNamespace(now=0.0)
    engine = engine_module.MuseEngine(SimpleNamespace(profile_dir=tempfile.gettempdir()))
    proxy = {"server": "http://fixture-proxy.test"}
    page = Mock()
    page.js.return_value = {"hostname": "muse.ai", "path": "/thread/new",
                            "readyState": "interactive", "body": body, "hasComposer": composer}
    page.send.return_value = {"result": {"frameId": "fixture"}}
    budgets = []

    def wait_ready(page, *, timeout):
        budgets.append(timeout)
        if len(budgets) == 2 and second_ready:
            return True
        clock.now += timeout
        return False

    with patch.object(engine_module, "time", SimpleNamespace(monotonic=lambda: clock.now)), \
         patch.object(engine, "_wait_ws_ready", side_effect=wait_ready):
        try:
            engine._wait_muse_composer(page, 60, proxy)
        except engine_module.MuseGenerationError as exc:
            assert error_type is type(exc), (body, exc)
            if expected_load:
                assert "Đã tải lại trang một lần trong cùng phiên" in str(exc)
        else:
            assert error_type is None, body
    assert budgets[0] == 40 and sum(budgets) <= 60, budgets
    assert page.send.call_count == int(expected_load), (body, page.send.call_args_list)
    if expected_load:
        assert page.send.call_args.args == ("Page.navigate", {"url": "https://muse.ai/thread/new"})
        assert 0 < page.send.call_args.kwargs["timeout"] <= 20

print("PASS: a missing or locked composer retries within its budget, with no reload for workspace/proxy failures")


class FakeClock:
    def __init__(self):
        self.now = 0.0
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def wait_case(states, timeout=600, expect="video"):
    clock = FakeClock()
    calls = {"n": 0}
    def js(_script):
        index = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        state = dict(states[index])
        state.setdefault("cnt", 0)
        state.setdefault("txt", "")
        state.setdefault("stop", False)
        state.setdefault("tail", "")
        state.setdefault("path", "/thread/new")
        return json.dumps(state)
    page = SimpleNamespace(js=js)
    e = engine_module.MuseEngine(SimpleNamespace(profile_dir=tempfile.gettempdir()))
    e.page = page
    e.attachments = lambda: []
    e._scroll_bottom = lambda: None
    with patch.object(engine_module, "time", clock):
        return e._wait_attachment("", timeout, expect)


refusal = "视频没生成成功：你这个账号所在的国家/地区目前不支持视频生成，这是持续限制。"
try:
    wait_case([
        {"stop": True, "tail": "正在处理", "path": "/thread/new"},
        {"stop": False, "tail": refusal, "path": "/thread/new"},
    ])
except engine_module.MuseModelRefusalError as exc:
    assert refusal in str(exc), str(exc)
else:
    raise AssertionError("explicit region refusal was not returned promptly")

# Generic page chrome containing region/video words is not sufficient evidence.
assert engine_module.MuseEngine._media_refusal_text(
    "Settings\\nRegion: US\\nVideo", "video") == ""

# A completed submission that remains idle on /thread/new stops before 600s,
# but only after the Stop lifecycle and a long quiet period.
try:
    wait_case([
        {"stop": True, "tail": "", "path": "/thread/new"},
        {"stop": False, "tail": "", "path": "/thread/new"},
    ])
except engine_module.MuseGenerationError as exc:
    assert "未建立视频任务线程" in str(exc), str(exc)
else:
    raise AssertionError("idle /thread/new task waited for full timeout")

# No observed submission lifecycle must not trigger the early no-thread error.
assert wait_case([{"stop": False, "tail": "", "path": "/thread/new"}], timeout=20) is None
print("PASS: explicit refusal propagated; idle no-thread stopped; normal wait preserved")
