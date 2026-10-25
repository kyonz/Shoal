"""Namespaces, members, users, groups, service accounts, roles, bindings, OIDC, API keys, audit."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from shoal.api.deps import Ctx, CtxDep
from shoal.api.schemas import (
    ApiKeyCreated,
    ApiKeyIn,
    ApiKeyOut,
    AuditOut,
    GroupIn,
    GroupMembersIn,
    GroupOut,
    GroupPatch,
    MemberOut,
    NamespaceIn,
    NamespaceOut,
    OIDCProviderIn,
    OIDCProviderOut,
    Page,
    PermissionOut,
    RoleBindingIn,
    RoleBindingOut,
    RoleIn,
    RoleOut,
    ServiceAccountIn,
    ServiceAccountOut,
    UserIn,
    UserOut,
    UserPatch,
)
from shoal.auth.permissions import ALL_PERMISSIONS, TENANT_PERMISSIONS, granted
from shoal.core import clock
from shoal.core.errors import Forbidden, InvalidRequest, NotFound
from shoal.store import tables as t

router = APIRouter(prefix="/v1")


# --------------------------------------------------------------------------- holding what you hand out


async def _require_holds(ctx: Ctx, patterns: Any, namespace: str | None, refusal: str) -> None:
    """Nobody can hand out permissions they do not hold themselves (where they would apply)."""
    if ctx.principal.is_system_admin:
        return
    for perm in ALL_PERMISSIONS:
        if granted(patterns, perm) and (namespace is None or perm not in TENANT_PERMISSIONS):
            if not await ctx.engine.authz.allowed(ctx.principal, perm, namespace):
                raise Forbidden(f"{refusal}: it includes '{perm}', which you do not hold")


async def _require_holds_bindings(ctx: Ctx, bindings: Any, refusal: str) -> None:
    roles = {r.name: r for r in await ctx.engine.directory.list_roles(ctx.tenant_id)}
    for binding in bindings:
        if (role := roles.get(binding.role)) is not None:
            await _require_holds(ctx, role.permissions, binding.namespace, refusal)


async def _require_outranks(ctx: Ctx, user: t.User, refusal: str) -> None:
    """Managing someone (deactivating, deleting, changing their email, minting their keys) is
    acting as them, so it needs every permission they hold."""
    await _require_holds_bindings(ctx, await ctx.engine.directory.bindings_for(ctx.tenant_id, user.id), refusal)


async def _require_group_change(ctx: Ctx, group_id: str) -> None:
    """Changing who is in a group (or what it is called, which OIDC groups claims match) grants its roles."""
    bindings = await ctx.engine.directory.list_bindings(ctx.tenant_id, subject_id=group_id)
    await _require_holds_bindings(ctx, bindings, "you cannot change this group's members")


# --------------------------------------------------------------------------- namespaces


@router.get("/namespaces", tags=["namespaces"], response_model=list[NamespaceOut])
async def list_namespaces(ctx: CtxDep) -> Any:
    visible = set(await ctx.engine.authz.visible_namespaces(ctx.principal))
    return [n for n in await ctx.engine.store.list_namespaces(ctx.tenant_id) if n.name in visible]


@router.put("/namespaces/{name}", tags=["namespaces"], response_model=NamespaceOut)
async def put_namespace(name: str, body: NamespaceIn, ctx: CtxDep, response: Response) -> Any:
    store = ctx.engine.store
    try:
        await store.get_namespace(ctx.tenant_id, name)
    except NotFound:
        await ctx.require("namespaces:create")
        row = await store.ensure_namespace(ctx.tenant_id, name, display_name=body.display_name, description=body.description, actor=ctx.actor)
        await ctx.audit("namespace.created", name)
        response.status_code = 201
        return row
    await ctx.require("members:manage", name)
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    return await store.update_namespace(ctx.tenant_id, name, **values) if values else await store.get_namespace(ctx.tenant_id, name)


@router.get("/namespaces/{name}", tags=["namespaces"], response_model=NamespaceOut)
async def get_namespace(name: str, ctx: CtxDep) -> Any:
    if name not in await ctx.engine.authz.visible_namespaces(ctx.principal):
        raise NotFound(f"namespace '{name}' not found")
    return await ctx.engine.store.get_namespace(ctx.tenant_id, name)


@router.delete("/namespaces/{name}", status_code=204, tags=["namespaces"])
async def delete_namespace(name: str, ctx: CtxDep) -> None:
    await ctx.require("namespaces:delete")
    await ctx.engine.store.delete_namespace(ctx.tenant_id, name)
    await ctx.audit("namespace.deleted", name)


async def _subject_name(ctx: Ctx, kind: str, subject_id: str) -> str:
    try:
        if kind == "user":
            return (await ctx.engine.directory.get_user(ctx.tenant_id, subject_id)).username
        return (await ctx.engine.directory.get_group(ctx.tenant_id, subject_id)).display_name
    except NotFound:
        return subject_id


@router.get("/namespaces/{name}/members", tags=["namespaces"], response_model=list[MemberOut])
async def namespace_members(name: str, ctx: CtxDep) -> Any:
    await ctx.namespace(name, "members:read")
    bindings = await ctx.engine.directory.list_bindings(ctx.tenant_id, namespace=name)
    tenant_wide = await ctx.engine.directory.list_bindings(ctx.tenant_id, tenant_wide=True)
    return [
        MemberOut(
            binding_id=b.id, role=b.role, subject_kind=b.subject_kind, subject_id=b.subject_id,
            subject_name=await _subject_name(ctx, b.subject_kind, b.subject_id), namespace=b.namespace,
        )
        for b in [*bindings, *tenant_wide]
    ]


# --------------------------------------------------------------------------- users


async def _user_out(ctx: Ctx, user: t.User) -> UserOut:
    out = UserOut.model_validate(user)
    out.groups = await ctx.engine.directory.groups_of(user.id)
    return out


@router.get("/users", tags=["users"], response_model=Page[UserOut])
async def list_users(ctx: CtxDep, q: str | None = None, offset: int = 0, limit: int = Query(100, le=500)) -> Any:
    await ctx.require("users:read")
    rows, total = await ctx.engine.directory.list_users(ctx.tenant_id, kind="user", query=q, offset=offset, limit=limit)
    return Page(items=[await _user_out(ctx, u) for u in rows], total=total, next_cursor=str(offset + limit) if offset + limit < total else None)


@router.post("/users", status_code=201, tags=["users"], response_model=UserOut)
async def create_user(body: UserIn, ctx: CtxDep) -> Any:
    await ctx.require("users:manage")
    user = await ctx.engine.directory.create_user(ctx.tenant_id, body.username, email=body.email, display_name=body.display_name, active=body.active)
    await ctx.audit("user.created", user.username)
    return await _user_out(ctx, user)


@router.get("/users/{user_id}", tags=["users"], response_model=UserOut)
async def get_user(user_id: str, ctx: CtxDep) -> Any:
    if user_id != ctx.principal.user_id:
        await ctx.require("users:read")
    return await _user_out(ctx, await ctx.engine.directory.get_user(ctx.tenant_id, user_id))


@router.patch("/users/{user_id}", tags=["users"], response_model=UserOut)
async def update_user(user_id: str, body: UserPatch, ctx: CtxDep) -> Any:
    await ctx.require("users:manage")
    values = body.model_dump(exclude_unset=True)
    if values.get("active") is False and user_id == ctx.principal.user_id:
        raise InvalidRequest("you cannot deactivate yourself")
    await _require_outranks(ctx, await ctx.engine.directory.get_user(ctx.tenant_id, user_id), "you cannot manage this user")
    user = await ctx.engine.directory.update_user(ctx.tenant_id, user_id, **values)
    await ctx.audit("user.updated", user.username, changes=values)
    return await _user_out(ctx, user)


@router.delete("/users/{user_id}", status_code=204, tags=["users"])
async def delete_user(user_id: str, ctx: CtxDep) -> None:
    await ctx.require("users:manage")
    if user_id == ctx.principal.user_id:
        raise InvalidRequest("you cannot delete yourself")
    await _require_outranks(ctx, await ctx.engine.directory.get_user(ctx.tenant_id, user_id), "you cannot manage this user")
    await ctx.engine.directory.delete_user(ctx.tenant_id, user_id)
    await ctx.audit("user.deleted", user_id)


# --------------------------------------------------------------------------- service accounts


def _sa_out(user: t.User) -> ServiceAccountOut:
    return ServiceAccountOut(
        id=user.id, name=user.username, display_name=user.display_name,
        description=(user.attributes or {}).get("description"), created_at=user.created_at,
    )


@router.get("/service-accounts", tags=["service accounts"], response_model=list[ServiceAccountOut])
async def list_service_accounts(ctx: CtxDep) -> Any:
    await ctx.require("users:read")
    rows, _ = await ctx.engine.directory.list_users(ctx.tenant_id, kind="service", limit=1000)
    return [_sa_out(u) for u in rows]


@router.put("/service-accounts/{name}", tags=["service accounts"], response_model=ServiceAccountOut)
async def put_service_account(name: str, body: ServiceAccountIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.require("users:manage")
    d = ctx.engine.directory
    user = await d.find_user(ctx.tenant_id, username=name, kind="service")
    attributes = {"description": body.description}
    if user is None:
        user = await d.create_user(ctx.tenant_id, name, kind="service", display_name=body.display_name, attributes=attributes)
        await ctx.audit("service_account.created", name)
        response.status_code = 201
    else:
        await _require_outranks(ctx, user, "you cannot manage this service account")
        user = await d.update_user(ctx.tenant_id, user.id, display_name=body.display_name or name, attributes=attributes)
    return _sa_out(user)


@router.get("/service-accounts/{name}", tags=["service accounts"], response_model=ServiceAccountOut)
async def get_service_account(name: str, ctx: CtxDep) -> Any:
    await ctx.require("users:read")
    user = await ctx.engine.directory.find_user(ctx.tenant_id, username=name, kind="service")
    if user is None:
        raise NotFound(f"service account '{name}' not found")
    return _sa_out(user)


@router.delete("/service-accounts/{name}", status_code=204, tags=["service accounts"])
async def delete_service_account(name: str, ctx: CtxDep) -> None:
    await ctx.require("users:manage")
    user = await ctx.engine.directory.find_user(ctx.tenant_id, username=name, kind="service")
    if user is None:
        raise NotFound(f"service account '{name}' not found")
    await _require_outranks(ctx, user, "you cannot manage this service account")
    await ctx.engine.directory.delete_user(ctx.tenant_id, user.id)
    await ctx.audit("service_account.deleted", name)


# --------------------------------------------------------------------------- groups


async def _group_out(ctx: Ctx, group: t.Group) -> GroupOut:
    out = GroupOut.model_validate(group)
    out.members = await ctx.engine.directory.group_members(group.id)
    return out


@router.get("/groups", tags=["groups"], response_model=Page[GroupOut])
async def list_groups(ctx: CtxDep, q: str | None = None, offset: int = 0, limit: int = Query(100, le=500)) -> Any:
    await ctx.require("users:read")
    rows, total = await ctx.engine.directory.list_groups(ctx.tenant_id, query=q, offset=offset, limit=limit)
    return Page(items=[await _group_out(ctx, g) for g in rows], total=total, next_cursor=str(offset + limit) if offset + limit < total else None)


@router.post("/groups", status_code=201, tags=["groups"], response_model=GroupOut)
async def create_group(body: GroupIn, ctx: CtxDep) -> Any:
    await ctx.require("users:manage")
    d = ctx.engine.directory
    group = await d.create_group(ctx.tenant_id, body.display_name)
    if body.members:
        await d.set_group_members(ctx.tenant_id, group.id, body.members)
    await ctx.audit("group.created", group.display_name)
    return await _group_out(ctx, group)


@router.get("/groups/{group_id}", tags=["groups"], response_model=GroupOut)
async def get_group(group_id: str, ctx: CtxDep) -> Any:
    await ctx.require("users:read")
    return await _group_out(ctx, await ctx.engine.directory.get_group(ctx.tenant_id, group_id))


@router.patch("/groups/{group_id}", tags=["groups"], response_model=GroupOut)
async def update_group(group_id: str, body: GroupPatch, ctx: CtxDep) -> Any:
    await ctx.require("users:manage")
    await _require_group_change(ctx, group_id)
    group = await ctx.engine.directory.update_group(ctx.tenant_id, group_id, **body.model_dump(exclude_unset=True))
    return await _group_out(ctx, group)


@router.put("/groups/{group_id}/members", tags=["groups"], response_model=GroupOut)
async def set_group_members(group_id: str, body: GroupMembersIn, ctx: CtxDep) -> Any:
    await ctx.require("users:manage")
    await _require_group_change(ctx, group_id)
    for user_id in body.members:
        await ctx.engine.directory.get_user(ctx.tenant_id, user_id)
    await ctx.engine.directory.set_group_members(ctx.tenant_id, group_id, body.members)
    await ctx.audit("group.members_set", group_id, members=body.members)
    return await _group_out(ctx, await ctx.engine.directory.get_group(ctx.tenant_id, group_id))


@router.delete("/groups/{group_id}", status_code=204, tags=["groups"])
async def delete_group(group_id: str, ctx: CtxDep) -> None:
    await ctx.require("users:manage")
    await _require_group_change(ctx, group_id)
    await ctx.engine.directory.delete_group(ctx.tenant_id, group_id)
    await ctx.audit("group.deleted", group_id)


# --------------------------------------------------------------------------- roles & bindings


@router.get("/permissions", tags=["roles"], response_model=list[PermissionOut])
async def list_permissions(ctx: CtxDep) -> Any:
    return [
        PermissionOut(name=name, description=desc, scope="tenant" if name in TENANT_PERMISSIONS else "namespace")
        for name, desc in sorted(ALL_PERMISSIONS.items())
    ]


def _role_out(role: Any) -> RoleOut:
    return RoleOut(name=role.name, description=role.description, permissions=list(role.permissions), builtin=role.builtin)


@router.get("/roles", tags=["roles"], response_model=list[RoleOut])
async def list_roles(ctx: CtxDep) -> Any:
    return [_role_out(r) for r in await ctx.engine.directory.list_roles(ctx.tenant_id)]


@router.put("/roles/{name}", tags=["roles"], response_model=RoleOut)
async def put_role(name: str, body: RoleIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.require("roles:manage")
    try:
        await ctx.engine.directory.get_role(ctx.tenant_id, name)
    except NotFound:
        response.status_code = 201
    await _require_holds(ctx, body.permissions, None, f"you cannot give role '{name}' these permissions")
    role = await ctx.engine.directory.put_role(ctx.tenant_id, name, body.permissions, body.description)
    await ctx.audit("role.updated", name, permissions=body.permissions)
    return _role_out(role)


@router.get("/roles/{name}", tags=["roles"], response_model=RoleOut)
async def get_role(name: str, ctx: CtxDep) -> Any:
    return _role_out(await ctx.engine.directory.get_role(ctx.tenant_id, name))


@router.delete("/roles/{name}", status_code=204, tags=["roles"])
async def delete_role(name: str, ctx: CtxDep) -> None:
    await ctx.require("roles:manage")
    await ctx.engine.directory.delete_role(ctx.tenant_id, name)
    await ctx.audit("role.deleted", name)


async def _can_grant(ctx: Ctx, role_name: str, namespace: str | None) -> None:
    """Tenant-wide grants need roles:manage; namespace grants need members:manage there,
    and nobody can grant permissions they do not hold themselves."""
    if namespace is None:
        await ctx.require("roles:manage")
    else:
        await ctx.namespace(namespace, "members:manage")
    role = await ctx.engine.directory.get_role(ctx.tenant_id, role_name)
    await _require_holds(ctx, role.permissions, namespace, f"you cannot grant '{role_name}'")


@router.get("/role-bindings", tags=["roles"], response_model=list[RoleBindingOut])
async def list_role_bindings(ctx: CtxDep, namespace: str | None = None, subject_id: str | None = None) -> Any:
    if namespace:
        await ctx.namespace(namespace, "members:read")
    else:
        await ctx.require("users:read")
    return await ctx.engine.directory.list_bindings(ctx.tenant_id, namespace=namespace, subject_id=subject_id)


@router.post("/role-bindings", status_code=201, tags=["roles"], response_model=RoleBindingOut)
async def create_role_binding(body: RoleBindingIn, ctx: CtxDep) -> Any:
    await _can_grant(ctx, body.role, body.namespace)
    binding = await ctx.engine.directory.bind(
        ctx.tenant_id, body.role, body.subject_kind, body.subject_id, namespace=body.namespace, actor=ctx.actor
    )
    await ctx.audit("role_binding.created", binding.id, **body.model_dump())
    return binding


async def _binding(ctx: Ctx, binding_id: str) -> t.RoleBinding:
    for b in await ctx.engine.directory.list_bindings(ctx.tenant_id):
        if b.id == binding_id:
            return b
    raise NotFound(f"role binding '{binding_id}' not found")


@router.get("/role-bindings/{binding_id}", tags=["roles"], response_model=RoleBindingOut)
async def get_role_binding(binding_id: str, ctx: CtxDep) -> Any:
    binding = await _binding(ctx, binding_id)
    if binding.namespace:
        await ctx.namespace(binding.namespace, "members:read")
    else:
        await ctx.require("users:read")
    return binding


@router.delete("/role-bindings/{binding_id}", status_code=204, tags=["roles"])
async def delete_role_binding(binding_id: str, ctx: CtxDep) -> None:
    binding = await _binding(ctx, binding_id)
    if binding.namespace:
        await ctx.namespace(binding.namespace, "members:manage")
    else:
        await ctx.require("roles:manage")
    await ctx.engine.directory.unbind(ctx.tenant_id, binding_id)
    await ctx.audit("role_binding.deleted", binding_id)


# --------------------------------------------------------------------------- OIDC providers


def _oidc_out(row: t.OIDCProvider) -> OIDCProviderOut:
    data = {c: getattr(row, c) for c in OIDCProviderOut.model_fields if c != "has_client_secret"}
    return OIDCProviderOut(**data, has_client_secret=bool(row.client_secret))


@router.get("/oidc-providers", tags=["auth"], response_model=list[OIDCProviderOut])
async def list_oidc(ctx: CtxDep) -> Any:
    await ctx.require("auth:manage")
    return [_oidc_out(p) for p in await ctx.engine.directory.list_oidc_providers(ctx.tenant_id)]


@router.put("/oidc-providers/{name}", tags=["auth"], response_model=OIDCProviderOut)
async def put_oidc(name: str, body: OIDCProviderIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.require("auth:manage")
    try:
        await ctx.engine.directory.get_oidc_provider(ctx.tenant_id, name)
    except NotFound:
        response.status_code = 201
    values = body.model_dump(exclude={"client_secret"})
    if body.default_role:  # everyone who signs up gets it: the same as granting it
        role = await ctx.engine.directory.get_role(ctx.tenant_id, body.default_role)
        if body.default_namespace:
            await ctx.engine.store.get_namespace(ctx.tenant_id, body.default_namespace)
        await _require_holds(ctx, role.permissions, body.default_namespace, f"you cannot make '{body.default_role}' the default role")
    secret = body.client_secret if "client_secret" in body.model_fields_set else None
    row = await ctx.engine.oidc.configure(ctx.tenant_id, name, client_secret=secret, **values)
    await ctx.audit("oidc_provider.updated", name)
    return _oidc_out(row)


@router.get("/oidc-providers/{name}", tags=["auth"], response_model=OIDCProviderOut)
async def get_oidc(name: str, ctx: CtxDep) -> Any:
    await ctx.require("auth:manage")
    return _oidc_out(await ctx.engine.directory.get_oidc_provider(ctx.tenant_id, name))


@router.delete("/oidc-providers/{name}", status_code=204, tags=["auth"])
async def delete_oidc(name: str, ctx: CtxDep) -> None:
    await ctx.require("auth:manage")
    await ctx.engine.directory.get_oidc_provider(ctx.tenant_id, name)
    await ctx.engine.directory.delete_oidc_provider(ctx.tenant_id, name)
    await ctx.audit("oidc_provider.deleted", name)


# --------------------------------------------------------------------------- API keys


@router.get("/api-keys", tags=["api keys"], response_model=list[ApiKeyOut])
async def list_api_keys(ctx: CtxDep, all: bool = False) -> Any:
    keys = await ctx.engine.store.list_api_keys(ctx.tenant_id)
    if all:
        await ctx.require("api-keys:manage")
        return keys
    return [k for k in keys if k.owner_id == ctx.principal.user_id]


@router.post("/api-keys", status_code=201, tags=["api keys"], response_model=ApiKeyCreated)
async def create_api_key(body: ApiKeyIn, ctx: CtxDep) -> Any:
    owner_id = ctx.principal.user_id
    if body.service_account:
        await ctx.require("api-keys:manage")
        sa = await ctx.engine.directory.find_user(ctx.tenant_id, username=body.service_account, kind="service")
        if sa is None:
            raise NotFound(f"service account '{body.service_account}' not found")
        await _require_outranks(ctx, sa, f"you cannot create keys for '{body.service_account}'")
        owner_id = sa.id
    if owner_id is None:
        raise InvalidRequest("the system admin token cannot own API keys; set 'service_account'")
    for ns in body.namespaces:
        await ctx.engine.store.get_namespace(ctx.tenant_id, ns)
    expires_at = body.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if max_days := ctx.engine.settings.api_key_max_days:
        latest = clock.now() + timedelta(days=max_days)
        if expires_at is None:
            expires_at = latest
        elif expires_at > latest + timedelta(minutes=5):  # slack for the client's clock
            raise InvalidRequest(f"API keys on this server expire within {max_days} days")
    p = ctx.principal
    if p.api_key_id:  # a key can only mint keys within its own limits
        if any(granted(body.scopes, perm) and not granted(p.key_scopes, perm) for perm in ALL_PERMISSIONS):
            raise Forbidden("an API key cannot create a key with broader scopes than its own")
        if p.key_namespaces and (not body.namespaces or set(body.namespaces) - set(p.key_namespaces)):
            raise Forbidden("an API key cannot create a key for namespaces outside its own")
    key, token = await ctx.engine.store.create_api_key(
        ctx.tenant_id, body.name, body.scopes, expires_at=expires_at, created_by=ctx.actor, owner_id=owner_id, namespaces=body.namespaces
    )
    await ctx.audit("api_key.created", key.id, name=body.name, owner_id=owner_id)
    return ApiKeyCreated(key=ApiKeyOut.model_validate(key), token=token)


@router.get("/api-keys/{key_id}", tags=["api keys"], response_model=ApiKeyOut, summary="An API key's metadata (never the token)")
async def get_api_key(key_id: str, ctx: CtxDep) -> Any:
    key = next((k for k in await ctx.engine.store.list_api_keys(ctx.tenant_id) if k.id == key_id), None)
    if key is None:
        raise NotFound(f"api key '{key_id}' not found")
    if key.owner_id != ctx.principal.user_id:
        await ctx.require("api-keys:manage")
    return key


@router.delete("/api-keys/{key_id}", status_code=204, tags=["api keys"])
async def revoke_api_key(key_id: str, ctx: CtxDep) -> None:
    keys = {k.id: k for k in await ctx.engine.store.list_api_keys(ctx.tenant_id)}
    key = keys.get(key_id)
    if key is None:
        raise NotFound(f"api key '{key_id}' not found")
    if key.owner_id != ctx.principal.user_id:
        await ctx.require("api-keys:manage")
    await ctx.engine.store.revoke_api_key(ctx.tenant_id, key_id)
    await ctx.audit("api_key.revoked", key_id)


# --------------------------------------------------------------------------- audit


def _audit_filters(
    action: str | None = Query(None, description="Action prefix, e.g. 'workflow.' or 'access.denied'."),
    actor: str | None = Query(None, description="Exact actor, e.g. 'user:ada@acme.io'."),
    target: str | None = Query(None, description="Target prefix, e.g. 'payments/'."),
    namespace: str | None = None,
    outcome: str | None = Query(None, description="success, denied or error."),
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    return {"action": action, "actor": actor, "target": target, "namespace": namespace, "outcome": outcome, "since": since, "until": until}


AuditFilters = Annotated[dict[str, Any], Depends(_audit_filters)]


def _audit_scope(ctx: Any) -> list[str] | None:
    """A namespace-restricted caller (scoped API key, federation hub) sees only those namespaces' entries."""
    return list(ctx.principal.key_namespaces) or None


@router.get("/audit", tags=["audit"], response_model=Page[AuditOut], summary="Search the audit log (newest first)")
async def audit(
    ctx: CtxDep, filters: AuditFilters, before: int | None = Query(None, description="Cursor: entries older than this id."),
    limit: int = Query(100, ge=1, le=1000),
) -> Any:
    await ctx.require("audit:read")
    rows = await ctx.engine.store.list_audit(ctx.tenant_id, limit=limit + 1, before=before, namespaces=_audit_scope(ctx), **filters)
    items = list(rows[:limit])
    return Page(items=[AuditOut.model_validate(r) for r in items], next_cursor=str(items[-1].id) if len(rows) > limit else None)


@router.get("/audit/export", tags=["audit"], summary="Export the audit log as JSON Lines (oldest entries last)")
async def audit_export(ctx: CtxDep, filters: AuditFilters) -> StreamingResponse:
    await ctx.require("audit:read")
    await ctx.audit("audit.exported", None, **{k: str(v) for k, v in filters.items() if v})
    store, tenant_id, scope = ctx.engine.store, ctx.tenant_id, _audit_scope(ctx)

    async def lines() -> AsyncIterator[bytes]:
        before = None
        while True:
            rows = await store.list_audit(tenant_id, limit=1000, before=before, namespaces=scope, **filters)
            for row in rows:
                yield AuditOut.model_validate(row).model_dump_json().encode() + b"\n"
            if len(rows) < 1000:
                return
            before = rows[-1].id

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'})
