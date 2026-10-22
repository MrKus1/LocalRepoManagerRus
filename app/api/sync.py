from datetime import datetime
import asyncio
import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.database import get_db, AsyncSessionLocal
from app.config import settings
from app.models import Mirror, SyncLog, SyncStatus
from app.schemas import SyncRequest, SyncLogOut
from app.services.deb import deb_service
from app.services.rpm import rpm_service
from app.services.procs import set_mirror, get_process, get_progress
from app.services.filelog import mirror_logger
from app.services.proxy import sync_blocked, load_max_sync, load_retry

router = APIRouter(prefix="/api/sync", tags=["sync"])

_count_cache = {}
_queue: list[int] = []
_queue_lock = asyncio.Lock()


def _du_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    try:
        import subprocess
        out = subprocess.check_output(["du", "-sb", str(root)], text=True, timeout=120)
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def _count_packages(root: Path, suffix: str):
    now = datetime.utcnow().timestamp()
    key = str(root)
    cached = _count_cache.get(key)
    if cached and now - cached[0] < 60:
        return cached[1], cached[2]
    files = 0
    size = _du_bytes(root)
    if root.exists():
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(suffix):
                    files += 1
    _count_cache[key] = (now, files, size)
    return files, size


async def _watch_disk(mirror_id: int, flag: dict):
    """Останавливает sync, если свободно меньше порога."""
    import signal
    while not flag.get("done"):
        await asyncio.sleep(30)
        if flag.get("done"):
            return
        reason = sync_blocked()
        if not reason:
            continue
        flag["reason"] = reason + " Sync остановлен."
        process = get_process(mirror_id)
        if process is not None and process.returncode is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return


async def _do_sync(mirror_id: int):
    """Фоновая задача синхронизации"""
    async with AsyncSessionLocal() as db:
        mirror = await db.get(Mirror, mirror_id)
        if not mirror:
            return

        mirror.status = SyncStatus.RUNNING.value
        mirror.last_error = None
        await db.execute(
            update(SyncLog)
            .where(SyncLog.mirror_id == mirror.id, SyncLog.status == SyncStatus.RUNNING.value)
            .values(status=SyncStatus.FAILED.value, finished_at=datetime.utcnow(), message="не прошла")
        )
        log = SyncLog(mirror_id=mirror.id, status=SyncStatus.RUNNING.value)
        db.add(log)
        await db.flush()
        log_id = log.id
        mirror_name = mirror.name
        mirror_type = mirror.type
        source_url = mirror.source_url
        distribution = mirror.distribution
        local_path = mirror.local_path
        repoid = mirror.repoid
        await db.commit()

        success = False
        message = ""
        set_mirror(mirror_id)
        flog = mirror_logger(mirror_name)
        flog.info("sync started")
        flag = {"done": False}
        watcher = asyncio.create_task(_watch_disk(mirror_id, flag))

        try:
            if mirror_type == "deb":
                dist = distribution or "stable"
                url = source_url.rstrip("/") + f"/dists/{dist}/Release"
                code, out, err = await deb_service.run_check(
                    ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", url]
                )
                if out.strip() != "200":
                    success = False
                    message = f"Release недоступен: {url} http={out.strip() or 'нет'}"
                else:
                    ok, msg = await deb_service.update_mirror(mirror_name)
                    if ok:
                        pok, pmsg = await deb_service.publish_mirror(mirror_name, dist)
                        ok = pok
                        msg = (msg or "") + "\n" + (pmsg or "")
                    success = ok
                    message = msg
            else:
                ok, msg = await rpm_service.sync_mirror(
                    dest_path=local_path,
                    repoid=repoid,
                    source_url=source_url,
                )
                success = ok
                message = msg
        except Exception as e:
            success = False
            message = str(e)[:500]
            flog.exception("sync failed")
        finally:
            flag["done"] = True
            watcher.cancel()

        if flag.get("reason"):
            success = False
            message = flag["reason"]

        status = SyncStatus.SUCCESS.value if success else SyncStatus.FAILED.value
        await db.execute(
            update(Mirror)
            .where(Mirror.id == mirror_id)
            .values(
                status=status,
                last_sync=datetime.utcnow(),
                last_error=None if success else (message or "")[:2000],
                updated_at=datetime.utcnow(),
            )
        )
        await db.execute(
            update(SyncLog)
            .where(SyncLog.id == log_id)
            .values(
                status=status,
                finished_at=datetime.utcnow(),
                message="прошла" if success else "не прошла",
            )
        )
        await db.commit()
        flog.info("sync %s\n%s", "прошла" if success else "не прошла", message or "")
        asyncio.create_task(_pump())
        if success:
            root = Path(local_path)
            if mirror_type == "deb":
                root = settings.STORAGE_ROOT / "public" / mirror_name
            asyncio.create_task(_save_size(mirror_id, root))


@router.get("/retries")
async def sync_retries():
    from app.services.scheduler import _retries
    retry = load_retry()
    data = _retries()
    return {
        key: {"count": value.get("count", 0) if isinstance(value, dict) else 0, "max": retry["retry_max"]}
        for key, value in data.items()
    }


@router.post("/{mirror_id}")
async def start_sync(mirror_id: int, force: bool = False):
    result = await request_sync(mirror_id, force=force)
    if not result.get("ok"):
        raise HTTPException(status_code=result.get("status", 400), detail=result.get("detail"))
    return {
        "message": "В очереди" if result.get("queued") else "Синхронизация запущена",
        "mirror_id": mirror_id,
        "queued": bool(result.get("queued")),
        "position": result.get("position"),
    }


async def _save_size(mirror_id: int, root: Path):
    total = await asyncio.get_running_loop().run_in_executor(None, _du_bytes, root)
    async with AsyncSessionLocal() as db:
        await db.execute(update(Mirror).where(Mirror.id == mirror_id).values(size_bytes=total))
        await db.commit()


async def request_sync(mirror_id: int, force: bool = False) -> dict:
    """Старт сразу или постановка в очередь. Общий вход для кнопки и планировщика."""
    async with AsyncSessionLocal() as db:
        mirror = await db.get(Mirror, mirror_id)
        if not mirror:
            return {"ok": False, "detail": "Зеркало не найдено", "status": 404}
        if mirror.status == SyncStatus.RUNNING.value and not force:
            return {"ok": False, "detail": "Синхронизация уже выполняется", "status": 409}
        process = get_process(mirror_id)
        if process is not None and process.returncode is None:
            return {"ok": False, "detail": "Синхронизация уже выполняется", "status": 409}
        if not force:
            running = await db.execute(select(Mirror).where(Mirror.status == SyncStatus.RUNNING.value))
            count = len(running.scalars().all())
            limit = load_max_sync()
            async with _queue_lock:
                if count >= limit or _queue:
                    if mirror_id not in _queue:
                        _queue.append(mirror_id)
                    mirror.status = "queued"
                    mirror.last_error = None
                    await db.commit()
                    return {"ok": True, "queued": True, "mirror_id": mirror_id, "position": _queue.index(mirror_id) + 1}
        asyncio.create_task(_do_sync(mirror_id))
        return {"ok": True, "queued": False, "mirror_id": mirror_id}


async def _pump():
    """Запускает следующие из очереди, пока есть свободный слот."""
    async with _queue_lock:
        async with AsyncSessionLocal() as db:
            running = await db.execute(select(Mirror).where(Mirror.status == SyncStatus.RUNNING.value))
            count = len(running.scalars().all())
            limit = load_max_sync()
            started = 0
            while _queue and count + started < limit:
                mirror_id = _queue.pop(0)
                mirror = await db.get(Mirror, mirror_id)
                if not mirror or not mirror.enabled or mirror.status == SyncStatus.RUNNING.value:
                    continue
                asyncio.create_task(_do_sync(mirror_id))
                started += 1


async def restore_queue():
    """После рестарта зеркала со статусом queued снова ставит в очередь."""
    async with _queue_lock:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Mirror).where(Mirror.status == "queued").order_by(Mirror.updated_at))
            for mirror in result.scalars().all():
                if mirror.id not in _queue:
                    _queue.append(mirror.id)
    await _pump()


@router.post("/{mirror_id}/stop")
async def stop_sync(mirror_id: int, db: AsyncSession = Depends(get_db)):
    import os
    import signal
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

    async with _queue_lock:
        if mirror_id in _queue:
            _queue.remove(mirror_id)
            mirror.status = SyncStatus.IDLE.value
            mirror.last_error = None
            await db.commit()
            return {"message": "Убрано из очереди", "mirror_id": mirror_id}

    process = get_process(mirror_id)
    if process is None or process.returncode is not None:
        mirror.status = SyncStatus.FAILED.value
        mirror.last_error = None
        await db.execute(
            update(SyncLog)
            .where(SyncLog.mirror_id == mirror.id, SyncLog.status == SyncStatus.RUNNING.value)
            .values(status=SyncStatus.FAILED.value, finished_at=datetime.utcnow(), message="не прошла")
        )
        await db.commit()
        return {"message": "Процесса уже не было, статус сброшен", "mirror_id": mirror_id}

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass

    mirror.status = SyncStatus.FAILED.value
    mirror.last_error = "Остановлено пользователем"
    await db.commit()
    return {"message": "Остановлено", "mirror_id": mirror_id}


@router.get("/{mirror_id}/progress")
async def sync_progress(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    item = dict(get_progress(mirror_id))
    if mirror.type == "deb":
        root = settings.STORAGE_ROOT / "public" / mirror.name
        suffix = ".deb"
    else:
        root = Path(mirror.local_path)
        suffix = ".rpm"
    cached = _count_cache.get(str(root))
    stale = not cached or datetime.utcnow().timestamp() - cached[0] >= 60
    if stale:
        asyncio.get_running_loop().run_in_executor(None, _count_packages, root, suffix)
    files, size = (cached[1], cached[2]) if cached else (0, 0)
    if not size and mirror.size_bytes:
        size = mirror.size_bytes
    return {
        "mirror_id": mirror_id,
        "status": mirror.status,
        "percent": item.get("percent"),
        "line": item.get("line") or "",
        "files": files,
        "size_bytes": size,
        "last_error": mirror.last_error or "",
        "last_sync": mirror.last_sync.strftime("%d.%m %H:%M") if mirror.last_sync else "",
    }


@router.get("/{mirror_id}/logs", response_model=list[SyncLogOut])
async def get_sync_logs(mirror_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SyncLog)
        .where(SyncLog.mirror_id == mirror_id)
        .order_by(SyncLog.started_at.desc())
        .limit(50)
    )
    return result.scalars().all()

