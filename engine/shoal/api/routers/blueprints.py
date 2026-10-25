"""Blueprint adoption: namespace owners adopt catalogue blueprints, and teams save
workflows as blueprints for others to adopt (architecture §10.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from shoal.api.deps import Ctx, CtxDep
from shoal.core.errors import NotFound
from shoal.core.models import KIND_BY_PLURAL, NAME_PATTERN
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


# --------------------------------------------------------------------------- save a workflow as a blueprint


DETAILS = {"title", "summary", "readme", "author", "icon", "tags", "categories", "namespaces"}


class FindingChoice(BaseModel):
    enabled: bool | None = Field(None, description="Turn the value into a parameter (credentials always are).")
    name: str | None = Field(None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="Parameter name.")
    description: str | None = None
    default: Any = Field(None, description="Default the adopter can keep; null makes the parameter required.")


class SaveAsBlueprintIn(BaseModel):
    blueprint: str | None = Field(None, pattern=NAME_PATTERN, description="Blueprint name; defaults to the workflow's.")
    target: Literal["new", "existing"] = Field(
        "new", description="'new' creates the blueprint; 'existing' publishes a new version of it."
    )
    version: str | None = Field(
        None, description="Semantic version; defaults to 0.1.0, or the next minor version of an existing blueprint."
    )
    title: str | None = None
    summary: str | None = None
    readme: str | None = None
    author: str | None = None
    icon: str | None = None
    tags: list[str] | None = None
    categories: list[str] | None = None
    namespaces: list[str] | None = Field(None, description="Namespaces that may adopt it (empty: all).")
    status: Literal["draft", "published"] | None = Field(
        None, description="Defaults to draft for a new blueprint and the current status for a new version."
    )
    include: list[ResourceRef] | None = Field(
        None, description="Resources to package besides the workflow; defaults to those in its namespace."
    )
    params: dict[str, FindingChoice] = Field(
        default_factory=dict, description="Finding id -> how to turn it into a parameter (see the dry run)."
    )
    dry_run: bool = False


class PackagedResource(ResourceRef):
    namespace: str
    reason: str = Field(description="Why it is part of the package.")
    included: bool
    root: bool = False


class FindingOut(BaseModel):
    id: str
    category: str = Field(description="connection, secret, credential, repository, organization, jira_project, "
                                      "model, sandbox or cloud_identity.")
    value: str = Field(description="The hard-coded value (empty for credentials, which are never echoed).")
    param: str
    type: str
    integration: str | None = None
    description: str
    default: Any = None
    required: bool
    enabled: bool
    locked: bool = Field(description="Credentials are always stripped and cannot be kept.")
    locations: list[str]


class SaveAsBlueprintOut(BaseModel):
    blueprint: dict[str, Any] = Field(description="The Blueprint document (published, or would be on a dry run).")
    resources: list[PackagedResource]
    findings: list[FindingOut]
    stripped: list[str] = Field(description="Places where a credential-looking value was redacted from text.")
    warnings: list[str]
    exists: bool = Field(description="A blueprint with this name already exists.")
    previous_version: str | None = None
    outcome: str | None = Field(None, description="created, updated or unchanged; absent on a dry run.")
    resource_version: int | None = None
    dry_run: bool


@router.post(
    "/namespaces/{namespace}/workflows/{name}/save-as-blueprint",
    tags=["blueprints"],
    response_model=SaveAsBlueprintOut,
    summary="Package a workflow (and what it uses) as a new blueprint or a new blueprint version",
)
async def save_as_blueprint(namespace: str, name: str, body: SaveAsBlueprintIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.namespace(namespace, "workflows:read")
    svc = ctx.engine.blueprints
    packaged = await svc.package_workflow(
        ctx.tenant_id,
        namespace,
        name,
        blueprint=body.blueprint,
        target=body.target,
        version=body.version,
        details=body.model_dump(include=DETAILS, exclude_unset=True),
        status=body.status,
        include={(r.kind, r.name) for r in body.include} if body.include is not None else None,
        choices={k: v.model_dump(exclude_unset=True) for k, v in body.params.items()},
    )
    pkg = packaged["package"]
    for kind in sorted({c.kind for c in pkg.candidates if c.included}):  # it copies each of these
        await ctx.require(f"{PLURAL_BY_KIND[kind]}:read", namespace)
    outcome, version = None, None
    if not body.dry_run:
        await ctx.require("blueprints:publish")
        row, outcome = await svc.publish_package(ctx.tenant_id, packaged, actor=ctx.actor)
        version = row.version
        if outcome == "created":
            response.status_code = 201
        if outcome != "unchanged":
            spec = packaged["blueprint"]["spec"]
            await ctx.audit("blueprint.saved_from_workflow", row.name, namespace=namespace, workflow=name,
                            version=spec["version"], status=spec["status"],
                            resources=[f"{c.kind}/{c.name}" for c in pkg.candidates if c.included])
    return SaveAsBlueprintOut(
        blueprint=packaged["blueprint"],
        resources=[PackagedResource(kind=c.kind, name=c.name, namespace=c.namespace, reason=c.reason,
                                    included=c.included, root=c.root) for c in pkg.candidates],
        findings=[FindingOut(id=f.id, category=f.category, value=f.value, param=f.param, type=f.type,
                             integration=f.integration, description=f.description, default=f.default,
                             required=f.required, enabled=f.enabled, locked=f.locked,
                             locations=[pkg.location(loc) for loc in f.locations]) for f in pkg.findings],
        stripped=pkg.stripped,
        warnings=pkg.warnings,
        exists=packaged["exists"],
        previous_version=packaged["previous_version"],
        outcome=outcome,
        resource_version=version,
        dry_run=body.dry_run,
    )
