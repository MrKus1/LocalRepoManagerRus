import shutil
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import LocalRepo, Package
from app.schemas import LocalRepoCreate, LocalRepoOut, PackageOut
from app.config import settings
from app.services.deb import deb_service
from app.services.rpm import rpm_service

router = APIRouter(prefix="/api", tags=["local-repos"])


# ========== Local Repos ==========

@router.get("/local-repos/", response_model=List[LocalRepoOut])
async def list_local_repos(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(LocalRepo).order_by(LocalRepo.name))
    return result.scalars().all()


@router.post("/local-repos/", response_model=LocalRepoOut, status_code=201)
async def create_local_repo(data: LocalRepoCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(LocalRepo).where(LocalRepo.name == data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Репозиторий с таким именем уже существует")

    local_path = settings.LOCAL_REPOS_DIR / data.type / data.name
    local_path.mkdir(parents=True, exist_ok=True)

    repo = LocalRepo(
        name=data.name,
        type=data.type,
        description=data.description,
        local_path=str(local_path),
        distribution=data.distribution,
        component=data.component or "main",
        architectures=data.architectures or "amd64",
        gpg_key_id=data.gpg_key_id,
    )

    if data.type == "deb":
        ok, msg = await deb_service.create_local_repo(data.name)
        if not ok:
            raise HTTPException(status_code=500, detail=f"aptly error: {msg}")
    else:
        ok, msg = await rpm_service.create_local_repo(str(local_path))
        if not ok:
            raise HTTPException(status_code=500, detail=f"createrepo error: {msg}")

    db.add(repo)
    await db.flush()
    await db.refresh(repo)
    return repo


@router.get("/local-repos/{repo_id}", response_model=LocalRepoOut)
async def get_local_repo(repo_id: int, db: AsyncSession = Depends(get_db)):
    repo = await db.get(LocalRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")
    return repo


@router.get("/local-repos/{repo_id}/contents")
async def local_repo_contents(
    repo_id: int,
    q: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    repo = await db.get(LocalRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    root = Path(repo.local_path)
    suffix = ".rpm" if repo.type == "rpm" else ".deb"
    matched = []
    size = 0
    if root.exists():
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
        "repo_id": repo_id,
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


@router.delete("/local-repos/{repo_id}", status_code=204)
async def delete_local_repo(
    repo_id: int,
    delete_files: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    repo = await db.get(LocalRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    result = await db.execute(select(Package).where(Package.repo_id == repo.id))
    for pkg in result.scalars().all():
        await db.delete(pkg)

    if delete_files:
        path = Path(repo.local_path)
        if path.exists() and path.is_dir():
            shutil.rmtree(path)

    await db.delete(repo)
    return None


# ========== Packages ==========

@router.get("/local-repos/{repo_id}/packages", response_model=List[PackageOut])
async def list_packages(repo_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Package).where(Package.repo_id == repo_id).order_by(Package.name)
    )
    return result.scalars().all()


@router.post("/local-repos/{repo_id}/packages", response_model=PackageOut, status_code=201)
async def upload_package(
    repo_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    repo = await db.get(LocalRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")

    filename = file.filename or "unknown"
    if repo.type == "deb" and not filename.endswith(".deb"):
        raise HTTPException(status_code=400, detail="Ожидается .deb файл")
    if repo.type == "rpm" and not filename.endswith(".rpm"):
        raise HTTPException(status_code=400, detail="Ожидается .rpm файл")

    # Сохраняем во временную папку
    upload_dir = settings.UPLOADS_DIR
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / filename

    content = await file.read()
    with open(temp_path, "wb") as f:
        f.write(content)

    size = len(content)

    # Добавляем в репозиторий
    if repo.type == "deb":
        ok, msg = await deb_service.add_package_to_repo(repo.name, str(temp_path))
        if not ok:
            temp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"aptly add failed: {msg}")
        # Публикуем (упрощённо)
        if repo.distribution:
            await deb_service.publish_repo(
                repo_name=repo.name,
                distribution=repo.distribution,
                component=repo.component or "main",
                architectures=repo.architectures or "amd64",
            )
    else:
        ok, msg = await rpm_service.add_package(repo.local_path, str(temp_path))
        if not ok:
            temp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"rpm add failed: {msg}")

    # Парсим имя/версию грубо (можно улучшить через python-debian / rpm)
    name_part = filename.rsplit(".", 1)[0]
    # Пример: package_1.2.3-1_amd64.deb или package-1.2.3-1.el10.x86_64.rpm
    name = name_part
    version = "unknown"
    arch = "noarch"

    pkg = Package(
        filename=filename,
        name=name,
        version=version,
        architecture=arch,
        size_bytes=size,
        repo_id=repo.id,
    )
    db.add(pkg)
    await db.flush()
    await db.refresh(pkg)

    # Удаляем временный файл (для rpm он уже скопирован)
    if repo.type == "deb":
        temp_path.unlink(missing_ok=True)

    return pkg
