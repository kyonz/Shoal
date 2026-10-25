"""Connection resolution, action calls and inbound webhook dispatch."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from shoal.core.errors import InvalidRequest, NotFound
from shoal.core.models import Connection
from shoal.core.scope import SHARED_NAMESPACE, current_namespace, use_namespace
from shoal.integrations.base import ConnectionContext, Integration, IntegrationEvent, Page

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)


class IntegrationManager:
    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self, key: str) -> Integration:
        return self.engine.plugin("integrations", key)

    def catalog(self) -> list[dict[str, Any]]:
        return [type(i).catalog() for i in self.engine.registry.available("integrations").values()]

    async def connection(
        self, tenant_id: str, name: str, *, initiator: dict[str, Any] | None = None, run_id: str | None = None
    ) -> ConnectionContext:
        row = await self.engine.store.get_resource_row(tenant_id, "Connection", name)
        resource = Connection.model_validate(row.document)
        integration = self.get(resource.spec.integration)
        values: dict[str, Any] = dict(resource.spec.config)
        # A connection shared from the `shared` namespace reads its secrets there, where its owners keep them.
        with use_namespace(row.namespace):
            for field, secret_name in resource.spec.secrets.items():
                value = await self.engine.secrets.get(tenant_id, secret_name)
                if value is None:
                    raise InvalidRequest(f"connection '{name}': secret '{secret_name}' for field '{field}' is not set")
                values[field] = value
        config = integration.connection_model.model_validate(values)
        return ConnectionContext(
            engine=self.engine,
            tenant_id=tenant_id,
            name=name,
            config=config,
            integration=integration,
            initiator=initiator,
            run_id=run_id,
        )

    async def resolve_name(self, tenant_id: str, integration: str, preferred: str | None) -> str:
        """Pick a connection: explicit/default name, else the only one for that integration."""
        if preferred:
            return preferred
        rows = [
            *await self.engine.store.list_resources(tenant_id, "Connection"),
            *await self.engine.store.list_resources(tenant_id, "Connection", namespace=SHARED_NAMESPACE),
        ]
        matches = list(dict.fromkeys(r.name for r in rows if r.document["spec"]["integration"] == integration))
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise InvalidRequest(f"no '{integration}' connection configured")
        raise InvalidRequest(
            f"several '{integration}' connections exist ({', '.join(matches)}); set 'connection' or "
            f"workflow 'connections.{integration}'"
        )

    async def call(
        self,
        tenant_id: str,
        action: str,
        params: dict[str, Any],
        *,
        connection: str | None = None,
        initiator: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        integration_key, _, action_name = action.partition(".")
        name = await self.resolve_name(tenant_id, integration_key, connection)
        ctx = await self.connection(tenant_id, name, initiator=initiator, run_id=run_id)
        if ctx.integration.key != integration_key:
            raise InvalidRequest(f"connection '{name}' is a {ctx.integration.key} connection, not {integration_key}")
        return await ctx.integration.call_action(ctx, action_name, params)

    async def query(
        self,
        tenant_id: str,
        connection: str,
        resource: str,
        filter: dict[str, Any],
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page:
        ctx = await self.connection(tenant_id, connection)
        return await ctx.integration.query(ctx, resource, filter, cursor=cursor, limit=limit)

    async def query_all(
        self, tenant_id: str, connection: str, resource: str, filter: dict[str, Any], *, max_items: int = 10000
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = None
        while len(items) < max_items:
            page = await self.query(tenant_id, connection, resource, filter, cursor=cursor)
            items.extend(page.items)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        return items[:max_items]

    async def webhook_url(self, tenant_id: str, connection_name: str) -> str:
        row = await self.engine.store.get_resource_row(tenant_id, "Connection", connection_name)
        endpoint = await self.engine.store.webhook_for_connection(tenant_id, connection_name, namespace=row.namespace)
        return f"{self.engine.settings.public_url.rstrip('/')}/v1/hooks/{endpoint.id}"

    async def handle_webhook(self, webhook_id: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        """Verify, parse, wake waiting runs and fire matching triggers."""
        endpoint = await self.engine.store.resolve_webhook(webhook_id)
        if endpoint is None:
            raise NotFound("unknown webhook")
        with use_namespace(endpoint.namespace):
            ctx = await self.connection(endpoint.tenant_id, endpoint.connection_name)
            headers = {k.lower(): v for k, v in headers.items()}
            if not await ctx.integration.verify_webhook(ctx, headers, body):
                raise InvalidRequest("webhook signature verification failed")
            events = await ctx.integration.parse_webhook(ctx, headers, body)
        woken: list[str] = []
        fired: list[dict[str, Any]] = []
        for event in events:
            woken += await self.engine.store.wake_by_keys(endpoint.tenant_id, event.wake_keys)
            fired += await self.engine.triggers.dispatch_event(
                endpoint.tenant_id, endpoint.connection_name, event, connection_namespace=endpoint.namespace
            )
        return {"events": len(events), "woken_runs": sorted(set(woken)), "triggers": fired}

    async def inject_event(self, tenant_id: str, connection: str, event: IntegrationEvent) -> dict[str, Any]:
        """Process an already-parsed event (polling integrations, tests, replays)."""
        woken = await self.engine.store.wake_by_keys(tenant_id, event.wake_keys)
        fired = await self.engine.triggers.dispatch_event(tenant_id, connection, event, connection_namespace=current_namespace())
        return {"woken_runs": woken, "triggers": fired}
