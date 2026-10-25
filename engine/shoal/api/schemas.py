"""API request/response models (the public, versioned contract).

Conventions, chosen so the API maps one-to-one onto Terraform resources:

* Declarative resources are addressed by ``/v1/namespaces/{namespace}/{plural}/{name}``;
  ``PUT`` creates or replaces (idempotent), ``GET`` returns the same shape
  plus server fields under ``metadata``, ``DELETE`` removes.
* Directory objects that the server names (users, groups, role bindings, API
  keys) are created with ``POST`` and addressed by ``id`` afterwards; objects
  that people name (namespaces, roles, service accounts, OIDC providers,
  secrets) use ``PUT`` by name.
* Write-only values (secret values, OIDC client secrets, API key tokens) are
  never returned after creation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from shoal.runtime.orchestrators import OrchestratorSetting

NAME = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str | None = None
    details: dict[str, Any] | list[Any] | None = None


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


# --------------------------------------------------------------------------- tenants & identity


class TenantIn(BaseModel):
    slug: str = Field(pattern=NAME)
    name: str | None = None
    description: str | None = None
    admin_email: str | None = Field(None, description="Optional first human admin (bound to tenant-admin).")


class TenantOut(Out):
    id: str
    slug: str
    name: str
    description: str | None = None
    status: str = "active"
    created_at: datetime


class TenantPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    status: Literal["active", "suspended"] | None = Field(
        None, description="Suspended tenants refuse every call except from instance admins; their data is kept."
    )


class TenantSummary(TenantOut):
    users: int = Field(description="Active people (not service accounts).")
    namespaces: int
    federated: bool = Field(description="Managed by a member deployment through federation.")


class MyTenant(BaseModel):
    slug: str
    name: str
    status: str
    member: bool = Field(description="False when an instance admin or auditor can open it without being a member.")
    current: bool


class SwitchTenantIn(BaseModel):
    tenant: str | None = Field(description="Tenant slug; null leaves every tenant (instance admins only).")


class SessionOut(BaseModel):
    session: str = Field(description="The new session token (also set as the session cookie).")
    tenant: TenantOut | None


class AccountOut(Out):
    id: str
    email: str | None
    display_name: str
    instance_role: str | None
    active: bool
    oidc_issuer: str
    created_at: datetime
    last_login_at: datetime | None


class AccountMembership(BaseModel):
    tenant: str
    tenant_name: str
    username: str
    roles: list[str]


class AccountDetail(AccountOut):
    tenants: list[AccountMembership]


class AccountPatch(BaseModel):
    instance_role: Literal["instance-admin", "instance-auditor"] | None = Field(
        None, description="Set to grant; send null to remove (use the field explicitly)."
    )
    active: bool | None = None


class TenantMemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field("tenant-admin", description="A role in that tenant, granted tenant-wide.")


class TenantMemberOut(BaseModel):
    user_id: str
    username: str
    email: str | None
    display_name: str
    roles: list[str]
    linked: bool = Field(description="Linked to an instance account (has signed in through the instance or SSO).")


class TenantCreated(BaseModel):
    tenant: TenantOut
    api_key: str = Field(description="Key for the tenant's bootstrap 'admin' service account. Shown once.")


class Me(BaseModel):
    tenant: TenantOut | None
    actor: str
    kind: str
    user_id: str | None
    display_name: str | None
    email: str | None
    is_system_admin: bool
    account_id: str | None = None
    instance_role: str | None = None
    namespaces: list[str] = Field(description="Namespaces visible here; for a federated tenant ask /v1/me/permissions, which the member answers.")
    federated: bool = Field(False, description="The tenant's data lives in a member deployment that this hub forwards to.")


class Permissions(BaseModel):
    tenant: list[str]
    namespaces: dict[str, list[str]]


# --------------------------------------------------------------------------- namespaces


class NamespaceIn(BaseModel):
    display_name: str | None = None
    description: str | None = None


class NamespaceOut(Out):
    name: str
    display_name: str
    description: str | None
    created_at: datetime
    created_by: str | None


class MemberOut(BaseModel):
    binding_id: str
    role: str
    subject_kind: str
    subject_id: str
    subject_name: str
    namespace: str | None


# --------------------------------------------------------------------------- users, groups, service accounts


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    email: str | None = None
    display_name: str | None = None
    active: bool = True


class UserPatch(BaseModel):
    email: str | None = None
    display_name: str | None = None
    active: bool | None = None


class UserOut(Out):
    id: str
    kind: str
    username: str
    email: str | None
    display_name: str
    active: bool
    external_id: str | None
    oidc_subject: str | None
    created_at: datetime
    last_login_at: datetime | None
    groups: list[str] = Field(default_factory=list)


class ServiceAccountIn(BaseModel):
    display_name: str | None = None
    description: str | None = None


class ServiceAccountOut(BaseModel):
    id: str
    name: str
    display_name: str
    description: str | None
    created_at: datetime


class GroupIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    members: list[str] = Field(default_factory=list, description="User ids.")


class GroupPatch(BaseModel):
    display_name: str | None = None


class GroupMembersIn(BaseModel):
    members: list[str]


class GroupOut(Out):
    id: str
    display_name: str
    source: str
    external_id: str | None
    created_at: datetime
    members: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- roles & bindings


class PermissionOut(BaseModel):
    name: str
    description: str
    scope: Literal["tenant", "namespace"]


class RoleIn(BaseModel):
    description: str | None = None
    permissions: list[str] = Field(min_length=1)


class RoleOut(BaseModel):
    name: str
    description: str
    permissions: list[str]
    builtin: bool


class RoleBindingIn(BaseModel):
    role: str
    subject_kind: Literal["user", "group", "hub_user", "hub_group"] = Field(description="hub_user and hub_group name people and groups from a federation hub (email or service actor, group name).")
    subject_id: str
    namespace: str | None = Field(None, description="Omit for a tenant-wide binding.")


class RoleBindingOut(Out):
    id: str
    role: str
    subject_kind: str
    subject_id: str
    namespace: str | None
    created_at: datetime
    created_by: str | None


# --------------------------------------------------------------------------- auth configuration


class OIDCProviderIn(BaseModel):
    display_name: str | None = None
    issuer: str
    client_id: str
    client_secret: str | None = Field(None, description="Write-only. Omit for public (PKCE-only) clients.")
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    groups_claim: str | None = Field(None, description="ID token claim with group names, e.g. 'groups'.")
    allowed_domains: list[str] = Field(default_factory=list)
    auto_provision: bool = True
    default_role: str | None = None
    default_namespace: str | None = None
    enabled: bool = True


class OIDCProviderOut(Out):
    name: str
    display_name: str
    issuer: str
    client_id: str
    has_client_secret: bool
    scopes: list[str]
    groups_claim: str | None
    allowed_domains: list[str]
    auto_provision: bool
    default_role: str | None
    default_namespace: str | None
    enabled: bool
    created_at: datetime


class LoginProvider(BaseModel):
    name: str
    display_name: str
    login_url: str


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    service_account: str | None = Field(None, description="Create the key for this service account instead of yourself.")
    scopes: list[str] = Field(default_factory=lambda: ["*"], description="Permission patterns the key is limited to.")
    namespaces: list[str] = Field(default_factory=list, description="Namespaces the key is limited to (empty: all).")
    expires_at: datetime | None = None


class ApiKeyOut(Out):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    namespaces: list[str]
    owner_id: str | None
    created_by: str | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(BaseModel):
    key: ApiKeyOut
    token: str = Field(description="The API key. Shown once; store it securely.")


# --------------------------------------------------------------------------- declarative resources


class ResourceMetadataIn(BaseModel):
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] | None = Field(
        None, description="Free-form tool metadata (e.g. 'ui.shoal.dev/layout'). Not versioned; omit to keep the current value."
    )


class ResourceIn(BaseModel):
    """Body of ``PUT /v1/namespaces/{ns}/{plural}/{name}`` (name and namespace come from the path)."""

    metadata: ResourceMetadataIn = Field(default_factory=ResourceMetadataIn)
    spec: dict[str, Any]


class ResourceMetadataOut(BaseModel):
    name: str
    namespace: str
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    version: int
    created_at: datetime
    updated_at: datetime
    updated_by: str | None = None


class ResourceOut(BaseModel):
    api_version: str = Field("shoal.dev/v1", alias="apiVersion")
    kind: str
    metadata: ResourceMetadataOut
    spec: dict[str, Any]
    status: dict[str, Any] = Field(default_factory=dict, description="Server-computed details, e.g. a connection's webhook URL.")

    model_config = ConfigDict(populate_by_name=True)


class ApplyResult(BaseModel):
    kind: str
    name: str
    namespace: str
    outcome: Literal["created", "updated", "unchanged", "invalid"]
    version: int | None = None
    error: str | None = None


class ResourceVersionOut(Out):
    version: int
    created_at: datetime
    created_by: str | None
    message: str | None = None
    document: dict[str, Any]
    source: dict[str, Any] | None = Field(
        None, description="The spec as the client wrote it, without server defaults (what rollback restores)."
    )


class RollbackIn(BaseModel):
    version: int = Field(ge=1, description="Version to restore; it becomes a new, current version.")
    message: str | None = Field(None, description="Change note; defaults to 'Roll back to version N'.")
    dry_run: bool = Field(False, description="Return the diff without changing anything.")


class VersionDiffOut(BaseModel):
    kind: str
    name: str
    from_version: int
    to_version: int
    changes: list[dict[str, Any]] = Field(description="[{op: add|remove|replace|reorder, path, from?, to?}] (JSON-pointer paths).")


class SecretIn(BaseModel):
    value: str = Field(min_length=1, description="Write-only.")


class SecretOut(Out):
    name: str
    namespace: str
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- runs


class StartRunIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class StepOut(Out):
    step_id: str
    type: str
    status: str
    attempt: int
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    usage: dict[str, Any]
    wake_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    state: dict[str, Any] = Field(default_factory=dict)


class RunOut(Out):
    id: str
    namespace: str
    workflow_name: str
    workflow_version: int
    status: str
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None
    error: dict[str, Any] | None
    usage: dict[str, Any]
    labels: dict[str, Any]
    started_by: str | None
    initiator: dict[str, Any]
    trigger_name: str | None
    campaign_id: str | None
    parent_run_id: str | None
    next_wake_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunDetail(RunOut):
    workflow: dict[str, Any]
    steps: list[StepOut]
    context: dict[str, Any]


class EventOut(Out):
    id: int
    run_id: str
    step_id: str | None
    type: str
    data: dict[str, Any]
    created_at: datetime


class ApprovalIn(BaseModel):
    approved: bool
    comment: str | None = None


class SignalIn(BaseModel):
    payload: Any = Field(None, description="Any JSON value; a waiting step outputs it as `payload`.")


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    source: str = Field(description="api, callback or event.")
    sender: str
    payload: Any = None
    created_at: datetime
    consumed_by: str | None = Field(None, description="The wait step that took it; null while queued.")
    consumed_at: datetime | None = None

    @classmethod
    def of(cls, row: Any) -> SignalOut:
        return cls(
            id=row.id, name=row.name, source=row.source, sender=row.sender, payload=(row.data or {}).get("payload"),
            created_at=row.created_at, consumed_by=row.consumed_by, consumed_at=row.consumed_at,
        )


# --------------------------------------------------------------------------- campaigns


class CampaignLaunchIn(BaseModel):
    name: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict, description="Extra/override workflow input expressions.")
    start: bool = True


class CampaignInlineIn(BaseModel):
    name: str = Field(pattern=NAME)
    spec: dict[str, Any]
    start: bool = True


class CampaignRunOut(Out):
    id: str
    namespace: str
    name: str
    spec: dict[str, Any]
    status: str
    stats: dict[str, Any]
    error: dict[str, Any] | None
    created_by: str | None
    initiator: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TargetOut(Out):
    id: str
    key: str
    data: dict[str, Any]
    status: str
    run_id: str | None
    attempts: int
    result: dict[str, Any] | None
    cost_usd: float
    started_at: datetime | None
    finished_at: datetime | None


class AddTargetsIn(BaseModel):
    items: list[dict[str, Any]]


# --------------------------------------------------------------------------- connections & triggers


class ActionCallIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class QueryIn(BaseModel):
    filter: dict[str, Any] = Field(default_factory=dict)
    cursor: str | None = None
    limit: int = Field(100, ge=1, le=1000)


class TriggerTestIn(BaseModel):
    event_type: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class DeliveryOut(Out):
    id: str
    dedupe_key: str | None
    event_type: str
    status: str
    run_id: str | None
    detail: dict[str, Any]
    created_at: datetime


class AuditOut(Out):
    id: int
    actor: str | None
    action: str
    target: str | None
    namespace: str | None = None
    outcome: str = "success"
    source: str | None = None
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    data: dict[str, Any]
    created_at: datetime


# --------------------------------------------------------------------------- orchestrator (instance console)


class OrchestratorEnvironment(BaseModel):
    """What SHOAL_ORCHESTRATOR and SHOAL_TEMPORAL_* say (used while nothing is saved in the console)."""

    orchestrator: str
    temporal_address: str
    temporal_namespace: str
    temporal_task_queue: str
    temporal_tls: bool = Field(description="TLS, an API key or client certificates are configured.")


class OrchestratorStatus(BaseModel):
    source: Literal["environment", "console"] = Field(description="Where the orchestrator for new runs comes from.")
    orchestrator: str = Field(description="What new runs start on.")
    driving: list[str] = Field(description="Every orchestrator this server drives runs with.")
    setting: OrchestratorSetting | None = Field(None, description="What the instance console saved.")
    environment: OrchestratorEnvironment
    runs: dict[str, int] = Field(description="Unfinished runs per orchestrator, across every tenant.")
    temporal_installed: bool = Field(description="The temporal extra is installed on this server.")
    error: str | None = Field(None, description="Why the saved setting could not be applied (e.g. a deleted secret).")
    revision: int | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None


class TemporalCheck(BaseModel):
    ok: bool
    detail: str
    server_version: str | None = None
    pollers: int | None = Field(None, description="Workers polling the task queue right now.")
