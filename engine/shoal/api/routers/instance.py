"""The instance: tenant switching for people, and tenant and account management for instance admins.

Instance admins (and auditors, read-only) are accounts with an ``instance_role``.
The ``SHOAL_ADMIN_TOKEN`` stays as a break-glass credential with the same rights.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from shoal.api.deps import SESSION_COOKIE, CtxDep, InstanceReaderCtxDep, SystemCtxDep
from shoal.api.schemas import (
    AccountDetail,
    AccountMembership,
    AccountOut,
    AccountPatch,
    AuditOut,
    MyTenant,
    SessionOut,
    SwitchTenantIn,
    TenantMemberIn,
    TenantMemberOut,
    TenantOut,
    TenantPatch,
    TenantSummary,
)
from shoal.auth.accounts import INSTANCE_ADMIN
from shoal.auth.sessions import issue_session
from shoal.core.errors import Forbidden, InvalidRequest, NotFound
from shoal.store import tables as t

router = APIRouter()


async def _tenant(ctx: Any, slug: str) -> t.Tenant:
    tenant = await ctx.engine.store.get_tenant_by_slug(slug)
    if tenant is None:
        raise NotFound(f"tenant '{slug}' not found")
    return tenant


# --------------------------------------------------------------------------- the caller's tenants


@router.get("/v1/me/tenants", tags=["me"], response_model=list[MyTenant], summary="Tenants you can switch to")
async def my_tenants(ctx: CtxDep) -> Any:
    p, engine = ctx.principal, ctx.engine
    if p.account_id is None:  # API keys and the admin token are bound to one tenant (or none)
        tenant = await engine.store.get_tenant(p.tenant_id) if p.tenant_id else None
        return [MyTenant(slug=tenant.slug, name=tenant.name, status=tenant.status, member=True, current=True)] if tenant else []
    out = {
        m.tenant.id: MyTenant(slug=m.tenant.slug, name=m.tenant.name, status=m.tenant.status, member=True,
                              current=m.tenant.id == p.tenant_id)
        for m in await engine.accounts.memberships(p.account_id)
    }
    if p.instance_role:
        for tenant in await engine.store.list_tenants():
            out.setdefault(tenant.id, MyTenant(slug=tenant.slug, name=tenant.name, status=tenant.status, member=False,
                                               current=tenant.id == p.tenant_id))
    return sorted(out.values(), key=lambda m: (not m.member, m.name.lower()))


@router.post("/v1/me/tenant", tags=["me"], response_model=SessionOut, summary="Switch your session to another tenant")
async def switch_tenant(body: SwitchTenantIn, request: Request, response: Response, ctx: CtxDep) -> Any:
    p, engine = ctx.principal, ctx.engine
    if p.account_id is None:
        raise InvalidRequest("only signed-in people can switch tenants; API keys belong to one tenant")
    tenant, user_id = None, None
    if body.tenant is not None:
        tenant = await _tenant(ctx, body.tenant)
        user = await engine.accounts.membership(p.account_id, tenant.id)
        if user is None and not p.instance_role:
            raise Forbidden(f"you are not a member of tenant '{body.tenant}'")
        if tenant.status == "suspended" and not p.instance_role:
            raise Forbidden(f"tenant '{body.tenant}' is suspended")
        user_id = user.id if user else None
    elif not p.instance_role:
        raise InvalidRequest("choose a tenant")
    session = issue_session(engine.settings, tenant_id=tenant.id if tenant else None, user_id=user_id,
                            account_id=p.account_id)
    await engine.store.audit(tenant.id if tenant else None, p.actor, "auth.tenant_switched", tenant.slug if tenant else None,
                             member=user_id is not None)
    response.set_cookie(SESSION_COOKIE, session, httponly=True, secure=engine.settings.public_url.startswith("https://"),
                        samesite="lax", max_age=12 * 3600, path="/")
    return SessionOut(session=session, tenant=TenantOut.model_validate(tenant) if tenant else None)


# --------------------------------------------------------------------------- tenants (instance admins)


@router.get("/v1/instance/tenants", tags=["instance"], response_model=list[TenantSummary],
            summary="Every tenant with people, namespace counts and federation")
async def tenant_summaries(ctx: InstanceReaderCtxDep) -> Any:
    store = ctx.engine.store
    async with store.session() as s:
        users = dict((await s.execute(select(t.User.tenant_id, func.count()).where(t.User.kind == "user", t.User.active.is_(True))
                                      .group_by(t.User.tenant_id))).all())
        namespaces = dict((await s.execute(select(t.Namespace.tenant_id, func.count()).group_by(t.Namespace.tenant_id))).all())
        federated = set((await s.execute(select(t.FederationLink.tenant_id).where(t.FederationLink.status != "revoked"))).scalars())
    return [
        TenantSummary(**TenantOut.model_validate(tenant).model_dump(), users=users.get(tenant.id, 0),
                      namespaces=namespaces.get(tenant.id, 0), federated=tenant.id in federated)
        for tenant in await store.list_tenants()
    ]


@router.patch("/v1/tenants/{slug}", tags=["tenants"], response_model=TenantOut, summary="Rename, describe, suspend or resume a tenant")
async def patch_tenant(slug: str, body: TenantPatch, ctx: SystemCtxDep) -> Any:
    tenant = await _tenant(ctx, slug)
    values = body.model_dump(exclude_unset=True)
    async with ctx.engine.store.session() as s:
        row = await s.get(t.Tenant, tenant.id)
        for key, value in values.items():
            if value is not None or key == "description":
                setattr(row, key, value)
    action = {"suspended": "tenant.suspended", "active": "tenant.resumed"}.get(values.get("status") or "", "tenant.updated")
    await ctx.engine.store.audit(None, ctx.actor, action, slug, **{k: v for k, v in values.items() if k != "description"})
    return await ctx.engine.store.get_tenant(tenant.id)


@router.get("/v1/tenants/{slug}/members", tags=["tenants"], response_model=list[TenantMemberOut],
            summary="People with tenant-wide roles in a tenant")
async def tenant_members(slug: str, ctx: InstanceReaderCtxDep) -> Any:
    tenant = await _tenant(ctx, slug)
    directory = ctx.engine.directory
    out = []
    users, _ = await directory.list_users(tenant.id, kind="user", limit=10000)
    for user in users:
        roles = sorted({b.role for b in await directory.bindings_for(tenant.id, user.id) if b.namespace is None})
        out.append(TenantMemberOut(user_id=user.id, username=user.username, email=user.email, display_name=user.display_name,
                                   roles=roles, linked=user.account_id is not None))
    return out


@router.post("/v1/tenants/{slug}/members", tags=["tenants"], response_model=TenantMemberOut, status_code=201,
             summary="Give someone a role in a tenant (invites them when they have not signed in yet)")
async def add_tenant_member(slug: str, body: TenantMemberIn, ctx: SystemCtxDep) -> Any:
    tenant = await _tenant(ctx, slug)
    user = await ctx.engine.accounts.add_to_tenant(tenant.id, body.email, body.role, actor=ctx.actor)
    await ctx.engine.store.audit(tenant.id, ctx.actor, "role_binding.created", body.email, role=body.role, via="instance")
    roles = sorted({b.role for b in await ctx.engine.directory.bindings_for(tenant.id, user.id) if b.namespace is None})
    user = await ctx.engine.directory.get_user(tenant.id, user.id)
    return TenantMemberOut(user_id=user.id, username=user.username, email=user.email, display_name=user.display_name,
                           roles=roles, linked=user.account_id is not None)


# --------------------------------------------------------------------------- accounts (instance admins)


@router.get("/v1/instance/accounts", tags=["instance"], response_model=list[AccountOut], summary="People who have signed in")
async def list_accounts(ctx: InstanceReaderCtxDep, q: str | None = None, instance_role: str | None = None) -> Any:
    accounts = await ctx.engine.accounts.list(query=q)
    return [a for a in accounts if instance_role is None or a.instance_role == instance_role]


async def _detail(ctx: Any, account: t.Account) -> AccountDetail:
    tenants = []
    for m in await ctx.engine.accounts.memberships(account.id):
        roles = sorted({b.role for b in await ctx.engine.directory.bindings_for(m.tenant.id, m.user.id) if b.namespace is None})
        tenants.append(AccountMembership(tenant=m.tenant.slug, tenant_name=m.tenant.name, username=m.user.username, roles=roles))
    return AccountDetail(**AccountOut.model_validate(account).model_dump(), tenants=tenants)


@router.get("/v1/instance/accounts/{account_id}", tags=["instance"], response_model=AccountDetail)
async def get_account(account_id: str, ctx: InstanceReaderCtxDep) -> Any:
    return await _detail(ctx, await ctx.engine.accounts.get(account_id))


@router.patch("/v1/instance/accounts/{account_id}", tags=["instance"], response_model=AccountDetail,
              summary="Make someone an instance admin or auditor, or deactivate them everywhere")
async def patch_account(account_id: str, body: AccountPatch, ctx: SystemCtxDep) -> Any:
    values = body.model_dump(exclude_unset=True)
    if account_id == ctx.principal.account_id and ("instance_role" in values or values.get("active") is False):
        raise Forbidden("you cannot change your own instance role or deactivate yourself; ask another instance admin")
    account = await ctx.engine.accounts.get(account_id)
    if account.instance_role == INSTANCE_ADMIN and (values.get("instance_role", INSTANCE_ADMIN) != INSTANCE_ADMIN
                                                    or values.get("active") is False):
        admins = [a for a in await ctx.engine.accounts.list() if a.instance_role == INSTANCE_ADMIN and a.active]
        if len(admins) <= 1 and not ctx.engine.settings.admin_token:
            raise Forbidden("this is the last instance admin; make someone else an instance admin first")
    account = await ctx.engine.accounts.update(account_id, **values)
    await ctx.engine.store.audit(None, ctx.actor, "account.updated", account.email or account.id, **values)
    return await _detail(ctx, account)


@router.get("/v1/instance/audit", tags=["instance"], response_model=list[AuditOut],
            summary="Instance-level audit: tenants created, suspended or deleted, instance roles, instance sign-ins")
async def instance_audit(ctx: InstanceReaderCtxDep, limit: int = 100, action: str | None = None, before: int | None = None) -> Any:
    return await ctx.engine.store.list_audit(None, min(limit, 1000), action=action, before=before)
