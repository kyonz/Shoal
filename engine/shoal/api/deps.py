"""Request authentication and the per-request context."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from shoal.auth.accounts import INSTANCE_ADMIN, INSTANCE_ROLES
from shoal.auth.rbac import Principal
from shoal.auth.sessions import SESSION_PREFIX, is_revoked, verify_session
from shoal.core.errors import Forbidden, InvalidRequest, NotFound, Unauthorized
from shoal.core.requestctx import current_request
from shoal.engine import Engine
from shoal.federation.member import LINK_HEADER
from shoal.federation.protocol import ASSERTION_HEADER

SESSION_COOKIE = "shoal_session"


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


EngineDep = Annotated[Engine, Depends(get_engine)]


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(SESSION_COOKIE)


async def authenticate(request: Request, engine: Engine) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    assertion = request.headers.get(ASSERTION_HEADER)
    if assertion:  # forwarded by this member's federation hub, through the tenant connector
        principal, claims = await engine.federation_member.principal_for(
            request.headers.get(LINK_HEADER, ""), assertion, method=request.method, path=request.url.path
        )
        info = current_request()
        if info is not None:
            info.source, info.via = "hub", f"hub:{claims.get('rid')}"
        return principal
    token = _bearer(request)
    if not token:
        raise Unauthorized("missing credentials: send 'Authorization: Bearer <api key or session token>'")
    settings = engine.settings
    admin = settings.admin_token.get_secret_value() if settings.admin_token else None
    if admin and hmac.compare_digest(token, admin):
        tenant_id = None
        slug = request.headers.get("x-shoal-tenant")
        if slug:
            tenant = await engine.store.get_tenant_by_slug(slug)
            if tenant is None:
                raise NotFound(f"tenant '{slug}' not found")
            tenant_id = tenant.id
        return Principal.system(tenant_id)
    if token.startswith(SESSION_PREFIX):
        return await _session_principal(engine, verify_session(settings, token))
    key = await engine.store.authenticate_api_key(token)
    if key is None:
        raise Unauthorized("invalid, expired or revoked credentials")
    if not key.owner_id:
        raise Unauthorized("API key has no owner; create a new key")
    owner = await engine.directory.get_user(key.tenant_id, key.owner_id)
    if not owner.active:
        raise Unauthorized("the key's owner is deactivated")
    prefix = "sa" if owner.kind == "service" else "user"
    await _require_active_tenant(engine, key.tenant_id)
    return Principal(
        tenant_id=key.tenant_id,
        actor=f"{prefix}:{owner.username}",
        kind=owner.kind,
        user_id=owner.id,
        display_name=owner.display_name,
        email=owner.email,
        api_key_id=key.id,
        key_scopes=tuple(key.scopes or ()),
        key_namespaces=tuple(key.namespaces or ()),
    )


async def _session_principal(engine: Engine, claims: dict[str, Any]) -> Principal:
    """A UI session: a tenant user, held by an instance account; instance admins may hold one without a tenant user."""
    tenant_id, user_id, account_id = claims.get("tid"), claims.get("sub"), claims.get("acc")
    account = None
    if account_id:
        try:
            account = await engine.accounts.get(account_id)
        except NotFound as exc:
            raise Unauthorized("session account no longer exists") from exc
        if not account.active:
            raise Unauthorized("account is deactivated on this instance")
        if is_revoked(account, claims):
            raise Unauthorized("signed out; sign in again")
    instance_role = account.instance_role if account else None
    if user_id:
        try:
            user = await engine.directory.get_user(tenant_id, user_id)
        except NotFound as exc:
            raise Unauthorized("session user no longer exists") from exc
        if not user.active:
            raise Unauthorized("account is deactivated")
        if is_revoked(user, claims):
            raise Unauthorized("signed out; sign in again")
        if account is not None and user.account_id not in (None, account.id):
            raise Unauthorized("session user belongs to another account")
        if instance_role is None:
            await _require_active_tenant(engine, tenant_id)
        return Principal(
            tenant_id=user.tenant_id, actor=f"user:{user.username}", kind=user.kind, user_id=user.id,
            display_name=user.display_name, email=user.email, account_id=account_id, instance_role=instance_role,
        )
    if account is None or (tenant_id and instance_role is None):
        raise Unauthorized("session is not valid for this tenant; sign in again")
    return Principal(  # an instance admin or auditor outside any tenant they belong to (audited under this actor)
        tenant_id=tenant_id, actor=f"account:{account.email or account.id}", kind="user", display_name=account.display_name,
        email=account.email, account_id=account.id, instance_role=instance_role,
    )


async def _require_active_tenant(engine: Engine, tenant_id: str | None) -> None:
    if tenant_id and (await engine.store.get_tenant(tenant_id)).status == "suspended":
        raise Forbidden("this tenant is suspended; contact your instance admin")


@dataclass
class Ctx:
    engine: Engine
    principal: Principal

    @property
    def tenant_id(self) -> str:
        if not self.principal.tenant_id:
            raise InvalidRequest("this call needs a tenant: use a tenant API key or send the 'X-Shoal-Tenant' header")
        return self.principal.tenant_id

    @property
    def actor(self) -> str:
        return self.principal.actor

    async def require(self, permission: str, namespace: str | None = None) -> None:
        await self.engine.authz.authorize(self.principal, permission, namespace)

    async def namespace(self, name: str, permission: str) -> str:
        """Authorize a namespaced call; 404 for namespaces that do not exist."""
        await self.engine.store.get_namespace(self.tenant_id, name)
        await self.require(permission, name)
        return name

    async def audit(self, action: str, target: str | None = None, *, namespace: str | None = None, **data: object) -> None:
        await self.engine.store.audit(self.principal.tenant_id, self.actor, action, target, namespace=namespace, **data)


async def get_ctx(request: Request, engine: EngineDep) -> Ctx:
    principal = await authenticate(request, engine)
    request.state.principal = principal
    return Ctx(engine, principal)


CtxDep = Annotated[Ctx, Depends(get_ctx)]


async def get_system_ctx(ctx: CtxDep) -> Ctx:
    if not (ctx.principal.is_system_admin or ctx.principal.instance_role == INSTANCE_ADMIN):
        raise Forbidden("this call needs an instance admin")
    return ctx


async def get_instance_reader_ctx(ctx: Annotated[Ctx, Depends(get_ctx)]) -> Ctx:
    if not (ctx.principal.is_system_admin or ctx.principal.instance_role in INSTANCE_ROLES):
        raise Forbidden("this call needs an instance admin or auditor")
    return ctx


SystemCtxDep = Annotated[Ctx, Depends(get_system_ctx)]  # instance admins (and the break-glass token)
InstanceReaderCtxDep = Annotated[Ctx, Depends(get_instance_reader_ctx)]
