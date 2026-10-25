"""Runtime configuration, read from ``SHOAL_*`` environment variables."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from shoal.integrations.egress import DEFAULT_BLOCKED_NETWORKS


class RemoteIntegration(BaseModel):
    url: str
    token: SecretStr | None = None


class InstanceOIDC(BaseModel):
    """The instance-wide sign-in: one identity provider for everyone, whichever tenants they belong to."""

    issuer: str
    client_id: str
    client_secret: SecretStr | None = None
    display_name: str = "Single sign-on"
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    allowed_domains: list[str] = Field(default_factory=list, description="Email domains allowed to sign in (empty: any).")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOAL_", env_file=".env", extra="ignore")

    database_url: str = Field(
        "sqlite+aiosqlite:///./shoal.db",
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host/shoal",
    )
    database_direct_url: str | None = Field(
        None,
        description="Direct database URL (bypassing a pooler such as PgBouncer) for migrations and LISTEN/NOTIFY; "
        "defaults to database_url.",
    )
    database_read_url: str | None = Field(
        None, description="Read replica for run lists, dashboards and audit queries (they may lag the primary slightly)."
    )
    db_pool_size: int = Field(
        20, ge=1, description="Connections each process keeps open. Size it to worker_concurrency plus a few on workers."
    )
    db_max_overflow: int = Field(10, ge=0, description="Extra connections a process may open briefly under load.")
    db_pool_timeout_seconds: float = Field(10.0, gt=0, description="How long a request waits for a free connection.")
    db_pool_recycle_seconds: int = Field(1800, ge=0, description="Replace connections older than this (0 keeps them).")
    db_pgbouncer: bool = Field(
        False, description="The database URL points at PgBouncer in transaction mode: turn off prepared statement caches."
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
    admin_token: SecretStr | None = Field(None, description="Break-glass system admin bearer token (tenant management).")
    instance_oidc: InstanceOIDC | None = Field(
        None,
        description="Instance-wide sign-in (JSON: issuer, client_id, client_secret, ...). People sign in once and "
        "switch between the tenants they belong to.",
    )
    instance_oidc_client_secret: SecretStr | None = Field(
        None, description="Client secret for the instance sign-in, when it is not in SHOAL_INSTANCE_OIDC (e.g. from a Kubernetes Secret)."
    )
    instance_admins: list[str] = Field(
        default_factory=list,
        description="Emails that become instance admins when they sign in (bootstrap; manage the rest in the admin console).",
    )
    default_blueprints: bool = Field(
        True,
        description="Put the blueprints that ship with Shoal (GitHub PR review, Jira bugfix, Slack bot and approvals, "
        "AWS report, quality eval, ...) in every new tenant's catalogue.",
    )
    public_url: str = Field("http://localhost:8080", description="External base URL (webhook URLs, A2A cards).")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    static_dir: str | None = Field(None, description="Serve the built frontend from this directory.")
    remote_integrations: list[RemoteIntegration] = Field(
        default_factory=list,
        description="Operator-wide remote integrations (open integrations protocol) registered at start-up, "
        'e.g. SHOAL_REMOTE_INTEGRATIONS=\'[{"url": "https://acme-crm.internal", "token": "..."}]\'.',
    )
    integration_allow_imports: bool = Field(
        False,
        description="Let tenant admins register custom integrations as `module:Class` import paths. Off by default: "
        "they can still register installed packages (shoal.integrations entry points) and remote services.",
    )
    integration_blocked_networks: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BLOCKED_NETWORKS),
        description="CIDRs integrations may never call (connection URLs are tenant-set). Defaults to the link-local and "
        "cloud metadata ranges; add private ranges, e.g. 10.0.0.0/8, to keep tenants off this deployment's network.",
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
    worker_concurrency: int = Field(
        16, ge=1, description="Runs a worker advances at once. Agent steps mostly wait on models, so agent-lane "
        "workers can run far more (e.g. 200) than CPU-bound ones."
    )
    worker_lanes: list[str] = Field(
        default_factory=lambda: ["standard", "agent"],
        description="Which runs this worker takes: 'agent' (workflows that call models) and/or 'standard' (the rest). "
        "Separate pools keep short workflows from queueing behind long agent runs.",
    )
    poll_interval_seconds: float = Field(1.0, gt=0)
    lease_seconds: int = Field(60, ge=5)
    dispatch_queue: str = Field(
        "database",
        description="How workers hear that a run is due: database (they poll the runs table) or sqs; plugins add "
        "more (shoal.dispatch_queues). The database stays the source of truth either way (docs/dispatch-queues.md).",
    )
    dispatch_sweep_seconds: float = Field(
        15.0, gt=0, description="With an external queue, how often workers still look in the database for due runs "
        "(a lost message, a worker that died mid-run).",
    )
    dispatch_wait_seconds: float = Field(
        20.0, ge=0, le=20, description="How long a worker's receive waits for a message (SQS long polling)."
    )
    dispatch_sqs_queue_url: str | None = Field(None, description="Standard SQS queue for wake-ups of every lane.")
    dispatch_sqs_lane_queue_urls: dict[str, str] = Field(
        default_factory=dict,
        description='A queue per lane, e.g. {"agent": "https://sqs.eu-west-1.amazonaws.com/123456789012/shoal-agent"}, '
        "so separate worker pools only receive their own runs.",
    )
    dispatch_sqs_region: str | None = Field(None, description="Region to sign for (default: from the queue URL).")
    dispatch_sqs_endpoint: str | None = Field(
        None, description="SQS endpoint override: LocalStack (http://localhost:4566) or a VPC endpoint."
    )
    dispatch_sqs_fair_queues: bool = Field(
        True, description="Send each tenant's messages in their own message group (SQS fair queues)."
    )
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
    rate_limit_requests_per_second: float = Field(
        100.0, ge=0, description="API requests per second per tenant, per API process (0: unlimited). Over it: 429."
    )
    rate_limit_burst: int = Field(200, ge=0, description="Requests a tenant may burst above the rate.")
    api_max_concurrent_requests: int = Field(
        64, ge=0, description="Requests one API process handles at once (0: unlimited). Others wait for a slot."
    )
    api_queue_timeout_seconds: float = Field(
        1.0, ge=0, description="How long a request waits for a slot before the API answers 503 with Retry-After."
    )
    max_pending_runs_per_tenant: int = Field(
        10_000, ge=0, description="Runs waiting for a worker before the API refuses new ones with 429 (0: unlimited)."
    )
    max_concurrent_runs_per_tenant: int = Field(
        0, ge=0, description="Runs of one tenant that workers advance at once (0: unlimited); keeps tenants fair."
    )
    auth_cache_seconds: float = Field(
        5.0, ge=0, description="How long a process reuses an authenticated API key or session (0: check every request)."
    )
    webhook_processing: Literal["queue", "inline"] = Field(
        "queue",
        description="queue: verify, store and answer 202 at once, and workers process the delivery; "
        "inline: process it inside the request (the response then lists woken runs and fired triggers).",
    )
    webhook_rate_limit_per_second: float = Field(100.0, ge=0, description="Deliveries per second per webhook URL, per API process.")
    webhook_rate_limit_burst: int = Field(500, ge=0)
    quota_cache_seconds: float = Field(10.0, ge=0, description="How long processes cache a tenant's quotas.")
    model_call_lease_seconds: int = Field(
        900, ge=10, description="A gateway concurrency slot is reclaimed after this long if its worker died."
    )
    model_retries: int = Field(
        4, ge=0, le=20, description="Rounds over a model's candidates when all are rate limited or down, before the step fails."
    )
    model_retry_backoff_seconds: float = Field(2.0, ge=0, description="First wait between rounds; doubles each round.")
    model_retry_max_backoff_seconds: float = Field(60.0, ge=0, description="Longest wait between rounds (Retry-After is capped here).")
    default_sandbox_backend: str = "local"
    sandbox_reap_interval_seconds: int = Field(
        600, ge=0, description="How often workers delete sandboxes no live run owns (Kubernetes pods the ttl ended, or "
        "left by a crashed worker) and their NetworkPolicies (0: never).",
    )
    default_decision_provider: str = Field("llm", description="Decision provider for decide/grade steps: llm or jev.")

    assistant_enabled: bool = Field(True, description="Serve the chat assistant (POST /v1/assistant/chat).")
    assistant_gateway: str = Field(
        "anthropic", description="Gateway resource (tenant-wide) or provider plugin name."
    )
    assistant_model: str = Field("claude-opus-5-5", description="Model the assistant runs on.")
    assistant_effort: str = Field("medium", description="Reasoning effort for the assistant's model calls.")
    assistant_max_turns: int = Field(16, ge=1, le=100, description="Model calls per user message before it stops.")

    api_key_max_days: int = Field(
        0, ge=0, description="Longest an API key may live: keys created without an expiry get this one, longer ones "
        "are refused (0: no limit, keys may never expire).",
    )
    trusted_proxies: list[str] = Field(
        default_factory=list,
        description="Addresses or CIDRs of reverse proxies and load balancers whose X-Forwarded-For is believed, "
        "so the audit log records the real client address, e.g. SHOAL_TRUSTED_PROXIES='[\"10.0.0.0/8\"]'.",
    )

    @field_validator("trusted_proxies")
    @classmethod
    def _proxies_are_networks(cls, value: list[str]) -> list[str]:
        for proxy in value:
            ipaddress.ip_network(proxy, strict=False)  # raises on a typo at start-up, not on every request
        return value

    audit_retention_days: int = Field(400, ge=0, description="Delete audit entries older than this (0 keeps them forever).")
    run_event_retention_days: int = Field(
        400, ge=0, description="Delete run events older than this once their run has finished (0 keeps them forever)."
    )

    log_level: str = "INFO"
    log_json: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
