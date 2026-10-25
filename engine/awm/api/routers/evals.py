"""Eval runs (§7.4): start, progress, per-case scores, version comparison and gates,
score history per workflow version, and eval platform sinks (export / dataset import)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from awm.api.deps import Ctx, CtxDep
from awm.api.routers.resources import to_out
from awm.api.schemas import Out, Page, ResourceOut
from awm.core.errors import InvalidRequest
from awm.core.models import Eval, EvalVariant
from awm.core.scope import use_namespace
from awm.store import tables as t

router = APIRouter(prefix="/v1")


# --------------------------------------------------------------------------- schemas


class EvalRunIn(BaseModel):
    version: int | None = Field(None, ge=1, description="Workflow version to evaluate (default: current). Ignored with variants.")
    variants: list[EvalVariant] | None = Field(None, description="Arms to compare; overrides the eval's variants.")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Input overrides merged into every case.")
    baseline: str | None = Field(None, description="'previous', 'version:N', an eval run id, or 'none'.")
    fail_under: float | None = Field(None, ge=0, le=1)
    max_regression: float | None = Field(None, ge=0, le=1)
    cases: list[str] | None = Field(None, description="Only run these case names.")
    labels: dict[str, str] = Field(default_factory=dict)


class EvalRunOut(Out):
    id: str
    namespace: str
    eval_name: str
    eval_version: int
    workflow_name: str
    workflow_version: int
    variants: list[dict[str, Any]]
    mode: str
    status: str
    summary: dict[str, Any]
    gate: dict[str, Any]
    baseline_id: str | None
    exports: dict[str, Any]
    error: dict[str, Any] | None
    labels: dict[str, Any]
    started_by: str | None
    initiator: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None


class EvalResultOut(Out):
    id: str
    variant: str
    workflow_version: int
    case_name: str
    case: dict[str, Any]
    run_id: str | None
    status: str
    outputs: dict[str, Any] | None
    scores: dict[str, Any]
    score: float | None
    passed: bool | None
    error: dict[str, Any] | None
    cost_usd: float
    duration_ms: float | None
    finished_at: datetime | None


class EvalRunDetail(EvalRunOut):
    results: list[EvalResultOut]


class SinkIn(BaseModel):
    sink: str = Field(description="Eval sink plugin, e.g. 'braintrust' or 'langfuse'.")
    config: dict[str, Any] = Field(default_factory=dict, description="Sink settings; secrets by name (api_key_secret, ...).")


class ExportOut(BaseModel):
    sink: str
    url: str | None
    ids: dict[str, Any]


class DatasetImportIn(SinkIn):
    dataset: str
    limit: int | None = Field(None, ge=1, le=10000)
    mode: str = Field("append", pattern="^(append|replace)$", description="Append to or replace the eval's cases.")


class SinkOut(BaseModel):
    name: str
    description: str
    config_schema: dict[str, Any]


# --------------------------------------------------------------------------- helpers


async def _eval_run(ctx: Ctx, eval_run_id: str, permission: str = "evals:read") -> t.EvalRun:
    row = await ctx.engine.evals.get(ctx.tenant_id, eval_run_id)
    await ctx.require(permission, row.namespace)
    return row


async def _initiator(ctx: Ctx) -> dict[str, Any]:
    return await ctx.engine.identities.resolve(
        ctx.tenant_id, actor=ctx.actor, source="api", email=ctx.principal.email, display_name=ctx.principal.display_name
    )


async def _detail(ctx: Ctx, row: t.EvalRun) -> EvalRunDetail:
    results = await ctx.engine.evals.results(row.id)
    return EvalRunDetail(
        **EvalRunOut.model_validate(row).model_dump(), results=[EvalResultOut.model_validate(r) for r in results]
    )


# --------------------------------------------------------------------------- eval runs


@router.post("/namespaces/{namespace}/evals/{name}/runs", status_code=201, tags=["eval runs"], response_model=EvalRunDetail)
async def start_eval_run(namespace: str, name: str, ctx: CtxDep, body: EvalRunIn | None = None) -> Any:
    body = body or EvalRunIn()
    await ctx.namespace(namespace, "evals:run")
    with use_namespace(namespace):
        await ctx.engine.store.get_resource_row(ctx.tenant_id, "Eval", name, namespace=namespace, fallback=False)
        row = await ctx.engine.evals.start(
            ctx.tenant_id, name, variants=body.variants, version=body.version, inputs=body.inputs, baseline=body.baseline,
            fail_under=body.fail_under, max_regression=body.max_regression, cases=body.cases, started_by=ctx.actor,
            initiator=await _initiator(ctx), labels=body.labels,
        )
    await ctx.audit("eval.run_started", row.id, eval=f"{namespace}/{name}", eval_version=row.eval_version,
                    variants=[{"name": v["name"], "workflow_version": v["workflow_version"]} for v in row.variants],
                    baseline=row.baseline_id)
    return await _detail(ctx, row)


@router.get("/namespaces/{namespace}/eval-runs", tags=["eval runs"], response_model=Page[EvalRunOut])
async def list_eval_runs(
    namespace: str,
    ctx: CtxDep,
    eval: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    await ctx.namespace(namespace, "evals:read")
    rows = await ctx.engine.evals.list_runs(ctx.tenant_id, namespace, eval_name=eval, status=status, limit=limit + 1, before=cursor)
    items = list(rows[:limit])
    return Page(items=[EvalRunOut.model_validate(r) for r in items], next_cursor=items[-1].id if len(rows) > limit else None)


@router.get("/eval-runs/{eval_run_id}", tags=["eval runs"], response_model=EvalRunDetail,
            summary="An eval run with per-case scores, per-variant aggregates and the diff against its baseline")
async def get_eval_run(eval_run_id: str, ctx: CtxDep) -> Any:
    return await _detail(ctx, await _eval_run(ctx, eval_run_id))


@router.get("/eval-runs/{eval_run_id}/results", tags=["eval runs"], response_model=list[EvalResultOut])
async def eval_run_results(eval_run_id: str, ctx: CtxDep, variant: str | None = None) -> Any:
    row = await _eval_run(ctx, eval_run_id)
    return await ctx.engine.evals.results(row.id, variant=variant)


@router.post("/eval-runs/{eval_run_id}/cancel", tags=["eval runs"], response_model=EvalRunOut)
async def cancel_eval_run(eval_run_id: str, ctx: CtxDep) -> Any:
    row = await _eval_run(ctx, eval_run_id, "evals:run")
    with use_namespace(row.namespace):
        row = await ctx.engine.evals.cancel(ctx.tenant_id, row.id)
    await ctx.audit("eval.run_cancelled", row.id)
    return row


@router.get("/eval-runs/{eval_run_id}/compare", tags=["eval runs"], summary="Case-by-case diff against another eval run")
async def compare_eval_runs(eval_run_id: str, ctx: CtxDep, baseline: str) -> dict[str, Any]:
    row = await _eval_run(ctx, eval_run_id)
    base = await _eval_run(ctx, baseline)
    return await ctx.engine.evals.compare_runs(ctx.tenant_id, base.id, row.id)


@router.post("/eval-runs/{eval_run_id}/export", tags=["eval runs"], response_model=ExportOut)
async def export_eval_run(eval_run_id: str, body: SinkIn, ctx: CtxDep) -> Any:
    row = await _eval_run(ctx, eval_run_id, "evals:run")
    result = await ctx.engine.evals.export(ctx.tenant_id, row.id, body.sink, body.config)
    await ctx.audit("eval.exported", row.id, sink=body.sink, url=result.url)
    return ExportOut(sink=body.sink, url=result.url, ids=result.ids)


# --------------------------------------------------------------------------- per-version scores, datasets


@router.get("/namespaces/{namespace}/evals/{name}/scores", tags=["eval runs"],
            summary="Scores per workflow version across completed runs of an eval")
async def eval_scores(namespace: str, name: str, ctx: CtxDep, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    await ctx.namespace(namespace, "evals:read")
    versions = await ctx.engine.evals.history(ctx.tenant_id, namespace, name, limit=limit)
    return {"eval": name, "namespace": namespace, "versions": versions}


@router.post("/namespaces/{namespace}/evals/{name}/import", tags=["eval runs"], response_model=ResourceOut,
             summary="Import a dataset from an eval platform as the eval's cases (creates a new Eval version)")
async def import_eval_dataset(namespace: str, name: str, body: DatasetImportIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.namespace(namespace, "evals:write")
    with use_namespace(namespace):
        row = await ctx.engine.store.get_resource_row(ctx.tenant_id, "Eval", name, namespace=namespace, fallback=False)
        resource = Eval.model_validate(row.document)
        imported = await ctx.engine.evals.import_dataset(ctx.tenant_id, body.sink, body.dataset, body.config, limit=body.limit)
        if not imported:
            raise InvalidRequest(f"dataset '{body.dataset}' has no items")
        existing = [] if body.mode == "replace" else [c for c in resource.spec.cases if c.name not in {i.name for i in imported}]
        spec = resource.spec.model_copy(update={"cases": [*existing, *imported]})
        updated = Eval.model_validate({**resource.to_document(), "spec": spec.model_dump(mode="json", by_alias=True, exclude_none=True)})
        source = updated.to_document()["spec"]
        new_row, outcome = await ctx.engine.store.apply_resource(ctx.tenant_id, updated, actor=ctx.actor, namespace=namespace, source=source)
    await ctx.audit("eval.dataset_imported", f"{namespace}/{name}", sink=body.sink, dataset=body.dataset,
                    cases=len(imported), mode=body.mode, version=new_row.version, outcome=outcome)
    return await to_out(ctx, new_row)


# --------------------------------------------------------------------------- sinks


@router.get("/eval-sinks", tags=["eval sinks"], response_model=list[SinkOut])
async def list_eval_sinks(ctx: CtxDep) -> Any:
    sinks = ctx.engine.registry.available("eval_sinks")
    return [SinkOut(name=n, description=getattr(s, "description", ""), config_schema=getattr(s, "config_schema", {}))
            for n, s in sinks.items()]


@router.post("/runs/{run_id}/export", tags=["eval sinks"], response_model=ExportOut,
             summary="Export a workflow run as a trace to an eval platform")
async def export_run_trace(run_id: str, body: SinkIn, ctx: CtxDep) -> Any:
    run = await ctx.engine.store.get_run(ctx.tenant_id, run_id)
    await ctx.require("runs:read", run.namespace)
    await ctx.require("evals:run", run.namespace)
    result = await ctx.engine.evals.export_run(ctx.tenant_id, run.id, body.sink, body.config)
    await ctx.audit("run.exported", run.id, sink=body.sink, url=result.url)
    return ExportOut(sink=body.sink, url=result.url, ids=result.ids)
