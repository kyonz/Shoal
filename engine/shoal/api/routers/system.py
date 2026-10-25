"""Health, server info, tenants, the caller, and catalogues."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, TypeAdapter

import shoal
from shoal.api.deps import CtxDep, EngineDep, InstanceReaderCtxDep, SystemCtxDep
from shoal.api.schemas import Me, Permissions, TenantCreated, TenantIn, TenantOut
from shoal.blueprints.defaults import install as install_default_blueprints
from shoal.core.models import KIND_BY_PLURAL, RESOURCE_KINDS, Step
from shoal.core.scope import TENANT_SCOPED_KINDS
from shoal.limits import TenantQuotas


class QuotasOut(BaseModel):
    tenant: TenantQuotas
    effective: TenantQuotas

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(engine: EngineDep) -> dict[str, str]:
    await engine.store.list_tenants()
    return {"status": "ready"}


@router.get("/.well-known/openid-configuration", tags=["system"], summary="OIDC discovery for workload identity tokens")
async def openid_configuration(engine: EngineDep) -> dict[str, Any]:
    return engine.workload.discovery()


@router.get("/.well-known/jwks.json", tags=["system"], summary="Keys that verify workload identity tokens")
async def jwks(engine: EngineDep) -> dict[str, Any]:
    return engine.workload.jwks()


@router.get("/v1/info", tags=["system"], summary="Server version, features and installed plugins")
async def info(engine: EngineDep) -> dict[str, Any]:
    reg = engine.registry
    return {
        "name": "shoal",
        "version": shoal.__version__,
        "api_version": "v1",
        "public_url": engine.settings.public_url,
        "features": ["namespaces", "rbac", "oidc", "scim", "campaigns", "blueprints", "skill-sources", "evals", "decisions", "a2a",
                     "assistant", "script-languages", "cloud-identities", "instance-accounts"],
        "instance_sign_in": engine.settings.instance_oidc is not None,
        "plugins": {group: reg.names(group) for group in ("integrations", "sandboxes", "providers", "agent_runtimes", "decision_providers", "skill_sources", "steps", "credentials")},
    }


@router.get("/metrics", include_in_schema=False)
async def metrics(engine: EngineDep) -> Response:
    lines = ["# TYPE shoal_up gauge", "shoal_up 1"]
    for tenant in await engine.store.list_tenants():
        for status, count in (await engine.store.run_status_counts(tenant.id)).items():
            lines.append(f'shoal_runs{{tenant="{tenant.slug}",status="{status}"}} {count}')
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# --------------------------------------------------------------------------- tenants (system admin)


@router.post("/v1/tenants", status_code=201, tags=["tenants"], response_model=TenantCreated)
async def create_tenant(body: TenantIn, ctx: SystemCtxDep) -> TenantCreated:
    engine = ctx.engine
    tenant = await engine.store.create_tenant(body.slug, body.name, description=body.description)
    admin = await engine.directory.create_user(tenant.id, "admin", kind="service", display_name="Bootstrap admin")
    await engine.directory.bind(tenant.id, "tenant-admin", "user", admin.id, actor=ctx.actor)
    if body.admin_email:  # links their instance account when they have one, so the tenant shows up in their switcher
        await engine.accounts.add_to_tenant(tenant.id, body.admin_email, "tenant-admin", actor=ctx.actor)
    _, token = await engine.store.create_api_key(tenant.id, "bootstrap", ["*"], owner_id=admin.id, created_by=ctx.actor)
    if engine.settings.default_blueprints:
        await install_default_blueprints(engine, tenant.id, actor=ctx.actor)
    await engine.store.audit(tenant.id, ctx.actor, "tenant.created", tenant.slug)
    await engine.store.audit(None, ctx.actor, "tenant.created", tenant.slug)
    return TenantCreated(tenant=TenantOut.model_validate(tenant), api_key=token)


@router.get("/v1/tenants", tags=["tenants"], response_model=list[TenantOut])
async def list_tenants(ctx: InstanceReaderCtxDep) -> Any:
    return await ctx.engine.store.list_tenants()


@router.get("/v1/tenants/{slug}", tags=["tenants"], response_model=TenantOut)
async def get_tenant(slug: str, ctx: InstanceReaderCtxDep) -> Any:
    from shoal.core.errors import NotFound

    tenant = await ctx.engine.store.get_tenant_by_slug(slug)
    if tenant is None:
        raise NotFound(f"tenant '{slug}' not found")
    return tenant


@router.delete("/v1/tenants/{slug}", status_code=204, tags=["tenants"])
async def delete_tenant(slug: str, ctx: SystemCtxDep) -> None:
    await ctx.engine.store.delete_tenant(slug)
    await ctx.engine.store.audit(None, ctx.actor, "tenant.deleted", slug)


async def _tenant(ctx: SystemCtxDep, slug: str) -> Any:
    from shoal.core.errors import NotFound

    tenant = await ctx.engine.store.get_tenant_by_slug(slug)
    if tenant is None:
        raise NotFound(f"tenant '{slug}' not found")
    return tenant


@router.get("/v1/tenants/{slug}/quotas", tags=["tenants"], response_model=QuotasOut)
async def get_tenant_quotas(slug: str, ctx: SystemCtxDep) -> QuotasOut:
    tenant = await _tenant(ctx, slug)
    return await _quotas(ctx, tenant)


@router.put("/v1/tenants/{slug}/quotas", tags=["tenants"], response_model=QuotasOut,
            summary="Set a tenant's quotas (unset fields use the server defaults; 0 means unlimited)")
async def put_tenant_quotas(slug: str, body: TenantQuotas, ctx: SystemCtxDep) -> QuotasOut:
    tenant = await _tenant(ctx, slug)
    own = body.model_dump(exclude_none=True)
    tenant = await ctx.engine.store.update_tenant_settings(tenant.id, quotas=own or None)
    ctx.engine.quotas.forget(tenant.id)
    await ctx.engine.store.audit(tenant.id, ctx.actor, "tenant.quotas", slug, quotas=own)
    return await _quotas(ctx, tenant)


async def _quotas(ctx: Any, tenant: Any) -> QuotasOut:
    own = TenantQuotas.model_validate((tenant.settings or {}).get("quotas") or {})
    ctx.engine.quotas.forget(tenant.id)
    return QuotasOut(tenant=own, effective=await ctx.engine.quotas.for_tenant(tenant.id))


@router.get("/v1/quotas", tags=["me"], response_model=QuotasOut, summary="The calling tenant's quotas")
async def my_quotas(ctx: CtxDep) -> QuotasOut:
    tenant = await ctx.engine.store.get_tenant(ctx.tenant_id)
    own = TenantQuotas.model_validate((tenant.settings or {}).get("quotas") or {})
    return QuotasOut(tenant=own, effective=await ctx.engine.quotas.for_tenant(tenant.id))


# --------------------------------------------------------------------------- the caller


@router.get("/v1/me", tags=["me"], response_model=Me)
async def me(ctx: CtxDep) -> Me:
    p = ctx.principal
    tenant = await ctx.engine.store.get_tenant(p.tenant_id) if p.tenant_id else None
    namespaces = await ctx.engine.authz.visible_namespaces(p) if p.tenant_id else []
    return Me(
        tenant=TenantOut.model_validate(tenant) if tenant else None,
        actor=p.actor,
        kind=p.kind,
        user_id=p.user_id,
        display_name=p.display_name,
        email=p.email,
        is_system_admin=p.is_system_admin,
        account_id=p.account_id,
        instance_role=p.instance_role,
        namespaces=namespaces,
        federated=bool(p.tenant_id and await ctx.engine.federation.federated_link_id(p.tenant_id)),
    )


@router.get("/v1/me/permissions", tags=["me"], response_model=Permissions)
async def my_permissions(ctx: CtxDep) -> Any:
    ctx.tenant_id  # noqa: B018 - requires a tenant
    return await ctx.engine.authz.summary(ctx.principal)


# --------------------------------------------------------------------------- catalogues


@router.get("/v1/integrations", tags=["catalog"], summary="Installed integrations and the ones the organisation registered")
async def integrations(ctx: CtxDep, namespace: str | None = Query(
    None, description="Only the registered integrations this namespace may use (default: all of them)."
)) -> list[dict[str, Any]]:
    return await ctx.engine.integrations.catalog(ctx.principal.tenant_id, namespace=namespace)


@router.get("/v1/integrations/{key}", tags=["catalog"])
async def integration(key: str, ctx: CtxDep) -> dict[str, Any]:
    if ctx.principal.tenant_id:
        found = await ctx.engine.integrations.resolve(ctx.principal.tenant_id, key)
    else:
        found = ctx.engine.integrations.get(key)
    return type(found).catalog()


@router.get("/v1/plugins", tags=["catalog"], summary="Installed plugins of every kind")
async def plugins(ctx: CtxDep) -> list[dict[str, Any]]:
    from shoal.sdk.plugins import PLUGIN_GROUPS

    out = []
    for group, info in PLUGIN_GROUPS.items():
        for name, plugin in ctx.engine.registry.available(group).items():
            cls = plugin if isinstance(plugin, type) else type(plugin)
            doc = getattr(plugin, "description", "") or (cls.__doc__ or "").strip().split("\n")[0]
            out.append({"group": group, "name": name, "description": doc, "module": cls.__module__, "kind": info.summary})
    return out


@router.get("/v1/sandbox-backends", tags=["catalog"])
async def sandbox_backends(ctx: CtxDep) -> list[dict[str, Any]]:
    out = []
    for name, backend in ctx.engine.registry.available("sandboxes").items():
        schema = backend.config_schema.model_json_schema() if getattr(backend, "config_schema", None) else None
        out.append({"name": name, "description": getattr(backend, "description", ""), "config_schema": schema,
                    "network_controls": sorted(getattr(backend, "network_controls", ()))})
    return out


def _describe(plugin: Any) -> str:
    """A plugin's ``description``, else the first line of its class docstring."""
    cls = plugin if isinstance(plugin, type) else type(plugin)
    return getattr(plugin, "description", "") or (cls.__doc__ or "").strip().split("\n")[0]


@router.get("/v1/providers", tags=["catalog"], summary="Model provider plugins (gateway types)")
async def providers(ctx: CtxDep) -> list[dict[str, Any]]:
    return [{"name": n, "description": _describe(p)} for n, p in ctx.engine.registry.available("providers").items()]


@router.get("/v1/decision-providers", tags=["catalog"])
async def decision_providers(ctx: CtxDep) -> list[dict[str, Any]]:
    return [
        {"name": n, "description": _describe(p), "default": n == ctx.engine.settings.default_decision_provider}
        for n, p in ctx.engine.registry.available("decision_providers").items()
    ]


@router.get("/v1/schemas", tags=["catalog"], summary="JSON Schemas of every resource kind")
async def schemas() -> dict[str, Any]:
    return {
        plural: {
            "kind": kind,
            "scope": "tenant" if kind in TENANT_SCOPED_KINDS else "namespace",
            "spec_schema": RESOURCE_KINDS[kind].model_fields["spec"].annotation.model_json_schema(),  # type: ignore[union-attr]
        }
        for plural, kind in KIND_BY_PLURAL.items()
    }


@router.get("/v1/schemas/steps", tags=["catalog"], summary="JSON Schema of workflow steps (a union keyed by `type`)")
async def step_schema() -> dict[str, Any]:
    return TypeAdapter(Step).json_schema()
