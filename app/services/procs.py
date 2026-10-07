import contextvars
import re

current_mirror_id = contextvars.ContextVar("current_mirror_id", default=None)
running = {}
progress = {}

_pct = re.compile(r"(\d{1,3})\s*%")


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
