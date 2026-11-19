"""Retention policy for generated media."""
from contextvars import ContextVar
import logging
import os
from pathlib import Path
import shutil
import stat
from tempfile import NamedTemporaryFile
from threading import RLock
import time
import uuid

from thumbnails import clear_thumbnail

CAPACITY_BYTES = 5_000_000_000
CLEANUP_BYTES = CAPACITY_BYTES * 70 // 100
_STAGING_PREFIX = ".mac2api-media-"
_lock = RLock()
_log = logging.getLogger("muse2api")


media_owner = ContextVar("media_owner", default=None)


def new_media_name(extension):
    owner = media_owner.get()
    prefix = f"client-{owner}--" if owner else ""
    return f"{prefix}{uuid.uuid4().hex}{extension}"


def media_entries(directory):
    """Snapshot regular media files, oldest first, with nanosecond precision."""
    with _lock:
        entries = []
        directory = Path(directory)
        if not directory.is_dir():
            return entries
        for path in directory.iterdir():
            if path.name.startswith("."):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                entries.append((path, info))
        return sorted(entries, key=lambda item: (item[1].st_mtime_ns, item[0].name))


def delete_media_file(path):
    path = Path(path)
    with _lock:
        path.unlink()
        try:
            clear_thumbnail(str(path.parent.parent / "thumbnails"), path.name)
        except OSError:
            _log.warning("Could not remove cached thumbnail for %s", path.name)


def _prune(directory, incoming_size=None):
    entries = media_entries(directory)
    total = sum(info.st_size for _, info in entries) + (incoming_size or 0)
    for index, (path, info) in enumerate(entries):
        if total < CLEANUP_BYTES:
            break
        # Keep a single oversized-but-admissible result when there's nothing else to evict.
        if incoming_size is None and index == len(entries) - 1 and total <= CAPACITY_BYTES:
            break
        delete_media_file(path)
        total -= info.st_size


def prune_media(directory):
    with _lock:
        # Remove staging files abandoned by a crash and older than any generation timeout.
        cutoff = time.time() - 86400
        for path in Path(directory).glob(f"{_STAGING_PREFIX}*"):
            try:
                if path.lstat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                pass
        _prune(directory)


def save_media(directory, name, *, data=None, source=None):
    """Stage a complete file, then enforce retention and publish under one lock."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    size = len(data) if source is None else os.stat(source).st_size
    if size > CAPACITY_BYTES:
        raise ValueError("Tệp vượt giới hạn kho media 5 GB.")
    staged = None
    destination = directory / name
    try:
        with NamedTemporaryFile(dir=directory, prefix=_STAGING_PREFIX, delete=False) as output:
            staged = Path(output.name)
            if source is None:
                output.write(data)
            else:
                with open(source, "rb") as input_file:
                    shutil.copyfileobj(input_file, output)
        size = staged.stat().st_size
        if size > CAPACITY_BYTES:
            raise ValueError("Tệp vượt giới hạn kho media 5 GB.")
        with _lock:
            _prune(directory, incoming_size=size)
            os.utime(staged, None)
            staged.replace(destination)
        return str(destination), size
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
