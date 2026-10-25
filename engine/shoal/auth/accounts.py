"""Instance accounts: one person, many tenants.

Tenants keep their own users, groups and role bindings (SCIM and per-tenant SSO
are unchanged). An *account* is the person behind those rows across the whole
instance: it is keyed by the identity provider's ``(issuer, sub)`` and linked to
at most one user in each tenant. A session belongs to an account and is pointed
at one tenant at a time, so switching tenants is issuing a new session.

Trust: any sign-in links the account to tenant users with the same
``(issuer, sub)``, which only that identity provider can assert. Linking by
email, and the ``instance_admins`` bootstrap, happen only through the
instance-wide sign-in (``SHOAL_INSTANCE_OIDC``), because tenant admins can
configure their own identity providers and those must not vouch for people in
other tenants.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update

from shoal.core import clock
from shoal.core.errors import Forbidden, InvalidRequest, NotFound
from shoal.core.ids import new_id
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

INSTANCE_ADMIN = "instance-admin"
INSTANCE_AUDITOR = "instance-auditor"
INSTANCE_ROLES = (INSTANCE_ADMIN, INSTANCE_AUDITOR)


@dataclass
class Membership:
    tenant: t.Tenant
    user: t.User


class Accounts:
    def __init__(self, engine: Engine):
        self.engine = engine

    @property
    def store(self) -> Any:
        return self.engine.store

    async def sign_in(self, *, issuer: str, subject: str, email: str | None, name: str | None,
                      trusted_email: bool = False) -> t.Account:
        """Find or create the account for an identity; ``trusted_email`` applies the instance admin bootstrap."""
        async with self.store.session() as s:
            q = select(t.Account).where(t.Account.oidc_issuer == issuer, t.Account.oidc_subject == subject)
            account = (await s.execute(q)).scalar_one_or_none()
            if account is None:
                account = t.Account(id=new_id("acc"), oidc_issuer=issuer, oidc_subject=subject,
                                    display_name=name or email or subject, active=True, revoked_sessions={})
                s.add(account)
            if not account.active:
                raise Forbidden("your account is deactivated on this instance")
            account.email = email or account.email
            account.display_name = name or account.display_name
            account.last_login_at = clock.now()
            bootstrap = {e.lower() for e in self.engine.settings.instance_admins}
            if trusted_email and email and email.lower() in bootstrap and account.instance_role is None:
                account.instance_role = INSTANCE_ADMIN
        return account

    async def link_user(self, account: t.Account, user: t.User) -> None:
        if user.account_id in (None, account.id) and user.kind == "user":
            async with self.store.session() as s:
                await s.execute(update(t.User).where(t.User.id == user.id, t.User.account_id.is_(None))
                                .values(account_id=account.id))
            self.store.auth_changed()

    async def link_matching_users(self, account: t.Account, *, verified_email: str | None) -> None:
        """Link unlinked tenant users that are this person: same identity, or (instance sign-in) same verified email."""
        match = (t.User.oidc_issuer == account.oidc_issuer) & (t.User.oidc_subject == account.oidc_subject)
        if verified_email:
            match = or_(match, func.lower(t.User.email) == verified_email.lower())
        async with self.store.session() as s:
            rows = (await s.execute(select(t.User).where(t.User.account_id.is_(None), t.User.kind == "user", match))).scalars().all()
            linked = {u.tenant_id for u in (await s.execute(select(t.User).where(t.User.account_id == account.id))).scalars()}
            for user in rows:
                if user.tenant_id not in linked:  # one user per tenant
                    user.account_id = account.id
                    linked.add(user.tenant_id)
        self.store.auth_changed()

    async def get(self, account_id: str) -> t.Account:
        async with self.store.session() as s:
            account = await s.get(t.Account, account_id)
        if account is None:
            raise NotFound(f"account '{account_id}' not found")
        return account

    async def find_by_email(self, email: str) -> t.Account | None:
        async with self.store.session() as s:
            q = select(t.Account).where(func.lower(t.Account.email) == email.lower()).order_by(t.Account.created_at)
            return (await s.execute(q.limit(1))).scalar_one_or_none()

    async def list(self, *, query: str | None = None, limit: int = 200) -> Sequence[t.Account]:
        q = select(t.Account).order_by(t.Account.display_name).limit(limit)
        if query:
            like = f"%{query.lower()}%"
            q = q.where(or_(func.lower(t.Account.email).like(like), func.lower(t.Account.display_name).like(like)))
        async with self.store.session() as s:
            return (await s.execute(q)).scalars().all()

    async def update(self, account_id: str, **values: Any) -> t.Account:
        if values.get("instance_role") not in (None, *INSTANCE_ROLES):
            raise InvalidRequest(f"instance_role must be one of {', '.join(INSTANCE_ROLES)} (or null)")
        async with self.store.session() as s:
            account = await s.get(t.Account, account_id)
            if account is None:
                raise NotFound(f"account '{account_id}' not found")
            for key, value in values.items():
                setattr(account, key, value)
        self.store.auth_changed()  # sign-outs, deactivation and instance roles apply to cached principals at once
        return account

    async def memberships(self, account_id: str) -> list[Membership]:
        """The tenants this account has an active user in."""
        async with self.store.session() as s:
            q = (select(t.User, t.Tenant).join(t.Tenant, t.Tenant.id == t.User.tenant_id)
                 .where(t.User.account_id == account_id, t.User.active.is_(True)).order_by(t.Tenant.name))
            return [Membership(tenant=tenant, user=user) for user, tenant in (await s.execute(q)).all()]

    async def membership(self, account_id: str, tenant_id: str) -> t.User | None:
        async with self.store.session() as s:
            q = select(t.User).where(t.User.account_id == account_id, t.User.tenant_id == tenant_id, t.User.active.is_(True))
            return (await s.execute(q)).scalar_one_or_none()

    async def add_to_tenant(self, tenant_id: str, email: str, role: str, *, actor: str) -> t.User:
        """Give a person a role in a tenant: links their account when they have one, otherwise invites by email."""
        directory = self.engine.directory
        account = await self.find_by_email(email)
        user = await directory.find_user(tenant_id, email=email)
        if user is None and account is not None:
            user = await self.membership(account.id, tenant_id)
        if user is None:
            user = await directory.create_user(tenant_id, email, email=email,
                                               display_name=account.display_name if account else email)
        if account is not None:
            await self.link_user(account, user)
        await directory.get_role(tenant_id, role)
        if not any(b.role == role and b.namespace is None for b in await directory.bindings_for(tenant_id, user.id)):
            await directory.bind(tenant_id, role, "user", user.id, actor=actor)
        return user


__all__ = ["INSTANCE_ADMIN", "INSTANCE_AUDITOR", "INSTANCE_ROLES", "Accounts", "Membership"]
