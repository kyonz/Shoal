"""Runtime configuration, read from ``SHOAL_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RemoteIntegration(BaseModel):
    url: str
    token: SecretStr | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOAL_", env_file=".env", extra="ignore")

    database_url: str = Field(
        "sqlite+aiosqlite:///./shoal.db",
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host/shoal",
    )
    secret_key: SecretStr = Field(
        SecretStr("dev-insecure-secret-key-change-me"),
        description="Key used to encrypt stored secrets and sign run identity tokens.",
    )
    admin_token: SecretStr | None = Field(None, description="System admin bearer token (tenant management).")
    public_url: str = Field("http://localhost:8080", description="External base URL (webhook URLs, A2A cards).")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    static_dir: str | None = Field(None, description="Serve the built frontend from this directory.")
    remote_integrations: list[RemoteIntegration] = Field(
        default_factory=list,
        description="Operator-wide remote integrations (open integrations protocol) registered at start-up, "
        'e.g. SHOAL_REMOTE_INTEGRATIONS=\'[{"url": "https://acme-crm.internal", "token": "..."}]\'.',
    )
    skill_source_cache_dir: str | None = Field(
        None, description="Where git skill sources keep their checkouts (default: a directory under the system temp dir)."
    )
    skill_source_allow_local: bool = Field(
        False, description="Let git skill sources read repositories from this server's disk (file:// URLs and paths)."
    )

    worker_id: str | None = None
    worker_concurrency: int = Field(16, ge=1)
    poll_interval_seconds: float = Field(1.0, gt=0)
    lease_seconds: int = Field(60, ge=5)
    default_sandbox_backend: str = "local"
    default_decision_provider: str = Field("llm", description="Decision provider for decide/grade steps: llm or jev.")

    assistant_enabled: bool = Field(True, description="Serve the chat assistant (POST /v1/assistant/chat).")
    assistant_gateway: str = Field(
        "anthropic", description="Gateway resource (tenant-wide) or provider plugin name."
    )
    assistant_model: str = Field("claude-opus-5-5", description="Model the assistant runs on.")
    assistant_effort: str = Field("medium", description="Reasoning effort for the assistant's model calls.")
    assistant_max_turns: int = Field(16, ge=1, le=100, description="Model calls per user message before it stops.")

    audit_retention_days: int = Field(400, ge=0, description="Delete audit entries older than this (0 keeps them forever).")

    log_level: str = "INFO"
    log_json: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
