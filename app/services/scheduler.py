"""Простой планировщик: раз в минуту сверяет cron и запускает sync, если зеркало не running."""

import asyncio
import logging
from datetime import datetime

from croniter import croniter
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Mirror, SyncStatus
from app.api.sync import _do_sync
from app.services.procs import get_process
from app.services.proxy import sync_blocked

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
    now = datetime.now().replace(second=0, microsecond=0)
    retries = _retries()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Mirror).where(Mirror.enabled == True))
        mirrors = result.scalars().all()
    for mirror in mirrors:
        process = get_process(mirror.id)
        if sync_blocked() or mirror.status == SyncStatus.RUNNING.value or (process and process.returncode is None):
            continue
        if mirror.status == SyncStatus.FAILED.value and mirror.updated_at:
            stamp = mirror.updated_at.isoformat()
            age = (datetime.utcnow() - mirror.updated_at).total_seconds()
            if age >= 3600 and retries.get(str(mirror.id)) != stamp:
                retries[str(mirror.id)] = stamp
                _save_retries(retries)
                logger.info("Повторный sync %s через час после ошибки", mirror.name)
                asyncio.create_task(_do_sync(mirror.id))
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
        asyncio.create_task(_do_sync(mirror.id))


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
