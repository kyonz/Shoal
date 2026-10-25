"""Blueprint adoption: namespace owners adopt catalogue blueprints (architecture §10.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from shoal.api.deps import Ctx, CtxDep
from shoal.core.errors import NotFound
from shoal.core.models import KIND_BY_PLURAL
from shoal.store import tables as t

router = APIRouter(prefix="/v1")
PLURAL_BY_KIND = {k: p for p, k in KIND_BY_PLURAL.items()}


class AdoptIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    prefix: str = Field(
        "",
        pattern=r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$",
        description="Prepended (with '-') to resource names through {{ prefix }} in the blueprint.",
    )
    version: str | None = Field(
        None, description="Blueprint spec.version to adopt; defaults to the current one."
    )
    dry_run: bool = False


class UpgradeIn(BaseModel):
    version: str | None = Field(
        None, description="Blueprint spec.version to move to; defaults to the current one."
    )
    params: dict[str, Any] | None = Field(
        None, description="Parameters to change; the rest keep their adopted values."
    )
    dry_run: bool = False


class ResourceRef(BaseModel):
    kind: str
    name: str


class AdoptionOut(BaseModel):
    id: str
    namespace: str
    blueprint: str
    blueprint_version: str
    prefix: str
    params: dict[str, Any]
    resources: list[ResourceRef]
    created_by: str
    created_at: datetime
    updated_at: datetime


class AppliedResource(ResourceRef):
    outcome: str | None = None
    version: int | None = None
    plural: str | None = None


class AdoptResult(BaseModel):
    adoption: AdoptionOut | None = Field(None, description="Absent on a dry run.")
    resources: list[AppliedResource]
    removed: list[ResourceRef] = Field(
        default_factory=list, description="Resources the new version no longer has."
    )
    dry_run: bool = False


def _out(row: t.BlueprintAdoption) -> AdoptionOut:
    return AdoptionOut.model_validate(row, from_attributes=True)


async def _authorize_resources(ctx: Ctx, namespace: str, name: str, version: str | None) -> None:
    """Adopting writes resources, so the caller needs write on every kind the blueprint renders."""
    svc = ctx.engine.blueprints
    blueprint = await svc.blueprint(ctx.tenant_id, name, version)
    kinds = {r.get("kind") for r in blueprint.spec.resources}
    for kind in sorted(k for k in kinds if k in PLURAL_BY_KIND):
        await ctx.require(f"{PLURAL_BY_KIND[kind]}:write", namespace)


def _result(raw: dict[str, Any]) -> AdoptResult:
    if raw.get("dry_run"):
        return AdoptResult(resources=raw["resources"], dry_run=True)
    return AdoptResult(adoption=_out(raw["adoption"]), resources=raw["resources"], removed=raw["removed"])


@router.post(
    "/namespaces/{namespace}/blueprints/{name}/adopt", tags=["blueprints"], response_model=AdoptResult
)
async def adopt(namespace: str, name: str, body: AdoptIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.namespace(namespace, "blueprints:adopt")
    await _authorize_resources(ctx, namespace, name, body.version)
    raw = await ctx.engine.blueprints.adopt(
        ctx.tenant_id,
        namespace,
        name,
        body.params,
        actor=ctx.actor,
        prefix=body.prefix,
        version=body.version,
        dry_run=body.dry_run,
        can_adopt_drafts=await ctx.engine.authz.allowed(ctx.principal, "blueprints:publish"),
    )
    if not body.dry_run:
        response.status_code = 201
        await ctx.audit(
            "blueprint.adopted",
            name,
            namespace=namespace,
            adoption=raw["adoption"].id,
            version=raw["adoption"].blueprint_version,
            prefix=body.prefix,
        )
    return _result(raw)


@router.get(
    "/namespaces/{namespace}/blueprint-adoptions", tags=["blueprints"], response_model=list[AdoptionOut]
)
async def list_adoptions(namespace: str, ctx: CtxDep, blueprint: str | None = None) -> Any:
    await ctx.namespace(namespace, "workflows:read")
    return [
        _out(r)
        for r in await ctx.engine.blueprints.adoptions(
            ctx.tenant_id, namespace=namespace, blueprint=blueprint
        )
    ]


async def _adoption(ctx: Ctx, namespace: str, adoption_id: str, permission: str) -> t.BlueprintAdoption:
    await ctx.namespace(namespace, permission)
    row = await ctx.engine.blueprints.adoption(ctx.tenant_id, adoption_id)
    if row.namespace != namespace:
        raise NotFound(f"adoption '{adoption_id}' not found")
    return row


@router.get(
    "/namespaces/{namespace}/blueprint-adoptions/{adoption_id}",
    tags=["blueprints"],
    response_model=AdoptionOut,
)
async def get_adoption(namespace: str, adoption_id: str, ctx: CtxDep) -> Any:
    return _out(await _adoption(ctx, namespace, adoption_id, "workflows:read"))


@router.put(
    "/namespaces/{namespace}/blueprint-adoptions/{adoption_id}",
    tags=["blueprints"],
    response_model=AdoptResult,
    summary="Upgrade an adoption to another blueprint version or change its parameters",
)
async def upgrade_adoption(namespace: str, adoption_id: str, body: UpgradeIn, ctx: CtxDep) -> Any:
    row = await _adoption(ctx, namespace, adoption_id, "blueprints:adopt")
    await _authorize_resources(ctx, namespace, row.blueprint, body.version)
    raw = await ctx.engine.blueprints.upgrade(
        ctx.tenant_id,
        adoption_id,
        actor=ctx.actor,
        version=body.version,
        params=body.params,
        dry_run=body.dry_run,
    )
    if not body.dry_run:
        await ctx.audit(
            "blueprint.upgraded",
            row.blueprint,
            namespace=namespace,
            adoption=adoption_id,
            version=raw["adoption"].blueprint_version,
        )
    return _result(raw)


@router.delete(
    "/namespaces/{namespace}/blueprint-adoptions/{adoption_id}",
    status_code=204,
    tags=["blueprints"],
    summary="Remove an adoption and the resources it created",
)
async def remove_adoption(namespace: str, adoption_id: str, ctx: CtxDep) -> Response:
    row = await _adoption(ctx, namespace, adoption_id, "blueprints:adopt")
    for ref in row.resources:
        if ref["kind"] in PLURAL_BY_KIND:
            await ctx.require(f"{PLURAL_BY_KIND[ref['kind']]}:delete", namespace)
    removed = await ctx.engine.blueprints.remove(ctx.tenant_id, adoption_id)
    await ctx.audit(
        "blueprint.removed", row.blueprint, namespace=namespace, adoption=adoption_id, resources=removed
    )
    return Response(status_code=204)
