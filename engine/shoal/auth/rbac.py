"""Principals and authorization: ``authorize(principal, permission, namespace)``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from shoal.auth.permissions import NAMESPACE_PERMISSIONS, TENANT_PERMISSIONS, expand, granted
from shoal.core.errors import Forbidden

if TYPE_CHECKING:
    from shoal.engine import Engine


@dataclass
class Grants:
    tenant: set[str] = field(default_factory=set)
    namespaces: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class Principal:
    """Who is calling: a user (OIDC session), a service account or user via API key, or the system admin."""

    tenant_id: str | None
    actor: str
    kind: str = "user"  # user | service | system
    user_id: str | None = None
    display_name: str | None = None
    email: str | None = None
    is_system_admin: bool = False
    api_key_id: str | None = None
    key_scopes: tuple[str, ...] = ()
    key_namespaces: tuple[str, ...] = ()
    hub_subjects: tuple[tuple[str, str], ...] = ()  # (hub_user|hub_group, id) for calls forwarded by a federation hub
    _grants: Grants | None = None

    @classmethod
    def system(cls, tenant_id: str | None = None) -> Principal:
        return cls(tenant_id=tenant_id, actor="system:admin", kind="system", display_name="System admin", is_system_admin=True)


class Authorizer:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def grants(self, principal: Principal) -> Grants:
        if principal._grants is not None:
            return principal._grants
        grants = Grants()
        if (principal.user_id or principal.hub_subjects) and principal.tenant_id:
            directory = self.engine.directory
            roles = {r.name: r for r in await directory.list_roles(principal.tenant_id)}
            bindings = (
                await directory.bindings_for(principal.tenant_id, principal.user_id)
                if principal.user_id
                else await directory.bindings_for_subjects(principal.tenant_id, principal.hub_subjects)
            )
            for binding in bindings:
                role = roles.get(binding.role)
                if role is None:
                    continue
                target = grants.tenant if binding.namespace is None else grants.namespaces.setdefault(binding.namespace, set())
                target.update(role.permissions)
        principal._grants = grants
        return grants

    def _allowed(self, principal: Principal, grants: Grants, permission: str, namespace: str | None) -> bool:
        if principal.is_system_admin:
            return True
        if principal.key_scopes and not granted(principal.key_scopes, permission):
            return False
        if principal.key_namespaces and namespace is not None and namespace not in principal.key_namespaces:
            return False
        if granted(grants.tenant, permission):
            return True
        if namespace is None or permission in TENANT_PERMISSIONS:
            return False  # tenant-level permissions only come from tenant-wide bindings
        return granted(grants.namespaces.get(namespace, set()), permission)

    async def allowed(self, principal: Principal, permission: str, namespace: str | None = None) -> bool:
        return self._allowed(principal, await self.grants(principal), permission, namespace)

    async def authorize(self, principal: Principal, permission: str, namespace: str | None = None) -> None:
        if not await self.allowed(principal, permission, namespace):
            where = f" in namespace '{namespace}'" if namespace and permission not in TENANT_PERMISSIONS else ""
            await self.engine.store.audit(
                principal.tenant_id, principal.actor, "access.denied", permission, outcome="denied",
                namespace=namespace if permission not in TENANT_PERMISSIONS else None,
            )
            raise Forbidden(f"{principal.display_name or principal.actor} lacks '{permission}'{where}")

    async def visible_namespaces(self, principal: Principal) -> list[str]:
        """Namespaces the principal can see anything in."""
        all_ns = [n.name for n in await self.engine.store.list_namespaces(principal.tenant_id or "")]
        grants = await self.grants(principal)
        visible = [ns for ns in all_ns if any(self._allowed(principal, grants, p, ns) for p in NAMESPACE_PERMISSIONS)]
        return visible

    async def summary(self, principal: Principal) -> dict[str, object]:
        """Concrete permissions, tenant-wide and per namespace (drives what the UI shows)."""
        grants = await self.grants(principal)
        tenant = sorted(p for p in TENANT_PERMISSIONS if self._allowed(principal, grants, p, None))
        namespaces = {}
        for ns in await self.visible_namespaces(principal):
            namespaces[ns] = sorted(p for p in NAMESPACE_PERMISSIONS if self._allowed(principal, grants, p, ns))
        return {"tenant": tenant, "namespaces": namespaces}


__all__ = ["Authorizer", "Grants", "Principal", "expand"]
