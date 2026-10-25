"""Federated tenants (architecture §11.2).

Hub side: operators link a tenant to a member deployment (``/v1/federation/links``);
the member enrolls with the one-time token and its tenant connector long-polls
``/v1/federation/tunnel``. Member side: tenant admins join a hub and set what it
may do (``/v1/federation/membership``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from shoal.api.deps import CtxDep, EngineDep, SystemCtxDep, _bearer
from shoal.core.errors import NotFound, Unauthorized
from shoal.federation import protocol as p
from shoal.store import tables as t

router = APIRouter(prefix="/v1/federation", tags=["federation"])

Mode = Literal["read", "read_write"]
Visibility = Literal["full", "metadata"]


# --------------------------------------------------------------------------- hub


class LinkIn(BaseModel):
    tenant: str = Field(description="Slug of an empty tenant whose data will live in a member deployment.")
    mode: Mode = Field("read_write", description="What the hub asks for; the member's ceiling can lower it.")


class LinkPatch(BaseModel):
    mode: Mode


class LinkOut(BaseModel):
    id: str
    tenant: str
    status: str = Field(description="pending (waiting for enrollment), active or revoked.")
    connected: bool
    requested_mode: Mode
    member_mode: Mode | None = Field(None, description="The member's ceiling.")
    effective_mode: Mode
    namespaces: list[str] = Field(description="Namespaces the member exposes (empty: all).")
    visibility: Visibility
    member: dict[str, Any] = Field(description="Version, URL and rollups the member reports.")
    last_seen_at: datetime | None
    created_at: datetime


class LinkCreated(BaseModel):
    link: LinkOut
    enrollment_token: str = Field(description="Single use; give it to the member's admin for `shoal federation join`.")
    enrollment_expires_at: datetime


class EnrollIn(BaseModel):
    token: str
    max_mode: Mode
    namespaces: list[str] = Field(default_factory=list)
    visibility: Visibility = "full"
    member: dict[str, Any] = Field(default_factory=dict)


class TunnelIn(BaseModel):
    responses: list[dict[str, Any]] = Field(default_factory=list)
    wait: float = Field(20, ge=0, le=25)
    limit: int = Field(20, ge=0, le=100, description="0 only delivers responses.")
    report: dict[str, Any] = Field(default_factory=dict)


async def _link_out(engine: Any, link: t.FederationLink) -> LinkOut:
    tenant = await engine.store.get_tenant(link.tenant_id)
    return LinkOut(
        id=link.id, tenant=tenant.slug, status=link.status, connected=engine.federation.connected(link),
        requested_mode=link.requested_mode, member_mode=link.member_mode,
        effective_mode=p.effective_mode(link.requested_mode, link.member_mode), namespaces=link.namespaces or [],
        visibility=link.visibility, member=link.member or {}, last_seen_at=link.last_seen_at, created_at=link.created_at,
    )


async def _tenant_id(engine: Any, slug: str) -> str:
    tenant = await engine.store.get_tenant_by_slug(slug)
    if tenant is None:
        raise NotFound(f"tenant '{slug}' not found")
    return tenant.id


@router.post("/links", status_code=201, response_model=LinkCreated, summary="Federate a tenant (operator)")
async def create_link(body: LinkIn, ctx: SystemCtxDep) -> Any:
    tenant_id = await _tenant_id(ctx.engine, body.tenant)
    link, token = await ctx.engine.federation.create_link(tenant_id, mode=body.mode, actor=ctx.actor)
    await ctx.engine.store.audit(tenant_id, ctx.actor, "federation.linked", link.id, mode=body.mode)
    return LinkCreated(link=await _link_out(ctx.engine, link), enrollment_token=token, enrollment_expires_at=link.enrollment_expires_at)


@router.get("/links", response_model=list[LinkOut])
async def list_links(ctx: SystemCtxDep) -> Any:
    return [await _link_out(ctx.engine, link) for link in await ctx.engine.federation.links()]


@router.get("/links/{tenant}", response_model=LinkOut)
async def get_link(tenant: str, ctx: SystemCtxDep) -> Any:
    link = await ctx.engine.federation.link_for_tenant(await _tenant_id(ctx.engine, tenant))
    if link is None:
        raise NotFound(f"tenant '{tenant}' is not federated")
    return await _link_out(ctx.engine, link)


@router.patch("/links/{tenant}", response_model=LinkOut, summary="Change the access the hub asks for")
async def patch_link(tenant: str, body: LinkPatch, ctx: SystemCtxDep) -> Any:
    tenant_id = await _tenant_id(ctx.engine, tenant)
    link = await ctx.engine.federation.set_mode(tenant_id, body.mode)
    await ctx.engine.store.audit(tenant_id, ctx.actor, "federation.mode_changed", link.id, mode=body.mode)
    return await _link_out(ctx.engine, link)


@router.post("/links/{tenant}/enrollment", response_model=LinkCreated, summary="Issue a new enrollment token")
async def reissue(tenant: str, ctx: SystemCtxDep) -> Any:
    tenant_id = await _tenant_id(ctx.engine, tenant)
    link, token = await ctx.engine.federation.reissue_enrollment(tenant_id)
    await ctx.engine.store.audit(tenant_id, ctx.actor, "federation.enrollment_issued", link.id)
    return LinkCreated(link=await _link_out(ctx.engine, link), enrollment_token=token, enrollment_expires_at=link.enrollment_expires_at)


@router.delete("/links/{tenant}", status_code=204, summary="Revoke the link; the member keeps running on its own")
async def revoke(tenant: str, ctx: SystemCtxDep) -> Response:
    tenant_id = await _tenant_id(ctx.engine, tenant)
    link = await ctx.engine.federation.revoke(tenant_id)
    await ctx.engine.store.audit(tenant_id, ctx.actor, "federation.revoked", link.id)
    return Response(status_code=204)


@router.post("/enroll", summary="Member enrollment (authenticated by the one-time token)")
async def enroll(body: EnrollIn, engine: EngineDep) -> dict[str, Any]:
    return await engine.federation.enroll(
        body.token, max_mode=body.max_mode, namespaces=body.namespaces, visibility=body.visibility, member=body.member
    )


@router.post("/tunnel", summary="Tenant connector long-poll (authenticated by the link credential)")
async def tunnel(body: TunnelIn, request: Request, engine: EngineDep) -> dict[str, Any]:
    token = _bearer(request)
    if not token:
        raise Unauthorized("missing link credential")
    link = await engine.federation.authenticate_link(token)
    return await engine.federation.tunnel(link, responses=body.responses, wait=body.wait, limit=body.limit, report=body.report)


# --------------------------------------------------------------------------- member


class GrantIn(BaseModel):
    role: str
    subject_kind: Literal["hub_user", "hub_group"]
    subject_id: str = Field(description="Hub user email (or service actor such as 'sa:terraform') or hub group name.")
    namespace: str | None = None


class JoinIn(BaseModel):
    hub_url: str
    token: str = Field(description="Enrollment token from the hub operator.")
    max_mode: Mode = Field("read", description="The most this member lets the hub do.")
    namespaces: list[str] = Field(default_factory=list, description="Namespaces exposed to the hub (empty: all).")
    visibility: Visibility = Field("full", description="'metadata' hides run inputs, outputs and step data from the hub.")
    grants: list[GrantIn] = Field(default_factory=list, description="Role bindings for hub people and groups, made on join.")


class MembershipPatch(BaseModel):
    max_mode: Mode | None = None
    namespaces: list[str] | None = None
    visibility: Visibility | None = None


class MembershipOut(BaseModel):
    hub_url: str
    link_id: str
    max_mode: Mode
    hub_mode: Mode | None = Field(None, description="What the hub currently asks for.")
    effective_mode: Mode
    namespaces: list[str]
    visibility: Visibility
    last_connected_at: datetime | None
    created_at: datetime


def _membership_out(row: t.FederationMembership) -> MembershipOut:
    return MembershipOut(
        hub_url=row.hub_url, link_id=row.link_id, max_mode=row.max_mode, hub_mode=row.hub_mode,
        effective_mode=p.effective_mode(row.hub_mode or "read", row.max_mode), namespaces=row.namespaces or [],
        visibility=row.visibility, last_connected_at=row.last_connected_at, created_at=row.created_at,
    )


@router.put("/membership", status_code=201, response_model=MembershipOut, summary="Join a federation hub")
async def join(body: JoinIn, ctx: CtxDep) -> Any:
    await ctx.require("federation:manage")
    row = await ctx.engine.federation_member.join(
        ctx.tenant_id, body.hub_url, body.token, max_mode=body.max_mode, namespaces=body.namespaces,
        visibility=body.visibility, grants=[g.model_dump() for g in body.grants], actor=ctx.actor,
    )
    await ctx.audit("federation.joined", body.hub_url, max_mode=body.max_mode, namespaces=body.namespaces)
    return _membership_out(row)


@router.get("/membership", response_model=MembershipOut)
async def get_membership(ctx: CtxDep) -> Any:
    await ctx.require("federation:manage")
    row = await ctx.engine.federation_member.membership(ctx.tenant_id)
    if row is None:
        raise NotFound("this tenant is not a federation member")
    return _membership_out(row)


@router.patch("/membership", response_model=MembershipOut, summary="Change what the hub may do")
async def patch_membership(body: MembershipPatch, ctx: CtxDep) -> Any:
    await ctx.require("federation:manage")
    row = await ctx.engine.federation_member.update(ctx.tenant_id, **body.model_dump())
    await ctx.audit("federation.ceiling_changed", row.hub_url, **body.model_dump(exclude_none=True))
    return _membership_out(row)


@router.delete("/membership", status_code=204, summary="Leave the hub; this deployment keeps running on its own")
async def leave(ctx: CtxDep) -> Response:
    await ctx.require("federation:manage")
    await ctx.engine.federation_member.leave(ctx.tenant_id)
    await ctx.audit("federation.left")
    return Response(status_code=204)
