from typing import List
from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete

from app.database import get_db
from app.models import Mirror, SyncStatus, SyncLog, SyncLog
from app.schemas import MirrorCreate, MirrorUpdate, MirrorOut
from app.config import settings
from app.services.deb import deb_service
from app.services.rpm import rpm_service

router = APIRouter(prefix="/api/mirrors", tags=["mirrors"])


@router.get("/", response_model=List[MirrorOut])
async def list_mirrors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Mirror).order_by(Mirror.name))
    return result.scalars().all()


@router.post("/", response_model=MirrorOut, status_code=status.HTTP_201_CREATED)
async def create_mirror(data: MirrorCreate, db: AsyncSession = Depends(get_db)):
    # Проверка уникальности имени
    existing = await db.execute(select(Mirror).where(Mirror.name == data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Зеркало с таким именем уже существует")

    # Локальный путь
    local_path = settings.MIRRORS_DIR / data.type / data.name
    local_path.mkdir(parents=True, exist_ok=True)

    mirror = Mirror(
        name=data.name,
        type=data.type,
        description=data.description,
        source_url=data.source_url,
        distribution=data.distribution,
        components=data.components,
        architectures=data.architectures or "amd64",
        repoid=data.repoid,
        local_path=str(local_path),
        schedule=data.schedule,
        enabled=data.enabled,
        status=SyncStatus.IDLE.value,
    )

    # Для deb создаём зеркало в aptly
    if data.type == "deb":
        if not data.distribution:
            raise HTTPException(status_code=400, detail="Для deb-зеркала нужен distribution")
        ok, msg = await deb_service.create_mirror(
            name=data.name,
            url=data.source_url,
            distribution=data.distribution,
            components=data.components or "main",
            architectures=data.architectures or "amd64",
        )
        if not ok:
            raise HTTPException(status_code=500, detail=f"Ошибка aptly: {msg}")

    db.add(mirror)
    await db.flush()
    await db.refresh(mirror)
    return mirror



@router.get("/export")
async def export_mirrors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Mirror).order_by(Mirror.name))
    lines = ["# имя|тип|url|distribution|components|architectures|cron"]
    for m in result.scalars().all():
        lines.append("|".join([
            m.name or "",
            m.type or "",
            m.source_url or "",
            m.distribution or "",
            m.components or "",
            m.architectures or "",
            m.schedule or "",
        ]))
    body = "\n".join(lines) + "\n"
    return PlainTextResponse(body, headers={"Content-Disposition": 'attachment; filename="mirrors.txt"'})


@router.post("/import")
async def import_mirrors(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    raw = (await file.read()).decode("utf-8", errors="replace")
    created = []
    errors = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            errors.append(f"строка {lineno}: нужно минимум имя|тип|url")
            continue
        name, typ, url = parts[0], parts[1].lower(), parts[2]
        distribution = parts[3] if len(parts) > 3 else ""
        components = parts[4] if len(parts) > 4 else "main"
        architectures = parts[5] if len(parts) > 5 else ("amd64" if typ == "deb" else "x86_64")
        schedule = parts[6] if len(parts) > 6 else ""
        if typ not in ("rpm", "deb"):
            errors.append(f"строка {lineno}: тип rpm или deb")
            continue
        exists = await db.execute(select(Mirror).where(Mirror.name == name))
        if exists.scalar_one_or_none():
            errors.append(f"строка {lineno}: {name} уже есть")
            continue
        local_path = settings.MIRRORS_DIR / typ / name
        local_path.mkdir(parents=True, exist_ok=True)
        mirror = Mirror(
            name=name,
            type=typ,
            source_url=url,
            distribution=distribution,
            components=components,
            architectures=architectures,
            schedule=schedule,
            local_path=str(local_path),
            enabled=True,
        )
        if typ == "deb":
            ok, msg = await deb_service.create_mirror(name, url, distribution or "stable", components, architectures)
            if not ok:
                errors.append(f"строка {lineno}: aptly: {msg[:200]}")
                continue
        db.add(mirror)
        created.append(name)
    await db.commit()
    return {"created": created, "errors": errors}


@router.get("/{mirror_id}", response_model=MirrorOut)
async def get_mirror(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    return mirror


@router.patch("/{mirror_id}", response_model=MirrorOut)
async def update_mirror(
    mirror_id: int,
    data: MirrorUpdate,
    db: AsyncSession = Depends(get_db),
):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(mirror, field, value)

    await db.flush()
    await db.refresh(mirror)
    return mirror


@router.get("/{mirror_id}/client-check")
async def client_check(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    if mirror.type == "rpm":
        url = f"http://127.0.0.1/repo/rpm/{mirror.name}/repodata/repomd.xml"
    else:
        dist = mirror.distribution or "stable"
        url = f"http://127.0.0.1/repo/deb/{mirror.name}/dists/{dist}/Release"
    code, out, err = await deb_service.run_check(["curl", "-fsS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15", url])
    ok = code == 0 and out.strip() == "200"
    return {"ok": ok, "url": url, "http_code": out.strip(), "error": err[:300]}
async def client_file(mirror_id: int, host: str = "192.168.3.48", db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    if mirror.type == "rpm":
        body = (
            f"[{mirror.name}]\n"
            f"name={mirror.name}\n"
            f"baseurl=http://{host}/repo/rpm/{mirror.name}\n"
            f"gpgcheck=0\nenabled=1\n"
        )
        filename = f"{mirror.name}.repo"
    else:
        body = f"deb http://{host}/repo/deb/{mirror.name} {mirror.distribution or 'stable'} {mirror.components or 'main'}\n"
        filename = f"{mirror.name}.list"
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/{mirror_id}/prune-old")
async def prune_old(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    if mirror.type != "rpm":
        raise HTTPException(status_code=400, detail="Только для RPM")
    if mirror.status == SyncStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Сначала дождитесь окончания sync")
    removed, err = await rpm_service.keep_newest(mirror.local_path)
    if err:
        raise HTTPException(status_code=500, detail=err)
    return {"removed": removed}
async def reachable(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    if mirror.type == "rpm":
        path = Path(mirror.local_path) / "repodata"
        ok = path.is_dir() and any(path.glob("repomd.xml"))
        detail = str(path / "repomd.xml")
    else:
        path = settings.STORAGE_ROOT / "public" / mirror.name / "dists" / (mirror.distribution or "stable") / "Release"
        ok = path.is_file()
        detail = str(path)
    return {"ok": ok, "path": detail}


@router.post("/cleanup-aptly")
async def cleanup_aptly():
    ok, msg = await deb_service.cleanup()
    if not ok:
        raise HTTPException(status_code=500, detail=msg[:1000])
    return {"message": "Очистка завершена", "output": msg[-1000:]}


@router.get("/{mirror_id}/contents")
async def mirror_contents(
    mirror_id: int,
    q: str = Query("", description="фильтр по имени файла"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

    root = Path(mirror.local_path)
    if mirror.type == "deb":
        published = settings.STORAGE_ROOT / "public"
        if published.exists():
            root = published
    if not root.exists():
        return {
            "mirror_id": mirror_id,
            "path": str(root),
            "total": 0,
            "offset": offset,
            "limit": limit,
            "size_bytes": 0,
            "packages": [],
        }

    suffix = ".rpm" if mirror.type == "rpm" else ".deb"
    matched = []
    size = 0
    for path in root.rglob(f"*{suffix}"):
        if not path.is_file():
            continue
        size += path.stat().st_size
        if q and q.lower() not in path.name.lower():
            continue
        matched.append(path)

    matched.sort(key=lambda p: p.name.lower())
    page = matched[offset:offset + limit]
    return {
        "mirror_id": mirror_id,
        "path": str(root),
        "total": len(matched),
        "offset": offset,
        "limit": limit,
        "size_bytes": size,
        "packages": [
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "relpath": str(p.relative_to(root)),
            }
            for p in page
        ],
    }


@router.delete("/{mirror_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mirror(
    mirror_id: int,
    delete_files: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

    if mirror.status == SyncStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Нельзя удалить зеркало во время синхронизации")

    if delete_files and mirror.type == "deb":
        try:
            await deb_service.drop_mirror(mirror.name, mirror.distribution or "stable")
        except Exception:
            pass

    await db.execute(sql_delete(SyncLog).where(SyncLog.mirror_id == mirror.id))
    await db.flush()

    if delete_files:
        path = Path(mirror.local_path)
        if path.exists() and path.is_dir():
            shutil.rmtree(path)

    await db.delete(mirror)
    return None
