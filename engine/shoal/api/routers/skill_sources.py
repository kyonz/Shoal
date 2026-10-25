"""Skill sources (marketplaces): browse them and import skills into a namespace.

Sources themselves are tenant-wide resources (``/v1/skill-sources``, managed
with ``skill-sources:manage``). Imported skills are ordinary ``Skill``
resources, so their history, diff and rollback are the skill routes'.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from shoal.api.deps import Ctx, CtxDep
from shoal.skills.base import SkillBundle, SkillSummary

router = APIRouter(prefix="/v1")
TAG = ["skill-sources"]


class SourceOut(BaseModel):
    name: str
    source: str = Field(description="Plugin: git, registry, ...")
    title: str
    description: str | None = None


class InstalledRef(BaseModel):
    name: str
    version: str | None = None
    ref: str | None = None


class CatalogSkill(SkillSummary):
    installed: list[InstalledRef] = Field(default_factory=list, description="Skills in this namespace imported from it.")


class CatalogPage(BaseModel):
    source: str
    items: list[CatalogSkill]
    next_cursor: str | None = None


class SkillDetail(SkillBundle):
    source: str
    installed: list[InstalledRef] = Field(default_factory=list)


class ImportIn(BaseModel):
    source: str = Field(description="SkillSource name.")
    skill: str = Field(description="Skill id within the source.")
    name: str | None = Field(
        None, pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", description="Skill resource name; defaults to the skill's name."
    )
    version: str | None = Field(None, description="Version or ref to import; defaults to what the source is pinned to.")
    dry_run: bool = Field(False, description="Return the diff without writing anything.")


class UpgradeIn(BaseModel):
    version: str | None = Field(None, description="Version or ref to move to; defaults to the source's current one.")
    dry_run: bool = False


class ImportResult(BaseModel):
    source: str
    skill_id: str
    name: str
    namespace: str
    version: str
    ref: str | None = None
    previous_version: str | None = None
    outcome: str = Field(description="created, updated or unchanged (what would happen, on a dry run).")
    changes: list[dict[str, Any]] = Field(description="Diff from the namespace's current skill, as {op, path, from?, to?}.")
    skipped_files: list[str] = Field(default_factory=list, description="Files left out (binary or too large).")
    resource_version: int | None = Field(None, description="The Skill's new version; roll back to an earlier one to undo.")
    dry_run: bool = False


class ImportedSkillOut(BaseModel):
    name: str
    namespace: str
    source: str | None
    skill_id: str | None
    version: str | None
    ref: str | None = None
    resource_version: int
    updated_at: datetime
    updated_by: str | None = None
    latest_version: str | None = None
    latest_ref: str | None = None
    update_available: bool | None = Field(None, description="Only with check=true; null when the source could not be reached.")
    check_error: str | None = None


def _source_out(resource: Any) -> SourceOut:
    return SourceOut(
        name=resource.metadata.name,
        source=resource.spec.source,
        title=resource.spec.title or resource.metadata.name,
        description=resource.spec.description or resource.metadata.description,
    )


@router.get("/skill-source-types", tags=["catalog"], summary="Skill source plugins and their config schemas")
async def source_types(ctx: CtxDep) -> list[dict[str, Any]]:
    return ctx.engine.skill_sources.catalog()


@router.post("/skill-sources/{name}/test", tags=TAG, summary="Check that a skill source can be reached")
async def test_source(name: str, ctx: CtxDep) -> dict[str, Any]:
    await ctx.require("skill-sources:manage")
    return await ctx.engine.skill_sources.test(ctx.tenant_id, name)


@router.get("/namespaces/{namespace}/skill-sources", tags=TAG, response_model=list[SourceOut],
            summary="Skill sources this namespace can browse")
async def visible_sources(namespace: str, ctx: CtxDep) -> Any:
    await ctx.namespace(namespace, "skills:read")
    return [_source_out(s) for s in await ctx.engine.skill_sources.visible(ctx.tenant_id, namespace)]


async def _installed(ctx: Ctx, namespace: str, source: str) -> dict[str, list[InstalledRef]]:
    out: dict[str, list[InstalledRef]] = {}
    for item in await ctx.engine.skill_sources.imports(ctx.tenant_id, namespace):
        if item["source"] == source and item["skill_id"]:
            out.setdefault(item["skill_id"], []).append(InstalledRef(name=item["name"], version=item["version"], ref=item["ref"]))
    return out


@router.get("/namespaces/{namespace}/skill-sources/{source}/skills", tags=TAG, response_model=CatalogPage,
            summary="List or search a source's skills")
async def browse(namespace: str, source: str, ctx: CtxDep, q: str | None = Query(None, max_length=200),
                 cursor: str | None = None, limit: int = Query(50, ge=1, le=200)) -> Any:
    await ctx.namespace(namespace, "skills:read")
    page = await ctx.engine.skill_sources.browse(ctx.tenant_id, namespace, source, query=q, cursor=cursor, limit=limit)
    installed = await _installed(ctx, namespace, source)
    items = [CatalogSkill(**s.model_dump(), installed=installed.get(s.id, [])) for s in page.items]
    return CatalogPage(source=source, items=items, next_cursor=page.next_cursor)


@router.get("/namespaces/{namespace}/skill-sources/{source}/skills/{skill_id:path}", tags=TAG, response_model=SkillDetail,
            summary="One skill from a source: its SKILL.md, files and version")
async def show(namespace: str, source: str, skill_id: str, ctx: CtxDep, version: str | None = None) -> Any:
    await ctx.namespace(namespace, "skills:read")
    bundle = await ctx.engine.skill_sources.fetch(ctx.tenant_id, namespace, source, skill_id, version)
    installed = await _installed(ctx, namespace, source)
    return SkillDetail(**bundle.model_dump(), source=source, installed=installed.get(bundle.id, []))


@router.get("/namespaces/{namespace}/skill-imports", tags=TAG, response_model=list[ImportedSkillOut],
            summary="Skills imported from skill sources, optionally checked for updates")
async def list_imports(namespace: str, ctx: CtxDep, check: bool = False) -> Any:
    await ctx.namespace(namespace, "skills:read")
    return await ctx.engine.skill_sources.imports(ctx.tenant_id, namespace, check=check)


async def _authorize_import(ctx: Ctx, namespace: str) -> None:
    await ctx.namespace(namespace, "skills:import")
    await ctx.require("skills:write", namespace)


@router.post("/namespaces/{namespace}/skill-imports", tags=TAG, response_model=ImportResult,
             summary="Import a skill from a source as a Skill resource", responses={201: {"description": "Created"}})
async def import_skill(namespace: str, body: ImportIn, ctx: CtxDep, response: Response) -> Any:
    await _authorize_import(ctx, namespace)
    result = await ctx.engine.skill_sources.import_skill(
        ctx.tenant_id, namespace, body.source, body.skill,
        actor=ctx.actor, name=body.name, version=body.version, dry_run=body.dry_run,
    )
    if not body.dry_run and result["outcome"] != "unchanged":
        await ctx.audit("skill.imported", f"{namespace}/{result['name']}", namespace=namespace, source=body.source,
                        skill=body.skill, version=result["version"], ref=result["ref"])
    if result["outcome"] == "created" and not body.dry_run:
        response.status_code = 201
    return result


@router.post("/namespaces/{namespace}/skill-imports/{name}/upgrade", tags=TAG, response_model=ImportResult,
             summary="Re-import a skill from its source; dry_run shows the diff first")
async def upgrade_skill(namespace: str, name: str, body: UpgradeIn, ctx: CtxDep) -> Any:
    await _authorize_import(ctx, namespace)
    result = await ctx.engine.skill_sources.upgrade(
        ctx.tenant_id, namespace, name, actor=ctx.actor, version=body.version, dry_run=body.dry_run
    )
    if not body.dry_run and result["outcome"] != "unchanged":
        await ctx.audit("skill.upgraded", f"{namespace}/{name}", namespace=namespace, source=result["source"],
                        skill=result["skill_id"], version=result["version"], previous=result["previous_version"])
    return result
