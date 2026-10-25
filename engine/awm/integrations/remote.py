"""Out-of-process integrations over HTTP (the remote integration protocol).

An integration can be written in any language and deployed on its own: it is
an HTTP service that serves a manifest and a handful of endpoints, and AWM
adapts it to the ordinary :class:`~awm.integrations.base.Integration`
interface, so its actions become workflow steps, agent tools and API
operations like any built-in.

Protocol ``awm.integration/v1`` (every request carries
``Authorization: Bearer <token>`` when a token is configured)::

    GET  /manifest                 -> RemoteManifest
    POST /test                     -> {"ok": bool, ...}                (manifest.test)
    POST /actions/{name}           -> {"output": {...}}
    POST /resources/{name}/query   -> {"items": [...], "next_cursor": str | null}
    POST /webhooks/verify          -> {"valid": bool}                  (manifest.webhooks)
    POST /webhooks/parse           -> {"events": [IntegrationEvent]}   (manifest.webhooks)

Every POST body includes ``connection`` (the resolved connection config,
secrets included) and ``context`` (tenant, connection name, run, initiator),
plus ``params`` / ``filter``+``cursor``+``limit`` / ``headers``+``body_b64``.
Errors are any non-2xx status with ``{"error": {"message": str, "retryable": bool}}``.

There are two ways to use a remote integration:

* **Per connection** - a tenant creates a ``remote`` connection with ``url``
  and ``token`` (and any settings the service needs); actions are called as
  ``remote.<action>`` on that connection.
* **Registered** - an operator registers the service under its own key with
  :func:`register_remote_integration`; it then appears in the catalogue like
  a built-in (``linear.create_issue``) and tenants connect to it normally.
"""

from __future__ import annotations

import base64
import ipaddress
import time
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, create_model

from awm.integrations.base import (
    ActionDef,
    ConnectionContext,
    EventDef,
    Integration,
    IntegrationError,
    IntegrationEvent,
    Page,
    ResourceDef,
)

PROTOCOL = "awm.integration/v1"
MANIFEST_TTL_SECONDS = 60.0


# --------------------------------------------------------------------------- manifest


class RemoteAction(BaseModel):
    name: str
    description: str = ""
    mutating: bool = True
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict[str, Any] | None = None


class RemoteResource(BaseModel):
    name: str
    description: str = ""
    filter_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    key_field: str = "id"


class RemoteEvent(BaseModel):
    name: str
    description: str = ""
    example: dict[str, Any] = Field(default_factory=dict)


class RemoteManifest(BaseModel):
    """What a remote integration service serves at ``GET /manifest``."""

    protocol: str = PROTOCOL
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str
    description: str = ""
    version: str | None = None
    connection_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    secret_fields: list[str] = Field(default_factory=list)
    actions: list[RemoteAction] = Field(default_factory=list)
    resources: list[RemoteResource] = Field(default_factory=list)
    events: list[RemoteEvent] = Field(default_factory=list)
    webhooks: bool = Field(False, description="The service implements /webhooks/verify and /webhooks/parse.")
    test: bool = Field(False, description="The service implements /test.")
    webhook_instructions: str | None = None

    def action(self, name: str) -> RemoteAction | None:
        return next((a for a in self.actions if a.name == name), None)

    def resource(self, name: str) -> RemoteResource | None:
        return next((r for r in self.resources if r.name == name), None)


class _Params(BaseModel):
    """Remote inputs are validated by the service; locally we only check required keys."""

    model_config = ConfigDict(extra="allow")


class RemoteActionDef(ActionDef):
    def __init__(self, spec: RemoteAction):
        super().__init__(
            name=spec.name,
            description=spec.description or spec.name,
            input_model=_Params,
            output_model=None,
            method="",
            mutating=spec.mutating,
        )
        self.input_schema = dict(spec.input_schema)
        self.output_schema = spec.output_schema

    def tool_schema(self) -> dict[str, Any]:
        schema = dict(self.input_schema)
        schema.pop("title", None)
        schema.setdefault("type", "object")
        return schema


def _action_defs(manifest: RemoteManifest) -> dict[str, ActionDef]:
    return {a.name: RemoteActionDef(a) for a in manifest.actions}


def _resource_defs(manifest: RemoteManifest) -> dict[str, ResourceDef]:
    return {
        r.name: ResourceDef(name=r.name, description=r.description, filter_model=_Params, method="", key_field=r.key_field)
        for r in manifest.resources
    }


def _check_required(schema: dict[str, Any], values: dict[str, Any], what: str) -> None:
    missing = [k for k in schema.get("required", []) if k not in values]
    if missing:
        raise IntegrationError(f"{what}: missing required field(s): {', '.join(missing)}", status=422)


# --------------------------------------------------------------------------- client


def check_url(url: str, *, allow_insecure: bool = False) -> str:
    """Remote services receive secrets, so plain http is only allowed to loopback unless opted in."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise IntegrationError(f"remote integration url must be http(s): {url!r}", status=422)
    if parts.username or parts.password:
        raise IntegrationError("remote integration url must not embed credentials; use the token", status=422)
    if parts.scheme == "http" and not allow_insecure:
        host = parts.hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost" or host.endswith(".localhost")
        if not loopback:
            raise IntegrationError(
                f"remote integration url {url!r} is not https; set allow_insecure to permit plain http", status=422
            )
    return url.rstrip("/")


class RemoteClient:
    """A thin protocol client; ``http`` is any ``httpx.AsyncClient`` factory."""

    def __init__(self, url: str, token: str | None, *, http: Any, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.token = token
        self._http = http
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "awm-remote-integration", "X-AWM-Protocol": PROTOCOL}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.url}{path}"
        try:
            async with self._http(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
                response = await client.request(method, url, json=body, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise IntegrationError(f"remote integration {method} {path} timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError(f"remote integration {method} {path} failed: {exc}", retryable=True) from exc
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"error": {"message": response.text[:500]}}
        if not response.is_success:
            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {"message": str(error or response.text[:500])}
            default_retryable = response.status_code in (408, 425, 429) or response.status_code >= 500
            raise IntegrationError(
                f"remote integration {method} {path} failed: {response.status_code} {error.get('message', '')}".rstrip(),
                status=response.status_code,
                retryable=bool(error.get("retryable", default_retryable)),
            )
        if not isinstance(payload, dict):
            raise IntegrationError(f"remote integration {method} {path} returned a non-object body")
        return payload

    async def manifest(self) -> RemoteManifest:
        payload = await self.request("GET", "/manifest")
        try:
            manifest = RemoteManifest.model_validate(payload)
        except ValidationError as exc:
            raise IntegrationError(f"remote integration at {self.url} served an invalid manifest: {exc}") from exc
        if manifest.protocol != PROTOCOL:
            raise IntegrationError(f"remote integration speaks {manifest.protocol!r}; AWM supports {PROTOCOL!r}")
        return manifest


_manifest_cache: dict[tuple[str, str | None], tuple[float, RemoteManifest]] = {}


async def _cached_manifest(client: RemoteClient, *, refresh: bool = False) -> RemoteManifest:
    key = (client.url, client.token)
    hit = _manifest_cache.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < MANIFEST_TTL_SECONDS:
        return hit[1]
    manifest = await client.manifest()
    _manifest_cache[key] = (time.monotonic(), manifest)
    return manifest


def _plain(model: BaseModel, exclude: set[str] = frozenset()) -> dict[str, Any]:  # type: ignore[assignment]
    """Connection values as JSON, secrets revealed (the remote service needs them)."""
    out: dict[str, Any] = {}
    values = {**{k: getattr(model, k) for k in type(model).model_fields}, **(model.model_extra or {})}
    for key, value in values.items():
        if key in exclude:
            continue
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        elif isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        out[key] = value
    return out


def _context(ctx: ConnectionContext) -> dict[str, Any]:
    return {"tenant_id": ctx.tenant_id, "connection": ctx.name, "run_id": ctx.run_id, "initiator": ctx.initiator}


# --------------------------------------------------------------------------- adapter


class _RemoteBase(Integration):
    """Shared dispatch; subclasses say where the service is and what config it receives."""

    def _client(self, ctx: ConnectionContext) -> RemoteClient:
        raise NotImplementedError

    def _connection_payload(self, ctx: ConnectionContext) -> dict[str, Any]:
        raise NotImplementedError

    async def _manifest(self, ctx: ConnectionContext) -> RemoteManifest:
        return await _cached_manifest(self._client(ctx))

    async def actions_for(self, ctx: ConnectionContext) -> dict[str, ActionDef]:
        return _action_defs(await self._manifest(ctx))

    async def resources_for(self, ctx: ConnectionContext) -> dict[str, ResourceDef]:
        return _resource_defs(await self._manifest(ctx))

    def _body(self, ctx: ConnectionContext, **extra: Any) -> dict[str, Any]:
        return {"protocol": PROTOCOL, "connection": self._connection_payload(ctx), "context": _context(ctx), **extra}

    async def test_connection(self, ctx: ConnectionContext) -> dict[str, Any]:
        client = self._client(ctx)
        manifest = await _cached_manifest(client, refresh=True)
        if not manifest.test:
            return {"ok": True, "integration": manifest.key, "version": manifest.version}
        return await client.request("POST", "/test", self._body(ctx))

    async def call_action(self, ctx: ConnectionContext, name: str, params: dict[str, Any]) -> dict[str, Any]:
        manifest = await self._manifest(ctx)
        spec = manifest.action(name)
        if spec is None:
            raise IntegrationError(f"{manifest.key} has no action '{name}'")
        _check_required(spec.input_schema, params, f"{manifest.key}.{name}")
        result = await self._client(ctx).request("POST", f"/actions/{quote(name, safe='')}", self._body(ctx, params=params))
        output = result.get("output", {})
        return output if isinstance(output, dict) else {"result": output}

    async def query(
        self, ctx: ConnectionContext, name: str, filter: dict[str, Any], *, cursor: str | None = None, limit: int = 100
    ) -> Page:
        manifest = await self._manifest(ctx)
        spec = manifest.resource(name)
        if spec is None:
            raise IntegrationError(f"{manifest.key} has no resource '{name}'")
        result = await self._client(ctx).request(
            "POST",
            f"/resources/{quote(name, safe='')}/query",
            self._body(ctx, filter=filter, cursor=cursor, limit=limit),
        )
        try:
            return Page.model_validate(result)
        except ValidationError as exc:
            raise IntegrationError(f"{manifest.key}.{name}: invalid page from remote integration: {exc}") from exc

    def _webhook_body(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        return self._body(ctx, headers=headers, body_b64=base64.b64encode(body).decode())

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        manifest = await self._manifest(ctx)
        if not manifest.webhooks:
            return False  # a service that does not take webhooks never accepts one
        result = await self._client(ctx).request("POST", "/webhooks/verify", self._webhook_body(ctx, headers, body))
        return result.get("valid") is True

    async def parse_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> list[IntegrationEvent]:
        manifest = await self._manifest(ctx)
        if not manifest.webhooks:
            return []
        result = await self._client(ctx).request("POST", "/webhooks/parse", self._webhook_body(ctx, headers, body))
        try:
            return [IntegrationEvent.model_validate(e) for e in result.get("events", [])]
        except ValidationError as exc:
            raise IntegrationError(f"{manifest.key}: invalid events from remote integration: {exc}") from exc


class RemoteConnection(BaseModel):
    """A connection to a remote integration service. Extra fields are passed to the service."""

    model_config = ConfigDict(extra="allow")

    url: str = Field(description="Base URL of the integration service (https).")
    token: SecretStr | None = Field(None, description="Bearer token AWM presents to the service.")
    timeout_seconds: float = Field(30.0, gt=0, le=300)
    allow_insecure: bool = Field(False, description="Permit plain http to a non-loopback host.")


class RemoteIntegration(_RemoteBase):
    """Generic per-connection adapter: ``integration: remote`` with ``url`` and ``token``."""

    key = "remote"
    title = "Remote integration (HTTP)"
    description = (
        "Connect an integration written in any language that speaks the AWM remote integration "
        "protocol. Its actions are called as remote.<action> on this connection."
    )
    connection_model = RemoteConnection

    def _client(self, ctx: ConnectionContext) -> RemoteClient:
        cfg: RemoteConnection = ctx.config
        url = check_url(cfg.url, allow_insecure=cfg.allow_insecure)
        token = cfg.token.get_secret_value() if cfg.token else None
        return RemoteClient(url, token, http=ctx.http, timeout=cfg.timeout_seconds)

    def _connection_payload(self, ctx: ConnectionContext) -> dict[str, Any]:
        return _plain(ctx.config, exclude={"url", "token", "timeout_seconds", "allow_insecure"})

    def webhook_setup_instructions(self, url: str) -> str:
        return f"Point the remote service's provider webhooks at {url}; AWM forwards them to the service to verify and parse."


class BoundRemoteIntegration(_RemoteBase):
    """Base for a remote service registered under its own key (see :func:`bind_remote`)."""

    manifest: ClassVar[RemoteManifest]
    service_url: ClassVar[str]
    service_token: ClassVar[str | None] = None
    timeout_seconds: ClassVar[float] = 30.0

    def _client(self, ctx: ConnectionContext) -> RemoteClient:
        return RemoteClient(self.service_url, self.service_token, http=ctx.http, timeout=self.timeout_seconds)

    async def _manifest(self, ctx: ConnectionContext) -> RemoteManifest:
        return self.manifest

    def _connection_payload(self, ctx: ConnectionContext) -> dict[str, Any]:
        return _plain(ctx.config)

    def webhook_setup_instructions(self, url: str) -> str:
        return self.manifest.webhook_instructions or f"Send webhooks to {url}"

    @classmethod
    def catalog(cls) -> dict[str, Any]:
        m = cls.manifest
        return {
            "key": m.key,
            "title": m.title,
            "description": m.description,
            "connection_schema": m.connection_schema,
            "secret_fields": list(m.secret_fields),
            "actions": [
                {
                    "name": a.name,
                    "description": a.description,
                    "mutating": a.mutating,
                    "input_schema": RemoteActionDef(a).tool_schema(),
                    "output_schema": a.output_schema,
                }
                for a in m.actions
            ],
            "resources": [
                {"name": r.name, "description": r.description, "filter_schema": r.filter_schema} for r in m.resources
            ],
            "events": [e.model_dump() for e in m.events],
            "remote": {"url": cls.service_url, "protocol": m.protocol, "version": m.version},
        }


def bind_remote(
    manifest: RemoteManifest, url: str, token: str | None = None, *, timeout_seconds: float = 30.0
) -> type[BoundRemoteIntegration]:
    """Build an Integration class for one remote service from its manifest."""
    fields: dict[str, Any] = {f: (SecretStr | None, None) for f in manifest.secret_fields}
    connection_model = create_model(  # type: ignore[call-overload]
        f"{manifest.key.title().replace('-', '').replace('_', '')}RemoteConnection",
        __config__=ConfigDict(extra="allow"),
        **fields,
    )
    cls = type(
        f"Remote_{manifest.key}",
        (BoundRemoteIntegration,),
        {
            "key": manifest.key,
            "title": manifest.title,
            "description": manifest.description,
            "connection_model": connection_model,
            "events": [EventDef(e.name, e.description, e.example) for e in manifest.events],
            "manifest": manifest,
            "service_url": url.rstrip("/"),
            "service_token": token,
            "timeout_seconds": timeout_seconds,
        },
    )
    cls.actions = _action_defs(manifest)
    cls.resources = _resource_defs(manifest)
    return cls


async def register_remote_integration(
    registry: Any,
    url: str,
    token: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    allow_insecure: bool = False,
    replace: bool = True,
) -> type[BoundRemoteIntegration]:
    """Fetch a service's manifest and register it in ``registry`` under its own key."""
    url = check_url(url, allow_insecure=allow_insecure)

    def http(**kwargs: Any) -> httpx.AsyncClient:
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    manifest = await RemoteClient(url, token, http=http).manifest()
    cls = bind_remote(manifest, url, token)
    registry.register("integrations", manifest.key, cls, replace=replace)
    return cls
