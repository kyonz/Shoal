"""Run an integration in isolation, or inside a real (throwaway) engine."""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from shoal.integrations.base import ConnectionContext, Integration, IntegrationError, IntegrationEvent, Page
from shoal.sdk.server import ServiceEngine


def _instance(integration: Integration | type[Integration]) -> Integration:
    return integration() if isinstance(integration, type) else integration


def _transport(http: Any) -> httpx.AsyncBaseTransport | None:
    if http is None or isinstance(http, httpx.AsyncBaseTransport):
        return http
    if hasattr(http, "transport"):
        return http.transport  # MockHTTP
    return httpx.MockTransport(http)  # a plain handler function


class IntegrationHarness:
    """Call an integration's actions, resources and webhook handlers with no database.

    ::

        api = MockHTTP()
        harness = IntegrationHarness(LinearIntegration, config={"api_key": "k"}, http=api)
        out = await harness.call("create_issue", {"title": "Bug"})
    """

    def __init__(
        self,
        integration: Integration | type[Integration],
        *,
        config: dict[str, Any] | None = None,
        http: Any = None,
        tenant_id: str = "test-tenant",
        connection: str = "test",
        initiator: dict[str, Any] | None = None,
        run_id: str | None = None,
    ):
        self.integration = _instance(integration)
        self.ctx = ConnectionContext(
            engine=ServiceEngine(_transport(http)),  # type: ignore[arg-type]
            tenant_id=tenant_id,
            name=connection,
            config=self.integration.connection_model.model_validate(config or {}),
            integration=self.integration,
            initiator=initiator,
            run_id=run_id,
        )

    async def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.integration.call_action(self.ctx, action, params or {})

    async def query(
        self, resource: str, filter: dict[str, Any] | None = None, *, cursor: str | None = None, limit: int = 100
    ) -> Page:
        return await self.integration.query(self.ctx, resource, filter or {}, cursor=cursor, limit=limit)

    async def query_all(self, resource: str, filter: dict[str, Any] | None = None, *, max_pages: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            page = await self.query(resource, filter, cursor=cursor)
            items.extend(page.items)
            if not page.next_cursor:
                return items
            cursor = page.next_cursor
        raise AssertionError(f"{resource}: still paginating after {max_pages} pages")

    async def test_connection(self) -> dict[str, Any]:
        return await self.integration.test_connection(self.ctx)

    async def verify_webhook(self, headers: dict[str, str], body: bytes | dict[str, Any]) -> bool:
        return await self.integration.verify_webhook(self.ctx, _lower(headers), _bytes(body))

    async def parse_webhook(self, headers: dict[str, str], body: bytes | dict[str, Any]) -> list[IntegrationEvent]:
        return await self.integration.parse_webhook(self.ctx, _lower(headers), _bytes(body))

    async def webhook(self, headers: dict[str, str], body: bytes | dict[str, Any]) -> list[IntegrationEvent]:
        """Verify then parse, like the engine does; raises if verification fails."""
        if not await self.verify_webhook(headers, body):
            raise IntegrationError("webhook signature verification failed", status=400)
        return await self.parse_webhook(headers, body)


def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _bytes(body: bytes | dict[str, Any]) -> bytes:
    return body if isinstance(body, bytes) else json.dumps(body).encode()


@dataclass
class EngineHarness:
    """A real engine (SQLite in a temp dir) with one tenant, for end-to-end integration tests."""

    engine: Any
    tenant_id: str

    async def secret(self, name: str, value: str) -> None:
        await self.engine.secrets.writable().put(self.tenant_id, name, value)

    async def apply(self, *documents: dict[str, Any] | str) -> None:
        """Apply resources as dicts or YAML text (Connection, Trigger, Workflow, Agent...)."""
        from shoal.core.loader import load_text

        for doc in documents:
            text = doc if isinstance(doc, str) else json.dumps(doc)
            for resource in load_text(text):
                row, _ = await self.engine.store.apply_resource(self.tenant_id, resource)
                if row.kind == "Trigger":  # register (or remove) its provider webhook, as the API does
                    await self.engine.webhooks.sync(self.tenant_id, row.namespace, row.name, changed=True)

    async def connect(
        self, name: str, integration: str, *, config: dict[str, Any] | None = None, secrets: dict[str, str] | None = None
    ) -> None:
        """Create a connection; ``secrets`` maps connection field -> secret *value* (stored for you)."""
        refs: dict[str, str] = {}
        for field, value in (secrets or {}).items():
            refs[field] = f"{name}-{field}"
            await self.secret(refs[field], value)
        await self.apply(
            {
                "apiVersion": "shoal.dev/v1",
                "kind": "Connection",
                "metadata": {"name": name},
                "spec": {"integration": integration, "config": config or {}, "secrets": refs},
            }
        )

    async def call(self, action: str, params: dict[str, Any] | None = None, *, connection: str | None = None) -> dict[str, Any]:
        return await self.engine.integrations.call(self.tenant_id, action, params or {}, connection=connection)

    async def query(self, connection: str, resource: str, filter: dict[str, Any] | None = None) -> Page:
        return await self.engine.integrations.query(self.tenant_id, connection, resource, filter or {})

    async def webhook(self, connection: str, headers: dict[str, str], body: bytes | dict[str, Any]) -> dict[str, Any]:
        """Deliver a webhook to the connection's URL, exactly as the HTTP endpoint would."""
        url = await self.engine.integrations.webhook_url(self.tenant_id, connection)
        return await self.engine.integrations.handle_webhook(url.rsplit("/", 1)[-1], headers, _bytes(body))

    async def deliver(self, url: str, headers: dict[str, str], body: bytes | dict[str, Any]) -> dict[str, Any]:
        """Deliver to any Shoal webhook URL (a connection's, or a registered trigger webhook's ``.../<id>``)."""
        prefix = f"{self.engine.settings.public_url.rstrip('/')}/v1/hooks/"
        assert url.startswith(prefix), f"not a Shoal webhook URL: {url}"
        webhook_id, _, registration_id = url[len(prefix):].partition("/")
        return await self.engine.integrations.handle_webhook(webhook_id, headers, _bytes(body), registration_id=registration_id or None)

    async def flush(self, provider: Any) -> list[dict[str, Any]]:
        """Deliver everything a stateful mock (``MockJira``) has queued, until it queues nothing more."""
        results = []
        for _ in range(100):
            deliveries = provider.take()
            if not deliveries:
                return results
            for delivery in deliveries:
                results.append(await self.deliver(delivery.url, delivery.headers, delivery.body))
        raise AssertionError("deliveries kept coming: a trigger loop?")

    async def runs(self) -> list[Any]:
        return await self.engine.store.list_runs(self.tenant_id)


@asynccontextmanager
async def engine_harness(
    *integrations: Integration | type[Integration],
    http: Any = None,
    directory: str | Path | None = None,
    tenant: str = "acme",
) -> AsyncIterator[EngineHarness]:
    """Start an engine with ``integrations`` registered (built-ins stay available).

    ::

        async with engine_harness(LinearIntegration, http=api) as world:
            await world.connect("linear", "linear", secrets={"api_key": "k"})
            await world.apply(TRIGGER_YAML, WORKFLOW_YAML)
            result = await world.webhook("linear", headers, body)
    """
    from shoal.config import Settings
    from shoal.core import clock
    from shoal.engine import Engine

    with tempfile.TemporaryDirectory(prefix="shoal-sdk-") as tmp:
        root = Path(directory or tmp)
        settings = Settings(database_url=f"sqlite+aiosqlite:///{root}/shoal.db", public_url="https://shoal.test")
        engine = Engine(settings, http_transport=_transport(http))
        for integration in integrations:
            instance = _instance(integration)
            engine.registry.register("integrations", instance.key, instance)
        await engine.start()
        try:
            row = await engine.store.create_tenant(tenant)
            yield EngineHarness(engine, row.id)
        finally:
            await engine.close()
            clock.reset()
