"""Blueprint adoption: namespace owners adopt catalogue blueprints (architecture §10.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from shoal.api.deps import Ctx, CtxDep
from shoal.blueprints import defaults
from shoal.blueprints.service import Authorize
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
        default_factory=list, description="Resources the new version no longer has (deleted, or to be on a dry run)."
    )
    dry_run: bool = False


def _out(row: t.BlueprintAdoption) -> AdoptionOut:
    return AdoptionOut.model_validate(row, from_attributes=True)


def _authorizer(ctx: Ctx, namespace: str) -> Authorize:
    """Adopting writes resources, so the caller needs write on every kind the blueprint renders,
    and delete on every kind an upgrade would remove."""

    async def check(written: set[str], removed: set[str]) -> None:
        for kind in sorted(k for k in written if k in PLURAL_BY_KIND):
            await ctx.require(f"{PLURAL_BY_KIND[kind]}:write", namespace)
        for kind in sorted(k for k in removed if k in PLURAL_BY_KIND):
            await ctx.require(f"{PLURAL_BY_KIND[kind]}:delete", namespace)

    return check


def _result(raw: dict[str, Any]) -> AdoptResult:
    if raw.get("dry_run"):
        return AdoptResult(resources=raw["resources"], removed=raw["removed"], dry_run=True)
    return AdoptResult(adoption=_out(raw["adoption"]), resources=raw["resources"], removed=raw["removed"])


@router.post(
    "/namespaces/{namespace}/blueprints/{name}/adopt", tags=["blueprints"], response_model=AdoptResult
)
async def adopt(namespace: str, name: str, body: AdoptIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.namespace(namespace, "blueprints:adopt")
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
        authorize=_authorizer(ctx, namespace),
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
    raw = await ctx.engine.blueprints.upgrade(
        ctx.tenant_id,
        adoption_id,
        actor=ctx.actor,
        version=body.version,
        params=body.params,
        dry_run=body.dry_run,
        authorize=_authorizer(ctx, namespace),
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


class KeptResource(ResourceRef):
    reason: str


class RemoveResult(BaseModel):
    removed: list[ResourceRef] = Field(description="Resources deleted with the adoption.")
    kept: list[KeptResource] = Field(
        default_factory=list, description="Resources left alone: already deleted, or no longer labelled with this adoption."
    )


@router.delete(
    "/namespaces/{namespace}/blueprint-adoptions/{adoption_id}",
    tags=["blueprints"],
    response_model=RemoveResult,
    summary="Remove an adoption and the resources it created",
)
async def remove_adoption(namespace: str, adoption_id: str, ctx: CtxDep) -> Any:
    row = await _adoption(ctx, namespace, adoption_id, "blueprints:adopt")
    for ref in row.resources:
        if ref["kind"] in PLURAL_BY_KIND:
            await ctx.require(f"{PLURAL_BY_KIND[ref['kind']]}:delete", namespace)
    result = await ctx.engine.blueprints.remove(ctx.tenant_id, adoption_id)
    await ctx.audit(
        "blueprint.removed", row.blueprint, namespace=namespace, adoption=adoption_id, resources=result["removed"],
        kept=result["kept"],
    )
    return result


class DefaultsIn(BaseModel):
    dry_run: bool = False


class DefaultBlueprint(BaseModel):
    name: str
    version: str
    outcome: str = Field(
        description="created, updated (an older bundled copy), unchanged, or kept (the tenant's own blueprint has "
        "the name, or its copy is newer)."
    )


@router.post(
    "/blueprints/defaults",
    tags=["blueprints"],
    response_model=list[DefaultBlueprint],
    summary="Add the blueprints that ship with Shoal to the catalogue",
)
async def install_defaults(body: DefaultsIn, ctx: CtxDep) -> Any:
    """Creates the bundled blueprints the catalogue lacks and upgrades older bundled copies. Blueprints the
    tenant wrote itself are never touched, and an upgraded copy keeps its status."""
    await ctx.require("blueprints:publish")
    await ctx.require("blueprints:feature")
    result = await defaults.install(ctx.engine, ctx.tenant_id, actor=ctx.actor, dry_run=body.dry_run)
    if not body.dry_run and any(r["outcome"] in ("created", "updated") for r in result):
        await ctx.audit(
            "blueprint.defaults_installed",
            "blueprints",
            installed=[r["name"] for r in result if r["outcome"] in ("created", "updated")],
        )
    return result
