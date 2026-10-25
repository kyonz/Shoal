"""Integrations an organisation registers itself (``CustomIntegration`` resources).

A tenant admin registers either

* a **remote** service speaking the Shoal integration protocol (``url`` and an
  optional ``token_secret`` in ``shared``): Shoal pulls its manifest, or
* an installed Python **package**: an entry point in ``shoal.integrations``, its
  distribution name, or ``module:Class`` when the operator allows imports.

Either way the integration appears in that tenant's catalogue under the
resource's name, with its connection settings schema, auth, actions,
resources and events, so connecting to it renders the same forms as a
built-in. Registrations are per tenant; nothing leaks into other tenants or
the process-wide plugin registry.
"""

from __future__ import annotations

import importlib
import logging
import re
import time
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

import httpx

from shoal.core.errors import InvalidRequest, NotFound, PluginError
from shoal.core.models import CustomIntegration, CustomIntegrationSpec
from shoal.core.scope import SHARED_NAMESPACE, use_namespace
from shoal.integrations.base import ConnectionScope, Integration, IntegrationError
from shoal.integrations.remote import MANIFEST_TTL_SECONDS, RemoteClient, bind_remote, check_url

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)


@dataclass
class Loaded:
    """One tenant's registration, built (``integration``) or not (``error``)."""

    resource: CustomIntegration
    version: int
    integration: Integration | None
    error: str | None
    loaded_at: float

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ready": self.integration is not None, "error": self.error}
        if self.integration is not None:
            catalog = type(self.integration).catalog()
            out.update({
                "title": catalog["title"],
                "connection_scopes": catalog["connection_scopes"],
                "secret_fields": catalog["secret_fields"],
                "auth": catalog.get("auth"),
                "actions": len(catalog["actions"]),
                "resources": len(catalog["resources"]),
                "events": len(catalog["events"]),
                "version": (catalog.get("remote") or {}).get("version"),
            })
        return out


def _normalise(dist: str) -> str:
    return re.sub(r"[-_.]+", "-", dist).lower()


def effective_scopes(declared: tuple[ConnectionScope, ...] | list[ConnectionScope], spec: CustomIntegrationSpec) -> tuple[ConnectionScope, ...]:
    """The integration's own scopes, narrowed by the registration."""
    if not spec.connection_scopes:
        return tuple(declared)
    scopes = tuple(s for s in declared if s in spec.connection_scopes)
    if not scopes:
        raise IntegrationError(
            f"connection_scopes {spec.connection_scopes} do not overlap what the integration supports ({', '.join(declared)})",
            status=422,
        )
    return scopes


class CustomIntegrations:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._cache: dict[tuple[str, str], Loaded] = {}

    # ------------------------------------------------------------------ lookup

    async def get(self, tenant_id: str, name: str) -> Integration | None:
        """The tenant's registered integration called ``name``; None when there is none (or it is disabled)."""
        loaded = await self.load(tenant_id, name)
        if loaded is None or not loaded.resource.spec.enabled:
            return None
        if loaded.integration is None:
            raise PluginError(f"custom integration '{name}' is not available: {loaded.error}")
        return loaded.integration

    async def load(self, tenant_id: str, name: str, *, refresh: bool = False) -> Loaded | None:
        try:
            row = await self.engine.store.get_resource_row(tenant_id, "CustomIntegration", name, namespace=SHARED_NAMESPACE)
        except NotFound:
            self._cache.pop((tenant_id, name), None)
            return None
        key = (tenant_id, name)
        hit = self._cache.get(key)
        fresh = hit is not None and hit.version == row.version and (
            hit.resource.spec.source == "package" or time.monotonic() - hit.loaded_at < MANIFEST_TTL_SECONDS
        )
        if hit is not None and fresh and not refresh:
            return hit
        resource = CustomIntegration.model_validate(row.document)
        try:
            integration, error = await self._build(tenant_id, resource), None
        except (IntegrationError, PluginError, InvalidRequest) as exc:
            # A service that is briefly down keeps serving the manifest it served last.
            stale = hit.integration if hit is not None and hit.version == row.version else None
            integration, error = stale, str(exc)
            log.warning("custom integration %s/%s: %s", tenant_id, name, exc)
        loaded = Loaded(resource, row.version, integration, error, time.monotonic())
        self._cache[key] = loaded
        return loaded

    async def list(self, tenant_id: str) -> list[Loaded]:
        rows = await self.engine.store.list_resources(tenant_id, "CustomIntegration")
        out = [await self.load(tenant_id, r.name) for r in rows]
        return [x for x in out if x is not None]

    @staticmethod
    def visible(spec: CustomIntegrationSpec, namespace: str | None) -> bool:
        return not spec.namespaces or namespace in (None, SHARED_NAMESPACE) or namespace in spec.namespaces

    def forget(self, tenant_id: str, name: str) -> None:
        self._cache.pop((tenant_id, name), None)

    # ------------------------------------------------------------------ registration checks

    async def check(self, tenant_id: str, resource: CustomIntegration) -> None:
        """Checks before a registration is stored: its name is free and the source can be found."""
        name, spec = resource.metadata.name, resource.spec
        installed = name in self.engine.registry.names("integrations")
        if installed and not (spec.source == "package" and spec.package == name):
            raise InvalidRequest(f"'{name}' is already an installed integration; register it under another name")
        if spec.source == "remote":
            try:
                check_url(spec.url or "", allow_insecure=spec.allow_insecure)
            except IntegrationError as exc:
                raise InvalidRequest(str(exc)) from exc
        else:
            try:
                self._package_class(spec.package or "")
            except PluginError as exc:
                raise InvalidRequest(str(exc)) from exc

    # ------------------------------------------------------------------ building

    async def _build(self, tenant_id: str, resource: CustomIntegration) -> Integration:
        spec = resource.spec
        cls = await self._remote_class(tenant_id, resource) if spec.source == "remote" else self._package_subclass(resource)
        instance = cls()
        setup = getattr(instance, "setup", None)
        if callable(setup):
            setup(self.engine)
        return instance

    async def _remote_class(self, tenant_id: str, resource: CustomIntegration) -> type[Integration]:
        spec = resource.spec
        url = check_url(spec.url or "", allow_insecure=spec.allow_insecure)
        token = None
        if spec.token_secret:
            with use_namespace(SHARED_NAMESPACE):
                token = await self.engine.secrets.get(tenant_id, spec.token_secret)
            if token is None:
                raise IntegrationError(f"secret '{spec.token_secret}' (in shared) is not set")
        transport = self.engine.http_transport

        def http(**kwargs: Any) -> httpx.AsyncClient:
            if transport is not None:
                kwargs["transport"] = transport
            return httpx.AsyncClient(**kwargs)

        manifest = await RemoteClient(url, token, http=http, timeout=spec.timeout_seconds).manifest()
        return bind_remote(
            manifest, url, token, timeout_seconds=spec.timeout_seconds, key=resource.metadata.name,
            title=spec.title, description=spec.description or resource.metadata.description,
            connection_scopes=list(effective_scopes(manifest.connection_scopes, spec)),
        )

    def _package_subclass(self, resource: CustomIntegration) -> type[Integration]:
        spec = resource.spec
        base = self._package_class(spec.package or "")
        sub: type[Integration] = type(f"Custom_{resource.metadata.name.replace('-', '_')}", (base,), {
            "key": resource.metadata.name,
            "title": spec.title or base.title,
            "description": spec.description or resource.metadata.description or base.description,
            "connection_scopes": effective_scopes(base.connection_scopes, spec),
        })
        sub.actions, sub.resources = dict(base.actions), dict(base.resources)  # bound remotes set theirs by hand
        return sub

    def _package_class(self, package: str) -> type[Integration]:
        """An installed integration class by plugin name, distribution, or ``module:Class``."""
        if ":" in package:
            if not self.engine.settings.integration_allow_imports:
                raise PluginError(
                    f"'{package}': importing integration modules is turned off on this server "
                    "(SHOAL_INTEGRATION_ALLOW_IMPORTS); install the package with a shoal.integrations entry point instead"
                )
            module, _, attr = package.partition(":")
            try:
                found: Any = getattr(importlib.import_module(module), attr)
            except (ImportError, AttributeError) as exc:
                raise PluginError(f"could not import '{package}': {exc}") from exc
            return _integration_class(found, package)
        if package in self.engine.registry.names("integrations"):
            return _integration_class(type(self.engine.registry.get("integrations", package)), package)
        wanted = _normalise(package)
        matches = [ep for ep in entry_points(group="shoal.integrations") if ep.dist is not None and _normalise(ep.dist.name) == wanted]
        if not matches:
            raise PluginError(f"no installed integration or package named '{package}'")
        if len(matches) > 1:
            names = ", ".join(sorted(ep.name for ep in matches))
            raise PluginError(f"package '{package}' provides several integrations ({names}); set package to one of them")
        try:
            return _integration_class(matches[0].load(), package)
        except ImportError as exc:
            raise PluginError(f"could not load '{package}': {exc}") from exc


def _integration_class(found: Any, what: str) -> type[Integration]:
    if isinstance(found, type) and issubclass(found, Integration):
        return found
    if isinstance(found, Integration):
        return type(found)
    raise PluginError(f"'{what}' is not a Shoal integration (subclass shoal.sdk.Integration)")
