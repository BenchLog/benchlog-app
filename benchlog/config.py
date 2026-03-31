from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "BENCHLOG_"}

    secret_key: str = "change-me"
    database_url: str = "postgresql+asyncpg://benchlog:benchlog@localhost/benchlog"
    base_url: str = "http://localhost:8000"

    # Auth (single-user Phase 1)
    username: str = "admin"
    password: str = "admin"

    # Storage
    storage_backend: str = "local"
    storage_local_path: str = "./data/files"
    storage_s3_bucket: str = ""
    storage_s3_endpoint: str = ""
    storage_s3_access_key: str = ""
    storage_s3_secret_key: str = ""
    storage_s3_region: str = ""

    # Uploads
    max_upload_size: int = 500 * 1024 * 1024  # 500MB
    allowed_extensions: str = "*"

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_local_path)


settings = Settings()
