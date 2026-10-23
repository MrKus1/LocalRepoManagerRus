import logging
import shutil
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, text

from app.config import settings
from app.services.proxy import load_proxy, save_proxy, load_min_free_gb, save_min_free_gb, load_max_sync, save_max_sync, load_retry, save_retry
from app.database import init_db, get_db, AsyncSessionLocal, engine
from app.models import Mirror, LocalRepo, SyncLog, SyncStatus, CacheRepo
from app.services.scheduler import start_scheduler, stop_scheduler
from app.api import mirrors, sync, packages, cache, jobs
from app.api import update as update_api

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting Repo Manager...")
    
    # Создаём директории
    for d in [
        settings.STORAGE_ROOT,
        settings.MIRRORS_DIR,
        settings.LOCAL_REPOS_DIR,
        settings.UPLOADS_DIR,
        settings.LOGS_DIR,
        settings.STORAGE_ROOT / "cache",
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)
    
    await init_db()
    async with AsyncSessionLocal() as db:
        running_logs = await db.execute(select(SyncLog).where(SyncLog.status == SyncStatus.RUNNING.value))
        delete_ids = set()
        for log in running_logs.scalars().all():
            if log.message == "удаление":
                delete_ids.add(log.mirror_id)
            else:
                log.message = "не прошла"
            log.status = SyncStatus.FAILED.value
            log.finished_at = datetime.utcnow()
        if delete_ids:
            await db.execute(
                update(Mirror)
                .where(Mirror.status == SyncStatus.RUNNING.value, Mirror.id.in_(delete_ids))
                .values(status=SyncStatus.FAILED.value, last_error="Удаление прервано перезапуском")
            )
            await db.execute(
                update(Mirror)
                .where(Mirror.status == SyncStatus.RUNNING.value, Mirror.id.notin_(delete_ids))
                .values(status=SyncStatus.FAILED.value, last_error="Прервано перезапуском сервиса")
            )
        else:
            await db.execute(
                update(Mirror)
                .where(Mirror.status == SyncStatus.RUNNING.value)
                .values(status=SyncStatus.FAILED.value, last_error="Прервано перезапуском сервиса")
            )
        await db.commit()
        logger.info("Сброшены зеркала, оставшиеся в running после перезапуска")
        from app.api.sync import restore_queue
        await restore_queue()
    start_scheduler()
    logger.info("Database initialized")
    
    yield
    
    # Shutdown
    stop_scheduler()
    logger.info("Shutting down...")


BASE_DIR = Path(__file__).resolve().parent
APP_VERSION = (BASE_DIR.parent / "VERSION").read_text().strip() if (BASE_DIR.parent / "VERSION").exists() else "0"

app = FastAPI(
    title="Repo Manager",
    description="Менеджер зеркалируемых репозиториев deb и rpm + публикация локальных пакетов",
    version=APP_VERSION,
    lifespan=lifespan,
)

# API
app.include_router(jobs.router)
app.include_router(mirrors.router)
app.include_router(sync.router)
app.include_router(packages.router)
app.include_router(cache.router)
app.include_router(cache.serve)
app.include_router(update_api.router)

# Static & Templates
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    mirrors_result = await db.execute(select(Mirror).order_by(Mirror.name))
    mirrors_list = mirrors_result.scalars().all()
    
    repos_result = await db.execute(select(LocalRepo).order_by(LocalRepo.name))
    repos_list = repos_result.scalars().all()
    
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "mirrors": mirrors_list,
            "local_repos": repos_list,
            "version": APP_VERSION,
        },
    )


@app.get("/mirrors/{mirror_id}", response_class=HTMLResponse)
async def mirror_page(mirror_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    logs = await db.execute(
        select(SyncLog).where(SyncLog.mirror_id == mirror_id).order_by(SyncLog.started_at.desc()).limit(50)
    )
    host = request.headers.get("host", "SERVER").split(":")[0]
    if mirror.type == "rpm":
        client_snippet = (
            f"[{mirror.name}]\n"
            f"name={mirror.name}\n"
            f"baseurl=http://{host}/repo/rpm/{mirror.name}\n"
            f"gpgcheck=0\nenabled=1\n"
        )
    else:
        client_snippet = f"deb http://{host}/repo/deb/{mirror.name} {mirror.distribution or 'stable'} {mirror.components or 'main'}\n"
    return templates.TemplateResponse(
        request,
        "mirror.html",
        {"mirror": mirror, "logs": logs.scalars().all(), "client_snippet": client_snippet, "version": APP_VERSION},
    )


@app.get("/api/proxy")
async def get_proxy():
    return load_proxy()


@app.put("/api/proxy")
async def put_proxy(payload: dict):
    return save_proxy(payload.get("proxy_url", ""), payload.get("no_proxy", ""))


@app.get("/api/min-free")
async def get_min_free():
    return {"min_free_gb": load_min_free_gb(), "max_sync": load_max_sync(), **load_retry()}


@app.put("/api/min-free")
async def put_min_free(payload: dict):
    return {
        "min_free_gb": save_min_free_gb(str(payload.get("min_free_gb") or "")),
        "max_sync": save_max_sync(payload.get("max_sync", 2)),
        **save_retry(payload.get("retry_minutes", 60), payload.get("retry_max", 3)),
    }


@app.get("/api/disk")
async def disk_usage():
    path = settings.STORAGE_ROOT
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


@app.get("/health")
async def health():
    db_ok = True
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health: postgres")
        db_ok = False
    usage = shutil.disk_usage(settings.STORAGE_ROOT)
    aptly = "нет"
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.APTLY_BIN,
            "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        line = (out.decode(errors="replace").strip().splitlines() or [""])[0][:80]
        aptly = line or "ошибка" if proc.returncode == 0 else "ошибка"
    except Exception:
        aptly = "нет"
    return JSONResponse(
        {"status": "ok" if db_ok else "db", "disk_free_bytes": usage.free, "aptly": aptly},
        status_code=200 if db_ok else 503,
    )
