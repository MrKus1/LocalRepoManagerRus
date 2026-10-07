from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict


# ========== Mirror ==========

class MirrorBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern="^(deb|rpm)$")
    description: Optional[str] = None
    source_url: str
    distribution: Optional[str] = None
    components: Optional[str] = None
    architectures: Optional[str] = "amd64"
    repoid: Optional[str] = None
    schedule: Optional[str] = None
    enabled: bool = True


class MirrorCreate(MirrorBase):
    pass


class MirrorUpdate(BaseModel):
    description: Optional[str] = None
    source_url: Optional[str] = None
    distribution: Optional[str] = None
    components: Optional[str] = None
    architectures: Optional[str] = None
    repoid: Optional[str] = None
    schedule: Optional[str] = None
    enabled: Optional[bool] = None


class MirrorOut(MirrorBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    local_path: str
    status: str
    last_sync: Optional[datetime] = None
    last_error: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# ========== LocalRepo ==========

class LocalRepoBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern="^(deb|rpm|files)$")
    description: Optional[str] = None
    distribution: Optional[str] = None
    component: Optional[str] = "main"
    architectures: Optional[str] = "amd64"
    gpg_key_id: Optional[str] = None


class LocalRepoCreate(LocalRepoBase):
    pass


class LocalRepoOut(LocalRepoBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    local_path: str
    created_at: datetime
    updated_at: datetime


# ========== Package ==========

class PackageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    name: str
    version: str
    architecture: str
    size_bytes: int
    repo_id: int
    uploaded_at: datetime


# ========== Sync ==========

class SyncRequest(BaseModel):
    force: bool = False


class SyncLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mirror_id: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str
    message: Optional[str] = None
