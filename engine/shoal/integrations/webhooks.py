"""Webhooks Shoal registers with providers on behalf of triggers (``Trigger.spec.webhook``).

The trigger is the desired state; a ``WebhookRegistration`` row and the provider's webhook are the
actual state. ``sync`` brings one trigger's registration in line (the API calls it whenever a
trigger changes, so people see the outcome straight away); ``reconcile`` does the same for every
trigger in the background, which also covers triggers changed by blueprints or skills, refreshes
webhooks before the provider expires them, retries failures with backoff and removes the webhooks
of triggers that were deleted, disabled or lost their ``webhook`` block.

Each registration has its own callback URL (``<connection webhook URL>/<registration id>``) and its
own signing secret, and a trigger with ``webhook`` only fires from deliveries to that URL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from shoal.core import clock
from shoal.core.errors import InvalidRequest, NotFound
from shoal.core.ids import new_id
from shoal.core.models import Connection, Trigger
from shoal.core.scope import use_namespace
from shoal.integrations.base import ConnectionContext, Integration, WebhookRegistration
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

RECONCILE_EVERY = timedelta(seconds=60)
REFRESH_BEFORE = timedelta(days=7)
LEASE = timedelta(minutes=2)
MAX_BACKOFF = timedelta(hours=1)


def wants_webhook(trigger: Trigger) -> bool:
    spec = trigger.spec
    return spec.webhook is not None and spec.enabled and spec.event != "schedule" and bool(spec.connection)


class WebhookRegistrations:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._last_reconcile: datetime | None = None

    @property
    def store(self) -> Any:
        return self.engine.store

    # ------------------------------------------------------------------ desired state

    async def _connection(self, tenant_id: str, namespace: str, name: str) -> tuple[t.Resource, Integration]:
        with use_namespace(namespace):
            row = await self.store.get_resource_row(tenant_id, "Connection", name)
        integration = self.engine.integrations.get(Connection.model_validate(row.document).spec.integration)
        return row, integration

    @staticmethod
    def config(integration: Integration, trigger: Trigger) -> Any:
        if integration.webhook_model is None:
            raise InvalidRequest(f"the {integration.key} integration cannot register webhooks; remove 'webhook'")
        assert trigger.spec.webhook is not None
        try:
            return integration.webhook_model.model_validate(trigger.spec.webhook.model_dump())
        except ValueError as exc:
            raise InvalidRequest(f"invalid webhook settings for {integration.key}: {exc}") from exc

    async def validate(self, tenant_id: str, namespace: str, trigger: Trigger) -> None:
        """Apply-time check of ``spec.webhook`` against the connection's integration."""
        if trigger.spec.webhook is None or not trigger.spec.connection:
            return
        try:
            _, integration = await self._connection(tenant_id, namespace, trigger.spec.connection)
        except NotFound:
            return  # the connection may be applied later; reconcile reports it
        self.config(integration, trigger)

    async def registration(self, row: t.WebhookRegistration, trigger: Trigger, integration: Integration, url: str) -> WebhookRegistration:
        return WebhookRegistration(
            id=row.id,
            url=url,
            name=f"Shoal {row.namespace}/{row.trigger_name}",
            event=trigger.spec.event,
            config=self.config(integration, trigger),
            secret=self.engine.secrets.cipher.decrypt(row.secret),
            state=dict(row.state or {}),
        )

    async def _callback_url(self, tenant_id: str, namespace: str, connection: str, registration_id: str) -> str:
        with use_namespace(namespace):
            base = await self.engine.integrations.webhook_url(tenant_id, connection)
        return f"{base}/{registration_id}"

    async def context(self, tenant_id: str, namespace: str, connection: str, actor: str | None) -> ConnectionContext:
        """The connection as whoever registered the webhook (their own credential, for per-user connections)."""
        initiator = None
        if actor:
            initiator = await self.engine.identities.resolve(tenant_id, actor=actor, source="trigger")
        with use_namespace(namespace):
            return await self.engine.integrations.connection(tenant_id, connection, initiator=initiator)

    # ------------------------------------------------------------------ rows

    async def get(self, tenant_id: str, namespace: str, trigger_name: str) -> t.WebhookRegistration | None:
        async with self.store.session() as s:
            q = select(t.WebhookRegistration).where(
                t.WebhookRegistration.tenant_id == tenant_id,
                t.WebhookRegistration.namespace == namespace,
                t.WebhookRegistration.trigger_name == trigger_name,
            )
            return (await s.execute(q)).scalar_one_or_none()

    async def by_id(self, registration_id: str) -> t.WebhookRegistration | None:
        async with self.store.session() as s:
            return await s.get(t.WebhookRegistration, registration_id)

    async def for_connection(self, tenant_id: str, connection_namespace: str, connection: str) -> list[t.WebhookRegistration]:
        """Registrations whose trigger uses this connection (from its own namespace or, for shared ones, any)."""
        async with self.store.session() as s:
            q = select(t.WebhookRegistration).where(
                t.WebhookRegistration.tenant_id == tenant_id, t.WebhookRegistration.connection_name == connection
            ).order_by(t.WebhookRegistration.namespace, t.WebhookRegistration.trigger_name)
            rows = list((await s.execute(q)).scalars().all())
        out = []
        for row in rows:
            try:
                conn_row, _ = await self._connection(tenant_id, row.namespace, connection)
            except NotFound:
                continue
            if conn_row.namespace == connection_namespace:
                out.append(row)
        return out

    async def _create(self, tenant_id: str, namespace: str, trigger: Trigger, integration: Integration) -> t.WebhookRegistration:
        row = t.WebhookRegistration(
            id=new_id("whr"),
            tenant_id=tenant_id,
            namespace=namespace,
            trigger_name=trigger.metadata.name,
            connection_name=trigger.spec.connection or "",
            integration=integration.key,
            secret=self.engine.secrets.cipher.encrypt(secrets.token_urlsafe(32)),
            state={},
        )
        try:
            async with self.store.session() as s:
                s.add(row)
        except IntegrityError:  # another worker created it first
            existing = await self.get(tenant_id, namespace, trigger.metadata.name)
            assert existing is not None
            return existing
        return row

    async def _claim(self, row_id: str) -> bool:
        """Take the row's lease so only one worker talks to the provider about it at a time."""
        now = clock.now()
        async with self.store.session() as s:
            result = await s.execute(
                update(t.WebhookRegistration)
                .where(
                    t.WebhookRegistration.id == row_id,
                    or_(t.WebhookRegistration.lease_expires_at.is_(None), t.WebhookRegistration.lease_expires_at < now),
                )
                .values(lease_expires_at=now + LEASE)
            )
            return bool(result.rowcount)

    async def _save(self, row_id: str, **values: Any) -> None:
        async with self.store.session() as s:
            await s.execute(
                update(t.WebhookRegistration)
                .where(t.WebhookRegistration.id == row_id)
                .values(**values, lease_expires_at=None, updated_at=clock.now())
            )

    async def _failed(self, row: t.WebhookRegistration, exc: Exception, *, status: str = "error") -> None:
        attempts = row.attempts + 1
        backoff = min(timedelta(minutes=2 ** min(attempts - 1, 10)), MAX_BACKOFF)
        log.warning("webhook registration %s (%s/%s) failed: %s", row.id, row.namespace, row.trigger_name, exc)
        await self._save(row.id, status=status, error=str(exc)[:2000], attempts=attempts, next_attempt_at=clock.now() + backoff)

    async def touch(self, registration_id: str) -> None:
        async with self.store.session() as s:
            await s.execute(
                update(t.WebhookRegistration).where(t.WebhookRegistration.id == registration_id).values(last_received_at=clock.now())
            )

    # ------------------------------------------------------------------ sync

    async def sync(
        self, tenant_id: str, namespace: str, trigger_name: str, *, force: bool = False, changed: bool = False
    ) -> t.WebhookRegistration | None:
        """Make the provider's webhook match the trigger; returns the registration (None when it wants none).

        ``changed``: the trigger was just saved, so try now even while backing off from a failure;
        ``force``: also re-register when nothing changed.
        """
        try:
            resource = await self.store.get_resource_row(tenant_id, "Trigger", trigger_name, namespace=namespace, fallback=False)
            trigger: Trigger | None = Trigger.model_validate(resource.document)
        except NotFound:
            trigger = None
        row = await self.get(tenant_id, namespace, trigger_name)
        if trigger is None or not wants_webhook(trigger):
            if row is not None:
                await self._remove(row, force=force or changed)
            return None
        assert trigger.spec.connection is not None
        try:
            _, integration = await self._connection(tenant_id, namespace, trigger.spec.connection)
        except NotFound as exc:
            if row is not None:
                await self._failed(row, InvalidRequest(f"connection '{trigger.spec.connection}' not found"))
            elif force:
                raise InvalidRequest(f"connection '{trigger.spec.connection}' not found") from exc
            return await self.get(tenant_id, namespace, trigger_name)
        if row is None:
            row = await self._create(tenant_id, namespace, trigger, integration)
        if row.status == "error" and not (force or changed) and row.next_attempt_at and row.next_attempt_at > clock.now():
            return row
        if not await self._claim(row.id):
            return row  # another worker has it
        try:
            await self._apply(tenant_id, namespace, trigger, integration, row, force=force)
        except Exception as exc:
            await self._failed(row, exc)
        return await self.get(tenant_id, namespace, trigger_name)

    async def _apply(
        self, tenant_id: str, namespace: str, trigger: Trigger, integration: Integration, row: t.WebhookRegistration, *, force: bool
    ) -> None:
        connection = trigger.spec.connection
        assert connection is not None
        trigger_row = await self.store.get_resource_row(tenant_id, "Trigger", trigger.metadata.name, namespace=namespace, fallback=False)
        actor = trigger_row.updated_by or row.registered_by
        url = await self._callback_url(tenant_id, namespace, connection, row.id)
        registration = await self.registration(row, trigger, integration, url)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"connection": connection, "integration": integration.key, "event": trigger.spec.event, "url": url,
                 "config": registration.config.model_dump(mode="json")},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        now = clock.now()
        if not force and row.status == "active" and row.fingerprint == fingerprint:
            if row.expires_at is None or row.expires_at - now > REFRESH_BEFORE:
                await self._save(row.id)  # nothing to do; release the lease
                return
            ctx = await self.context(tenant_id, namespace, connection, row.registered_by or actor)
            state = await integration.refresh_webhook(ctx, registration)
        else:
            if registration.state and (row.connection_name != connection or row.integration != integration.key):
                # moved to another connection: take the webhook down where it was first
                old = await self.context(tenant_id, namespace, row.connection_name, row.registered_by)
                await old.integration.unregister_webhook(old, registration)
                registration.state = {}
            ctx = await self.context(tenant_id, namespace, connection, actor)
            state = await integration.register_webhook(ctx, registration)
        expires = state.get("expires_at")
        await self._save(
            row.id,
            status="active",
            fingerprint=fingerprint,
            state=state,
            connection_name=connection,
            integration=integration.key,
            registered_by=actor,
            error=None,
            attempts=0,
            next_attempt_at=None,
            expires_at=datetime.fromisoformat(expires) if expires else None,
            last_synced_at=now,
        )
        await self.store.audit(
            tenant_id, actor or "system", "trigger.webhook_registered", f"{namespace}/{trigger.metadata.name}", namespace=namespace,
            integration=integration.key, registration=row.id,
        )

    async def _remove(self, row: t.WebhookRegistration, *, force: bool = False) -> None:
        if row.status == "removing" and not force and row.next_attempt_at and row.next_attempt_at > clock.now():
            return
        if not await self._claim(row.id):
            return
        try:
            if row.state:
                try:
                    ctx = await self.context(row.tenant_id, row.namespace, row.connection_name, row.registered_by)
                except NotFound:
                    log.warning("webhook registration %s: connection '%s' is gone; delete its webhook in %s by hand",
                                row.id, row.connection_name, row.integration)
                else:
                    integration = ctx.integration
                    registration = WebhookRegistration(
                        id=row.id, url="", name="", event="", config=None,
                        secret=self.engine.secrets.cipher.decrypt(row.secret), state=dict(row.state),
                    )
                    await integration.unregister_webhook(ctx, registration)
        except Exception as exc:
            await self._failed(row, exc, status="removing")
            return
        async with self.store.session() as s:
            await s.execute(delete(t.WebhookRegistration).where(t.WebhookRegistration.id == row.id))
        await self.store.audit(
            row.tenant_id, row.registered_by or "system", "trigger.webhook_removed", f"{row.namespace}/{row.trigger_name}",
            namespace=row.namespace, integration=row.integration, registration=row.id,
        )

    async def reconcile(self, *, force: bool = False) -> int:
        """Bring every registration in line; at most once a minute unless forced. Returns registrations touched."""
        now = clock.now()
        if not force and self._last_reconcile is not None and now - self._last_reconcile < RECONCILE_EVERY:
            return 0
        self._last_reconcile = now
        wanted: set[tuple[str, str, str]] = set()
        for resource in await self.store.list_resources_all_tenants("Trigger"):
            if resource.document.get("spec", {}).get("webhook") is None:
                continue
            trigger = Trigger.model_validate(resource.document)
            if wants_webhook(trigger):
                wanted.add((resource.tenant_id, resource.namespace, resource.name))
        async with self.store.session() as s:
            rows = list((await s.execute(select(t.WebhookRegistration))).scalars().all())
        have = {(r.tenant_id, r.namespace, r.trigger_name): r for r in rows}
        touched = 0
        for key in sorted(wanted | set(have)):
            try:
                if key in wanted:
                    await self.sync(*key)
                else:
                    await self._remove(have[key])
                touched += 1
            except Exception:
                log.exception("webhook reconcile failed for %s", "/".join(key))
        return touched

    # ------------------------------------------------------------------ views

    async def describe(self, row: t.WebhookRegistration | None, trigger: Trigger | None = None) -> dict[str, Any] | None:
        if row is None:
            return None
        url = await self._callback_url(row.tenant_id, row.namespace, row.connection_name, row.id) if row.connection_name else None
        state = {k: v for k, v in (row.state or {}).items()}
        out: dict[str, Any] = {
            "id": row.id,
            "trigger": row.trigger_name,
            "namespace": row.namespace,
            "connection": row.connection_name,
            "integration": row.integration,
            "status": row.status,
            "url": url,
            "state": state,
            "error": row.error,
            "attempts": row.attempts,
            "registered_by": row.registered_by,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
            "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
            "last_received_at": row.last_received_at.isoformat() if row.last_received_at else None,
        }
        if trigger is not None and wants_webhook(trigger):
            try:
                _, integration = await self._connection(row.tenant_id, row.namespace, row.connection_name)
                registration = await self.registration(row, trigger, integration, url or "")
                out["desired"] = integration.describe_webhook(registration)
            except Exception as exc:  # shown, not raised: the view must not fail on a broken config
                out["desired"] = {"error": str(exc)}
        return out
