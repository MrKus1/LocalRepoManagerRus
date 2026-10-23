import contextvars
import os
import re
import signal
import time

current_mirror_id = contextvars.ContextVar("current_mirror_id", default=None)
running = {}
progress = {}

_pct = re.compile(r"(\d{1,3})\s*%")
_downloaded = re.compile(r"Downloaded\s+(\d+)\s+packages", re.I)
_disk_at = 0.0


def set_mirror(mirror_id: int):
    progress[mirror_id] = {
        "percent": None,
        "line": "запуск",
        "files": 0,
        "size_bytes": 0,
    }
    return current_mirror_id.set(mirror_id)


def track(process):
    mirror_id = current_mirror_id.get()
    if mirror_id is not None:
        running[mirror_id] = process
    return mirror_id


def note(line: str):
    mirror_id = current_mirror_id.get()
    if mirror_id is None:
        return
    item = progress.setdefault(mirror_id, {})
    text = line.strip()
    if text:
        item["line"] = text[:300]
    match = _pct.search(text)
    if match:
        item["percent"] = int(match.group(1))
    done = _downloaded.search(text)
    low = text.lower()
    if done:
        item["files"] = int(done.group(1))
    elif "downloading" in low and ".deb" in low:
        item["files"] = int(item.get("files") or 0) + 1
    _guard_disk(mirror_id)


def _guard_disk(mirror_id: int):
    global _disk_at
    now = time.time()
    if now - _disk_at < 10:
        return
    _disk_at = now
    from app.services.proxy import sync_blocked

    reason = sync_blocked()
    if not reason:
        return
    item = progress.setdefault(mirror_id, {})
    item["stop_reason"] = reason.replace("не запущен", "остановлен")
    process = running.get(mirror_id)
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def mark_stopped(mirror_id: int, reason: str = "Остановлено пользователем"):
    item = progress.setdefault(mirror_id, {})
    item["cancelled"] = True
    item["stop_reason"] = reason


def set_files(files: int, size_bytes: int):
    mirror_id = current_mirror_id.get()
    if mirror_id is None:
        return
    item = progress.setdefault(mirror_id, {})
    item["files"] = files
    item["size_bytes"] = size_bytes


def untrack(mirror_id):
    if mirror_id is not None:
        running.pop(mirror_id, None)


def get_process(mirror_id: int):
    return running.get(mirror_id)


def get_progress(mirror_id: int):
    return progress.get(mirror_id, {})
