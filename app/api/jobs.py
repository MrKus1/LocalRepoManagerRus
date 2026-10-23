from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Mirror, SyncLog
from app.services.jobs import build_tasks, list_jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
async def jobs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SyncLog, Mirror.name)
        .join(Mirror, Mirror.id == SyncLog.mirror_id)
        .order_by(SyncLog.started_at.desc())
        .limit(40)
    )
    waiting = await db.execute(select(Mirror).where(Mirror.status.in_(("running", "queued"))))
    return build_tasks(list_jobs(), result.all(), waiting.scalars().all())
