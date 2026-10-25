"""API request/response models (the public, versioned contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str | None = None
    details: dict[str, Any] | list[Any] | None = None


class TenantIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
    name: str | None = None


class TenantOut(Out):
    id: str
    slug: str
    name: str
    created_at: datetime


class TenantCreated(BaseModel):
    tenant: TenantOut
    api_key: str = Field(description="Admin API key for the new tenant. Shown once.")


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=lambda: ["read", "write", "run"])
    expires_at: datetime | None = None


class ApiKeyOut(Out):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_by: str | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(BaseModel):
    key: ApiKeyOut
    token: str = Field(description="The API key. Shown once; store it securely.")


class Me(BaseModel):
    tenant: TenantOut | None
    actor: str
    scopes: list[str]
    is_admin: bool


class ResourceOut(BaseModel):
    kind: str
    name: str
    version: int
    document: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    updated_by: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ApplyResult(BaseModel):
    kind: str
    name: str
    outcome: str
    version: int | None = None


class ResourceVersionOut(Out):
    version: int
    created_at: datetime
    created_by: str | None
    document: dict[str, Any]


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
    workflow_name: str
    workflow_version: int
    status: str
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None
    error: dict[str, Any] | None
    usage: dict[str, Any]
    labels: dict[str, Any]
    started_by: str | None
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


class RunList(BaseModel):
    items: list[RunOut]
    next_cursor: str | None


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


class CampaignLaunchIn(BaseModel):
    name: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict, description="Extra/override workflow input expressions.")
    start: bool = True


class CampaignInlineIn(BaseModel):
    name: str
    spec: dict[str, Any]
    start: bool = True


class CampaignRunOut(Out):
    id: str
    name: str
    spec: dict[str, Any]
    status: str
    stats: dict[str, Any]
    error: dict[str, Any] | None
    created_by: str | None
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


class SecretIn(BaseModel):
    value: str = Field(min_length=1)


class SecretOut(Out):
    name: str
    created_at: datetime
    updated_at: datetime


class ActionCallIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


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
    data: dict[str, Any]
    created_at: datetime
