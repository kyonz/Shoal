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

from awm.core import clock

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
    """A custom role. Built-in roles live in code (awm.auth.permissions)."""

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
    initiator: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="Who drove this run (see awm.identity).")
    trigger_name: Mapped[str | None] = mapped_column(String(63), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(40), index=True)
    campaign_target_id: Mapped[str | None] = mapped_column(String(40))
    parent_run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    parent_step_id: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
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
