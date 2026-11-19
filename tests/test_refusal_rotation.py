"""A refusal or an uncertain delivery must not send on a second account."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("refusal_rotation_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    first = app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                                   "hatch_native_auth_device": "one"}, "one")
    app.store.add_account({"hatch_sess": "two", "hatch_vml": "two",
                           "hatch_native_auth_device": "two"}, "two")
    for error in (app.MuseModelRefusalError("模型未生成媒体，仅返回文本: 地区不可用"),
                  app.MusePostSendError("Không xác minh được Muse đã gửi yêu cầu.")):
        with patch.object(app, "_renew_and_persist", side_effect=lambda ident, **_: app.store.get_account(ident)), \
             patch.object(app.engine, "start"), patch.object(app.engine, "generate", side_effect=error) as generate, \
             patch.object(app.engine, "reset_thread"), patch.object(app.engine, "close_current_page"):
            try:
                app._run_generation_locked("video", "video", 30, account_id=first["id"],
                                           deadline=app.time.monotonic() + 30)
            except type(error) as actual:
                assert actual is error
            else:
                raise AssertionError("terminal error was not propagated")
        generate.assert_called_once()
        assert generate.call_args.args[0]["hatch_sess"] == "one"
print("PASS: model refusal and uncertain delivery do not automatically resubmit")
