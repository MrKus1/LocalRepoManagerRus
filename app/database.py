from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.guards import fk_needs_rebuild

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
    pool_size=5,
    max_overflow=5,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE IF EXISTS mirrors ALTER COLUMN size_bytes TYPE BIGINT"))
        await conn.execute(text("ALTER TABLE IF EXISTS packages ALTER COLUMN size_bytes TYPE BIGINT"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_sync_logs_mirror_id ON sync_logs (mirror_id)"
        ))
        row = await conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'sync_logs_mirror_id_fkey'"
        ))
        if fk_needs_rebuild(row.scalar()):
            await conn.execute(text("ALTER TABLE sync_logs DROP CONSTRAINT IF EXISTS sync_logs_mirror_id_fkey"))
            await conn.execute(text(
                "ALTER TABLE sync_logs ADD CONSTRAINT sync_logs_mirror_id_fkey "
                "FOREIGN KEY (mirror_id) REFERENCES mirrors(id) ON DELETE CASCADE"
            ))
