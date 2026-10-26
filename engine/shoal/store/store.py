"""Data access. Every tenant-owned query takes ``tenant_id`` explicitly.

There is deliberately no API for unscoped reads of tenant data except the few
system operations the scheduler needs (claiming runs, due timers), which return
ids and tenant ids together.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from shoal.core.errors import Conflict, InvalidRequest, NotFound
from shoal.core.ids import new_id
from shoal.core.models import RESOURCE_KINDS, ResourceBase, RunStatus, StepStatus
from shoal.core.requestctx import current_request
from shoal.core.scope import (
    DEFAULT_NAMESPACE,
    NAMESPACE_PATTERN,
    SHARED_NAMESPACE,
    current_namespace,
    resolve_namespace,
)
from shoal.runtime.queues import Wakeup
from shoal.store import tables as t
from shoal.store.tables import utcnow

API_KEY_PREFIX = "shoal"
EVENTS_CHANNEL = "shoal_run_events"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Taking a lease that is still owned means the last worker never released it: it died mid-pass.
_LOST_LEASE = case((t.Run.lease_owner.is_not(None), t.Run.lease_losses + 1), else_=t.Run.lease_losses)


class Store:
    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = 20,
        max_overflow: int = 10,
        pool_timeout: float = 10.0,
        pool_recycle: int = 1800,
        pgbouncer: bool = False,
        direct_url: str | None = None,
        read_url: str | None = None,
    ):
        kwargs: dict[str, Any] = {"echo": echo}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
        else:
            # A pool at least as large as the work a process runs at once, so steady load never churns
            # overflow connections (each one a new backend and TLS handshake).
            kwargs.update(
                pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout,
                pool_recycle=pool_recycle or -1, pool_pre_ping=True,
            )
            if pgbouncer:  # transaction pooling hands each transaction a different server connection
                kwargs["connect_args"] = {
                    "statement_cache_size": 0,
                    "prepared_statement_cache_size": 0,
                    "prepared_statement_name_func": lambda: f"__shoal_{uuid.uuid4().hex}__",
                }
        self.url = url
        self.direct_url = direct_url or url
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        # Dashboards, lists and audit reads can go to a replica; anything that decides what to do next reads the primary.
        self.read_engine: AsyncEngine = create_async_engine(read_url, **kwargs) if read_url else self.engine
        self.is_sqlite = url.startswith("sqlite")
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._read_sessions = async_sessionmaker(self.read_engine, expire_on_commit=False)
        # Called with the run id after every wake_run (the engine wires in its orchestrator).
        self.wake_hooks: list[Callable[[str], Awaitable[None]]] = []
        # told (after commit) whenever a built-in run's next_wake_at is set: the dispatch queue
        self.due_hooks: list[Callable[[Sequence[Wakeup]], Awaitable[None]]] = []
        # Bumped whenever keys, users, groups, roles or bindings change, so request caches can drop old answers.
        self.auth_generation = 0
        self.resource_generation = 0  # bumped by every resource apply or delete in this process

    def auth_changed(self) -> None:
        self.auth_generation += 1

    async def create_all(self) -> None:
        """Bring the schema up to date (Alembic migrations)."""
        await self.migrate()

    async def migrate(self, revision: str = "head") -> str | None:
        from shoal.store import migrate

        engine = self.engine if self.direct_url == self.url else create_async_engine(self.direct_url)
        try:
            return await self._migrate(engine, revision, migrate)
        finally:
            if engine is not self.engine:
                await engine.dispose()

    async def _migrate(self, engine: AsyncEngine, revision: str, migrate: Any) -> str | None:
        from sqlalchemy import inspect

        async with engine.begin() as conn:
            if self.is_sqlite:
                await conn.exec_driver_sql("PRAGMA journal_mode=WAL")

            def run(sync_conn: Any) -> str | None:
                if migrate.current(sync_conn) is None and inspect(sync_conn).has_table("tenants"):
                    # created before migrations existed (pre-release create_all): adopt it as the baseline
                    migrate.stamp(sync_conn, "0001")
                migrate.upgrade(sync_conn, revision)
                return migrate.current(sync_conn)

            return await conn.run_sync(run)

    async def close(self) -> None:
        await self.engine.dispose()
        if self.read_engine is not self.engine:
            await self.read_engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session, session.begin():
            yield session

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """A read-only session on the replica when one is configured (may lag the primary slightly)."""
        async with self._read_sessions() as session, session.begin():
            yield session

    # ------------------------------------------------------------------ tenants

    async def create_tenant(self, slug: str, name: str | None = None, *, description: str | None = None) -> t.Tenant:
        tenant = t.Tenant(id=new_id("ten"), slug=slug, name=name or slug, description=description)
        try:
            async with self.session() as s:
                s.add(tenant)
        except IntegrityError as exc:
            raise Conflict(f"tenant '{slug}' already exists") from exc
        await self.ensure_namespace(tenant.id, DEFAULT_NAMESPACE, display_name="Default")
        await self.ensure_namespace(
            tenant.id, SHARED_NAMESPACE, display_name="Shared", description="Resources every namespace in the tenant can use."
        )
        return tenant

    async def get_tenant(self, tenant_id: str) -> t.Tenant:
        async with self.session() as s:
            tenant = await s.get(t.Tenant, tenant_id)
        if tenant is None:
            raise NotFound(f"tenant '{tenant_id}' not found")
        return tenant

    async def update_tenant_settings(self, tenant_id: str, **values: Any) -> t.Tenant:
        """Merge ``values`` into the tenant's settings (``None`` removes a key)."""
        async with self.session() as s:
            tenant = await s.get(t.Tenant, tenant_id)
            if tenant is None:
                raise NotFound(f"tenant '{tenant_id}' not found")
            merged = {**(tenant.settings or {}), **values}
            tenant.settings = {k: v for k, v in merged.items() if v is not None}
        return tenant

    async def get_tenant_by_slug(self, slug: str) -> t.Tenant | None:
        async with self.session() as s:
            return (await s.execute(select(t.Tenant).where(t.Tenant.slug == slug))).scalar_one_or_none()

    async def delete_tenant(self, slug: str) -> None:
        tenant = await self.get_tenant_by_slug(slug)
        if tenant is None:
            raise NotFound(f"tenant '{slug}' not found")
        async with self.session() as s:
            for table in reversed(t.Base.metadata.sorted_tables):
                if "tenant_id" in table.c and table.name != "tenants":
                    await s.execute(table.delete().where(table.c.tenant_id == tenant.id))
            await s.execute(delete(t.Tenant).where(t.Tenant.id == tenant.id))
        self.auth_changed()

    async def list_tenants(self) -> Sequence[t.Tenant]:
        async with self.session() as s:
            return (await s.execute(select(t.Tenant).order_by(t.Tenant.created_at))).scalars().all()

    # ------------------------------------------------------------------ api keys

    async def create_api_key(
        self,
        tenant_id: str,
        name: str,
        scopes: list[str],
        *,
        expires_at: datetime | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> tuple[t.ApiKey, str]:
        """Returns the stored key and the plaintext token (shown once)."""
        prefix = pysecrets.token_hex(6)
        secret = pysecrets.token_urlsafe(32)
        token = f"{API_KEY_PREFIX}_{prefix}_{secret}"
        key = t.ApiKey(
            id=new_id("key"),
            tenant_id=tenant_id,
            name=name,
            prefix=prefix,
            hash=hash_token(token),
            scopes=scopes,
            expires_at=expires_at,
            created_by=created_by,
            owner_id=owner_id,
            namespaces=namespaces or [],
        )
        async with self.session() as s:
            s.add(key)
        return key, token

    async def authenticate_api_key(self, token: str) -> t.ApiKey | None:
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != API_KEY_PREFIX:
            return None
        async with self.session() as s:
            key = (await s.execute(select(t.ApiKey).where(t.ApiKey.prefix == parts[1]))).scalar_one_or_none()
            if key is None or not pysecrets.compare_digest(key.hash, hash_token(token)):
                return None
            now = utcnow()
            if key.revoked_at or (key.expires_at and key.expires_at < now):
                return None
            if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=1):
                key.last_used_at = now
            return key

    async def list_api_keys(self, tenant_id: str) -> Sequence[t.ApiKey]:
        async with self.session() as s:
            q = select(t.ApiKey).where(t.ApiKey.tenant_id == tenant_id).order_by(t.ApiKey.created_at.desc())
            return (await s.execute(q)).scalars().all()

    async def revoke_api_key(self, tenant_id: str, key_id: str) -> None:
        async with self.session() as s:
            key = await s.get(t.ApiKey, key_id)
            if key is None or key.tenant_id != tenant_id:
                raise NotFound(f"api key '{key_id}' not found")
            key.revoked_at = utcnow()
        self.auth_changed()

    # ------------------------------------------------------------------ namespaces

    async def ensure_namespace(
        self, tenant_id: str, name: str, *, display_name: str | None = None, description: str | None = None, actor: str | None = None
    ) -> t.Namespace:
        if not NAMESPACE_PATTERN.match(name):
            raise InvalidRequest(f"invalid namespace name '{name}'")
        async with self.session() as s:
            q = select(t.Namespace).where(t.Namespace.tenant_id == tenant_id, t.Namespace.name == name)
            row = (await s.execute(q)).scalar_one_or_none()
            if row is None:
                row = t.Namespace(
                    id=new_id("ns"), tenant_id=tenant_id, name=name, display_name=display_name or name,
                    description=description, created_by=actor,
                )
                s.add(row)
            return row

    async def get_namespace(self, tenant_id: str, name: str) -> t.Namespace:
        async with self.session() as s:
            q = select(t.Namespace).where(t.Namespace.tenant_id == tenant_id, t.Namespace.name == name)
            row = (await s.execute(q)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"namespace '{name}' not found")
        return row

    async def list_namespaces(self, tenant_id: str) -> Sequence[t.Namespace]:
        async with self.session() as s:
            q = select(t.Namespace).where(t.Namespace.tenant_id == tenant_id).order_by(t.Namespace.name)
            return (await s.execute(q)).scalars().all()

    async def update_namespace(self, tenant_id: str, name: str, **values: Any) -> t.Namespace:
        row = await self.get_namespace(tenant_id, name)
        async with self.session() as s:
            await s.execute(update(t.Namespace).where(t.Namespace.id == row.id).values(**values))
        return await self.get_namespace(tenant_id, name)

    async def delete_namespace(self, tenant_id: str, name: str) -> None:
        if name in (DEFAULT_NAMESPACE, SHARED_NAMESPACE):
            raise InvalidRequest(f"the '{name}' namespace cannot be deleted")
        async with self.session() as s:
            count = (
                await s.execute(
                    select(func.count()).select_from(t.Resource).where(t.Resource.tenant_id == tenant_id, t.Resource.namespace == name)
                )
            ).scalar_one()
            if count:
                raise Conflict(f"namespace '{name}' still contains {count} resource(s)")
            q = select(t.OIDCProvider.name).where(t.OIDCProvider.tenant_id == tenant_id, t.OIDCProvider.default_namespace == name)
            if provider := (await s.execute(q.limit(1))).scalar_one_or_none():
                raise Conflict(f"OIDC provider '{provider}' grants its default role in namespace '{name}'; change it first")
            # What belongs to the namespace goes with it, so a namespace created later with the
            # same name starts without the old members, secrets, personal credentials or webhook URLs.
            for table in (t.RoleBinding, t.Secret, t.UserCredential, t.WebhookEndpoint):
                await s.execute(delete(table).where(table.tenant_id == tenant_id, table.namespace == name))
            await s.execute(delete(t.Namespace).where(t.Namespace.tenant_id == tenant_id, t.Namespace.name == name))
        self.auth_changed()

    # ------------------------------------------------------------------ resources

    async def apply_resource(
        self,
        tenant_id: str,
        resource: ResourceBase,
        *,
        actor: str | None = None,
        namespace: str | None = None,
        source: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> tuple[t.Resource, str]:
        """Create or update. Returns (row, 'created' | 'updated' | 'unchanged').

        The namespace is ``metadata.namespace``, else ``namespace``, else the
        current one; it is stored as a column, not in the document. ``source``
        is the spec as the client wrote it, kept so reads return what was
        written (declarative clients such as Terraform diff against it).
        """
        document = resource.to_document()
        kind = document["kind"]
        name = resource.metadata.name
        ns = resolve_namespace(document["metadata"].pop("namespace", None) or namespace, kind)
        annotations = document["metadata"].pop("annotations", None)  # unversioned; None keeps the current ones
        await self.ensure_namespace(tenant_id, ns)
        async with self.session() as s:
            row = (
                await s.execute(
                    select(t.Resource).where(
                        t.Resource.tenant_id == tenant_id,
                        t.Resource.namespace == ns,
                        t.Resource.kind == kind,
                        t.Resource.name == name,
                    )
                )
            ).scalar_one_or_none()
            if row is not None and row.document == document:
                if source is not None and row.source != source:
                    row.source = source  # same meaning, different spelling: no new version
                if annotations is not None and row.annotations != annotations:
                    row.annotations = annotations
                return row, "unchanged"
            if row is None:
                row = t.Resource(
                    id=new_id("res"),
                    tenant_id=tenant_id,
                    namespace=ns,
                    kind=kind,
                    name=name,
                    version=1,
                    document=document,
                    source=source,
                    annotations=annotations or {},
                    labels=resource.metadata.labels,
                    updated_by=actor,
                )
                s.add(row)
                outcome = "created"
            else:
                row.version += 1
                row.document = document
                row.source = source
                if annotations is not None:
                    row.annotations = annotations
                row.labels = resource.metadata.labels
                row.updated_at = utcnow()
                row.updated_by = actor
                outcome = "updated"
            await s.flush()
            s.add(
                t.ResourceVersion(
                    resource_id=row.id,
                    tenant_id=tenant_id,
                    version=row.version,
                    document=document,
                    source=source,
                    message=message,
                    created_by=actor,
                )
            )
        self.resource_generation += 1
        return row, outcome

    async def _resource_row(self, tenant_id: str, namespace: str, kind: str, name: str) -> t.Resource | None:
        async with self.session() as s:
            return (
                await s.execute(
                    select(t.Resource).where(
                        t.Resource.tenant_id == tenant_id,
                        t.Resource.namespace == namespace,
                        t.Resource.kind == kind,
                        t.Resource.name == name,
                    )
                )
            ).scalar_one_or_none()

    async def get_resource_row(
        self, tenant_id: str, kind: str, name: str, *, namespace: str | None = None, fallback: bool = True
    ) -> t.Resource:
        """Look up in the namespace, then (``fallback``) in the tenant's shared namespace."""
        ns = resolve_namespace(namespace, kind)
        row = await self._resource_row(tenant_id, ns, kind, name)
        if row is None and fallback and ns != SHARED_NAMESPACE:
            row = await self._resource_row(tenant_id, SHARED_NAMESPACE, kind, name)
            if row is not None and kind == "Connection":  # organisation connections can be limited to some namespaces
                allowed = (row.document.get("spec") or {}).get("namespaces") or []
                if allowed and ns not in allowed:
                    row = None
        if row is None:
            raise NotFound(f"{kind} '{name}' not found")
        return row

    async def get_resource(self, tenant_id: str, kind: str, name: str, *, namespace: str | None = None) -> ResourceBase:
        row = await self.get_resource_row(tenant_id, kind, name, namespace=namespace)
        return RESOURCE_KINDS[kind].model_validate(row.document)

    async def find_resource(self, tenant_id: str, kind: str, name: str, *, namespace: str | None = None) -> ResourceBase | None:
        try:
            return await self.get_resource(tenant_id, kind, name, namespace=namespace)
        except NotFound:
            return None

    async def list_resources(
        self,
        tenant_id: str,
        kind: str,
        *,
        label: dict[str, str] | None = None,
        namespace: str | None = None,
        all_namespaces: bool = False,
    ) -> Sequence[t.Resource]:
        async with self.session() as s:
            q = select(t.Resource).where(t.Resource.tenant_id == tenant_id, t.Resource.kind == kind)
            if not all_namespaces:
                q = q.where(t.Resource.namespace == resolve_namespace(namespace, kind))
            rows = (await s.execute(q.order_by(t.Resource.namespace, t.Resource.name))).scalars().all()
        if label:
            rows = [r for r in rows if all(r.labels.get(k) == v for k, v in label.items())]
        return rows

    async def list_resources_all_tenants(self, kind: str) -> Sequence[t.Resource]:
        """System use only (scheduler): all resources of a kind with their tenant ids and namespaces."""
        async with self.session() as s:
            return (await s.execute(select(t.Resource).where(t.Resource.kind == kind))).scalars().all()

    async def resource_versions(self, tenant_id: str, kind: str, name: str, *, namespace: str | None = None) -> Sequence[t.ResourceVersion]:
        row = await self.get_resource_row(tenant_id, kind, name, namespace=namespace, fallback=False)
        async with self.session() as s:
            q = (
                select(t.ResourceVersion)
                .where(t.ResourceVersion.resource_id == row.id)
                .order_by(t.ResourceVersion.version.desc())
            )
            return (await s.execute(q)).scalars().all()

    async def get_resource_version(
        self, tenant_id: str, kind: str, name: str, version: int, *, namespace: str | None = None
    ) -> dict[str, Any]:
        return (await self.get_resource_version_row(tenant_id, kind, name, version, namespace=namespace)).document

    async def get_resource_version_row(
        self, tenant_id: str, kind: str, name: str, version: int, *, namespace: str | None = None
    ) -> t.ResourceVersion:
        row = await self.get_resource_row(tenant_id, kind, name, namespace=namespace)
        async with self.session() as s:
            q = select(t.ResourceVersion).where(
                t.ResourceVersion.resource_id == row.id, t.ResourceVersion.version == version
            )
            ver = (await s.execute(q)).scalar_one_or_none()
        if ver is None:
            raise NotFound(f"{kind} '{name}' version {version} not found")
        return ver

    async def delete_resource(self, tenant_id: str, kind: str, name: str, *, namespace: str | None = None) -> None:
        row = await self.get_resource_row(tenant_id, kind, name, namespace=namespace, fallback=False)
        async with self.session() as s:
            await s.execute(delete(t.Resource).where(t.Resource.id == row.id))
        self.resource_generation += 1

    # ------------------------------------------------------------------ runs

    async def create_run(self, run: t.Run, steps: list[t.StepRun], extra: Sequence[Any] = ()) -> t.Run:
        """Insert a run, its steps and ``extra`` rows (its first event, the audit entry) in one transaction.

        With an idempotency key already used in the tenant, returns that run instead.
        """
        try:
            async with self.session() as s:
                s.add(run)
                await s.flush()
                s.add_all([*steps, *extra])
                if any(isinstance(row, t.RunEvent) for row in extra):
                    await self.notify_events(s, run.id)
        except IntegrityError:
            existing = await self.run_by_idempotency_key(run.tenant_id, run.idempotency_key)
            if existing is None:
                raise
            return existing
        if run.orchestrator == "builtin" and run.next_wake_at is not None:
            await self._due([Wakeup(run.id, run.tenant_id, run.lane, run.next_wake_at)])
        return run

    async def run_by_idempotency_key(self, tenant_id: str, key: str | None) -> t.Run | None:
        if not key:
            return None
        async with self.session() as s:
            q = select(t.Run).where(t.Run.tenant_id == tenant_id, t.Run.idempotency_key == key)
            return (await s.execute(q)).scalar_one_or_none()

    async def get_run(self, tenant_id: str | None, run_id: str) -> t.Run:
        async with self.session() as s:
            run = await s.get(t.Run, run_id)
        if run is None or (tenant_id is not None and run.tenant_id != tenant_id):
            raise NotFound(f"run '{run_id}' not found")
        return run

    async def list_runs(
        self,
        tenant_id: str,
        *,
        workflow: str | None = None,
        status: str | None = None,
        campaign_id: str | None = None,
        trigger: str | None = None,
        parent_run_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        before: str | None = None,
        replica: bool = False,
    ) -> Sequence[t.Run]:
        q = select(t.Run).where(t.Run.tenant_id == tenant_id)
        if namespace:
            q = q.where(t.Run.namespace == namespace)
        if workflow:
            q = q.where(t.Run.workflow_name == workflow)
        if status:
            q = q.where(t.Run.status.in_(status.split(",")))
        if campaign_id:
            q = q.where(t.Run.campaign_id == campaign_id)
        if trigger:
            q = q.where(t.Run.trigger_name == trigger)
        if parent_run_id:
            q = q.where(t.Run.parent_run_id == parent_run_id)
        if before:
            q = q.where(t.Run.id < before)
        q = q.order_by(t.Run.id.desc()).limit(limit)
        async with (self.read_session() if replica else self.session()) as s:
            return (await s.execute(q)).scalars().all()

    async def count_runs(self, tenant_id: str, *, statuses: list[str] | None = None, **filters: Any) -> int:
        q = select(func.count()).select_from(t.Run).where(t.Run.tenant_id == tenant_id)
        if statuses:
            q = q.where(t.Run.status.in_(statuses))
        for key, value in filters.items():
            q = q.where(getattr(t.Run, key) == value)
        async with self.session() as s:
            return int((await s.execute(q)).scalar_one())

    async def run_stats(self, tenant_id: str, since: datetime, *, namespace: str | None = None) -> dict[str, int]:
        q = select(t.Run.status, func.count()).where(t.Run.tenant_id == tenant_id, t.Run.created_at >= since)
        if namespace:
            q = q.where(t.Run.namespace == namespace)
        async with self.read_session() as s:
            return {status: int(n) for status, n in (await s.execute(q.group_by(t.Run.status))).all()}

    async def run_status_counts(self, tenant_id: str) -> dict[str, int]:
        q = select(t.Run.status, func.count()).where(t.Run.tenant_id == tenant_id).group_by(t.Run.status)
        async with self.read_session() as s:
            return {status: int(n) for status, n in (await s.execute(q)).all()}

    async def run_usage(self, tenant_id: str, since: datetime, *, namespace: str | None = None) -> list[dict[str, Any]]:
        """Per-workflow run counts and summed usage (for dashboards)."""
        q = select(t.Run.workflow_name, t.Run.namespace, t.Run.status, t.Run.usage).where(
            t.Run.tenant_id == tenant_id, t.Run.created_at >= since
        )
        if namespace:
            q = q.where(t.Run.namespace == namespace)
        out: dict[tuple[str, str], dict[str, Any]] = {}
        async with self.read_session() as s:
            for workflow, ns, status, usage in (await s.execute(q)).all():
                row = out.setdefault((ns, workflow), {"namespace": ns, "workflow": workflow, "runs": 0, "succeeded": 0, "failed": 0, "cost_usd": 0.0})
                row["runs"] += 1
                row["succeeded"] += status == "succeeded"
                row["failed"] += status == "failed"
                row["cost_usd"] = round(row["cost_usd"] + float((usage or {}).get("cost_usd", 0)), 6)
        return sorted(out.values(), key=lambda r: -r["runs"])

    async def update_run(self, run_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        async with self.session() as s:
            due = await self._update_run(s, run_id, values)
        await self._due(due)

    async def _update_run(self, s: AsyncSession, run_id: str, values: dict[str, Any], *where: Any) -> list[Wakeup]:
        """Update a run; when that sets a built-in run's next_wake_at, return the wake-up to publish after commit."""
        q = update(t.Run).where(t.Run.id == run_id, *where).values(**values)
        if not self.due_hooks or values.get("next_wake_at") is None:
            await s.execute(q)
            return []
        rows = (await s.execute(q.returning(t.Run.tenant_id, t.Run.lane, t.Run.orchestrator))).all()
        return [Wakeup(run_id, tenant_id, lane, values["next_wake_at"]) for tenant_id, lane, orchestrator in rows
                if orchestrator == "builtin"]

    async def _due(self, wakeups: Sequence[Wakeup]) -> None:
        if wakeups:
            for hook in self.due_hooks:
                await hook(wakeups)

    async def wake_run(self, run_id: str, at: datetime | None = None) -> None:
        """Ask a worker to look at a run (no-op for finished runs)."""
        at = at or utcnow()
        async with self.session() as s:
            due = await self._update_run(
                s, run_id, {"next_wake_at": at},
                t.Run.status.in_([RunStatus.pending, RunStatus.running, RunStatus.waiting]),
                or_(t.Run.next_wake_at.is_(None), t.Run.next_wake_at > at),
            )
        await self._due(due)
        for hook in self.wake_hooks:
            await hook(run_id)

    async def claim_runs(
        self,
        worker_id: str,
        *,
        limit: int,
        lease: timedelta,
        tenant_limits: Callable[[set[str]], Awaitable[dict[str, int]]] | None = None,
        lanes: Sequence[str] | None = None,
        run_ids: Sequence[str] | None = None,
    ) -> list[str]:
        """Claim built-in runs that are due, taking turns across tenants.

        ``tenant_limits`` returns the most runs each tenant may have leased at once; tenants at their limit
        are skipped. ``run_ids`` only considers those runs (wake-ups from a dispatch queue).
        Optimistic per-row claim works on SQLite and Postgres.
        """
        now = utcnow()
        due = and_(
            t.Run.orchestrator == "builtin",
            t.Run.status.in_([RunStatus.pending, RunStatus.running, RunStatus.waiting]),
            t.Run.next_wake_at.is_not(None),
            t.Run.next_wake_at <= now,
            or_(t.Run.lease_expires_at.is_(None), t.Run.lease_expires_at < now),
            *([t.Run.lane.in_(list(lanes))] if lanes else []),
            *([t.Run.id.in_(list(run_ids))] if run_ids is not None else []),
        )

        def candidates(*where: Any) -> Any:
            q = select(t.Run.id, t.Run.tenant_id).where(due, *where).order_by(t.Run.next_wake_at).limit(limit * 2)
            return q if self.is_sqlite else q.with_for_update(skip_locked=True)

        claimed: list[str] = []
        async with self.session() as s:
            by_tenant: dict[str, list[str]] = {}
            for run_id, tenant_id in (await s.execute(candidates())).all():
                by_tenant.setdefault(tenant_id, []).append(run_id)
            if by_tenant and run_ids is None:  # then the oldest due runs of other tenants, so one tenant's backlog cannot starve them
                for run_id, tenant_id in (await s.execute(candidates(t.Run.tenant_id.not_in(list(by_tenant))))).all():
                    by_tenant.setdefault(tenant_id, []).append(run_id)
            room: dict[str, int] = {}
            if tenant_limits is not None and by_tenant:
                limits = await tenant_limits(set(by_tenant))
                if limits:
                    leased = select(t.Run.tenant_id, func.count()).where(
                        t.Run.tenant_id.in_(list(limits)), t.Run.lease_expires_at > now
                    ).group_by(t.Run.tenant_id)
                    active = {tid: int(n) for tid, n in (await s.execute(leased)).all()}
                    room = {tid: max(0, cap - active.get(tid, 0)) for tid, cap in limits.items()}
            # one run per tenant per turn, oldest due first within a tenant
            order: list[str] = []
            queues = [list(ids) for ids in by_tenant.values()]
            tenants = list(by_tenant)
            while any(queues):
                for tenant_id, queue in zip(tenants, queues, strict=True):
                    if queue and room.get(tenant_id, 1) > 0:
                        order.append(queue.pop(0))
                        if tenant_id in room:
                            room[tenant_id] -= 1
                    elif queue:
                        queue.clear()  # at its limit
            for run_id in order:
                if len(claimed) >= limit:
                    break
                result = await s.execute(
                    update(t.Run)
                    .where(
                        t.Run.id == run_id,
                        or_(t.Run.lease_expires_at.is_(None), t.Run.lease_expires_at < now),
                    )
                    .values(lease_owner=worker_id, lease_expires_at=now + lease, lease_losses=_LOST_LEASE)
                )
                if result.rowcount:
                    claimed.append(run_id)
        return claimed

    async def dispatch_view(self, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """What a worker needs to decide about a wake-up it could not claim."""
        if not run_ids:
            return {}
        cols = (t.Run.id, t.Run.status, t.Run.orchestrator, t.Run.next_wake_at, t.Run.lease_expires_at)
        async with self.session() as s:
            rows = (await s.execute(select(*cols).where(t.Run.id.in_(list(run_ids))))).all()
        return {
            row.id: {"status": row.status, "orchestrator": row.orchestrator, "next_wake_at": row.next_wake_at,
                     "lease_expires_at": row.lease_expires_at}
            for row in rows
        }

    async def due_runs(
        self, orchestrator: str, before: datetime, *, include_leased: bool = False, limit: int = 200
    ) -> Sequence[t.Run]:
        """Unfinished runs of an orchestrator that are due by ``before`` (leased or not).

        ``include_leased`` adds every run whose lease is held, finished or not: one still being advanced.
        """
        due = and_(
            t.Run.status.in_([RunStatus.pending, RunStatus.running, RunStatus.waiting]),
            t.Run.next_wake_at.is_not(None),
            t.Run.next_wake_at <= before,
        )
        if include_leased:
            due = or_(due, t.Run.lease_expires_at > utcnow())
        q = select(t.Run).where(t.Run.orchestrator == orchestrator, due).order_by(t.Run.id).limit(limit)
        async with self.session() as s:
            return (await s.execute(q)).scalars().all()

    async def unfinished_runs_by_orchestrator(self) -> dict[str, int]:
        """How many unfinished runs each orchestrator drives (every tenant)."""
        q = (select(t.Run.orchestrator, func.count())
             .where(t.Run.status.in_([RunStatus.pending, RunStatus.running, RunStatus.waiting]))
             .group_by(t.Run.orchestrator))
        async with self.session() as s:
            return {name: count for name, count in (await s.execute(q)).all()}

    async def lease_run(self, run_id: str, worker_id: str, lease: timedelta) -> bool:
        """Take the lease on one run (free, expired or already ours); for orchestrators that schedule runs themselves."""
        now = utcnow()
        async with self.session() as s:
            result = await s.execute(
                update(t.Run)
                .where(
                    t.Run.id == run_id,
                    or_(t.Run.lease_expires_at.is_(None), t.Run.lease_expires_at < now, t.Run.lease_owner == worker_id),
                )
                .values(lease_owner=worker_id, lease_expires_at=now + lease, lease_losses=_LOST_LEASE)
            )
            return bool(result.rowcount)

    async def extend_lease(self, run_id: str, worker_id: str, lease: timedelta) -> bool:
        async with self.session() as s:
            result = await s.execute(
                update(t.Run)
                .where(t.Run.id == run_id, t.Run.lease_owner == worker_id)
                .values(lease_expires_at=utcnow() + lease)
            )
            return bool(result.rowcount)

    async def release_lease(self, run_id: str, worker_id: str) -> None:
        async with self.session() as s:
            await s.execute(
                update(t.Run)
                .where(t.Run.id == run_id, t.Run.lease_owner == worker_id)
                .values(lease_owner=None, lease_expires_at=None, lease_losses=0)
            )

    # ------------------------------------------------------------------ steps

    async def get_steps(self, run_id: str) -> list[t.StepRun]:
        async with self.session() as s:
            q = select(t.StepRun).where(t.StepRun.run_id == run_id)
            return list((await s.execute(q)).scalars().all())

    async def get_step(self, run_id: str, step_id: str) -> t.StepRun:
        async with self.session() as s:
            q = select(t.StepRun).where(t.StepRun.run_id == run_id, t.StepRun.step_id == step_id)
            step = (await s.execute(q)).scalar_one_or_none()
        if step is None:
            raise NotFound(f"step '{step_id}' not found")
        return step

    async def update_step_if_unchanged(self, step: t.StepRun, **values: Any) -> bool:
        """Compare-and-set: update the step only if nobody saved it since it was read."""
        values["updated_at"] = utcnow()
        async with self.session() as s:
            result = await s.execute(
                update(t.StepRun)
                .where(t.StepRun.id == step.id, t.StepRun.updated_at == step.updated_at)
                .values(**values)
            )
            return bool(result.rowcount)

    async def save_step(self, step: t.StepRun) -> None:
        step.updated_at = utcnow()
        async with self.session() as s:
            await s.merge(step)

    async def checkpoint(
        self,
        run_id: str,
        tenant_id: str,
        *,
        steps: Sequence[t.StepRun] = (),
        run_values: dict[str, Any] | None = None,
        events: Sequence[tuple[str, dict[str, Any], str | None]] = (),
        clear_waits: str | bool | None = None,
        set_waits: tuple[str, list[str]] | None = None,
        release_lease: str | None = None,
    ) -> None:
        """Persist one step of progress in a single transaction.

        Saves ``steps``, updates the run with ``run_values``, appends ``events`` (type, data, step id), clears
        wait keys (a step id, or ``True`` for the whole run), replaces one step's wait keys (``set_waits``) and
        releases ``release_lease``'s lease on the run.
        """
        now = utcnow()
        async with self.session() as s:
            for step in steps:
                step.updated_at = now
                await s.merge(step)
            values = dict(run_values or {})
            if release_lease is not None:
                await s.execute(
                    update(t.Run).where(t.Run.id == run_id, t.Run.lease_owner == release_lease)
                    .values(lease_owner=None, lease_expires_at=None, lease_losses=0)
                )
            due = await self._update_run(s, run_id, {**values, "updated_at": now}) if values else []
            if clear_waits:
                q = delete(t.WaitSubscription).where(t.WaitSubscription.run_id == run_id)
                if isinstance(clear_waits, str):
                    q = q.where(t.WaitSubscription.step_id == clear_waits)
                await s.execute(q)
            if set_waits is not None:
                step_id, keys = set_waits
                await s.execute(
                    delete(t.WaitSubscription).where(t.WaitSubscription.run_id == run_id, t.WaitSubscription.step_id == step_id)
                )
                s.add_all(
                    [t.WaitSubscription(tenant_id=tenant_id, key=k, run_id=run_id, step_id=step_id) for k in sorted(set(keys))]
                )
            if events:
                s.add_all(
                    [t.RunEvent(tenant_id=tenant_id, run_id=run_id, step_id=sid, type=kind, data=data) for kind, data, sid in events]
                )
                await self.notify_events(s, run_id)
        await self._due(due)

    # ------------------------------------------------------------------ events

    async def add_event(
        self, tenant_id: str, run_id: str, type: str, data: dict[str, Any] | None = None, step_id: str | None = None
    ) -> t.RunEvent:
        event = t.RunEvent(tenant_id=tenant_id, run_id=run_id, step_id=step_id, type=type, data=data or {})
        async with self.session() as s:
            s.add(event)
            await self.notify_events(s, run_id)
        return event

    async def notify_events(self, session: AsyncSession, run_id: str) -> None:
        """Tell API processes listening on Postgres that a run has new events (sent when the transaction commits)."""
        if not self.is_sqlite:
            await session.execute(text("SELECT pg_notify(:channel, :run_id)"), {"channel": EVENTS_CHANNEL, "run_id": run_id})

    async def list_events(
        self, tenant_id: str, run_id: str, *, after: int = 0, limit: int = 500, step_id: str | None = None
    ) -> Sequence[t.RunEvent]:
        q = select(t.RunEvent).where(
            t.RunEvent.tenant_id == tenant_id, t.RunEvent.run_id == run_id, t.RunEvent.id > after
        )
        if step_id:
            q = q.where(t.RunEvent.step_id == step_id)
        async with self.session() as s:
            return (await s.execute(q.order_by(t.RunEvent.id).limit(limit))).scalars().all()

    # ------------------------------------------------------------------ wait subscriptions

    async def set_wait_keys(self, tenant_id: str, run_id: str, step_id: str, keys: list[str]) -> None:
        async with self.session() as s:
            await s.execute(
                delete(t.WaitSubscription).where(
                    t.WaitSubscription.run_id == run_id, t.WaitSubscription.step_id == step_id
                )
            )
            for key in sorted(set(keys)):
                s.add(t.WaitSubscription(tenant_id=tenant_id, key=key, run_id=run_id, step_id=step_id))

    async def clear_wait_keys(self, run_id: str, step_id: str | None = None) -> None:
        q = delete(t.WaitSubscription).where(t.WaitSubscription.run_id == run_id)
        if step_id:
            q = q.where(t.WaitSubscription.step_id == step_id)
        async with self.session() as s:
            await s.execute(q)

    async def wake_by_keys(self, tenant_id: str, keys: list[str]) -> list[str]:
        if not keys:
            return []
        async with self.session() as s:
            q = select(t.WaitSubscription.run_id, t.WaitSubscription.step_id).where(
                t.WaitSubscription.tenant_id == tenant_id, t.WaitSubscription.key.in_(keys)
            )
            matches = (await s.execute(q)).all()
            now = utcnow()
            for run_id, step_id in matches:
                await s.execute(
                    update(t.StepRun)
                    .where(t.StepRun.run_id == run_id, t.StepRun.step_id == step_id)
                    .values(wake_at=now)
                )
        run_ids = sorted({m[0] for m in matches})
        for run_id in run_ids:
            await self.wake_run(run_id)
        return run_ids

    # ------------------------------------------------------------------ webhooks & triggers

    async def webhook_for_connection(
        self, tenant_id: str, connection_name: str, *, namespace: str | None = None
    ) -> t.WebhookEndpoint:
        ns = namespace or current_namespace()
        async with self.session() as s:
            q = select(t.WebhookEndpoint).where(
                t.WebhookEndpoint.tenant_id == tenant_id,
                t.WebhookEndpoint.namespace == ns,
                t.WebhookEndpoint.connection_name == connection_name,
            )
            endpoint = (await s.execute(q)).scalar_one_or_none()
            if endpoint is None:
                endpoint = t.WebhookEndpoint(
                    id=f"wh_{pysecrets.token_urlsafe(18)}", tenant_id=tenant_id, namespace=ns, connection_name=connection_name
                )
                s.add(endpoint)
            return endpoint

    async def get_webhook_endpoint(self, webhook_id: str) -> t.WebhookEndpoint | None:
        async with self.session() as s:
            return await s.get(t.WebhookEndpoint, webhook_id)

    async def resolve_webhook(self, webhook_id: str) -> t.WebhookEndpoint | None:
        async with self.session() as s:
            endpoint = await s.get(t.WebhookEndpoint, webhook_id)
            if endpoint is not None:
                endpoint.last_received_at = utcnow()
            return endpoint

    async def enqueue_webhook(self, endpoint: t.WebhookEndpoint, headers: dict[str, str], body: bytes) -> str:
        row = t.WebhookInbox(
            id=new_id("whk"), tenant_id=endpoint.tenant_id, webhook_id=endpoint.id, headers=headers,
            body=base64.b64encode(body).decode(), status="queued", attempts=0, next_attempt_at=utcnow(),
        )
        async with self.session() as s:
            s.add(row)
        return row.id

    async def claim_webhooks(self, *, limit: int, lease: timedelta) -> list[t.WebhookInbox]:
        """Take queued deliveries that are due (and ones whose worker died mid-way)."""
        now = utcnow()
        q = (
            select(t.WebhookInbox)
            .where(
                t.WebhookInbox.status == "queued",
                t.WebhookInbox.next_attempt_at <= now,
                or_(t.WebhookInbox.lease_expires_at.is_(None), t.WebhookInbox.lease_expires_at < now),
            )
            .order_by(t.WebhookInbox.next_attempt_at)
            .limit(limit)
        )
        async with self.session() as s:
            if not self.is_sqlite:
                q = q.with_for_update(skip_locked=True)
            rows = list((await s.execute(q)).scalars().all())
            for row in rows:
                row.lease_expires_at = now + lease
                row.attempts += 1
        return rows

    async def finish_webhook(self, row_id: str, *, result: dict[str, Any] | None = None, retry_at: datetime | None = None,
                             failed: bool = False) -> None:
        values: dict[str, Any] = {"lease_expires_at": None, "result": result}
        if retry_at is not None:
            values["next_attempt_at"] = retry_at
        else:
            values["status"] = "error" if failed else "done"
        async with self.session() as s:
            await s.execute(update(t.WebhookInbox).where(t.WebhookInbox.id == row_id).values(**values))

    async def get_webhook_delivery(self, row_id: str) -> t.WebhookInbox | None:
        async with self.session() as s:
            return await s.get(t.WebhookInbox, row_id)

    async def prune_webhooks(self, older_than: datetime) -> int:
        async with self.session() as s:
            result = await s.execute(
                delete(t.WebhookInbox).where(t.WebhookInbox.status != "queued", t.WebhookInbox.created_at < older_than)
            )
            return result.rowcount or 0

    async def record_delivery(self, delivery: t.TriggerDelivery) -> bool:
        """Insert a delivery; returns False when the dedupe key was already seen."""
        try:
            async with self.session() as s:
                s.add(delivery)
            return True
        except IntegrityError:
            return False

    async def recent_deliveries(
        self, tenant_id: str, namespace: str, trigger_name: str, *, since: datetime, status: str | None = None
    ) -> list[t.TriggerDelivery]:
        q = select(t.TriggerDelivery).where(
            t.TriggerDelivery.tenant_id == tenant_id, t.TriggerDelivery.namespace == namespace,
            t.TriggerDelivery.trigger_name == trigger_name, t.TriggerDelivery.created_at >= since,
        )
        if status:
            q = q.where(t.TriggerDelivery.status == status)
        async with self.session() as s:
            return list((await s.execute(q.limit(10000))).scalars().all())

    async def update_delivery(self, delivery_id: str, **values: Any) -> None:
        async with self.session() as s:
            await s.execute(update(t.TriggerDelivery).where(t.TriggerDelivery.id == delivery_id).values(**values))

    async def list_deliveries(
        self, tenant_id: str, trigger_name: str, limit: int = 50, *, namespace: str | None = None
    ) -> Sequence[t.TriggerDelivery]:
        q = (
            select(t.TriggerDelivery)
            .where(
                t.TriggerDelivery.tenant_id == tenant_id,
                t.TriggerDelivery.namespace == (namespace or current_namespace()),
                t.TriggerDelivery.trigger_name == trigger_name,
            )
            .order_by(t.TriggerDelivery.created_at.desc())
            .limit(limit)
        )
        async with self.session() as s:
            return (await s.execute(q)).scalars().all()

    async def add_connection_activity(self, row: t.ConnectionActivity) -> int:
        async with self.session() as s:
            s.add(row)
            await s.flush()
            return row.id

    async def update_connection_activity(self, activity_id: int, **values: Any) -> None:
        async with self.session() as s:
            await s.execute(
                update(t.ConnectionActivity).where(t.ConnectionActivity.id == activity_id).values(updated_at=utcnow(), **values)
            )

    async def record_connection_use(
        self, tenant_id: str, namespace: str, connection: str, run_id: str, step_id: str | None, action: str | None,
        *, max_actions: int = 20,
    ) -> None:
        """Count one use of a connection by a run step: one row per (run, step), its calls counted."""
        a = t.ConnectionActivity
        async with self.session() as s:
            q = select(a).where(
                a.tenant_id == tenant_id, a.namespace == namespace, a.connection_name == connection, a.kind == "use",
                a.run_id == run_id, a.step_id.is_(None) if step_id is None else a.step_id == step_id,
            )
            row = (await s.execute(q.limit(1))).scalar_one_or_none()
            if row is not None:
                actions = list((row.data or {}).get("actions") or [])
                if action and action not in actions and len(actions) < max_actions:
                    row.data = {**row.data, "actions": [*actions, action]}
                row.count = (row.count or 0) + 1
                row.updated_at = utcnow()
                return
            run = await s.get(t.Run, run_id)
            s.add(a(
                tenant_id=tenant_id, namespace=namespace, connection_name=connection, kind="use", status="ok",
                run_id=run_id, step_id=step_id, run_namespace=run.namespace if run else None,
                workflow_name=run.workflow_name if run else None, actor=(run.started_by if run else None),
                data={"actions": [action] if action else []}, count=1,
            ))

    async def list_connection_activity(
        self, tenant_id: str, namespace: str, connection: str, *, kind: str | None = None, status: str | None = None,
        since: datetime | None = None, run_id: str | None = None, before: int | None = None, limit: int = 50,
    ) -> Sequence[t.ConnectionActivity]:
        a = t.ConnectionActivity
        q = select(a).where(a.tenant_id == tenant_id, a.namespace == namespace, a.connection_name == connection)
        if kind:
            q = q.where(a.kind.in_(kind.split(",")))
        if status:
            q = q.where(a.status.in_(status.split(",")))
        if since:
            q = q.where(a.updated_at >= since)
        if run_id:
            q = q.where(a.run_id == run_id)
        if before:
            q = q.where(a.id < before)
        async with self.read_session() as s:
            return (await s.execute(q.order_by(a.id.desc()).limit(limit))).scalars().all()

    async def connection_activity_summary(self, tenant_id: str, namespace: str, connection: str) -> dict[str, Any]:
        """Counts by kind and status, and the oldest entry kept (what the retention limit left)."""
        a = t.ConnectionActivity
        where = (a.tenant_id == tenant_id, a.namespace == namespace, a.connection_name == connection)
        async with self.read_session() as s:
            counts = (await s.execute(select(a.kind, a.status, func.count()).where(*where).group_by(a.kind, a.status))).all()
            oldest = (await s.execute(select(func.min(a.created_at)).where(*where))).scalar()
        out: dict[str, dict[str, int]] = {}
        for kind, status, n in counts:
            out.setdefault(kind, {})[status] = n
        return {"counts": out, "oldest": oldest}

    async def prune_connection_activity(self, older_than: datetime | None, max_rows: int) -> int:
        """Delete entries older than ``older_than``, then each connection's oldest beyond ``max_rows`` (0: no cap)."""
        a = t.ConnectionActivity
        removed = 0
        async with self.session() as s:
            if older_than is not None:
                removed += (await s.execute(delete(a).where(a.updated_at < older_than))).rowcount or 0
            if not max_rows:
                return removed
            over = (await s.execute(
                select(a.tenant_id, a.namespace, a.connection_name)
                .group_by(a.tenant_id, a.namespace, a.connection_name)
                .having(func.count() > max_rows)
            )).all()
            for tenant_id, namespace, connection in over:
                where = (a.tenant_id == tenant_id, a.namespace == namespace, a.connection_name == connection)
                cutoff = (await s.execute(
                    select(a.id).where(*where).order_by(a.id.desc()).offset(max_rows - 1).limit(1)
                )).scalar()
                if cutoff is not None:
                    removed += (await s.execute(delete(a).where(*where, a.id < cutoff))).rowcount or 0
        return removed

    async def get_trigger_state(self, tenant_id: str, trigger_name: str, *, namespace: str | None = None) -> t.TriggerState:
        ns = namespace or current_namespace()
        async with self.session() as s:
            q = select(t.TriggerState).where(
                t.TriggerState.tenant_id == tenant_id,
                t.TriggerState.namespace == ns,
                t.TriggerState.trigger_name == trigger_name,
            )
            state = (await s.execute(q)).scalar_one_or_none()
            if state is None:
                state = t.TriggerState(id=new_id("trs"), tenant_id=tenant_id, namespace=ns, trigger_name=trigger_name)
                s.add(state)
            return state

    async def save_trigger_state(self, state: t.TriggerState) -> None:
        async with self.session() as s:
            await s.merge(state)

    async def claim_trigger_tick(self, state_id: str, expected_next: datetime | None, new_next: datetime) -> bool:
        """Advance a schedule exactly once across workers (compare-and-set on next_fire_at)."""
        cond = t.TriggerState.next_fire_at.is_(None) if expected_next is None else (
            t.TriggerState.next_fire_at == expected_next
        )
        async with self.session() as s:
            result = await s.execute(
                update(t.TriggerState)
                .where(t.TriggerState.id == state_id, cond)
                .values(next_fire_at=new_next, last_fired_at=utcnow(), fire_count=t.TriggerState.fire_count + 1)
            )
            return bool(result.rowcount)

    async def set_trigger_next(self, state_id: str, next_fire_at: datetime | None, *, schedule: str | None = None) -> None:
        """Set the next fire time; ``schedule`` records the cron and time zone it was computed from."""
        async with self.session() as s:
            values: dict[str, Any] = {"next_fire_at": next_fire_at}
            if schedule is not None:
                state = await s.get(t.TriggerState, state_id)
                values["cursor"] = {**((state.cursor if state else None) or {}), "schedule": schedule}
            await s.execute(update(t.TriggerState).where(t.TriggerState.id == state_id).values(**values))

    # ------------------------------------------------------------------ campaigns

    async def create_campaign(self, row: t.CampaignRow) -> t.CampaignRow:
        async with self.session() as s:
            s.add(row)
        return row

    async def get_campaign(self, tenant_id: str | None, campaign_id: str) -> t.CampaignRow:
        async with self.session() as s:
            row = await s.get(t.CampaignRow, campaign_id)
        if row is None or (tenant_id is not None and row.tenant_id != tenant_id):
            raise NotFound(f"campaign '{campaign_id}' not found")
        return row

    async def list_campaigns(
        self, tenant_id: str, status: str | None = None, *, namespace: str | None = None
    ) -> Sequence[t.CampaignRow]:
        q = select(t.CampaignRow).where(t.CampaignRow.tenant_id == tenant_id)
        if namespace:
            q = q.where(t.CampaignRow.namespace == namespace)
        if status:
            q = q.where(t.CampaignRow.status == status)
        async with self.session() as s:
            return (await s.execute(q.order_by(t.CampaignRow.created_at.desc()))).scalars().all()

    async def update_campaign(self, campaign_id: str, **values: Any) -> None:
        async with self.session() as s:
            await s.execute(update(t.CampaignRow).where(t.CampaignRow.id == campaign_id).values(**values))

    async def due_campaigns(self, limit: int = 50) -> Sequence[t.CampaignRow]:
        now = utcnow()
        q = (
            select(t.CampaignRow)
            .where(
                t.CampaignRow.status == "running",
                or_(t.CampaignRow.next_tick_at.is_(None), t.CampaignRow.next_tick_at <= now),
                or_(t.CampaignRow.lease_expires_at.is_(None), t.CampaignRow.lease_expires_at < now),
            )
            .limit(limit)
        )
        async with self.session() as s:
            return (await s.execute(q)).scalars().all()

    async def claim_campaign(self, campaign_id: str, lease: timedelta) -> bool:
        now = utcnow()
        async with self.session() as s:
            result = await s.execute(
                update(t.CampaignRow)
                .where(
                    t.CampaignRow.id == campaign_id,
                    or_(t.CampaignRow.lease_expires_at.is_(None), t.CampaignRow.lease_expires_at < now),
                )
                .values(lease_expires_at=now + lease)
            )
            return bool(result.rowcount)

    async def add_targets(self, targets: list[t.CampaignTarget]) -> int:
        added = 0
        for target in targets:
            try:
                async with self.session() as s:
                    s.add(target)
                added += 1
            except IntegrityError:
                continue
        return added

    async def list_targets(
        self, tenant_id: str, campaign_id: str, *, status: str | None = None, limit: int = 1000, offset: int = 0
    ) -> Sequence[t.CampaignTarget]:
        q = select(t.CampaignTarget).where(
            t.CampaignTarget.tenant_id == tenant_id, t.CampaignTarget.campaign_id == campaign_id
        )
        if status:
            q = q.where(t.CampaignTarget.status.in_(status.split(",")))
        async with self.session() as s:
            return (await s.execute(q.order_by(t.CampaignTarget.key).offset(offset).limit(limit))).scalars().all()

    async def target_counts(self, campaign_id: str) -> dict[str, int]:
        q = (
            select(t.CampaignTarget.status, func.count())
            .where(t.CampaignTarget.campaign_id == campaign_id)
            .group_by(t.CampaignTarget.status)
        )
        async with self.session() as s:
            return {status: int(n) for status, n in (await s.execute(q)).all()}

    async def campaign_cost(self, campaign_id: str) -> float:
        """Spend so far: finished targets plus what their runs still in flight have used."""
        q = select(func.coalesce(func.sum(t.CampaignTarget.cost_usd), 0.0)).where(
            t.CampaignTarget.campaign_id == campaign_id
        )
        running = select(t.Run.usage).where(
            t.Run.campaign_id == campaign_id,
            t.Run.campaign_target_id.is_not(None),
            t.Run.status.in_([RunStatus.pending, RunStatus.running, RunStatus.waiting]),
        )
        async with self.session() as s:
            finished = float((await s.execute(q)).scalar_one())
            in_flight = sum(float((usage or {}).get("cost_usd", 0) or 0) for usage in (await s.execute(running)).scalars())
        return finished + in_flight

    async def runs_started_since(self, campaign_id: str, since: datetime) -> int:
        q = select(func.count()).select_from(t.CampaignTarget).where(
            t.CampaignTarget.campaign_id == campaign_id, t.CampaignTarget.started_at >= since
        )
        async with self.session() as s:
            return int((await s.execute(q)).scalar_one())

    async def update_target(self, target_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        async with self.session() as s:
            await s.execute(update(t.CampaignTarget).where(t.CampaignTarget.id == target_id).values(**values))

    async def reset_targets(self, campaign_id: str, statuses: list[str]) -> int:
        async with self.session() as s:
            result = await s.execute(
                update(t.CampaignTarget)
                .where(and_(t.CampaignTarget.campaign_id == campaign_id, t.CampaignTarget.status.in_(statuses)))
                .values(status="pending", run_id=None, result=None, updated_at=utcnow())
            )
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------ secrets
    # Secrets never fall back to the shared namespace: a namespace only reads its own.

    async def put_secret(self, tenant_id: str, name: str, ciphertext: str, *, namespace: str | None = None) -> None:
        ns = namespace or current_namespace()
        async with self.session() as s:
            q = select(t.Secret).where(t.Secret.tenant_id == tenant_id, t.Secret.namespace == ns, t.Secret.name == name)
            row = (await s.execute(q)).scalar_one_or_none()
            if row is None:
                s.add(t.Secret(id=new_id("sec"), tenant_id=tenant_id, namespace=ns, name=name, ciphertext=ciphertext))
            else:
                row.ciphertext = ciphertext
                row.updated_at = utcnow()

    async def get_secret(self, tenant_id: str, name: str, *, namespace: str | None = None) -> str | None:
        ns = namespace or current_namespace()
        async with self.session() as s:
            q = select(t.Secret.ciphertext).where(t.Secret.tenant_id == tenant_id, t.Secret.namespace == ns, t.Secret.name == name)
            return (await s.execute(q)).scalar_one_or_none()

    async def list_secrets(self, tenant_id: str, *, namespace: str | None = None) -> Sequence[t.Secret]:
        ns = namespace or current_namespace()
        async with self.session() as s:
            q = select(t.Secret).where(t.Secret.tenant_id == tenant_id, t.Secret.namespace == ns).order_by(t.Secret.name)
            return (await s.execute(q)).scalars().all()

    async def delete_secret(self, tenant_id: str, name: str, *, namespace: str | None = None) -> None:
        ns = namespace or current_namespace()
        async with self.session() as s:
            result = await s.execute(
                delete(t.Secret).where(t.Secret.tenant_id == tenant_id, t.Secret.namespace == ns, t.Secret.name == name)
            )
            if not result.rowcount:
                raise NotFound(f"secret '{name}' not found")

    # ------------------------------------------------------------------ audit

    # ------------------------------------------------------------------ instance settings

    async def get_instance_setting(self, key: str) -> t.InstanceSetting | None:
        async with self.session() as s:
            return await s.get(t.InstanceSetting, key)

    async def put_instance_setting(self, key: str, value: dict[str, Any], *, actor: str | None) -> t.InstanceSetting:
        async with self.session() as s:
            row = await s.get(t.InstanceSetting, key)
            if row is None:
                row = t.InstanceSetting(key=key, value=value, revision=1, updated_by=actor, updated_at=utcnow())
                s.add(row)
            else:
                row.value, row.revision, row.updated_by, row.updated_at = value, row.revision + 1, actor, utcnow()
            await s.flush()
            return row

    async def delete_instance_setting(self, key: str) -> None:
        async with self.session() as s:
            await s.execute(delete(t.InstanceSetting).where(t.InstanceSetting.key == key))

    async def audit(
        self,
        tenant_id: str | None,
        actor: str | None,
        action: str,
        target: str | None = None,
        *,
        outcome: str = "success",
        namespace: str | None = None,
        **data: Any,
    ) -> None:
        """Append an audit entry; request id, client and source come from the request context."""
        entry = self.audit_entry(tenant_id, actor, action, target, outcome=outcome, namespace=namespace, **data)
        async with self.session() as s:
            s.add(entry)

    def audit_entry(
        self,
        tenant_id: str | None,
        actor: str | None,
        action: str,
        target: str | None = None,
        *,
        outcome: str = "success",
        namespace: str | None = None,
        **data: Any,
    ) -> t.AuditEntry:
        """An audit row to add in the caller's own transaction (marks the request as audited)."""
        req = current_request()
        if req is not None:
            req.audited = True
            if req.via:
                data.setdefault("via", req.via)
        return t.AuditEntry(
            tenant_id=tenant_id, actor=actor, action=action, target=target, data=data, outcome=outcome,
            namespace=namespace, source=req.source if req else "engine", request_id=req.request_id if req else None,
            ip=req.ip if req else None, user_agent=(req.user_agent or "")[:300] if req else None,
        )

    def _audit_query(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        namespace: str | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        before: int | None = None,
        namespaces: Sequence[str] | None = None,
    ) -> Any:
        q = select(t.AuditEntry).where(t.AuditEntry.tenant_id == tenant_id)
        if namespaces is not None:  # tenant-wide entries too, except those about other namespaces
            e = t.AuditEntry
            q = q.where(or_(e.namespace.in_(namespaces),
                            and_(e.namespace.is_(None), or_(~e.action.startswith("namespace."), e.target.in_(namespaces)))))
        if action:
            q = q.where(t.AuditEntry.action.startswith(action))
        if actor:
            q = q.where(t.AuditEntry.actor == actor)
        if target:
            q = q.where(t.AuditEntry.target.startswith(target))
        if namespace:
            q = q.where(t.AuditEntry.namespace == namespace)
        if outcome:
            q = q.where(t.AuditEntry.outcome == outcome)
        if since:
            q = q.where(t.AuditEntry.created_at >= since)
        if until:
            q = q.where(t.AuditEntry.created_at < until)
        if before:
            q = q.where(t.AuditEntry.id < before)
        return q.order_by(t.AuditEntry.id.desc())

    async def list_audit(self, tenant_id: str, limit: int = 100, **filters: Any) -> Sequence[t.AuditEntry]:
        async with self.read_session() as s:
            return (await s.execute(self._audit_query(tenant_id, **filters).limit(limit))).scalars().all()

    async def prune_run_events(self, older_than: datetime, *, batch: int = 10_000) -> int:
        """Delete events older than ``older_than`` of finished runs, one batch at a time (short locks)."""
        finished = select(t.Run.id).where(t.Run.status.in_([s.value for s in RunStatus if s.terminal]))
        ids = (
            select(t.RunEvent.id)
            .where(t.RunEvent.created_at < older_than, t.RunEvent.run_id.in_(finished))
            .limit(batch)
        )
        async with self.session() as s:
            result = await s.execute(delete(t.RunEvent).where(t.RunEvent.id.in_(ids)))
            return result.rowcount or 0

    async def prune_audit(self, older_than: datetime) -> int:
        async with self.session() as s:
            result = await s.execute(delete(t.AuditEntry).where(t.AuditEntry.created_at < older_than))
            return result.rowcount or 0


def new_step_row(tenant_id: str, run_id: str, step_id: str, step_type: str) -> t.StepRun:
    return t.StepRun(
        id=new_id("stp"),
        tenant_id=tenant_id,
        run_id=run_id,
        step_id=step_id,
        type=step_type,
        status=StepStatus.pending,
        attempt=0,
        state={},
        usage={},
    )


def store_from_settings(settings: Any) -> Store:
    return Store(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pgbouncer=settings.db_pgbouncer,
        direct_url=settings.database_direct_url,
        read_url=settings.database_read_url,
    )
