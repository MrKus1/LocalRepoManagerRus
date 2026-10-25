import shutil
import subprocess
import tarfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.guards import tar_member_error
from app.models import Mirror, SyncStatus

router = APIRouter(prefix="/api/update", tags=["update"])

UPDATES = Path("/var/lib/repo-manager/updates")
INSTALL = Path("/opt/repo-manager")
BACKUP = Path("/opt/repo-manager.bak")
HELPER = "/usr/local/sbin/repo-manager-update"
ARCHIVE = UPDATES / "pending.tar.gz"


def current_version() -> str:
    path = INSTALL / "VERSION"
    return path.read_text().strip() if path.exists() else "0"


def parse_version(value: str) -> tuple:
    """1.2.9-1 и 1.2.8.16 сравниваются как числа, дефис не отбрасывает хвост."""
    parts = []
    buf = ""
    for char in (value or "").strip():
        if char.isdigit():
            buf += char
        elif buf:
            parts.append(int(buf))
            buf = ""
    if buf:
        parts.append(int(buf))
    return tuple(parts) or (0,)


def archive_version(path: Path) -> str:
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            err = tar_member_error(member.name)
            if err:
                raise ValueError(err)
            if member.issym() or member.islnk():
                raise ValueError(f"ссылки в архиве запрещены: {member.name}")
        member = tar.getmember("repo-manager/VERSION")
        data = tar.extractfile(member)
        if data is None:
            raise ValueError("VERSION пустой")
        return data.read().decode().strip()


@router.get("")
async def update_status():
    return {
        "current": current_version(),
        "has_backup": BACKUP.exists(),
        "helper": Path(HELPER).exists(),
        "pending": ARCHIVE.exists(),
    }


@router.get("/log")
async def update_log():
    path = Path("/var/lib/repo-manager/logs/update.log")
    if not path.exists():
        return {"lines": []}
    lines = path.read_text(errors="replace").splitlines()[-40:]
    return {"lines": lines}


@router.post("/upload")
async def upload_archive(file: UploadFile = File(...)):
    name = file.filename or ""
    if not name.endswith((".tar.gz", ".tgz")):
        raise HTTPException(status_code=400, detail="Нужен архив .tar.gz")
    UPDATES.mkdir(parents=True, exist_ok=True)
    tmp = UPDATES / "upload.tar.gz"
    with tmp.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    try:
        version = archive_version(tmp)
    except (tarfile.TarError, KeyError, ValueError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Архив не похож на Repo Manager: {exc}") from exc
    current = current_version()
    if parse_version(version) <= parse_version(current):
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"В архиве {version}, сейчас {current}. Нужна новее.")
    tmp.replace(ARCHIVE)
    return {"current": current, "archive": version}


@router.post("/apply")
async def apply_update(db: AsyncSession = Depends(get_db)):
    if not ARCHIVE.exists():
        raise HTTPException(status_code=400, detail="Сначала загрузите архив")
    if not Path(HELPER).exists():
        raise HTTPException(
            status_code=500,
            detail="Нет /usr/local/sbin/repo-manager-update. Один раз от root: bash /opt/repo-manager/scripts/repo-manager-update.sh install-helper",
        )
    running = await db.execute(select(Mirror).where(Mirror.status == SyncStatus.RUNNING.value))
    busy = running.scalars().first()
    if busy:
        raise HTTPException(status_code=409, detail=f"Идёт sync {busy.name}. Дождитесь окончания.")
    proc = subprocess.run(
        ["sudo", "-n", HELPER, "apply", str(ARCHIVE)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "sudo не смог запустить обновление").strip()
        raise HTTPException(status_code=500, detail=detail[:500])
    return {"message": "Обновление запущено"}


@router.post("/rollback")
async def rollback_update(db: AsyncSession = Depends(get_db)):
    if not BACKUP.exists():
        raise HTTPException(status_code=400, detail="Нет резервной копии")
    if not Path(HELPER).exists():
        raise HTTPException(status_code=500, detail="Нет helper. Установите scripts/repo-manager-update.sh")
    running = await db.execute(select(Mirror).where(Mirror.status == SyncStatus.RUNNING.value))
    if running.scalars().first():
        raise HTTPException(status_code=409, detail="Идёт sync, откат отменён")
    proc = subprocess.run(
        ["sudo", "-n", HELPER, "rollback"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "откат не запустился").strip()
        raise HTTPException(status_code=500, detail=detail[:500])
    return {"message": "Откат запущен"}
