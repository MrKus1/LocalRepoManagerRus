import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.config import settings
from app.services.proxy import load_proxy, save_proxy, load_min_free_gb, save_min_free_gb
from app.database import init_db, get_db, AsyncSessionLocal
from app.models import Mirror, LocalRepo, SyncLog, SyncStatus
from app.services.scheduler import start_scheduler, stop_scheduler
from app.api import mirrors, sync, packages

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
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)
    
    await init_db()
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Mirror)
            .where(Mirror.status == SyncStatus.RUNNING.value)
            .values(status=SyncStatus.FAILED.value, last_error="Прервано перезапуском сервиса")
        )
        await db.commit()
        logger.info("Сброшены зеркала, оставшиеся в running после перезапуска")
    start_scheduler()
    logger.info("Database initialized")
    
    yield
    
    # Shutdown
    stop_scheduler()
    logger.info("Shutting down...")


app = FastAPI(
    title="Repo Manager",
    description="Менеджер зеркалируемых репозиториев deb и rpm + публикация локальных пакетов",
    version="0.1.0",
    lifespan=lifespan,
)

# API
app.include_router(mirrors.router)
app.include_router(sync.router)
app.include_router(packages.router)

# Static & Templates
BASE_DIR = Path(__file__).resolve().parent
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
        {"mirror": mirror, "logs": logs.scalars().all(), "client_snippet": client_snippet},
    )


@app.get("/api/proxy")
async def get_proxy():
    return load_proxy()


@app.put("/api/proxy")
async def put_proxy(payload: dict):
    return save_proxy(payload.get("proxy_url", ""), payload.get("no_proxy", ""))


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
    return {"status": "ok"}
