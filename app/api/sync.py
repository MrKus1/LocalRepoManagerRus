from datetime import datetime
import asyncio
import logging
import os
import signal
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
from app.services.procs import set_mirror, get_process, get_progress, mark_stopped
from app.services.filelog import mirror_logger
from app.services.proxy import sync_blocked, load_max_sync, load_retry

router = APIRouter(prefix="/api/sync", tags=["sync"])
logger = logging.getLogger(__name__)

_count_cache = {}
_walks = set()
_queue: list[int] = []
_starting: set[int] = set()
_active: set[int] = set()
_deb_holder: int | None = None
_queue_lock = asyncio.Lock()
_finish_lock = asyncio.Lock()


def plan_starts(ids: list[int], occupied: int, limit: int, deb_holder, types: dict[int, str]):
    """RPM идут параллельно. Второй DEB не стартует, пока первый держит aptly."""
    start = []
    deferred = []
    rest = []
    count = occupied
    holder = deb_holder
    for mirror_id in ids:
        if count >= limit:
            rest.append(mirror_id)
            continue
        if types.get(mirror_id) == "deb" and holder:
            deferred.append(mirror_id)
            continue
        start.append(mirror_id)
        count += 1
        if types.get(mirror_id) == "deb":
            holder = mirror_id
    return start, deferred + rest


def _du_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    try:
        import subprocess
        out = subprocess.check_output(["du", "-sb", str(root)], text=True, timeout=120)
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def _start_walk(key: str, func, *args):
    """Один обход каталога на путь. Второй не стартует, пока первый не кончился."""
    if key in _walks:
        return
    _walks.add(key)

    def job():
        try:
            func(*args)
        finally:
            _walks.discard(key)

    asyncio.get_running_loop().run_in_executor(None, job)


def _remember_du(key: str, root: Path):
    _count_cache[key] = (datetime.utcnow().timestamp(), 0, _du_bytes(root))


def _remember_names(key: str, root: Path, suffix: str):
    files = 0
    if root.exists():
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(suffix):
                    files += 1
    prev = _count_cache.get(key)
    size = prev[2] if prev else 0
    _count_cache[key] = (datetime.utcnow().timestamp(), files, size)


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
    _count_cache[key] = (datetime.utcnow().timestamp(), files, size)
    return files, size


async def _watch_disk(mirror_id: int, flag: dict):
    """Останавливает sync, если кончилось место или прошли сутки."""
    import signal
    import time
    started = time.monotonic()
    while not flag.get("done"):
        await asyncio.sleep(15)
        if flag.get("done"):
            return
        reason = ""
        if time.monotonic() - started >= 24 * 3600:
            reason = "Превышены 24 часа, синхронизация остановлена"
        else:
            blocked = sync_blocked()
            if blocked:
                reason = blocked.replace("не запущен", "остановлен")
        if not reason:
            continue
        flag["reason"] = reason
        process = get_process(mirror_id)
        if process is not None and process.returncode is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return


async def _do_sync(mirror_id: int):
    """Фоновая задача синхронизации"""
    try:
        await _run_sync(mirror_id)
    finally:
        _starting.discard(mirror_id)


async def _run_sync(mirror_id: int):
    global _deb_holder
    _active.add(mirror_id)
    try:
        await _run_sync_body(mirror_id)
    finally:
        _active.discard(mirror_id)
        if _deb_holder == mirror_id:
            _deb_holder = None
        asyncio.create_task(_pump())


async def _run_sync_body(mirror_id: int):
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
        architectures = mirror.architectures
        local_path = mirror.local_path
        repoid = mirror.repoid
        await db.commit()
    _starting.discard(mirror_id)

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
                    pok, pmsg = await deb_service.publish_mirror(mirror_name, dist, architectures or "amd64")
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
    try:
        await watcher
    except asyncio.CancelledError:
        pass

    if flag.get("reason"):
        success = False
        message = flag["reason"]
    stop = (get_progress(mirror_id) or {}).get("stop_reason")
    if stop:
        success = False
        message = stop

    async with _finish_lock:
        fresh = get_progress(mirror_id) or {}
        if fresh.get("cancelled") or fresh.get("stop_reason"):
            success = False
            message = fresh.get("stop_reason") or "Остановлено пользователем"
        status = SyncStatus.SUCCESS.value if success else SyncStatus.FAILED.value
        async with AsyncSessionLocal() as db:
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


async def _occupied(db: AsyncSession) -> int:
    running = await db.execute(select(Mirror.id).where(Mirror.status == SyncStatus.RUNNING.value))
    ids = {row[0] for row in running.all()}
    return len(ids | _starting)


async def request_sync(mirror_id: int, force: bool = False) -> dict:
    """Старт сразу или постановка в очередь. Слот занимается до записи running в базу."""
    global _deb_holder
    async with _queue_lock:
        async with AsyncSessionLocal() as db:
            mirror = await db.get(Mirror, mirror_id)
            if not mirror:
                return {"ok": False, "detail": "Зеркало не найдено", "status": 404}
            if mirror_id in _starting or mirror_id in _active or mirror.status == SyncStatus.RUNNING.value:
                return {"ok": False, "detail": "Синхронизация уже выполняется", "status": 409}
            from app.services.jobs import busy
            if busy():
                return {"ok": False, "detail": "Идёт задача aptly, дождитесь окончания", "status": 409}
            process = get_process(mirror_id)
            if process is not None and process.returncode is None:
                return {"ok": False, "detail": "Синхронизация уже выполняется", "status": 409}
            deb_busy = mirror.type == "deb" and _deb_holder is not None
            if not force:
                count = await _occupied(db)
                limit = load_max_sync()
                if count >= limit or _queue or deb_busy:
                    if mirror_id not in _queue:
                        _queue.append(mirror_id)
                    mirror.status = "queued"
                    mirror.last_error = None
                    await db.commit()
                    return {"ok": True, "queued": True, "mirror_id": mirror_id, "position": _queue.index(mirror_id) + 1}
            if deb_busy:
                if mirror_id not in _queue:
                    _queue.append(mirror_id)
                mirror.status = "queued"
                mirror.last_error = None
                await db.commit()
                return {"ok": True, "queued": True, "mirror_id": mirror_id, "position": _queue.index(mirror_id) + 1}
            if mirror_id in _queue:
                _queue.remove(mirror_id)
            if mirror.type == "deb":
                _deb_holder = mirror_id
            _starting.add(mirror_id)
            asyncio.create_task(_do_sync(mirror_id))
            return {"ok": True, "queued": False, "mirror_id": mirror_id}


async def _pump():
    """Запускает следующие из очереди, пока есть свободный слот."""
    global _deb_holder
    async with _queue_lock:
        async with AsyncSessionLocal() as db:
            count = await _occupied(db)
            limit = load_max_sync()
            eligible = []
            types = {}
            for mirror_id in list(_queue):
                if mirror_id in _starting or mirror_id in _active:
                    continue
                mirror = await db.get(Mirror, mirror_id)
                if not mirror or not mirror.enabled or mirror.status == SyncStatus.RUNNING.value:
                    if mirror and mirror.status == "queued":
                        mirror.status = SyncStatus.IDLE.value
                    continue
                eligible.append(mirror_id)
                types[mirror_id] = mirror.type
            start, back = plan_starts(eligible, count, limit, _deb_holder, types)
            for mirror_id in start:
                if types.get(mirror_id) == "deb":
                    _deb_holder = mirror_id
                _starting.add(mirror_id)
                asyncio.create_task(_do_sync(mirror_id))
            _queue[:] = back
            await db.commit()


async def sweep_stale():
    """Сбрасывает running, если задача уже умерла, а статус остался."""
    async with _finish_lock:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Mirror).where(Mirror.status == SyncStatus.RUNNING.value))
            changed = False
            for mirror in result.scalars().all():
                if mirror.id in _active or mirror.id in _starting:
                    continue
                process = get_process(mirror.id)
                if process is not None and process.returncode is None:
                    continue
                mirror.status = SyncStatus.FAILED.value
                mirror.last_error = "Процесс уже не идёт, статус сброшен"
                await db.execute(
                    update(SyncLog)
                    .where(SyncLog.mirror_id == mirror.id, SyncLog.status == SyncStatus.RUNNING.value)
                    .values(status=SyncStatus.FAILED.value, finished_at=datetime.utcnow(), message="не прошла")
                )
                changed = True
            if changed:
                await db.commit()


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
    alive = process is not None and process.returncode is None
    if alive:
        mark_stopped(mirror_id)
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    async with _finish_lock:
        mirror = await db.get(Mirror, mirror_id)
        if mirror.status == SyncStatus.SUCCESS.value:
            return {"message": "Уже завершено", "mirror_id": mirror_id}
        if not alive and mirror.status not in (SyncStatus.RUNNING.value, "queued"):
            return {"message": "Уже завершено", "mirror_id": mirror_id}
        mirror.status = SyncStatus.FAILED.value
        mirror.last_error = "Остановлено пользователем"
        await db.execute(
            update(SyncLog)
            .where(SyncLog.mirror_id == mirror.id, SyncLog.status == SyncStatus.RUNNING.value)
            .values(status=SyncStatus.FAILED.value, finished_at=datetime.utcnow(), message="не прошла")
        )
        await db.commit()
    return {"message": "Остановлено" if alive else "Процесса уже не было, статус сброшен", "mirror_id": mirror_id}


@router.get("/{mirror_id}/progress")
async def sync_progress(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    item = dict(get_progress(mirror_id))
    try:
        return _progress_body(mirror, item)
    except Exception:
        logger.exception("progress %s", mirror_id)
        return {
            "mirror_id": mirror_id,
            "status": mirror.status,
            "percent": None,
            "line": "",
            "files": 0,
            "size_bytes": mirror.size_bytes or 0,
            "size_kind": "mirror",
            "position": None,
            "last_error": mirror.last_error or "",
            "last_sync": "",
        }


def _progress_body(mirror, item: dict):
    size_kind = "mirror"
    if mirror.type == "deb" and mirror.status == SyncStatus.RUNNING.value:
        files = int(item.get("files") or 0)
        pool = settings.STORAGE_ROOT / "aptly" / "pool"
        key = "aptly-pool"
        cached = _count_cache.get(key)
        stale = not cached or datetime.utcnow().timestamp() - cached[0] >= 60
        if stale:
            _start_walk(key, _remember_du, key, pool)
        size = cached[2] if cached else (item.get("size_bytes") or mirror.size_bytes or 0)
        size_kind = "pool"
    else:
        files = int(item.get("files") or 0)
        size = item.get("size_bytes") or mirror.size_bytes or 0
        if mirror.status != "queued":
            if mirror.type == "deb":
                root = settings.STORAGE_ROOT / "public" / mirror.name
                suffix = ".deb"
            else:
                root = Path(mirror.local_path)
                suffix = ".rpm"
            if mirror.type != "deb" and mirror.status == SyncStatus.RUNNING.value:
                key = "names:" + str(root)
                cached = _count_cache.get(key)
                stale = not cached or datetime.utcnow().timestamp() - cached[0] >= 60
                if stale:
                    _start_walk(key, _remember_names, key, root, suffix)
                if cached and cached[1]:
                    files = cached[1]
            else:
                cached = _count_cache.get(str(root))
                stale = not cached or datetime.utcnow().timestamp() - cached[0] >= 60
                if stale:
                    _start_walk(str(root), _count_packages, root, suffix)
                if cached:
                    files, size = cached[1], cached[2]
                elif mirror.size_bytes:
                    size = mirror.size_bytes
    position = None
    if mirror.status == "queued":
        try:
            position = _queue.index(mirror.id) + 1
        except ValueError:
            position = None
    return {
        "mirror_id": mirror.id,
        "status": mirror.status,
        "percent": item.get("percent"),
        "line": item.get("line") or "",
        "files": files,
        "size_bytes": size,
        "size_kind": size_kind,
        "position": position,
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

