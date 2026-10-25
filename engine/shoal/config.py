"""Runtime configuration, read from ``SHOAL_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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
    secret_providers: list[str] = Field(
        default_factory=lambda: ["db", "env"],
        description="Secret backends (shoal.secrets plugins) in lookup order; the first writable one stores new secrets.",
    )
    secret_env_shared: bool = Field(
        True,
        description="Let the env secret provider fall back to SHOAL_SECRET_<NAME>, which every tenant can read; "
        "turn off on instances shared by several organisations (SHOAL_SECRET_<TENANT>__<NAME> always works).",
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

    oidc_issuer: str | None = Field(
        None,
        description="Issuer of Shoal's workload identity tokens (defaults to public_url). Cloud providers fetch "
        "<issuer>/.well-known/openid-configuration; a private deployment can publish those two documents "
        "on public HTTPS (e.g. an S3 bucket) and set the issuer to that URL.",
    )
    identity_signing_key: SecretStr | None = Field(
        None, description="PEM EC P-256 private key for workload identity tokens; derived from secret_key when unset."
    )
    aws_ambient: bool = Field(
        False,
        description="Let CloudIdentities with trust 'ambient' assume roles with this deployment's own AWS identity "
        "(env, IRSA, EKS Pod Identity, ECS or instance role).",
    )
    aws_deployment_role_arn: str | None = Field(
        None, description="This deployment's own IAM role, shown in generated trust policies for 'ambient' identities."
    )
    aws_sts_endpoint: str | None = Field(
        None, description="STS endpoint override (VPC endpoint, FIPS); defaults to https://sts.<region>.amazonaws.com."
    )
    aws_audience: str = Field("sts.amazonaws.com", description="Audience of web identity tokens presented to AWS STS.")

    worker_id: str | None = None
    worker_concurrency: int = Field(16, ge=1)
    poll_interval_seconds: float = Field(1.0, gt=0)
    lease_seconds: int = Field(60, ge=5)
    orchestrator: str = Field(
        "builtin", description="What drives runs: builtin (the database) or temporal (needs the temporal extra)."
    )
    temporal_address: str = Field("localhost:7233", description="Temporal frontend host:port.")
    temporal_namespace: str = "default"
    temporal_task_queue: str = "shoal"
    temporal_api_key: SecretStr | None = Field(None, description="API key (Temporal Cloud); implies TLS.")
    temporal_tls: bool = False
    temporal_tls_cert_path: str | None = Field(None, description="Client certificate for mTLS.")
    temporal_tls_key_path: str | None = None
    temporal_tls_ca_path: str | None = Field(None, description="CA bundle for the server certificate.")
    temporal_pass_timeout_seconds: int = Field(
        24 * 3600, ge=60, description="Longest pass over a run (one activity); bounds the longest single step."
    )
    temporal_reconcile_after_seconds: int = Field(
        60, ge=5, description="Signal a run's workflow again when the run has been due this long without a pass."
    )
    temporal_build_id: str | None = Field(None, description="Turns on Temporal worker versioning with this build id.")
    temporal_deployment_name: str | None = Field(None, description="Worker deployment name (default: the task queue).")
    temporal_versioning_behavior: Literal["auto-upgrade", "pinned"] = "auto-upgrade"
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
