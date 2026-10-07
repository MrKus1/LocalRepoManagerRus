import asyncio
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import CacheRepo
import json
import time

from app.services.proxy import load_proxy, save_proxy

router = APIRouter(prefix="/api/cache", tags=["cache"])
serve = APIRouter(tags=["cache-serve"])
_locks: dict[str, asyncio.Lock] = {}
_ALLOWED = (".deb", ".rpm", ".gz", ".bz2", ".xz", ".zst", ".xml")
_NAMES = {"Packages", "Release", "InRelease", "repomd.xml"}


def _root(name: str) -> Path:
    path = settings.STORAGE_ROOT / "cache" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _allowed(rel: str) -> bool:
    name = Path(rel).name
    return name in _NAMES or name.endswith(_ALLOWED)


def cleanup_cache(days: int) -> dict:
    root = settings.STORAGE_ROOT / "cache"
    if not root.exists() or days <= 0:
        return {"removed": 0, "bytes": 0}
    cutoff = time.time() - days * 86400
    removed = 0
    freed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        freed += path.stat().st_size
        path.unlink()
        removed += 1
    return {"removed": removed, "bytes": freed}


def load_cleanup() -> dict:
    path = Path("/var/lib/repo-manager/proxy.json")
    if not path.exists():
        return {"cron": "", "days": ""}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"cron": "", "days": ""}
    return {"cron": data.get("cache_cleanup_cron") or "", "days": data.get("cache_max_age_days") or ""}


@router.get("/cleanup")
async def get_cleanup():
    return load_cleanup()


@router.put("/cleanup")
async def put_cleanup(payload: dict):
    data = load_proxy()
    cron = (payload.get("cron") or "").strip()
    days = str(payload.get("days") or "").strip()
    if cron:
        from croniter import croniter
        if not croniter.is_valid(cron):
            raise HTTPException(status_code=400, detail="Неверный cron")
    if days and not days.isdigit():
        raise HTTPException(status_code=400, detail="Дни должны быть числом")
    saved = save_proxy(data.get("proxy_url") or "", data.get("no_proxy") or "")
    raw = json.loads(Path("/var/lib/repo-manager/proxy.json").read_text())
    raw["cache_cleanup_cron"] = cron
    raw["cache_max_age_days"] = days
    Path("/var/lib/repo-manager/proxy.json").write_text(json.dumps(raw))
    return {"cron": cron, "days": days, "proxy_url": saved.get("proxy_url", "")}


@router.post("/cleanup/run")
async def run_cleanup():
    days = int(load_cleanup().get("days") or 14)
    return cleanup_cache(days)


@router.get("/")
async def list_caches(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(CacheRepo).order_by(CacheRepo.name))
    return [
        {
            "id": r.id,
            "name": r.name,
            "type": r.type,
            "source_url": r.source_url,
            "distribution": r.distribution,
            "component": r.component,
            "architectures": r.architectures,
        }
        for r in rows.scalars().all()
    ]


@router.post("/")
async def create_cache(payload: dict, db: AsyncSession = Depends(get_db)):
    name = (payload.get("name") or "").strip()
    typ = (payload.get("type") or "").strip()
    url = (payload.get("source_url") or "").strip().rstrip("/")
    if not name or typ not in ("deb", "rpm") or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Нужны имя, тип deb/rpm и http URL")
    exists = await db.execute(select(CacheRepo).where(CacheRepo.name == name))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Кэш с таким именем уже есть")
    row = CacheRepo(
        name=name,
        type=typ,
        source_url=url,
        distribution=payload.get("distribution") or None,
        component=payload.get("component") or "main",
        architectures=payload.get("architectures") or ("amd64" if typ == "deb" else "x86_64"),
    )
    _root(name)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.put("/{cache_id}")
async def update_cache(cache_id: int, payload: dict, db: AsyncSession = Depends(get_db)):
    row = await db.get(CacheRepo, cache_id)
    if not row:
        raise HTTPException(status_code=404, detail="Кэш не найден")
    url = (payload.get("source_url") or "").strip().rstrip("/")
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Нужен http URL")
    row.source_url = url
    await db.flush()
    return {"id": row.id, "source_url": row.source_url}


@router.get("/{cache_id}/files")
async def cache_files(cache_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(CacheRepo, cache_id)
    if not row:
        raise HTTPException(status_code=404, detail="Кэш не найден")
    root = _root(row.name)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(root)), "size": path.stat().st_size})
    return {"files": files[:500], "count": len(files)}
async def delete_cache(cache_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(CacheRepo, cache_id)
    if not row:
        raise HTTPException(status_code=404, detail="Кэш не найден")
    root = _root(row.name)
    if root.exists():
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    await db.delete(row)
    return {"ok": True}


@serve.get("/cache/{name}/{path:path}")
async def fetch_cached(name: str, path: str, db: AsyncSession = Depends(get_db)):
    if ".." in path or not _allowed(path):
        raise HTTPException(status_code=404, detail="Файл не кэшируется")
    result = await db.execute(select(CacheRepo).where(CacheRepo.name == name))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Кэш не найден")
    dest = _root(name) / path
    if dest.is_file():
        return FileResponse(dest)
    lock = _locks.setdefault(f"{name}/{path}", asyncio.Lock())
    async with lock:
        if dest.is_file():
            return FileResponse(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        proxy = load_proxy()
        url = repo.source_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            async with httpx.AsyncClient(proxy=proxy.get("proxy_url") or None, timeout=120, follow_redirects=True) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from exc
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="источник не отдал файл")
        dest.write_bytes(response.content)
    return FileResponse(dest)
