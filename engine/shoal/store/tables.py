"""SQLAlchemy tables. Every tenant-owned row carries ``tenant_id``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from shoal.core import clock

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return clock.now()


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes on every backend (SQLite returns naive values)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType, datetime: UTCDateTime}


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Namespace(Base):
    __tablename__ = "namespaces"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(63))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(200))


class User(Base):
    """A human (OIDC / SCIM) or a service account (owns API keys for automation)."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username"),
        Index("ix_users_subject", "tenant_id", "oidc_issuer", "oidc_subject"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="user")  # user | service
    username: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200), doc="SCIM externalId from the IdP.")
    oidc_issuer: Mapped[str | None] = mapped_column(String(500))
    oidc_subject: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column()


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("tenant_id", "display_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20), default="local")  # local | scim | oidc
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class Role(Base):
    """A custom role. Built-in roles live in code (shoal.auth.permissions)."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(63))
    description: Mapped[str | None] = mapped_column(Text)
    permissions: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class RoleBinding(Base):
    """Grants a role to a user or group, tenant-wide (namespace NULL) or in one namespace."""

    __tablename__ = "role_bindings"
    __table_args__ = (UniqueConstraint("tenant_id", "role", "subject_kind", "subject_id", "namespace"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(63))
    subject_kind: Mapped[str] = mapped_column(String(20))  # user | group
    subject_id: Mapped[str] = mapped_column(String(40), index=True)
    namespace: Mapped[str | None] = mapped_column(String(63), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(200))


class OIDCProvider(Base):
    __tablename__ = "oidc_providers"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(63))
    display_name: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[str] = mapped_column(String(300))
    client_secret: Mapped[str | None] = mapped_column(Text, doc="Encrypted with the secrets cipher.")
    scopes: Mapped[list[Any]] = mapped_column(default=lambda: ["openid", "email", "profile"])
    groups_claim: Mapped[str | None] = mapped_column(String(100))
    allowed_domains: Mapped[list[Any]] = mapped_column(default=list)
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=True)
    default_role: Mapped[str | None] = mapped_column(String(63))
    default_namespace: Mapped[str | None] = mapped_column(String(63))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LoginState(Base):
    """Pending OIDC authorization requests (state, nonce, PKCE verifier)."""

    __tablename__ = "login_states"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # the `state` parameter
    tenant_id: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(63))
    nonce: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    redirect_to: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(32), unique=True)
    hash: Mapped[str] = mapped_column(String(128))
    scopes: Mapped[list[Any]] = mapped_column(default=list, doc="Permission patterns the key is limited to.")
    owner_id: Mapped[str | None] = mapped_column(String(40), index=True, doc="User or service account the key acts as.")
    namespaces: Mapped[list[Any]] = mapped_column(default=list, doc="Namespaces the key is limited to (empty: all).")
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column()
    last_used_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()


class Resource(Base):
    """Current version of a declarative resource (Workflow, Agent, ...)."""

    __tablename__ = "resources"
    __table_args__ = (UniqueConstraint("tenant_id", "namespace", "kind", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default")
    kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(63))
    version: Mapped[int] = mapped_column(Integer, default=1)
    document: Mapped[dict[str, Any]] = mapped_column(doc="Validated, normalized document the engine runs.")
    source: Mapped[dict[str, Any] | None] = mapped_column(doc="The spec exactly as submitted (returned by the API).")
    annotations: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Unversioned tool metadata (UI layout, ...).")
    labels: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(200))


class ResourceVersion(Base):
    __tablename__ = "resource_versions"
    __table_args__ = (UniqueConstraint("resource_id", "version"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column()
    source: Mapped[dict[str, Any] | None] = mapped_column(doc="Spec as written, restored on rollback.")
    message: Mapped[str | None] = mapped_column(Text, doc="Why this version was made (change note).")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(200))


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_claim", "status", "next_wake_at"),
        Index("ix_runs_tenant_created", "tenant_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default", index=True)
    workflow_name: Mapped[str] = mapped_column(String(63), index=True)
    workflow_version: Mapped[int] = mapped_column(Integer)
    workflow: Mapped[dict[str, Any]] = mapped_column(doc="Snapshot of the workflow spec for this run.")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    outputs: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[dict[str, Any] | None] = mapped_column()
    context: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="trigger / campaign / target context.")
    state: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Runtime scratch (sandboxes, ...).")
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    labels: Mapped[dict[str, Any]] = mapped_column(default=dict)
    started_by: Mapped[str | None] = mapped_column(String(200))
    initiator: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Who drove this run (see shoal.identity).")
    trigger_name: Mapped[str | None] = mapped_column(String(63), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(40), index=True)
    campaign_target_id: Mapped[str | None] = mapped_column(String(40))
    parent_run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    parent_step_id: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    orchestrator: Mapped[str] = mapped_column(
        String(20), default="builtin", server_default="builtin", doc="What drives this run (see shoal.runtime.orchestrator)."
    )
    next_wake_at: Mapped[datetime | None] = mapped_column()
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class StepRun(Base):
    __tablename__ = "step_runs"
    __table_args__ = (UniqueConstraint("run_id", "step_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    step_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[dict[str, Any] | None] = mapped_column()
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    wake_at: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class WaitSubscription(Base):
    """Event keys that wake a waiting step early (e.g. ``github:pr:acme/api#12``)."""

    __tablename__ = "wait_subscriptions"
    __table_args__ = (Index("ix_wait_key", "tenant_id", "key"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(500))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(String(64))


class RunSignal(Base):
    """Data sent into a run (API, callback URL or integration event), buffered until a wait step takes it."""

    __tablename__ = "run_signals"
    __table_args__ = (Index("ix_run_signals_run_name", "run_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {"payload": ...}
    source: Mapped[str] = mapped_column(String(20))  # api, callback or event
    sender: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    consumed_by: Mapped[str | None] = mapped_column(String(64))  # step id
    consumed_at: Mapped[datetime | None] = mapped_column()


class UserCredential(Base):
    """A person's own credential for one connection (token or OAuth), encrypted; used when runs act for them."""

    __tablename__ = "user_credentials"
    __table_args__ = (UniqueConstraint("tenant_id", "owner", "namespace", "connection"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(String(200))  # actor, e.g. user:dana
    namespace: Mapped[str] = mapped_column(String(63))
    connection: Mapped[str] = mapped_column(String(63))
    kind: Mapped[str] = mapped_column(String(20), default="token")  # token or oauth
    ciphertext: Mapped[str] = mapped_column(Text)  # encrypted JSON: connection field -> value (+ OAuth refresh data)
    fields: Mapped[list[Any]] = mapped_column(default=list)  # field names held (not values)
    expires_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (UniqueConstraint("tenant_id", "namespace", "connection_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default")
    connection_name: Mapped[str] = mapped_column(String(63))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_received_at: Mapped[datetime | None] = mapped_column()


class TriggerState(Base):
    __tablename__ = "trigger_state"
    __table_args__ = (UniqueConstraint("tenant_id", "namespace", "trigger_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default")
    trigger_name: Mapped[str] = mapped_column(String(63))
    next_fire_at: Mapped[datetime | None] = mapped_column(index=True)
    last_fired_at: Mapped[datetime | None] = mapped_column()
    fire_count: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[dict[str, Any]] = mapped_column(default=dict)
    lease_expires_at: Mapped[datetime | None] = mapped_column()


class TriggerDelivery(Base):
    __tablename__ = "trigger_deliveries"
    __table_args__ = (UniqueConstraint("tenant_id", "namespace", "trigger_name", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default")
    trigger_name: Mapped[str] = mapped_column(String(63), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(500))
    event_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))  # fired | filtered | duplicate | error
    run_id: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CampaignRow(Base):
    __tablename__ = "campaigns"
    __table_args__ = (Index("ix_campaign_status", "status"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default", index=True)
    name: Mapped[str] = mapped_column(String(63))
    spec: Mapped[dict[str, Any]] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="draft")
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(200))
    initiator: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    next_tick_at: Mapped[datetime | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column()


class CampaignTarget(Base):
    __tablename__ = "campaign_targets"
    __table_args__ = (UniqueConstraint("campaign_id", "key"), Index("ix_target_status", "campaign_id", "status"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    run_id: Mapped[str | None] = mapped_column(String(40))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column()
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Secret(Base):
    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("tenant_id", "namespace", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default")
    name: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class BlueprintAdoption(Base):
    """A blueprint rendered into a namespace; owns the resources it created."""

    __tablename__ = "blueprint_adoptions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), index=True)
    blueprint: Mapped[str] = mapped_column(String(63), index=True)
    blueprint_version: Mapped[str] = mapped_column(String(40))
    prefix: Mapped[str] = mapped_column(String(40), default="")
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    resources: Mapped[list[Any]] = mapped_column(default=list, doc="[{kind, name}] it applied.")
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditEntry(Base):
    """Append-only record of who did what. Written for every mutating API call,
    sign-in, denied request and governance decision."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_id", "tenant_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[str | None] = mapped_column(String(40), index=True)
    actor: Mapped[str | None] = mapped_column(String(200), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target: Mapped[str | None] = mapped_column(String(300))
    namespace: Mapped[str | None] = mapped_column(String(63))
    outcome: Mapped[str] = mapped_column(String(20), default="success")  # success | denied | error
    source: Mapped[str | None] = mapped_column(String(20))  # api | ui | scim | hub | system | engine
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class EvalRun(Base):
    """One execution of an Eval over one or more variants (workflow version + input overrides).

    Scores are recorded per resource version so versions can be compared and gated.
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_eval_runs_eval", "tenant_id", "namespace", "eval_name", "created_at"),
        Index("ix_eval_runs_status", "status", "next_tick_at"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63), default="default")
    eval_name: Mapped[str] = mapped_column(String(63))
    eval_version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(doc="Snapshot of the eval spec for this run.")
    workflow_name: Mapped[str] = mapped_column(String(63))
    workflow_version: Mapped[int] = mapped_column(Integer, doc="Workflow version of the first variant.")
    variants: Mapped[list[Any]] = mapped_column(
        default=list, doc="[{name, workflow_version, inputs, versions: {'Kind/name': version}}]."
    )
    mode: Mapped[str] = mapped_column(String(20), default="cases")  # cases | runs
    status: Mapped[str] = mapped_column(String(20), default="running")
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    gate: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Gate settings and, once finished, its verdict.")
    baseline_id: Mapped[str | None] = mapped_column(String(40))
    exports: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Sink -> export status.")
    error: Mapped[dict[str, Any] | None] = mapped_column()
    labels: Mapped[dict[str, Any]] = mapped_column(default=dict)
    started_by: Mapped[str | None] = mapped_column(String(200))
    initiator: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column()
    next_tick_at: Mapped[datetime | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column()


class EvalResult(Base):
    """One case of one variant of an eval run: the workflow run it produced and every grader's score."""

    __tablename__ = "eval_results"
    __table_args__ = (
        UniqueConstraint("eval_run_id", "variant", "case_name"),
        Index("ix_eval_results_status", "eval_run_id", "status"),
        Index("ix_eval_results_version", "tenant_id", "workflow_name", "workflow_version"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    variant: Mapped[str] = mapped_column(String(100), default="default")
    workflow_name: Mapped[str] = mapped_column(String(63))
    workflow_version: Mapped[int] = mapped_column(Integer)
    case_name: Mapped[str] = mapped_column(String(300))
    position: Mapped[int] = mapped_column(Integer, default=0, doc="Order of the case in the dataset.")
    case: Mapped[dict[str, Any]] = mapped_column(default=dict)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | running | graded | error | cancelled
    outputs: Mapped[dict[str, Any] | None] = mapped_column()
    scores: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Grader -> {score, passed, detail}.")
    score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[dict[str, Any] | None] = mapped_column()
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column()


# --------------------------------------------------------------------------- federation (architecture §11.2)


class FederationLink(Base):
    """Hub side: a federated tenant whose data and execution live in a member deployment."""

    __tablename__ = "federation_links"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | active | revoked
    requested_mode: Mapped[str] = mapped_column(String(20), default="read_write", doc="What the hub asks for.")
    member_mode: Mapped[str | None] = mapped_column(String(20), doc="The member's ceiling (read | read_write).")
    namespaces: Mapped[list[Any]] = mapped_column(default=list, doc="Namespaces the member exposes (empty: all).")
    visibility: Mapped[str] = mapped_column(String(20), default="full")  # full | metadata
    enrollment_hash: Mapped[str | None] = mapped_column(String(128))
    enrollment_expires_at: Mapped[datetime | None] = mapped_column()
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    member: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Version, URL, health and rollups the member reports.")
    last_seen_at: Mapped[datetime | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class FederationRequest(Base):
    """Hub side: an API call queued for a member's tenant connector to pick up and answer."""

    __tablename__ = "federation_requests"
    __table_args__ = (Index("ix_federation_requests_link_status", "link_id", "status"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    link_id: Mapped[str] = mapped_column(ForeignKey("federation_links.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | claimed | done
    request: Mapped[dict[str, Any]] = mapped_column()
    response: Mapped[dict[str, Any] | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column()


class FederationMembership(Base):
    """Member side: this deployment's tenant is managed from a hub."""

    __tablename__ = "federation_memberships"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), unique=True)
    hub_url: Mapped[str] = mapped_column(String(500))
    link_id: Mapped[str] = mapped_column(String(40))
    secret_ciphertext: Mapped[str] = mapped_column(Text)
    max_mode: Mapped[str] = mapped_column(String(20), default="read")
    namespaces: Mapped[list[Any]] = mapped_column(default=list)
    visibility: Mapped[str] = mapped_column(String(20), default="full")
    hub_mode: Mapped[str | None] = mapped_column(String(20), doc="Effective mode last agreed with the hub.")
    last_connected_at: Mapped[datetime | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
