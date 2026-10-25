"""Runtime configuration, read from ``AWM_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AWM_", env_file=".env", extra="ignore")

    database_url: str = Field(
        "sqlite+aiosqlite:///./awm.db",
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host/awm",
    )
    secret_key: SecretStr = Field(
        SecretStr("dev-insecure-secret-key-change-me"),
        description="Key used to encrypt stored secrets and sign run identity tokens.",
    )
    admin_token: SecretStr | None = Field(None, description="System admin bearer token (tenant management).")
    public_url: str = Field("http://localhost:8080", description="External base URL (webhook URLs, A2A cards).")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    static_dir: str | None = Field(None, description="Serve the built frontend from this directory.")

    worker_id: str | None = None
    worker_concurrency: int = Field(16, ge=1)
    poll_interval_seconds: float = Field(1.0, gt=0)
    lease_seconds: int = Field(60, ge=5)
    default_sandbox_backend: str = "local"

    log_level: str = "INFO"
    log_json: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
