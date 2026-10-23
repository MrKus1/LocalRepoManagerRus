"""Простой планировщик: раз в минуту сверяет cron и запускает sync, если зеркало не running."""

import asyncio
import logging
from datetime import datetime

from croniter import croniter
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Mirror, SyncStatus
from app.api.cache import cleanup_cache, load_cleanup
from app.services.procs import get_process
from app.services.proxy import sync_blocked, load_max_sync, load_retry

logger = logging.getLogger(__name__)
_task = None
_fired = set()


import json
from pathlib import Path

RETRY_FILE = Path("/var/lib/repo-manager/retries.json")


def _retries() -> dict:
    if not RETRY_FILE.exists():
        return {}
    try:
        return json.loads(RETRY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_retries(data: dict):
    RETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    RETRY_FILE.write_text(json.dumps(data))


async def _tick():
    from app.api.sync import request_sync, sweep_stale
    try:
        await sweep_stale()
    except Exception:
        logger.exception("sweep stale failed")
    now = datetime.now().replace(second=0, microsecond=0)
    retries = _retries()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Mirror).where(Mirror.enabled == True))
        mirrors = result.scalars().all()
    running_now = sum(1 for item in mirrors if item.status == SyncStatus.RUNNING.value)
    limit = load_max_sync()
    for mirror in mirrors:
        process = get_process(mirror.id)
        if sync_blocked() or mirror.status == SyncStatus.RUNNING.value or (process and process.returncode is None):
            continue
        if mirror.status == "queued":
            continue
        over = running_now >= limit
        if mirror.status == SyncStatus.FAILED.value and mirror.updated_at and not over:
            retry = load_retry()
            if retry["retry_max"] > 0:
                stamp = mirror.updated_at.isoformat()
                item = retries.get(str(mirror.id)) or {}
                if item.get("stamp") != stamp:
                    item = {"stamp": stamp, "count": 0}
                age = (datetime.utcnow() - mirror.updated_at).total_seconds()
                wait = retry["retry_minutes"] * 60
                if item["count"] < retry["retry_max"] and age >= wait * (item["count"] + 1):
                    item["count"] += 1
                    retries[str(mirror.id)] = item
                    _save_retries(retries)
                    logger.info("Повторный sync %s, попытка %s/%s", mirror.name, item["count"], retry["retry_max"])
                    asyncio.create_task(request_sync(mirror.id))
                    running_now += 1
                    continue
        expr = (mirror.schedule or "").strip()
        if not expr:
            continue
        try:
            if not croniter.match(expr, now):
                continue
        except Exception:
            logger.warning("Плохой cron у %s: %s", mirror.name, expr)
            continue
        key = (mirror.id, now.isoformat())
        if key in _fired:
            continue
        _fired.add(key)
        logger.info("Плановый sync %s", mirror.name)
        asyncio.create_task(request_sync(mirror.id))
        running_now += 1
    cleanup = load_cleanup()
    expr = (cleanup.get("cron") or "").strip()
    days = str(cleanup.get("days") or "").strip()
    if expr and days.isdigit():
        try:
            if croniter.match(expr, now):
                key = ("cache-cleanup", now.isoformat())
                if key not in _fired:
                    _fired.add(key)
                    logger.info("Очистка кэша: %s", cleanup_cache(int(days)))
        except Exception:
            logger.warning("Плохой cron очистки кэша: %s", expr)


async def _loop():
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(30)


def start_scheduler():
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())


def stop_scheduler():
    global _task
    if _task:
        _task.cancel()
        _task = None
