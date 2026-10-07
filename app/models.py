from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import String, Text, Boolean, DateTime, Integer, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RepoType(str, Enum):
    DEB = "deb"
    RPM = "rpm"


class SyncStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class Mirror(Base):
    __tablename__ = "mirrors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(10))  # deb / rpm
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Источник
    source_url: Mapped[str] = mapped_column(String(500))
    
    # Deb-specific
    distribution: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # jammy, bookworm...
    components: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)   # main,universe
    architectures: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # amd64,arm64
    
    # RPM-specific
    repoid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # для reposync
    
    # Локальный путь
    local_path: Mapped[str] = mapped_column(String(500))
    
    # Расписание (cron)
    schedule: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # "0 3 * * *"
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Статус
    status: Mapped[str] = mapped_column(String(20), default=SyncStatus.IDLE.value)
    last_sync: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Mirror {self.name} ({self.type})>"


class LocalRepo(Base):
    """Локальные репозитории для публикации своих пакетов"""
    __tablename__ = "local_repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(10))  # deb / rpm
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Путь
    local_path: Mapped[str] = mapped_column(String(500))
    
    # Deb-specific
    distribution: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    component: Mapped[Optional[str]] = mapped_column(String(50), default="main")
    architectures: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    
    # GPG (опционально)
    gpg_key_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    packages: Mapped[list["Package"]] = relationship("Package", back_populates="repo")

    def __repr__(self):
        return f"<LocalRepo {self.name} ({self.type})>"


class Package(Base):
    """Загруженные свои пакеты"""
    __tablename__ = "packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(150), index=True)
    version: Mapped[str] = mapped_column(String(100))
    architecture: Mapped[str] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(Integer)
    
    repo_id: Mapped[int] = mapped_column(ForeignKey("local_repos.id"))
    repo: Mapped["LocalRepo"] = relationship("LocalRepo", back_populates="packages")
    
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Package {self.name}-{self.version}>"


class SyncLog(Base):
    """Логи синхронизаций"""
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    mirror_id: Mapped[int] = mapped_column(ForeignKey("mirrors.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
