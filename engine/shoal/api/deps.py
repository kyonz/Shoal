"""Request authentication and the per-request context."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from shoal.auth.rbac import Principal
from shoal.auth.sessions import SESSION_PREFIX, verify_session
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


class AuthCache:
    """Recently authenticated principals (with their grants), keyed by a hash of the credential.

    Entries live ``auth_cache_seconds`` and are dropped as soon as this process changes keys, users, groups,
    roles or bindings; a change made by another process takes effect within the TTL.
    """

    def __init__(self, engine: Engine, max_entries: int = 10_000):
        self.engine, self.max_entries = engine, max_entries
        self._entries: dict[str, tuple[Any, int, float]] = {}
        self.pending: dict[str, asyncio.Future[Any]] = {}

    def _key(self, *parts: str) -> str:
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()

    def get(self, *parts: str) -> Any:
        entry = self._entries.get(self._key(*parts))
        if entry is None:
            return None
        value, generation, expires = entry
        if generation != self.engine.store.auth_generation or time.monotonic() > expires:
            self._entries.pop(self._key(*parts), None)
            return None
        return value

    def put(self, value: Any, *parts: str) -> None:
        ttl = self.engine.settings.auth_cache_seconds
        if ttl <= 0:
            return
        if len(self._entries) >= self.max_entries:
            self._entries.pop(next(iter(self._entries)))  # oldest first (dicts keep insertion order)
        self._entries[self._key(*parts)] = (value, self.engine.store.auth_generation, time.monotonic() + ttl)


def auth_cache(engine: Engine) -> AuthCache:
    cache = getattr(engine, "_auth_cache", None)
    if cache is None:
        cache = engine._auth_cache = AuthCache(engine)  # type: ignore[attr-defined]
    return cache


async def authenticate(request: Request, engine: Engine) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token = _bearer(request)
    if token and not request.headers.get(ASSERTION_HEADER):
        tenant_header = request.headers.get("x-shoal-tenant", "")
        cache = auth_cache(engine)
        hit = cache.get("principal", token, tenant_header)
        if hit is not None:
            return hit
        # Concurrent requests with the same credential share one lookup (no stampede when an entry expires).
        key = cache._key("principal", token, tenant_header)  # noqa: SLF001
        pending = cache.pending.get(key)
        if pending is None:
            pending = cache.pending[key] = asyncio.ensure_future(_authenticate(request, engine))
            pending.add_done_callback(lambda _: cache.pending.pop(key, None))
        principal = await asyncio.shield(pending)
        cache.put(principal, "principal", token, tenant_header)
        return principal
    return await _authenticate(request, engine)


async def _authenticate(request: Request, engine: Engine) -> Principal:
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
        claims = verify_session(settings, token)
        try:
            user = await engine.directory.get_user(claims["tid"], claims["sub"])
        except NotFound as exc:
            raise Unauthorized("session user no longer exists") from exc
        if not user.active:
            raise Unauthorized("account is deactivated")
        return Principal(
            tenant_id=user.tenant_id, actor=f"user:{user.username}", kind=user.kind, user_id=user.id,
            display_name=user.display_name, email=user.email,
        )
    key = await engine.store.authenticate_api_key(token)
    if key is None:
        raise Unauthorized("invalid, expired or revoked credentials")
    if not key.owner_id:
        raise Unauthorized("API key has no owner; create a new key")
    owner = await engine.directory.get_user(key.tenant_id, key.owner_id)
    if not owner.active:
        raise Unauthorized("the key's owner is deactivated")
    prefix = "sa" if owner.kind == "service" else "user"
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
        cache = auth_cache(self.engine)
        if not cache.get("namespace", self.tenant_id, name):
            await self.engine.store.get_namespace(self.tenant_id, name)
            cache.put(True, "namespace", self.tenant_id, name)
        await self.require(permission, name)
        return name

    async def audit(self, action: str, target: str | None = None, *, namespace: str | None = None, **data: object) -> None:
        await self.engine.store.audit(self.principal.tenant_id, self.actor, action, target, namespace=namespace, **data)


async def get_ctx(request: Request, engine: EngineDep) -> Ctx:
    principal = await authenticate(request, engine)
    request.state.principal = principal
    if not principal.is_system_admin:
        await engine.quotas.check_request(principal.tenant_id)
    return Ctx(engine, principal)


CtxDep = Annotated[Ctx, Depends(get_ctx)]


async def get_system_ctx(ctx: CtxDep) -> Ctx:
    if not ctx.principal.is_system_admin:
        raise Forbidden("this call needs the system admin token")
    return ctx


SystemCtxDep = Annotated[Ctx, Depends(get_system_ctx)]
