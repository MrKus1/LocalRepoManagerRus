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

logger = logging.getLogger(__name__)
_task = None
_fired = set()


async def _tick():
    now = datetime.now().replace(second=0, microsecond=0)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Mirror).where(Mirror.enabled == True))
        mirrors = result.scalars().all()
    for mirror in mirrors:
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
        process = get_process(mirror.id)
        if mirror.status == SyncStatus.RUNNING.value or (process and process.returncode is None):
            logger.info("Пропуск %s: sync уже идёт", mirror.name)
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
