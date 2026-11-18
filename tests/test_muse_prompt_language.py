"""Verify saved Muse prompt settings through the authenticated API."""
import asyncio
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run():
    import app

    original = "Một chú mèo trên bãi cỏ"
    assert app.store.muse_prompt_language() == "vi"
    video = app.VideoRequest(prompt=original, size="9:16", duration=5)
    prompt = app.build_video_prompt(video, language="vi")
    assert all(text in prompt for text in (original, "9:16", "720x1280", "5 giây", "tiếng Việt"))
    assert not re.search(r"[\u4e00-\u9fff]", prompt)

    mixed = app.VideoRequest(prompt="[图1] dùng cùng video", images=["a"], videos=["b"])
    prompt = app.build_video_prompt(mixed, ["image", "video"], language="en")
    assert "[图1] is attachment 1 (image)" in prompt and "Attachment 2: video." in prompt
    assert "opening frame" not in prompt
    assert mixed.prompt in prompt
    image = app.ImageRequest(prompt=original, aspect_ratio="4/3", image="ref")
    assert "4:3" in app.build_image_prompt(image, language="vi")
    assert "watermarks" in app.build_image_prompt(image, language="en")
    messages = [app.ChatMessage(role="user", content=original)]
    assert app.build_chat_prompt(messages, language="vi") == original
    messages.insert(0, app.ChatMessage(role="system", content="Hãy trả lời ngắn"))
    assert "Người dùng:" in app.build_chat_prompt(messages, language="vi")
    assert "User:" in app.build_chat_prompt(messages, language="en")
    assert "Tham số:" in app.build_tools_prompt([{"name": "lookup"}], language="vi")

    headers = {"Authorization": "Bearer test-key"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                 base_url="http://test") as client:
        assert (await client.put("/admin/muse-prompt", json={"language": "en"})).status_code == 401
        assert (await client.put("/admin/muse-prompt", headers=headers,
                                 json={"language": "invalid"})).status_code == 422
        for language, expected in [("vi", "tiếng Việt"), ("en", "English"), ("zh", "全新文生图创作")]:
            response = await client.put("/admin/muse-prompt", headers=headers, json={"language": language})
            assert response.status_code == 200
            assert app.Store(app.CFG).muse_prompt_language() == language
            with patch.object(app, "_queue_image", return_value={"ok": True}) as queue:
                response = await client.post("/v1/images/tasks", headers=headers, json={"prompt": original})
            assert response.status_code == 200
            assert expected in queue.call_args.args[1] and original in queue.call_args.args[1]
        assert (await client.get("/admin/muse-prompt", headers=headers)).json() == {"language": "zh"}
    print("PASS: default Vietnamese, three persisted languages, authenticated settings, media and chat prompts")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        "MUSE2API_HOME": tmp, "MUSE2API_PROFILE_ROOT": tmp, "MUSE2API_KEY": "test-key",
    }):
        asyncio.run(run())
