from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Основной путь хранения
    STORAGE_ROOT: Path = Path("/var/lib/repo-manager")
    
    # Поддиректории
    MIRRORS_DIR: Path = STORAGE_ROOT / "mirrors"
    LOCAL_REPOS_DIR: Path = STORAGE_ROOT / "local"
    UPLOADS_DIR: Path = STORAGE_ROOT / "uploads"
    LOGS_DIR: Path = STORAGE_ROOT / "logs"
    
    # PostgreSQL
    POSTGRES_USER: str = "repoman"
    POSTGRES_PASSWORD: str = "changeme"
    POSTGRES_HOST: str = "127.0.0.1"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "repo_manager"
    
    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )
    
    # Aptly
    APTLY_BIN: str = "aptly"
    APTLY_CONFIG: Path = Path("/etc/aptly.conf")
    
    # RPM tools
    REPOSYNC_BIN: str = "reposync"
    CREATEREPO_BIN: str = "createrepo_c"
    
    # Web
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = True
    
    # Прокси для синхронизации. Пусто = напрямую.
    PROXY_URL: str = ""
    NO_PROXY: str = "127.0.0.1,localhost"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
