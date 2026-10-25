"""Фоновые задачи aptly и общий список для окна «Задачи»."""

import asyncio
import json
from datetime import datetime

from app.config import settings

_jobs = []
_seq = 0
_active = None
_lock = asyncio.Lock()


def _store() -> None:
    path = settings.LOGS_DIR / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jobs, ensure_ascii=False), encoding="utf-8")


def interrupt_job(job: dict) -> dict:
    if job.get("status") == "running":
        job["status"] = "failed"
        job["line"] = "прервано перезапуском"
    return job


def _load() -> None:
    global _seq
    path = settings.LOGS_DIR / "jobs.json"
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(raw, list):
        return
    dirty = False
    for job in raw:
        if not isinstance(job, dict):
            continue
        if job.get("status") == "running":
            dirty = True
        interrupt_job(job)
        _jobs.append(job)
    del _jobs[20:]
    ids = [int(job["id"]) for job in _jobs if str(job.get("id", "")).isdigit()]
    _seq = max(ids) if ids else 0
    if dirty:
        try:
            _store()
        except OSError:
            pass


_load()


def busy() -> bool:
    return _active is not None


def list_jobs() -> list:
    return list(_jobs)


def _stamp(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d.%m %H:%M")
    return value or ""


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return value or ""


def build_tasks(aptly: list, logs: list, waiting: list) -> list:
    """Синхронизации из базы и фоновые задачи aptly. Синхронизации переживают рестарт."""
    rows = []
    for job in aptly:
        rows.append({
            "id": f"job-{job['id']}",
            "title": job.get("title") or "Задача",
            "status": job.get("status") or "failed",
            "line": job.get("line") or "",
            "started_at": _stamp(job.get("started_at")),
            "sort": _iso(job.get("started_at")),
        })
    open_mirrors = set()
    for log, name in logs:
        status = log.status if log.status in ("running", "success", "queued") else "failed"
        if status == "running":
            open_mirrors.add(log.mirror_id)
        rows.append({
            "id": f"sync-{log.id}",
            "title": f"Sync {name}",
            "status": status,
            "line": "",
            "started_at": _stamp(log.started_at),
            "sort": _iso(log.started_at),
        })
    for mirror in waiting:
        if mirror.status == "running" and mirror.id in open_mirrors:
            continue
        status = "queued" if mirror.status == "queued" else "running"
        rows.append({
            "id": f"mirror-{mirror.id}",
            "title": f"Sync {mirror.name}",
            "status": status,
            "line": "в очереди" if status == "queued" else "",
            "started_at": _stamp(mirror.updated_at),
            "sort": _iso(mirror.updated_at),
        })
    rows.sort(key=lambda item: item["sort"], reverse=True)
    rows.sort(key=lambda item: 0 if item["status"] in ("running", "queued") else 1)
    for item in rows:
        item.pop("sort", None)
    return rows


async def enqueue(title: str, worker):
    global _seq, _active
    async with _lock:
        if _active is not None:
            return None
        _seq += 1
        job = {
            "id": _seq,
            "title": title,
            "status": "running",
            "line": "запуск",
            "started_at": datetime.now().strftime("%d.%m %H:%M"),
            "finished_at": "",
        }
        _jobs.insert(0, job)
        del _jobs[20:]
        _active = job
        _store()
    asyncio.create_task(_run(job, worker))
    return job


async def _run(job: dict, worker):
    global _active
    try:
        await worker(job)
        if job["status"] == "running":
            job["status"] = "success"
    except Exception as exc:
        job["status"] = "failed"
        job["line"] = str(exc)[:300]
    finally:
        job["finished_at"] = datetime.now().strftime("%d.%m %H:%M")
        _active = None
        _store()
