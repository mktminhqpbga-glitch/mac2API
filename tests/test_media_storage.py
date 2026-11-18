"""Verify oldest-first eviction and newest-first listing in an isolated media library."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run():
    import app
    import media_storage as storage
    from thumbnails import _prefix

    directory = Path(app.CFG.media_dir)
    directory.mkdir(parents=True, exist_ok=True)
    thumbnail = directory.parent / "thumbnails" / f"{_prefix('old.mp4')}-test.jpg"
    thumbnail.parent.mkdir()
    thumbnail.write_bytes(b"preview")
    archive = {"old.mp4": (20, 1_000_000_001), "recent.png": (40, 1_000_000_002)}
    for name, (size, date) in archive.items():
        path = directory / name
        path.write_bytes(b"x" * size)
        os.utime(path, ns=(date, date))
    (directory / ".gitkeep").touch()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                 base_url="http://test") as client:
        headers = {"Authorization": "Bearer test-key"}
        response = await client.get("/admin/media", headers=headers)
        assert [item["name"] for item in response.json()["media"]] == ["recent.png", "old.mp4"]
        assert response.json()["capacity_bytes"] == 5_000_000_000
        assert response.json()["cleanup_bytes"] == 3_500_000_000

        source = directory.parent / "download.mp4"
        source.write_bytes(b"x" * 10)
        with patch.object(storage, "CAPACITY_BYTES", 100), patch.object(storage, "CLEANUP_BYTES", 70):
            output, size = storage.save_media(directory, "new.mp4", source=source)
            assert size == 10 and Path(output).read_bytes() == source.read_bytes()
            assert not thumbnail.exists() and not (directory / "old.mp4").exists()
            assert (directory / "recent.png").is_file()
            assert (directory / ".gitkeep").is_file()

            try:
                storage.save_media(directory, "oversized.mp4", data=b"x" * 101)
            except ValueError:
                pass
            else:
                raise AssertionError("Oversized file admitted")
            assert Path(output).is_file()

            result = await client.get("/admin/media", headers=headers)
            assert [item["name"] for item in result.json()["media"]] == ["new.mp4", "recent.png"]
            assert result.json()["used_bytes"] == 50
            assert (await client.get("/v1/media/old.mp4")).status_code == 404

            storage.save_media(directory, "large.mp4", data=b"x" * 100)
            storage.prune_media(directory)
            assert [path.name for path, _ in storage.media_entries(directory)] == ["large.mp4"]
            storage.save_media(directory, "new.png", data=b"x")
            assert [path.name for path, _ in storage.media_entries(directory)] == ["new.png"]
            (directory / "externally-added.mp4").write_bytes(b"x" * 101)
            storage.prune_media(directory)
            assert not storage.media_entries(directory)
    print("PASS: exact trigger, oldest-first eviction, newest-first API, hard cap and thumbnail cleanup")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        "MUSE2API_HOME": tmp, "MUSE2API_PROFILE_ROOT": tmp, "MUSE2API_KEY": "test-key",
    }):
        asyncio.run(run())
