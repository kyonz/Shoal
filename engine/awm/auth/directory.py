"""Users, groups, roles and role bindings (the tenant directory)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from awm.auth.permissions import ALL_PERMISSIONS, BUILTIN_ROLES, RoleDef, matches
from awm.core.errors import Conflict, InvalidRequest, NotFound
from awm.core.ids import new_id
from awm.store import tables as t
from awm.store.store import Store
from awm.store.tables import utcnow

HUB_SUBJECT_KINDS = ("hub_user", "hub_group")


class Directory:
    def __init__(self, store: Store):
        self.store = store
        self.session = store.session

    # ------------------------------------------------------------------ users

    async def create_user(
        self,
        tenant_id: str,
        username: str,
        *,
        kind: str = "user",
        email: str | None = None,
        display_name: str | None = None,
        external_id: str | None = None,
        oidc_issuer: str | None = None,
        oidc_subject: str | None = None,
        active: bool = True,
        attributes: dict[str, Any] | None = None,
    ) -> t.User:
        user = t.User(
            id=new_id("usr" if kind == "user" else "sa"),
            tenant_id=tenant_id,
            kind=kind,
            username=username,
            email=email,
            display_name=display_name or username,
            external_id=external_id,
            oidc_issuer=oidc_issuer,
            oidc_subject=oidc_subject,
            active=active,
            attributes=attributes or {},
        )
        try:
            async with self.session() as s:
                s.add(user)
        except IntegrityError as exc:
            raise Conflict(f"user '{username}' already exists") from exc
        return user

    async def get_user(self, tenant_id: str, user_id: str) -> t.User:
        async with self.session() as s:
            user = await s.get(t.User, user_id)
        if user is None or user.tenant_id != tenant_id:
            raise NotFound(f"user '{user_id}' not found")
        return user

    async def find_user(self, tenant_id: str, **by: Any) -> t.User | None:
        q = select(t.User).where(t.User.tenant_id == tenant_id)
        for key, value in by.items():
            column = getattr(t.User, key)
            q = q.where(func.lower(column) == value.lower() if key in ("username", "email") else column == value)
        async with self.session() as s:
            return (await s.execute(q.limit(1))).scalar_one_or_none()

    async def list_users(
        self, tenant_id: str, *, kind: str | None = None, query: str | None = None, offset: int = 0, limit: int = 100
    ) -> tuple[Sequence[t.User], int]:
        q = select(t.User).where(t.User.tenant_id == tenant_id)
        if kind:
            q = q.where(t.User.kind == kind)
        if query:
            like = f"%{query.lower()}%"
            q = q.where(
                or_(func.lower(t.User.username).like(like), func.lower(t.User.display_name).like(like), func.lower(t.User.email).like(like))
            )
        async with self.session() as s:
            total = int((await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one())
            rows = (await s.execute(q.order_by(t.User.username).offset(offset).limit(limit))).scalars().all()
        return rows, total

    async def update_user(self, tenant_id: str, user_id: str, **values: Any) -> t.User:
        await self.get_user(tenant_id, user_id)
        values["updated_at"] = utcnow()
        async with self.session() as s:
            await s.execute(update(t.User).where(t.User.id == user_id).values(**values))
        if values.get("active") is False:
            await self._revoke_keys_of(tenant_id, user_id)
        return await self.get_user(tenant_id, user_id)

    async def delete_user(self, tenant_id: str, user_id: str) -> None:
        await self.get_user(tenant_id, user_id)
        await self._revoke_keys_of(tenant_id, user_id)
        async with self.session() as s:
            await s.execute(delete(t.RoleBinding).where(t.RoleBinding.subject_kind == "user", t.RoleBinding.subject_id == user_id))
            await s.execute(delete(t.User).where(t.User.id == user_id))

    async def _revoke_keys_of(self, tenant_id: str, user_id: str) -> None:
        async with self.session() as s:
            await s.execute(
                update(t.ApiKey)
                .where(t.ApiKey.tenant_id == tenant_id, t.ApiKey.owner_id == user_id, t.ApiKey.revoked_at.is_(None))
                .values(revoked_at=utcnow())
            )

    # ------------------------------------------------------------------ groups

    async def create_group(
        self, tenant_id: str, display_name: str, *, external_id: str | None = None, source: str = "local"
    ) -> t.Group:
        group = t.Group(id=new_id("grp"), tenant_id=tenant_id, display_name=display_name, external_id=external_id, source=source)
        try:
            async with self.session() as s:
                s.add(group)
        except IntegrityError as exc:
            raise Conflict(f"group '{display_name}' already exists") from exc
        return group

    async def get_group(self, tenant_id: str, group_id: str) -> t.Group:
        async with self.session() as s:
            group = await s.get(t.Group, group_id)
        if group is None or group.tenant_id != tenant_id:
            raise NotFound(f"group '{group_id}' not found")
        return group

    async def find_group(self, tenant_id: str, display_name: str) -> t.Group | None:
        async with self.session() as s:
            q = select(t.Group).where(t.Group.tenant_id == tenant_id, t.Group.display_name == display_name)
            return (await s.execute(q)).scalar_one_or_none()

    async def list_groups(self, tenant_id: str, *, query: str | None = None, offset: int = 0, limit: int = 100) -> tuple[Sequence[t.Group], int]:
        q = select(t.Group).where(t.Group.tenant_id == tenant_id)
        if query:
            q = q.where(func.lower(t.Group.display_name).like(f"%{query.lower()}%"))
        async with self.session() as s:
            total = int((await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one())
            rows = (await s.execute(q.order_by(t.Group.display_name).offset(offset).limit(limit))).scalars().all()
        return rows, total

    async def update_group(self, tenant_id: str, group_id: str, **values: Any) -> t.Group:
        await self.get_group(tenant_id, group_id)
        values["updated_at"] = utcnow()
        async with self.session() as s:
            await s.execute(update(t.Group).where(t.Group.id == group_id).values(**values))
        return await self.get_group(tenant_id, group_id)

    async def delete_group(self, tenant_id: str, group_id: str) -> None:
        await self.get_group(tenant_id, group_id)
        async with self.session() as s:
            await s.execute(delete(t.RoleBinding).where(t.RoleBinding.subject_kind == "group", t.RoleBinding.subject_id == group_id))
            await s.execute(delete(t.GroupMember).where(t.GroupMember.group_id == group_id))
            await s.execute(delete(t.Group).where(t.Group.id == group_id))

    async def group_members(self, group_id: str) -> list[str]:
        async with self.session() as s:
            q = select(t.GroupMember.user_id).where(t.GroupMember.group_id == group_id)
            return list((await s.execute(q)).scalars().all())

    async def set_group_members(self, tenant_id: str, group_id: str, user_ids: list[str]) -> None:
        await self.get_group(tenant_id, group_id)
        async with self.session() as s:
            await s.execute(delete(t.GroupMember).where(t.GroupMember.group_id == group_id))
            for user_id in dict.fromkeys(user_ids):
                s.add(t.GroupMember(tenant_id=tenant_id, group_id=group_id, user_id=user_id))

    async def add_group_members(self, tenant_id: str, group_id: str, user_ids: list[str]) -> None:
        current = set(await self.group_members(group_id))
        await self.set_group_members(tenant_id, group_id, [*current, *user_ids])

    async def remove_group_members(self, tenant_id: str, group_id: str, user_ids: list[str]) -> None:
        current = await self.group_members(group_id)
        await self.set_group_members(tenant_id, group_id, [u for u in current if u not in set(user_ids)])

    async def groups_of(self, user_id: str) -> list[str]:
        async with self.session() as s:
            q = select(t.GroupMember.group_id).where(t.GroupMember.user_id == user_id)
            return list((await s.execute(q)).scalars().all())

    # ------------------------------------------------------------------ roles

    async def list_roles(self, tenant_id: str) -> list[RoleDef]:
        async with self.session() as s:
            rows = (await s.execute(select(t.Role).where(t.Role.tenant_id == tenant_id).order_by(t.Role.name))).scalars().all()
        return [*BUILTIN_ROLES.values(), *(RoleDef(r.name, r.description or "", tuple(r.permissions), builtin=False) for r in rows)]

    async def get_role(self, tenant_id: str, name: str) -> RoleDef:
        if name in BUILTIN_ROLES:
            return BUILTIN_ROLES[name]
        async with self.session() as s:
            row = (await s.execute(select(t.Role).where(t.Role.tenant_id == tenant_id, t.Role.name == name))).scalar_one_or_none()
        if row is None:
            raise NotFound(f"role '{name}' not found")
        return RoleDef(row.name, row.description or "", tuple(row.permissions), builtin=False)

    @staticmethod
    def _check_permissions(permissions: list[str]) -> None:
        unknown = [p for p in permissions if not any(matches(p, known) for known in ALL_PERMISSIONS)]
        if unknown:
            raise InvalidRequest(f"unknown permissions: {', '.join(unknown)}")

    async def put_role(self, tenant_id: str, name: str, permissions: list[str], description: str | None = None) -> RoleDef:
        if name in BUILTIN_ROLES:
            raise InvalidRequest(f"'{name}' is a built-in role and cannot be changed")
        self._check_permissions(permissions)
        async with self.session() as s:
            row = (await s.execute(select(t.Role).where(t.Role.tenant_id == tenant_id, t.Role.name == name))).scalar_one_or_none()
            if row is None:
                s.add(t.Role(id=new_id("role"), tenant_id=tenant_id, name=name, description=description, permissions=permissions))
            else:
                row.permissions, row.description, row.updated_at = permissions, description, utcnow()
        return RoleDef(name, description or "", tuple(permissions), builtin=False)

    async def delete_role(self, tenant_id: str, name: str) -> None:
        if name in BUILTIN_ROLES:
            raise InvalidRequest(f"'{name}' is a built-in role and cannot be deleted")
        async with self.session() as s:
            in_use = (
                await s.execute(select(func.count()).select_from(t.RoleBinding).where(t.RoleBinding.tenant_id == tenant_id, t.RoleBinding.role == name))
            ).scalar_one()
            if in_use:
                raise Conflict(f"role '{name}' is used by {in_use} binding(s)")
            await s.execute(delete(t.Role).where(t.Role.tenant_id == tenant_id, t.Role.name == name))

    # ------------------------------------------------------------------ bindings

    async def bind(
        self,
        tenant_id: str,
        role: str,
        subject_kind: str,
        subject_id: str,
        *,
        namespace: str | None = None,
        actor: str | None = None,
    ) -> t.RoleBinding:
        await self.get_role(tenant_id, role)
        if subject_kind == "user":
            await self.get_user(tenant_id, subject_id)
        elif subject_kind == "group":
            await self.get_group(tenant_id, subject_id)
        elif subject_kind not in HUB_SUBJECT_KINDS:  # hub people and groups live in the hub's directory
            raise InvalidRequest("subject_kind must be 'user', 'group', 'hub_user' or 'hub_group'")
        if namespace is not None:
            await self.store.get_namespace(tenant_id, namespace)
        async with self.session() as s:
            q = select(t.RoleBinding).where(
                t.RoleBinding.tenant_id == tenant_id,
                t.RoleBinding.role == role,
                t.RoleBinding.subject_kind == subject_kind,
                t.RoleBinding.subject_id == subject_id,
                t.RoleBinding.namespace.is_(None) if namespace is None else t.RoleBinding.namespace == namespace,
            )
            existing = (await s.execute(q)).scalar_one_or_none()
            if existing is not None:
                return existing
            binding = t.RoleBinding(
                id=new_id("rb"), tenant_id=tenant_id, role=role, subject_kind=subject_kind, subject_id=subject_id,
                namespace=namespace, created_by=actor,
            )
            s.add(binding)
        return binding

    async def unbind(self, tenant_id: str, binding_id: str) -> None:
        async with self.session() as s:
            result = await s.execute(delete(t.RoleBinding).where(t.RoleBinding.tenant_id == tenant_id, t.RoleBinding.id == binding_id))
            if not result.rowcount:
                raise NotFound(f"role binding '{binding_id}' not found")

    async def list_bindings(
        self, tenant_id: str, *, namespace: str | None = None, tenant_wide: bool | None = None, subject_id: str | None = None
    ) -> Sequence[t.RoleBinding]:
        q = select(t.RoleBinding).where(t.RoleBinding.tenant_id == tenant_id)
        if namespace is not None:
            q = q.where(t.RoleBinding.namespace == namespace)
        if tenant_wide:
            q = q.where(t.RoleBinding.namespace.is_(None))
        if subject_id:
            q = q.where(t.RoleBinding.subject_id == subject_id)
        async with self.session() as s:
            return (await s.execute(q.order_by(t.RoleBinding.created_at))).scalars().all()

    async def bindings_for_subjects(self, tenant_id: str, subjects: Sequence[tuple[str, str]]) -> list[t.RoleBinding]:
        """Bindings for hub principals (federation): matched by subject kind and id."""
        wanted = set(subjects)
        async with self.session() as s:
            q = select(t.RoleBinding).where(
                t.RoleBinding.tenant_id == tenant_id, t.RoleBinding.subject_kind.in_(HUB_SUBJECT_KINDS),
                t.RoleBinding.subject_id.in_([sid for _, sid in wanted]),
            )
            return [b for b in (await s.execute(q)).scalars().all() if (b.subject_kind, b.subject_id) in wanted]

    async def bindings_for(self, tenant_id: str, user_id: str) -> Sequence[t.RoleBinding]:
        subjects = [user_id, *await self.groups_of(user_id)]
        async with self.session() as s:
            q = select(t.RoleBinding).where(t.RoleBinding.tenant_id == tenant_id, t.RoleBinding.subject_id.in_(subjects))
            return (await s.execute(q)).scalars().all()

    # ------------------------------------------------------------------ OIDC providers

    async def put_oidc_provider(self, tenant_id: str, name: str, **values: Any) -> t.OIDCProvider:
        async with self.session() as s:
            q = select(t.OIDCProvider).where(t.OIDCProvider.tenant_id == tenant_id, t.OIDCProvider.name == name)
            row = (await s.execute(q)).scalar_one_or_none()
            if row is None:
                row = t.OIDCProvider(id=new_id("oidc"), tenant_id=tenant_id, name=name, display_name=values.pop("display_name", None) or name, **values)
                s.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        return row

    async def get_oidc_provider(self, tenant_id: str, name: str) -> t.OIDCProvider:
        async with self.session() as s:
            q = select(t.OIDCProvider).where(t.OIDCProvider.tenant_id == tenant_id, t.OIDCProvider.name == name)
            row = (await s.execute(q)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"OIDC provider '{name}' not found")
        return row

    async def list_oidc_providers(self, tenant_id: str) -> Sequence[t.OIDCProvider]:
        async with self.session() as s:
            q = select(t.OIDCProvider).where(t.OIDCProvider.tenant_id == tenant_id).order_by(t.OIDCProvider.name)
            return (await s.execute(q)).scalars().all()

    async def delete_oidc_provider(self, tenant_id: str, name: str) -> None:
        async with self.session() as s:
            await s.execute(delete(t.OIDCProvider).where(t.OIDCProvider.tenant_id == tenant_id, t.OIDCProvider.name == name))
