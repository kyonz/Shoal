"""Workflow runs, live events (SSE), approvals, campaign runs and stats."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from sse_starlette.sse import EventSourceResponse

from shoal.api.deps import Ctx, CtxDep
from shoal.api.schemas import (
    AddTargetsIn,
    ApprovalIn,
    CampaignInlineIn,
    CampaignLaunchIn,
    CampaignRunOut,
    EventOut,
    Page,
    RunDetail,
    RunOut,
    SignalIn,
    SignalOut,
    StartRunIn,
    StepOut,
    TargetOut,
)
from shoal.core import clock
from shoal.core.errors import Conflict, InvalidRequest
from shoal.core.models import CampaignSpec, RunStatus
from shoal.core.scope import use_namespace
from shoal.store import tables as t

router = APIRouter(prefix="/v1")
TERMINAL_EVENTS = {f"run.{status.value}" for status in RunStatus if status.terminal}


async def _run(ctx: Ctx, run_id: str, permission: str = "runs:read") -> t.Run:
    run = await ctx.engine.store.get_run(ctx.tenant_id, run_id)
    await ctx.require(permission, run.namespace)
    return run


async def _initiator(ctx: Ctx) -> dict[str, Any]:
    return await ctx.engine.identities.resolve(
        ctx.tenant_id, actor=ctx.actor, source="api", email=ctx.principal.email, display_name=ctx.principal.display_name
    )


# --------------------------------------------------------------------------- runs


@router.post("/namespaces/{namespace}/workflows/{name}/runs", status_code=201, tags=["runs"], response_model=RunOut)
async def start_run(
    namespace: str,
    name: str,
    body: StartRunIn,
    ctx: CtxDep,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(max_length=200, description="Retrying with the same key returns the run the first call started (200)."),
    ] = None,
) -> Any:
    await ctx.namespace(namespace, "runs:create")
    with use_namespace(namespace):
        row = await ctx.engine.store.get_resource_row(ctx.tenant_id, "Workflow", name, namespace=namespace, fallback=False)
        try:
            run = await ctx.engine.executor.start_run(
                ctx.tenant_id, name, body.inputs, version=body.version, started_by=ctx.actor, labels=body.labels,
                initiator=await _initiator(ctx), idempotency_key=idempotency_key, admit=True, row=row,
                audit={"workflow": name, "version": row.version if body.version is None else body.version},
            )
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
    if getattr(run, "replayed", False):
        if run.namespace != namespace:
            raise Conflict("idempotency key was already used in another namespace")
        response.status_code = 200
    return run


@router.get("/namespaces/{namespace}/runs", tags=["runs"], response_model=Page[RunOut])
async def list_runs(
    namespace: str,
    ctx: CtxDep,
    workflow: str | None = None,
    status: str | None = None,
    campaign_id: str | None = None,
    trigger: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, le=200),
) -> Any:
    await ctx.namespace(namespace, "runs:read")
    rows = await ctx.engine.store.list_runs(
        ctx.tenant_id, namespace=namespace, workflow=workflow, status=status, campaign_id=campaign_id,
        trigger=trigger, limit=limit + 1, before=cursor, replica=True,
    )
    items = list(rows[:limit])
    return Page(items=[RunOut.model_validate(r) for r in items], next_cursor=items[-1].id if len(rows) > limit else None)


@router.get("/runs/{run_id}", tags=["runs"], response_model=RunDetail)
async def get_run(run_id: str, ctx: CtxDep) -> Any:
    run = await _run(ctx, run_id)
    steps = await ctx.engine.store.get_steps(run.id)
    order = {s["id"]: i for i, s in enumerate(run.workflow.get("steps", []))}
    steps.sort(key=lambda s: order.get(s.step_id, 1_000))
    return RunDetail(
        **RunOut.model_validate(run).model_dump(),
        workflow=run.workflow,
        steps=[StepOut.model_validate(s) for s in steps],
        context=run.context,
    )


@router.get("/runs/{run_id}/events", tags=["runs"], summary="Run events; streams as SSE with Accept: text/event-stream")
async def run_events(
    run_id: str, request: Request, ctx: CtxDep, after: int = 0, step_id: str | None = None,
    wait: float = Query(0, ge=0, le=25, description="Seconds to wait for new events when there are none yet (long poll)."),
) -> Any:
    run = await _run(ctx, run_id)
    store, bus = ctx.engine.store, ctx.engine.events
    if "text/event-stream" not in request.headers.get("accept", ""):
        events = await store.list_events(ctx.tenant_id, run.id, after=after, step_id=step_id)
        deadline = time.monotonic() + wait
        while not events and not RunStatus(run.status).terminal and (remaining := deadline - time.monotonic()) > 0:
            woken = await bus.wait(run.id, timeout=min(remaining, bus.poll_seconds))
            events = await store.list_events(ctx.tenant_id, run.id, after=after, step_id=step_id)
            if not woken:
                run = await store.get_run(ctx.tenant_id, run.id)
        return [EventOut.model_validate(e) for e in events]
    last = int(request.headers.get("last-event-id") or after)

    async def stream() -> AsyncIterator[dict[str, Any]]:
        nonlocal last
        finished = RunStatus(run.status).terminal
        while True:
            events = await store.list_events(ctx.tenant_id, run.id, after=last, step_id=step_id)
            for event in events:
                last = event.id
                finished = finished or event.type in TERMINAL_EVENTS
                yield {"id": str(event.id), "event": event.type, "data": EventOut.model_validate(event).model_dump_json()}
            if finished and not events:
                current = await store.get_run(ctx.tenant_id, run.id)
                yield {"event": "end", "data": json.dumps({"status": current.status})}
                return
            if await request.is_disconnected():
                return
            if not await bus.wait(run.id, timeout=bus.poll_seconds) and not finished:
                # nothing woke us: re-check the run (a missed notification, or a filtered step stream)
                finished = RunStatus((await store.get_run(ctx.tenant_id, run.id)).status).terminal

    return EventSourceResponse(stream(), ping=15)


@router.post("/runs/{run_id}/cancel", tags=["runs"], response_model=RunOut)
async def cancel_run(run_id: str, ctx: CtxDep) -> Any:
    run = await _run(ctx, run_id, "runs:cancel")
    await ctx.engine.executor.request_cancel(ctx.tenant_id, run.id)
    await ctx.audit("run.cancel_requested", run.id, namespace=run.namespace)
    return await ctx.engine.store.get_run(ctx.tenant_id, run.id)


@router.post("/runs/{run_id}/steps/{step_id}/approval", tags=["runs"], response_model=StepOut)
async def approve_step(run_id: str, step_id: str, body: ApprovalIn, ctx: CtxDep) -> Any:
    run = await _run(ctx, run_id, "runs:approve")
    row = await ctx.engine.executor.submit_approval(
        ctx.tenant_id, run.id, step_id, approved=body.approved, by=ctx.principal.display_name or ctx.actor, comment=body.comment,
        decider=ctx.principal,
    )
    await ctx.audit("run.approval", f"{run.id}/{step_id}", namespace=run.namespace, approved=body.approved, comment=body.comment)
    return row


@router.post("/runs/{run_id}/signals/{name}", status_code=202, tags=["runs"], response_model=SignalOut,
             summary="Send a signal to a run; a wait step with `signal: {name: ...}` takes it")
async def send_signal(run_id: str, name: str, body: SignalIn, ctx: CtxDep) -> Any:
    run = await _run(ctx, run_id, "runs:signal")
    row = await ctx.engine.signals.send(run, name, body.payload, source="api", sender=ctx.actor)
    await ctx.audit("run.signal", f"{run.id}/{name}", namespace=run.namespace, signal=row.id)
    return SignalOut.of(row)


@router.get("/runs/{run_id}/signals", tags=["runs"], response_model=list[SignalOut], summary="Signals sent to a run")
async def list_signals(run_id: str, ctx: CtxDep) -> Any:
    run = await _run(ctx, run_id)
    return [SignalOut.of(r) for r in await ctx.engine.signals.list(run.id)]


@router.post("/runs/{run_id}/rerun", status_code=201, tags=["runs"], response_model=RunOut)
async def rerun(run_id: str, ctx: CtxDep) -> Any:
    run = await _run(ctx, run_id, "runs:create")
    with use_namespace(run.namespace):
        # Same inputs and context ({{ event }}, {{ trigger }}, {{ target }}), so expressions resolve as they did.
        new = await ctx.engine.executor.start_run(
            ctx.tenant_id, run.workflow_name, run.inputs, version=run.workflow_version, started_by=ctx.actor,
            labels={**run.labels, "rerun_of": run.id}, initiator=await _initiator(ctx), admit=True,
            context={k: v for k, v in (run.context or {}).items() if k != "parent"},
        )
    await ctx.audit("run.started", new.id, namespace=run.namespace, workflow=run.workflow_name, rerun_of=run.id)
    return new


# --------------------------------------------------------------------------- campaign runs


async def _campaign(ctx: Ctx, campaign_id: str, permission: str = "campaigns:read") -> t.CampaignRow:
    row = await ctx.engine.store.get_campaign(ctx.tenant_id, campaign_id)
    await ctx.require(permission, row.namespace)
    return row


@router.post("/namespaces/{namespace}/campaigns/{name}/launch", status_code=201, tags=["campaign runs"], response_model=CampaignRunOut)
async def launch_campaign(namespace: str, name: str, body: CampaignLaunchIn, ctx: CtxDep) -> Any:
    await ctx.namespace(namespace, "campaigns:launch")
    with use_namespace(namespace):
        await ctx.engine.store.get_resource_row(ctx.tenant_id, "Campaign", name, namespace=namespace, fallback=False)
        row = await ctx.engine.campaigns.launch(
            ctx.tenant_id, name, name=body.name, started_by=ctx.actor, extra_inputs=body.inputs, start=body.start,
            initiator=await _initiator(ctx),
        )
    await ctx.audit("campaign.launched", row.id, namespace=namespace, campaign=name)
    return row


@router.post("/namespaces/{namespace}/campaign-runs", status_code=201, tags=["campaign runs"], response_model=CampaignRunOut)
async def launch_inline(namespace: str, body: CampaignInlineIn, ctx: CtxDep) -> Any:
    await ctx.namespace(namespace, "campaigns:launch")
    try:
        spec = CampaignSpec.model_validate(body.spec)
    except ValueError as exc:
        raise InvalidRequest(f"invalid campaign spec: {exc}") from exc
    with use_namespace(namespace):
        row = await ctx.engine.campaigns.launch(
            ctx.tenant_id, spec, name=body.name, started_by=ctx.actor, start=body.start, initiator=await _initiator(ctx)
        )
    await ctx.audit("campaign.launched", row.id, namespace=namespace, inline=True)
    return row


@router.get("/namespaces/{namespace}/campaign-runs", tags=["campaign runs"], response_model=list[CampaignRunOut])
async def list_campaign_runs(namespace: str, ctx: CtxDep, status: str | None = None) -> Any:
    await ctx.namespace(namespace, "campaigns:read")
    return await ctx.engine.store.list_campaigns(ctx.tenant_id, status, namespace=namespace)


@router.get("/campaign-runs/{campaign_id}", tags=["campaign runs"], response_model=CampaignRunOut)
async def get_campaign_run(campaign_id: str, ctx: CtxDep) -> Any:
    row = await _campaign(ctx, campaign_id)
    await ctx.engine.campaigns.refresh_stats(row.id)
    return await ctx.engine.store.get_campaign(ctx.tenant_id, row.id)


@router.get("/campaign-runs/{campaign_id}/targets", tags=["campaign runs"], response_model=list[TargetOut])
async def campaign_targets(campaign_id: str, ctx: CtxDep, status: str | None = None, offset: int = 0, limit: int = Query(500, le=5000)) -> Any:
    row = await _campaign(ctx, campaign_id)
    return await ctx.engine.store.list_targets(ctx.tenant_id, row.id, status=status, offset=offset, limit=limit)


@router.post("/campaign-runs/{campaign_id}/targets", tags=["campaign runs"], response_model=CampaignRunOut)
async def add_campaign_targets(campaign_id: str, body: AddTargetsIn, ctx: CtxDep) -> Any:
    row = await _campaign(ctx, campaign_id, "campaigns:manage")
    await ctx.engine.campaigns.add_targets(ctx.tenant_id, row.id, body.items)
    return await ctx.engine.store.get_campaign(ctx.tenant_id, row.id)


@router.post("/campaign-runs/{campaign_id}/{operation}", tags=["campaign runs"], response_model=CampaignRunOut)
async def campaign_operation(campaign_id: str, operation: str, ctx: CtxDep) -> Any:
    row = await _campaign(ctx, campaign_id, "campaigns:manage")
    service = ctx.engine.campaigns
    with use_namespace(row.namespace):
        if operation == "pause":
            await service.pause(ctx.tenant_id, row.id, reason=f"paused by {ctx.actor}")
        elif operation == "resume":
            await service.resume(ctx.tenant_id, row.id)
        elif operation == "cancel":
            await service.cancel(ctx.tenant_id, row.id)
        elif operation == "retry":
            await service.retry_failed(ctx.tenant_id, row.id)
        else:
            raise InvalidRequest("operation must be pause, resume, cancel or retry")
    await ctx.audit(f"campaign.{operation}", row.id, namespace=row.namespace)
    return await ctx.engine.store.get_campaign(ctx.tenant_id, row.id)


# --------------------------------------------------------------------------- stats


@router.get("/stats", tags=["stats"], summary="Run counts and spend for dashboards")
async def stats(ctx: CtxDep, namespace: str | None = None, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    if namespace:
        await ctx.namespace(namespace, "runs:read")
        allowed = [namespace]
    else:
        allowed = [ns for ns in await ctx.engine.authz.visible_namespaces(ctx.principal) if await ctx.engine.authz.allowed(ctx.principal, "runs:read", ns)]
    since = clock.now() - timedelta(days=days)
    by_status: dict[str, int] = {}
    workflows: list[dict[str, Any]] = []
    for ns in allowed:
        for status, count in (await ctx.engine.store.run_stats(ctx.tenant_id, since, namespace=ns)).items():
            by_status[status] = by_status.get(status, 0) + count
        workflows += await ctx.engine.store.run_usage(ctx.tenant_id, since, namespace=ns)
    active_campaigns = 0
    for ns in allowed:
        active_campaigns += len(await ctx.engine.store.list_campaigns(ctx.tenant_id, "running", namespace=ns))
    return {
        "since": since.isoformat(),
        "runs": by_status,
        "total_runs": sum(by_status.values()),
        "cost_usd": round(sum(w["cost_usd"] for w in workflows), 4),
        "workflows": sorted(workflows, key=lambda w: -w["runs"])[:20],
        "active_campaigns": active_campaigns,
    }
