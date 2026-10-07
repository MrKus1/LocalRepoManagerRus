from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db, AsyncSessionLocal
from app.config import settings
from app.models import Mirror, SyncLog, SyncStatus
from app.schemas import SyncRequest, SyncLogOut
from app.services.deb import deb_service
from app.services.rpm import rpm_service
from app.services.procs import set_mirror, get_process, get_progress
from app.services.filelog import mirror_logger

router = APIRouter(prefix="/api/sync", tags=["sync"])


async def _do_sync(mirror_id: int):
    """Фоновая задача синхронизации"""
    async with AsyncSessionLocal() as db:
        mirror = await db.get(Mirror, mirror_id)
        if not mirror:
            return

        mirror.status = SyncStatus.RUNNING.value
        log = SyncLog(mirror_id=mirror.id, status=SyncStatus.RUNNING.value)
        db.add(log)
        await db.commit()

        success = False
        message = ""
        set_mirror(mirror.id)
        flog = mirror_logger(mirror.name)
        flog.info("sync started")

        try:
            if mirror.type == "deb":
                ok, msg = await deb_service.update_mirror(mirror.name)
                if ok:
                    pok, pmsg = await deb_service.publish_mirror(
                        mirror.name,
                        mirror.distribution or "stable",
                    )
                    ok = pok
                    msg = (msg or "") + "\n" + (pmsg or "")
                success = ok
                message = msg
            else:  # rpm
                ok, msg = await rpm_service.sync_mirror(
                    dest_path=mirror.local_path,
                    repoid=mirror.repoid,
                    source_url=mirror.source_url,
                )
                success = ok
                message = msg

            mirror.status = SyncStatus.SUCCESS.value if success else SyncStatus.FAILED.value
            mirror.last_sync = datetime.utcnow()
            if not success:
                mirror.last_error = message[:2000]
            else:
                mirror.last_error = None

            log.status = mirror.status
            log.finished_at = datetime.utcnow()
            log.message = "прошла" if success else "не прошла"
            log.log_output = None
            flog.info("sync %s\n%s", log.message, message or "")

        except Exception as e:
            mirror.status = SyncStatus.FAILED.value
            mirror.last_error = str(e)[:500]
            log.status = SyncStatus.FAILED.value
            log.finished_at = datetime.utcnow()
            log.message = "не прошла"
            flog.exception("sync failed")

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
        raise HTTPException(status_code=409, detail="Синхронизация не выполняется")

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
    if mirror.type == "deb":
        root = settings.STORAGE_ROOT / "aptly"
    files = 0
    size = 0
    suffix = ".rpm" if mirror.type == "rpm" else ".deb"
    if root.exists():
        for path in root.rglob(f"*{suffix}"):
            if path.is_file() and not path.is_symlink():
                files += 1
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
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
