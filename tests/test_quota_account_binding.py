"""Offline regression for per-account and repeated quota page isolation."""
import importlib.util
import sys
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
from unittest.mock import patch

sys.modules["cdp"] = SimpleNamespace(CDP=object, CDPError=RuntimeError, http_json=lambda *args, **kwargs: {})
engine_path = (Path(sys.argv[1]) if len(sys.argv) > 1
               else Path(__file__).resolve().parents[1] / "engine.py")
spec = importlib.util.spec_from_file_location("candidate_engine", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

instance = engine.MuseEngine.__new__(engine.MuseEngine)
instance.page = None
instance.current_context_id = None
instance.current_acc_id = None
seen = []
closed = []
payloads = iter([
    """Usage
Free plan
Weekly limit resets on Oct 17
2% used
Additional tokens
Never expires
0% used (8B tokens left)""",
    """Mức sử dụng
Gói miễn phí
Hạn mức tuần đặt lại vào 17 tháng 10
Đã sử dụng 24%
Token bổ sung
Không hết hạn
0% đã dùng (còn 1 tỷ token)""",
    """Usage
Free plan
Weekly limit resets on Oct 17
2% used
Additional tokens
Never expires
0% used (8B tokens left)""",
])


class Page:
    def __init__(self, quota):
        self.quota = quota
        self.usage_open = not quota.startswith("Mức sử dụng")

    def js(self, script):
        if script == instance._QUOTA_DIALOG_JS:
            return self.quota if self.usage_open else "Cài đặt"
        if script == instance._QUOTA_USAGE_JS:
            return '{"x": 3, "y": 4}'
        return "{\"x\": 1, \"y\": 2}"

    def send(self, *args, **kwargs):
        pass


def ensure(cookies, expires, account_id=None):
    seen.append(account_id)
    assert instance.page is None, "quota reused a previous page"
    instance.page = Page(next(payloads))
    instance.current_acc_id = account_id
    return instance.page


def close():
    if instance.page is not None:
        closed.append(instance.current_acc_id)
    instance.page = None
    instance.current_acc_id = None


instance.ensure_page = ensure
instance._close_page = close
def click(x, y):
    if (x, y) == (3, 4):
        instance.page.usage_open = True


instance._click_point = click
with patch("time.sleep"):
    qa1 = instance.quota({"fixture": "a"}, account_id="account-a")
    qb = instance.quota({"fixture": "b"}, account_id="account-b")
    qa2 = instance.quota({"fixture": "a"}, account_id="account-a")
assert seen == ["account-a", "account-b", "account-a"]
assert closed == ["account-a", "account-b", "account-a"]
assert instance.page is None and instance.current_acc_id is None
assert qa1["weekly_used_pct"] == qa2["weekly_used_pct"] == 2
assert qa1["extra_left"] == qa2["extra_left"] == "8B tokens left"
assert qb["weekly_used_pct"] == 24 and qb["extra_left"] == "còn 1 tỷ token"
print("PASS: account-bound and repeated quota reads use disposable isolated pages")
