"""Connection resolution, action calls and inbound webhook dispatch.

Integrations come from the process-wide plugin registry (built-ins and installed
packages) and from each tenant's own registrations (``CustomIntegration``, see
:mod:`shoal.integrations.custom`); :meth:`IntegrationManager.resolve` looks in both.

Connections live in a namespace (one team's) or in ``shared`` (the organisation's,
shared to every namespace or only to those in ``spec.namespaces``). Each integration
says which of the two it allows (``connection_scopes``).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from shoal.core import clock
from shoal.core.errors import CredentialRequired, InvalidRequest, NotFound, PluginError, Unauthorized
from shoal.core.models import Connection
from shoal.core.scope import SHARED_NAMESPACE, current_namespace, use_namespace
from shoal.integrations.base import ConnectionContext, Integration, IntegrationEvent, Page
from shoal.integrations.custom import CustomIntegrations

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

INSTALL_STATE_TTL = timedelta(minutes=15)
# Key spaces the engine itself waits on (signals, approvals, child runs, credentials); never set by an event.
INTERNAL_WAKE_PREFIXES = {"signal", "event", "approval", "run", "credential"}


class IntegrationManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.custom = CustomIntegrations(engine)

    def get(self, key: str) -> Integration:
        """An installed integration (built-in or plugin); tenants' own registrations need :meth:`resolve`."""
        return self.engine.plugin("integrations", key)

    async def resolve(self, tenant_id: str, key: str, *, namespace: str | None = None) -> Integration:
        """The tenant's registered integration ``key``, else the installed one.

        With ``namespace``, a registration limited to other namespaces is not found."""
        loaded = await self.custom.load(tenant_id, key)
        if loaded is None:
            return self.get(key)
        if not loaded.resource.spec.enabled:
            raise PluginError(f"integration '{key}' is turned off for this organisation")
        if namespace is not None and not self.custom.visible(loaded.resource.spec, namespace):
            raise PluginError(f"integration '{key}' is not available in namespace '{namespace}'")
        if loaded.integration is None:
            raise PluginError(f"custom integration '{key}' is not available: {loaded.error}")
        return loaded.integration

    async def catalog(self, tenant_id: str | None = None, *, namespace: str | None = None) -> list[dict[str, Any]]:
        """Installed integrations plus, for a tenant, the ones it registered (those visible to ``namespace``)."""
        out: dict[str, dict[str, Any]] = {
            key: {**type(i).catalog(), "source": "installed"} for key, i in self.engine.registry.available("integrations").items()
        }
        if tenant_id:
            for loaded in await self.custom.list(tenant_id):
                spec = loaded.resource.spec
                if loaded.integration is None or not spec.enabled or not self.custom.visible(spec, namespace):
                    out.pop(loaded.resource.metadata.name, None)  # a registration of an installed package governs it
                    continue
                out[loaded.resource.metadata.name] = {
                    **type(loaded.integration).catalog(), "source": "custom",
                    "custom": {"source": spec.source, "url": spec.url, "package": spec.package, "namespaces": spec.namespaces},
                }
        return sorted(out.values(), key=lambda c: c["key"])

    async def check_connection(self, tenant_id: str, connection: Connection, namespace: str) -> None:
        """Apply-time checks: the integration allows connections at this level, and the namespace may use it."""
        key, scope = connection.spec.integration, ("org" if namespace == SHARED_NAMESPACE else "namespace")
        if scope == "namespace" and connection.spec.namespaces:
            raise InvalidRequest("`namespaces` only applies to organisation connections (in the shared namespace)")
        try:
            integration = await self.resolve(tenant_id, key, namespace=namespace)
        except PluginError as exc:
            if await self.custom.load(tenant_id, key) is not None:
                raise InvalidRequest(str(exc)) from exc
            return  # not installed here: kept as written, as before
        if scope not in integration.connection_scopes:
            where = "the organisation (admin console)" if scope == "namespace" else "a namespace"
            raise InvalidRequest(
                f"{key} connections cannot be created at the {'organisation' if scope == 'org' else 'namespace'} level; "
                f"create it in {where}"
            )

    async def connection(
        self,
        tenant_id: str,
        name: str,
        *,
        initiator: dict[str, Any] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> ConnectionContext:
        row = await self.engine.store.get_resource_row(tenant_id, "Connection", name)
        resource = Connection.model_validate(row.document)
        spec = resource.spec
        integration = await self.resolve(tenant_id, spec.integration, namespace=current_namespace())
        values: dict[str, Any] = dict(spec.config)
        user_fields = set(spec.user_fields or spec.secrets)
        if spec.oauth:
            user_fields.add(spec.oauth.token_field)
        personal = await self._personal(tenant_id, row.namespace, name, spec, initiator, user_fields)
        # A connection shared from the `shared` namespace reads its secrets there, where its owners keep them.
        with use_namespace(row.namespace):
            for field, secret_name in spec.secrets.items():
                if personal is not None and field in user_fields:
                    continue
                value = await self.engine.secrets.get(tenant_id, secret_name)
                if value is None:
                    raise InvalidRequest(f"connection '{name}': secret '{secret_name}' for field '{field}' is not set")
                values[field] = value
        acting_as = None
        if personal is not None:
            values.update({k: v for k, v in personal[1].items() if k in user_fields})
            acting_as = personal[0]
        config = integration.connection_model.model_validate(values)
        return ConnectionContext(
            engine=self.engine,
            tenant_id=tenant_id,
            name=name,
            config=config,
            integration=integration,
            initiator=initiator,
            run_id=run_id,
            step_id=step_id,
            acting_as=acting_as,
        )

    async def _personal(
        self, tenant_id: str, namespace: str, name: str, spec: Any, initiator: dict[str, Any] | None, fields: set[str]
    ) -> tuple[str, dict[str, Any]] | None:
        """(owner, values) when the call uses the initiator's own credential; None for the shared one."""
        if spec.credentials == "shared":
            return None
        creds = self.engine.user_credentials
        owner = await creds.owner_for(tenant_id, initiator)
        values = await creds.get(tenant_id, owner, namespace, name) if owner else None
        if values is not None and values.get("_oauth"):
            values = await self.engine.oauth.fresh(tenant_id, owner, namespace, name, spec, values)  # type: ignore[arg-type]
        if values is not None:
            return owner, values  # type: ignore[return-value]
        if spec.credentials == "per_user_or_shared":
            return None
        who = (initiator or {}).get("display_name") or owner or "the person this run acts for"
        raise CredentialRequired(
            f"connection '{name}' uses each person's own credential and {who} has not connected it"
            + ("" if owner else " (runs started by service accounts or unknown people cannot use it)"),
            tenant_id=tenant_id, owner=owner, namespace=namespace, connection=name, fields=sorted(fields),
        )

    async def resolve_name(self, tenant_id: str, integration: str, preferred: str | None) -> str:
        """Pick a connection: explicit/default name, else the only one for that integration."""
        if preferred:
            return preferred
        rows = [
            *await self.engine.store.list_resources(tenant_id, "Connection"),
            *await self.engine.store.list_resources(tenant_id, "Connection", namespace=SHARED_NAMESPACE),
        ]
        ns = current_namespace()
        matches = list(dict.fromkeys(
            r.name for r in rows if r.document["spec"]["integration"] == integration and shared_with(r.document, r.namespace, ns)
        ))
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
        step_id: str | None = None,
    ) -> dict[str, Any]:
        integration_key, _, action_name = action.partition(".")
        name = await self.resolve_name(tenant_id, integration_key, connection)
        ctx = await self.connection(tenant_id, name, initiator=initiator, run_id=run_id, step_id=step_id)
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
        initiator: dict[str, Any] | None = None,
    ) -> Page:
        ctx = await self.connection(tenant_id, connection, initiator=initiator)
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

    async def accept_webhook(self, webhook_id: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        """Verify a delivery and queue it for a worker; the sender gets its answer without waiting for runs to start."""
        endpoint = await self.engine.store.resolve_webhook(webhook_id)
        if endpoint is None:
            raise NotFound("unknown webhook")
        await self.engine.quotas.check_webhook(endpoint.id)
        headers = {k.lower(): v for k, v in headers.items()}
        with use_namespace(endpoint.namespace):
            ctx = await self.connection(endpoint.tenant_id, endpoint.connection_name)
            if not await ctx.integration.verify_webhook(ctx, headers, body):
                raise InvalidRequest("webhook signature verification failed")
        if type(ctx.integration).webhook_reply is not Integration.webhook_reply:
            # the integration answers the delivery itself (a URL check, an interactive message): it cannot wait
            return await self.handle_webhook(webhook_id, headers, body, verified=True)
        delivery = await self.engine.store.enqueue_webhook(endpoint, headers, body)
        return {"queued": True, "delivery": delivery}

    async def process_queued_webhooks(self, limit: int = 50) -> int:
        """Worker side: parse queued deliveries, wake waiting runs and fire triggers. Returns how many were handled."""
        store = self.engine.store
        rows = await store.claim_webhooks(limit=limit, lease=timedelta(minutes=5))
        for row in rows:
            body = base64.b64decode(row.body)
            try:
                result = await self.handle_webhook(row.webhook_id, row.headers, body, verified=True)
            except Exception as exc:
                log.warning("webhook delivery %s failed (attempt %d): %s", row.id, row.attempts, exc)
                retry = row.attempts < 5 and not isinstance(exc, (NotFound, InvalidRequest))
                await store.finish_webhook(
                    row.id, result={"error": str(exc)[:1000]}, failed=not retry,
                    retry_at=clock.now() + timedelta(seconds=10 * 2**row.attempts) if retry else None,
                )
                continue
            await store.finish_webhook(row.id, result=result)
        return len(rows)

    async def handle_webhook(
        self, webhook_id: str, headers: dict[str, str], body: bytes, *, verified: bool = False
    ) -> dict[str, Any]:
        """Verify, parse, wake waiting runs and fire matching triggers."""
        store = self.engine.store
        endpoint = await (store.get_webhook_endpoint(webhook_id) if verified else store.resolve_webhook(webhook_id))
        if endpoint is None:
            raise NotFound("unknown webhook")
        with use_namespace(endpoint.namespace):
            ctx = await self.connection(endpoint.tenant_id, endpoint.connection_name)
            headers = {k.lower(): v for k, v in headers.items()}
            if not verified and not await ctx.integration.verify_webhook(ctx, headers, body):
                raise InvalidRequest("webhook signature verification failed")
            events = await ctx.integration.parse_webhook(ctx, headers, body)
        for event in events:
            event.wake_keys = self.own_wake_keys(ctx.integration.key, event.wake_keys)
        woken: list[str] = []
        fired: list[dict[str, Any]] = []
        interactions: list[dict[str, Any]] = []
        for event in events:
            if event.interaction is not None:
                interactions.append(await self.engine.interactions.handle(
                    endpoint.tenant_id, endpoint.connection_name, ctx.integration.key, event
                ))
                continue
            woken += await self.engine.store.wake_by_keys(endpoint.tenant_id, event.wake_keys)
            woken += await self.engine.signals.deliver_event(endpoint.tenant_id, event)
            fired += await self.engine.triggers.dispatch_event(
                endpoint.tenant_id, endpoint.connection_name, event, connection_namespace=endpoint.namespace
            )
        result: dict[str, Any] = {"events": len(events), "woken_runs": sorted(set(woken)), "triggers": fired}
        if interactions:
            result["interactions"] = interactions
        with use_namespace(endpoint.namespace):
            reply = await ctx.integration.webhook_reply(ctx, headers, body, {**result, "parsed": events})
        if reply is not None:
            result["reply"] = reply
        return result

    def own_wake_keys(self, integration_key: str, keys: list[str]) -> list[str]:
        """Drop wake keys an integration may not claim, so a delivery cannot steer runs waiting on another system.

        Wake keys are tenant-wide (``github:pr:acme/api#12``) and a waiting step trusts the event it receives,
        so a key whose prefix names a *different* registered integration (a generic webhook sending
        ``X-Shoal-Wake-Key: github:pr:...``) or one of the engine's own key spaces is ignored.
        """
        reserved = (set(self.engine.registry.names("integrations")) | INTERNAL_WAKE_PREFIXES) - {integration_key}
        kept = [k for k in keys if k.partition(":")[0] not in reserved]
        if len(kept) != len(keys):
            log.warning("ignored wake keys %s from a %s webhook", sorted(set(keys) - set(kept)), integration_key)
        return kept

    async def inject_event(self, tenant_id: str, connection: str, event: IntegrationEvent) -> dict[str, Any]:
        """Process an already-parsed event (polling integrations, tests, replays)."""
        woken = await self.engine.store.wake_by_keys(tenant_id, event.wake_keys)
        woken += await self.engine.signals.deliver_event(tenant_id, event)
        fired = await self.engine.triggers.dispatch_event(tenant_id, connection, event, connection_namespace=current_namespace())
        return {"woken_runs": woken, "triggers": fired}

    # ------------------------------------------------------------------ app install (OAuth, e.g. "Add to Slack")

    @property
    def install_redirect_uri(self) -> str:
        return f"{self.engine.settings.public_url.rstrip('/')}/v1/install/callback"

    async def _partial(self, tenant_id: str, namespace: str, name: str) -> tuple[Connection, ConnectionContext]:
        """The connection with whichever secrets are set: an install runs before the token exists."""
        with use_namespace(namespace):
            row = await self.engine.store.get_resource_row(tenant_id, "Connection", name, namespace=namespace)
            resource = Connection.model_validate(row.document)
            integration = await self.resolve(tenant_id, resource.spec.integration, namespace=namespace)
            values: dict[str, Any] = dict(resource.spec.config)
            for field, secret_name in resource.spec.secrets.items():
                value = await self.engine.secrets.get(tenant_id, secret_name)
                if value is not None:
                    values[field] = value
        try:
            config = integration.connection_model.model_validate(values)
        except ValueError as exc:
            raise InvalidRequest(f"connection '{name}' is not ready to install: {exc}") from exc
        return resource, ConnectionContext(engine=self.engine, tenant_id=tenant_id, name=name, config=config, integration=integration)

    async def install_start(self, tenant_id: str, namespace: str, name: str, actor: str, *, then: str | None = None) -> str:
        """The URL to send the admin to; ``then`` (e.g. ``"setup"``) is the connection page to come back to."""
        resource, ctx = await self._partial(tenant_id, namespace, name)
        if not ctx.integration.install_fields:
            raise InvalidRequest(f"{ctx.integration.title} connections have no app install")
        state = self.engine.secrets.cipher.encrypt(json.dumps({
            "t": tenant_id, "n": namespace, "c": name, "a": actor, "exp": (clock.now() + INSTALL_STATE_TTL).timestamp(),
            **({"then": then} if then else {}),
        }))
        return ctx.integration.install_url(ctx.config, self.install_redirect_uri, state)

    async def install_callback(self, code: str, state: str) -> tuple[str, str, str, str, list[str], str | None]:
        """Exchange the code and store what the install returns as the connection's secrets.

        Returns (tenant, namespace, connection, actor, fields stored, page to return to). A field with no secret
        named in the connection gets one (``<connection>-<field>``) and the connection is updated.
        """
        try:
            data = json.loads(self.engine.secrets.cipher.decrypt(state))
        except Exception as exc:
            raise Unauthorized("invalid install state") from exc
        if clock.now().timestamp() > data["exp"]:
            raise Unauthorized("the install took too long; start again")
        tenant_id, namespace, name, actor = data["t"], data["n"], data["c"], data["a"]
        resource, ctx = await self._partial(tenant_id, namespace, name)
        values = await ctx.integration.complete_install(ctx, code, self.install_redirect_uri)
        secrets = dict(resource.spec.secrets)
        stored = []
        with use_namespace(namespace):
            for field in ctx.integration.install_fields:
                if field not in values:
                    continue
                secrets.setdefault(field, f"{name}-{field.replace('_', '-')}")
                await self.engine.secrets.writable().put(tenant_id, secrets[field], values[field])
                stored.append(field)
            if secrets != resource.spec.secrets:
                updated = resource.model_copy(update={"spec": resource.spec.model_copy(update={"secrets": secrets})})
                await self.engine.store.apply_resource(tenant_id, updated, actor=actor, namespace=namespace,
                                                       message="secrets added by app install")
        return tenant_id, namespace, name, actor, stored, data.get("then")


def shared_with(document: dict[str, Any], stored_in: str, namespace: str) -> bool:
    """Whether a connection stored in ``stored_in`` may be used from ``namespace``."""
    if stored_in != SHARED_NAMESPACE or namespace == SHARED_NAMESPACE:
        return True
    allowed = (document.get("spec") or {}).get("namespaces") or []
    return not allowed or namespace in allowed
