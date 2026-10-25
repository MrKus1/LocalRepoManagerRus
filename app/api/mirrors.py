import logging
import time
import asyncio
from typing import List
from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete, update

from app.database import get_db
from app.models import Mirror, SyncStatus, SyncLog, SyncLog
from app.schemas import MirrorCreate, MirrorUpdate, MirrorOut
from app.config import settings
from app.guards import inside, mirror_field_error, name_error
from app.services.deb import deb_service
from app.services.rpm import rpm_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mirrors", tags=["mirrors"])
_rpm_list_cache: dict[str, tuple] = {}


def _check_name(name: str) -> None:
    err = name_error(name)
    if err:
        raise HTTPException(status_code=400, detail=err)


def _check_inside(path: Path, root: Path) -> None:
    if not inside(path, root):
        raise HTTPException(status_code=400, detail="Небезопасный путь зеркала")


def _check_fields(payload: dict) -> None:
    err = mirror_field_error(payload)
    if err:
        raise HTTPException(status_code=400, detail=err)


def _package_arch_ok(line: str, architectures: str | None) -> bool:
    allowed = {"all", "noarch"}
    for item in (architectures or "amd64").replace(",", " ").split():
        if item.strip():
            allowed.add(item.strip())
    return any(line.endswith("_" + arch) for arch in allowed)


def _scan_rpms(root: Path) -> tuple[list[dict], int]:
    rows = []
    size = 0
    for path in root.rglob("*.rpm"):
        if not path.is_file():
            continue
        file_size = path.stat().st_size
        size += file_size
        rows.append({"name": path.name, "size_bytes": file_size, "relpath": str(path.relative_to(root))})
    rows.sort(key=lambda item: item["name"].lower())
    return rows, size


@router.get("/", response_model=List[MirrorOut])
async def list_mirrors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Mirror).order_by(Mirror.name))
    return result.scalars().all()


@router.post("/", response_model=MirrorOut, status_code=status.HTTP_201_CREATED)
async def create_mirror(data: MirrorCreate, db: AsyncSession = Depends(get_db)):
    _check_name(data.name)
    _check_fields(data.model_dump())
    existing = await db.execute(select(Mirror).where(Mirror.name == data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Зеркало с таким именем уже существует")

    local_path = settings.MIRRORS_DIR / data.type / data.name
    _check_inside(local_path, settings.MIRRORS_DIR)
    local_path.mkdir(parents=True, exist_ok=True)
    architectures = data.architectures or ("x86_64" if data.type == "rpm" else "amd64")

    mirror = Mirror(
        name=data.name,
        type=data.type,
        description=data.description,
        source_url=data.source_url,
        distribution=data.distribution,
        components=data.components,
        architectures=architectures,
        repoid=data.repoid,
        local_path=str(local_path),
        schedule=data.schedule,
        enabled=data.enabled,
        status=SyncStatus.IDLE.value,
    )

    aptly_created = False
    try:
        if data.type == "deb":
            if not data.distribution:
                raise HTTPException(status_code=400, detail="Для deb-зеркала нужен distribution")
            ok, msg = await deb_service.create_mirror(
                name=data.name,
                url=data.source_url,
                distribution=data.distribution,
                components=data.components or "main",
                architectures=architectures,
            )
            if not ok:
                raise HTTPException(status_code=500, detail=f"Ошибка aptly: {msg}")
            aptly_created = "подхвачено" not in (msg or "")

        db.add(mirror)
        await db.flush()
        await db.refresh(mirror)
        return mirror
    except Exception:
        if aptly_created:
            await deb_service.drop_mirror(data.name, data.distribution or "stable", cleanup=False)
        raise


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
    created_aptly = []
    try:
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
            if name_error(name) is not None:
                errors.append(f"строка {lineno}: {name_error(name)}")
                continue
            field_err = mirror_field_error({
                "source_url": url,
                "schedule": schedule,
                "distribution": distribution,
                "components": components,
                "architectures": architectures,
            })
            if field_err:
                errors.append(f"строка {lineno}: {field_err}")
                continue
            exists = await db.execute(select(Mirror).where(Mirror.name == name))
            if exists.scalar_one_or_none():
                errors.append(f"строка {lineno}: {name} уже есть")
                continue
            local_path = settings.MIRRORS_DIR / typ / name
            try:
                _check_inside(local_path, settings.MIRRORS_DIR)
            except HTTPException as exc:
                errors.append(f"строка {lineno}: {exc.detail}")
                continue
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
                if "подхвачено" not in (msg or ""):
                    created_aptly.append((name, distribution or "stable"))
            db.add(mirror)
            created.append(name)
        await db.commit()
    except Exception:
        await db.rollback()
        for aptly_name, dist in created_aptly:
            await deb_service.drop_mirror(aptly_name, dist, cleanup=False)
        raise
    return {"created": created, "errors": errors}


@router.post("/bulk-delete")
async def bulk_delete(payload: dict, db: AsyncSession = Depends(get_db)):
    """Удаляет сразу несколько зеркал. Файлы DEB снимает одной фоновой задачей."""
    from app.database import AsyncSessionLocal
    from app.services.jobs import busy, enqueue
    from app.services.procs import set_mirror, note, get_process
    from app.api.sync import _queue, _queue_lock, _starting

    raw_ids = payload.get("ids") or []
    delete_files = bool(payload.get("delete_files"))
    ids = []
    for item in raw_ids:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбраны зеркала")

    errors = []
    debs = []
    now = []
    for mirror_id in ids:
        mirror = await db.get(Mirror, mirror_id)
        if not mirror:
            errors.append(f"{mirror_id}: нет в списке")
            continue
        process = get_process(mirror.id)
        if mirror.id in _starting or (process is not None and process.returncode is None) or mirror.status in (SyncStatus.RUNNING.value, "queued"):
            errors.append(f"{mirror.name}: идёт синхронизация")
            continue
        if delete_files and mirror.type == "deb":
            debs.append(mirror)
        else:
            now.append(mirror)

    if debs and busy():
        raise HTTPException(status_code=409, detail="Уже выполняется другая задача")

    async with _queue_lock:
        for mirror in now + debs:
            if mirror.id in _queue:
                _queue.remove(mirror.id)

    deleted = []
    for mirror in now:
        await db.execute(sql_delete(SyncLog).where(SyncLog.mirror_id == mirror.id))
        if delete_files:
            _safe_rmtree(Path(mirror.local_path), settings.MIRRORS_DIR)
        deleted.append(mirror.name)
        await db.delete(mirror)
    await db.commit()

    if not debs:
        return {"deleted": deleted, "started": [], "errors": errors}

    planned = []
    for mirror in debs:
        mirror.status = SyncStatus.RUNNING.value
        mirror.last_error = None
        db.add(SyncLog(mirror_id=mirror.id, status=SyncStatus.RUNNING.value, message="удаление"))
        planned.append({
            "id": mirror.id,
            "name": mirror.name,
            "distribution": mirror.distribution or "stable",
            "local_path": mirror.local_path,
        })
    await db.commit()
    db.expunge_all()

    async def worker(job):
        done = []
        failed = []
        for item in planned:
            job["line"] = "удаляю " + item["name"]
            set_mirror(item["id"])
            note("удаление " + item["name"])
            try:
                ok, msg = await deb_service.drop_mirror(item["name"], item["distribution"], cleanup=False)
                if not ok:
                    raise RuntimeError((msg or "aptly не удалил зеркало")[:500])
                _safe_rmtree(settings.STORAGE_ROOT / "public" / item["name"], settings.STORAGE_ROOT / "public")
                _safe_rmtree(Path(item["local_path"]), settings.MIRRORS_DIR)
                async with AsyncSessionLocal() as session:
                    await session.execute(sql_delete(SyncLog).where(SyncLog.mirror_id == item["id"]))
                    row = await session.get(Mirror, item["id"])
                    if row:
                        await session.delete(row)
                    await session.commit()
                done.append(item["name"])
            except Exception as exc:
                from datetime import datetime
                failed.append(item["name"])
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(Mirror)
                        .where(Mirror.id == item["id"])
                        .values(status=SyncStatus.FAILED.value, last_error=str(exc)[:500])
                    )
                    await session.execute(
                        update(SyncLog)
                        .where(SyncLog.mirror_id == item["id"], SyncLog.status == SyncStatus.RUNNING.value)
                        .values(status=SyncStatus.FAILED.value, finished_at=datetime.utcnow(), message="не прошла")
                    )
                    await session.commit()
        job["line"] = ("Удалено: " + ", ".join(done)) if done else "ничего не удалено"
        if failed:
            job["status"] = "failed"
            job["line"] = (job["line"] + ". Ошибка: " + ", ".join(failed))[:300]

    title = "Удаление " + (planned[0]["name"] if len(planned) == 1 else f"{len(planned)} зеркал")
    job = await enqueue(title, worker)
    if not job:
        for item in planned:
            await db.execute(update(Mirror).where(Mirror.id == item["id"]).values(status=SyncStatus.IDLE.value))
        await db.commit()
        raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
    return {"deleted": deleted, "started": [item["name"] for item in planned], "errors": errors, "job": job}


@router.get("/{mirror_id}", response_model=MirrorOut)
async def get_mirror(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    return mirror


@router.get("/{mirror_id}/estimate")
async def mirror_estimate(mirror_id: int, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    if mirror.type != "deb":
        return {"packages": None, "bytes": None, "error": ""}
    return await deb_service.estimate(
        mirror.source_url,
        mirror.distribution,
        mirror.components,
        mirror.architectures,
    )


@router.patch("/{mirror_id}", response_model=MirrorOut)
async def update_mirror(
    mirror_id: int,
    data: MirrorUpdate,
    db: AsyncSession = Depends(get_db),
):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

    payload = data.model_dump(exclude_unset=True)
    _check_fields(payload)
    if mirror.type == "deb" and any(key in payload for key in ("source_url", "distribution", "components", "architectures")):
        if mirror.status in (SyncStatus.RUNNING.value, "queued"):
            raise HTTPException(status_code=409, detail="Нельзя менять настройки DEB во время синхронизации")
        ok, msg = await deb_service.reconfigure(
            name=mirror.name,
            url=payload.get("source_url", mirror.source_url),
            distribution=payload.get("distribution", mirror.distribution) or "stable",
            components=payload.get("components", mirror.components) or "main",
            architectures=payload.get("architectures", mirror.architectures) or "amd64",
            old_distribution=mirror.distribution or "stable",
            old_components=mirror.components or "main",
        )
        if not ok:
            raise HTTPException(status_code=500, detail=f"aptly не принял настройки: {(msg or '')[:500]}")

    for field, value in payload.items():
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


@router.get("/{mirror_id}/client-file")
async def client_file(mirror_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    host = request.headers.get("host", "127.0.0.1").split(":")[0]
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


@router.post("/cleanup-orphans")
async def cleanup_orphans(db: AsyncSession = Depends(get_db)):
    """Убирает каталоги и зеркала aptly, которых нет в базе. Пул не чистит."""
    from app.models import LocalRepo
    from app.services.jobs import busy, enqueue

    if busy():
        raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
    active = await db.execute(select(Mirror).where(Mirror.status.in_([SyncStatus.RUNNING.value, "queued"])))
    if active.first():
        raise HTTPException(status_code=409, detail="Дождитесь окончания синхронизации")
    mirror_names = await db.execute(select(Mirror.name))
    local_names = await db.execute(select(LocalRepo.name))
    keep = {row[0] for row in mirror_names.all()} | {row[0] for row in local_names.all()}

    async def worker(job):
        job["line"] = "снимаю каталоги"
        removed, errors = await deb_service.drop_orphans(keep)
        if errors and not removed:
            raise RuntimeError(errors)
        job["line"] = ("Убрано: " + ", ".join(removed)) if removed else "Лишних каталогов нет"
        if errors:
            job["status"] = "failed"
            job["line"] = (job["line"] + ". " + errors)[:300]

    job = await enqueue("Каталоги без зеркал", worker)
    if not job:
        raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
    return job


@router.post("/cleanup-aptly")
async def cleanup_aptly(db: AsyncSession = Depends(get_db)):
    """Чистит пул aptly. Каталоги зеркал не удаляет."""
    from app.services.jobs import busy, enqueue

    if busy():
        raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
    active = await db.execute(select(Mirror).where(Mirror.status.in_([SyncStatus.RUNNING.value, "queued"])))
    if active.first():
        raise HTTPException(status_code=409, detail="Дождитесь окончания синхронизации")

    async def worker(job):
        job["line"] = "чищу пул"
        ok, msg = await deb_service.cleanup()
        if not ok:
            raise RuntimeError((msg or "очистка не удалась")[:300])
        job["line"] = "Пул почищен"

    job = await enqueue("Очистка пула", worker)
    if not job:
        raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
    return job


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

    if mirror.type == "deb":
        code, out, err = await deb_service.run_check([
            deb_service.aptly, "mirror", "show", "-with-packages", mirror.name
        ])
        if code != 0:
            busy = "database" in (err or out).lower() or "lock" in (err or out).lower() or "open database" in (err or out).lower()
            return {
                "mirror_id": mirror_id,
                "path": "aptly",
                "total": 0,
                "offset": offset,
                "limit": limit,
                "size_bytes": 0,
                "packages": [],
                "error": "база занята синхронизацией" if busy else (err or out)[:300],
            }
        names = []
        for line in out.splitlines():
            line = line.strip()
            if " " in line or "_" not in line:
                continue
            if _package_arch_ok(line, mirror.architectures):
                names.append(line)
        if q:
            names = [n for n in names if q.lower() in n.lower()]
        page = names[offset:offset + limit]
        return {
            "mirror_id": mirror_id,
            "path": "aptly:" + mirror.name,
            "total": len(names),
            "offset": offset,
            "limit": limit,
            "size_bytes": 0,
            "packages": [{"name": n, "size_bytes": 0, "relpath": n} for n in page],
        }

    root = Path(mirror.local_path)
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

    key = str(root)
    now = time.time()
    cached = _rpm_list_cache.get(key)
    if not cached or now - cached[0] >= 60:
        rows, size = await asyncio.get_running_loop().run_in_executor(None, _scan_rpms, root)
        _rpm_list_cache[key] = (now, rows, size)
    else:
        _, rows, size = cached
    matched = rows
    if q:
        matched = [item for item in rows if q.lower() in item["name"].lower()]
    page = matched[offset:offset + limit]
    return {
        "mirror_id": mirror_id,
        "path": str(root),
        "total": len(matched),
        "offset": offset,
        "limit": limit,
        "size_bytes": size,
        "packages": page,
    }


def _safe_rmtree(path: Path, root: Path):
    root_resolved = root.resolve()
    target = path.resolve()
    if target == root_resolved or root_resolved not in target.parents:
        raise HTTPException(status_code=400, detail="Небезопасный путь удаления")
    if target.exists() and target.is_dir():
        shutil.rmtree(target)


@router.delete("/{mirror_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mirror(
    mirror_id: int,
    delete_files: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    mirror = await db.get(Mirror, mirror_id)
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")

    from app.api.sync import _active, _queue, _queue_lock, _starting
    from app.services.procs import get_process

    process = get_process(mirror.id)
    if mirror.id in _starting or mirror.id in _active or (process is not None and process.returncode is None):
        raise HTTPException(status_code=409, detail="Нельзя удалить зеркало, пока идёт синхронизация")
    async with _queue_lock:
        if mirror.id in _queue:
            _queue.remove(mirror.id)

    if delete_files and mirror.type == "deb":
        from app.database import AsyncSessionLocal
        from app.services.jobs import busy, enqueue
        from app.services.procs import set_mirror, note

        if busy():
            raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
        mirror.status = SyncStatus.RUNNING.value
        mirror.last_error = None
        db.add(SyncLog(mirror_id=mirror.id, status=SyncStatus.RUNNING.value, message="удаление"))
        await db.commit()
        db.expunge_all()
        mirror_id = mirror.id
        mirror_name = mirror.name
        distribution = mirror.distribution or "stable"
        local_path = mirror.local_path

        async def worker(job):
            job["line"] = "удаляю файлы"
            set_mirror(mirror_id)
            note("удаление файлов")
            try:
                ok, msg = await deb_service.drop_mirror(mirror_name, distribution, cleanup=False)
                if not ok:
                    raise RuntimeError((msg or "aptly не удалил зеркало")[:500])
                _safe_rmtree(settings.STORAGE_ROOT / "public" / mirror_name, settings.STORAGE_ROOT / "public")
                _safe_rmtree(Path(local_path), settings.MIRRORS_DIR)
                async with AsyncSessionLocal() as session:
                    await session.execute(sql_delete(SyncLog).where(SyncLog.mirror_id == mirror_id))
                    row = await session.get(Mirror, mirror_id)
                    if row:
                        await session.delete(row)
                    await session.commit()
                job["line"] = "Файлы удалены. Пул aptly не уменьшился — меню, «Почистить пул»."
            except Exception as exc:
                from datetime import datetime
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(Mirror)
                        .where(Mirror.id == mirror_id)
                        .values(status=SyncStatus.FAILED.value, last_error=str(exc)[:500])
                    )
                    await session.execute(
                        update(SyncLog)
                        .where(SyncLog.mirror_id == mirror_id, SyncLog.status == SyncStatus.RUNNING.value)
                        .values(status=SyncStatus.FAILED.value, finished_at=datetime.utcnow(), message="не прошла")
                    )
                    await session.commit()
                raise

        job = await enqueue("Удаление " + mirror_name, worker)
        if not job:
            await db.execute(update(Mirror).where(Mirror.id == mirror_id).values(status=SyncStatus.IDLE.value))
            await db.commit()
            raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
        return JSONResponse(job, status_code=202)

    await db.execute(sql_delete(SyncLog).where(SyncLog.mirror_id == mirror.id))
    await db.flush()

    if delete_files:
        _safe_rmtree(Path(mirror.local_path), settings.MIRRORS_DIR)

    await db.delete(mirror)
    return None
