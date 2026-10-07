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
from app.services.proxy import sync_blocked

router = APIRouter(prefix="/api/sync", tags=["sync"])

_count_cache = {}


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
    if suffix == ".deb":
        return (cached[1], cached[2]) if cached else (0, 0)
    files = 0
    size = _du_bytes(root)
    if root.exists():
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(suffix):
                    files += 1
    _count_cache[key] = (now, files, size)
    return files, size


def refresh_deb_cache():
    root = settings.STORAGE_ROOT / "aptly"
    now = datetime.utcnow().timestamp()
    files = 0
    size = _du_bytes(root)
    if root.exists():
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".deb"):
                    files += 1
    _count_cache[str(root)] = (now, files, size)


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
        if success:
            root = Path(local_path)
            if mirror_type == "deb":
                root = settings.STORAGE_ROOT / "public" / mirror_name
            total = 0
            if root.exists():
                for path in root.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        try:
                            total += path.stat().st_size
                        except OSError:
                            pass
            await db.execute(update(Mirror).where(Mirror.id == mirror_id).values(size_bytes=total))
            await db.commit()


@router.post("/{mirror_id}")
async def start_sync(
    mirror_id: int,
    background_tasks: BackgroundTasks,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

    if mirror.status == SyncStatus.RUNNING.value and not force:
        raise HTTPException(status_code=409, detail="Синхронизация уже выполняется")

    process = get_process(mirror_id)
    if process is not None and process.returncode is None:
        raise HTTPException(status_code=409, detail="Синхронизация уже выполняется")

    background_tasks.add_task(_do_sync, mirror_id)
    return {"message": "Синхронизация запущена", "mirror_id": mirror_id}


@router.post("/{mirror_id}/stop")
async def stop_sync(mirror_id: int, db: AsyncSession = Depends(get_db)):
    import os
    import signal
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

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
    root = Path(mirror.local_path)
    suffix = ".rpm"
    if mirror.type == "deb":
        root = settings.STORAGE_ROOT / "aptly"
        suffix = ".deb"
    files, size = _count_packages(root, suffix)
    if mirror.type == "deb":
        cached = _count_cache.get(str(root))
        if not cached or datetime.utcnow().timestamp() - cached[0] >= 60:
            asyncio.get_running_loop().run_in_executor(None, refresh_deb_cache)
    if not size and mirror.size_bytes:
        size = mirror.size_bytes
    return {
        "mirror_id": mirror_id,
        "status": mirror.status,
        "percent": item.get("percent"),
        "line": item.get("line") or "",
        "files": files,
        "size_bytes": size,
    }


@router.get("/{mirror_id}/logs", response_model=list[SyncLogOut])
async def get_sync_logs(mirror_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SyncLog)
        .where(SyncLog.mirror_id == mirror_id)
        .order_by(SyncLog.started_at.desc())
        .limit(20)
    )
    return result.scalars().all()
