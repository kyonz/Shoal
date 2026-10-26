"""Publish workflows as Agent2Agent (A2A) agents.

Each workflow gets an Agent Card and a JSON-RPC endpoint:

* ``GET  /v1/namespaces/{ns}/a2a/{workflow}/.well-known/agent-card.json``
* ``POST /v1/namespaces/{ns}/a2a/{workflow}`` with ``message/send``,
  ``tasks/get`` or ``tasks/cancel``.

A task is a run. The first ``data`` part of the message becomes the run's
inputs; text parts fill the workflow's single string input (or one named
``prompt``/``message``/``task``). Waiting runs (approvals) report
``input-required``; outputs come back as a ``data`` artifact.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from shoal.api.deps import Ctx, CtxDep
from shoal.core.errors import NotFound, ShoalError
from shoal.core.models import Workflow
from shoal.core.scope import use_namespace
from shoal.store import tables as t

router = APIRouter(prefix="/v1/namespaces/{namespace}/a2a", tags=["a2a"])

STATES = {
    "pending": "submitted",
    "running": "working",
    "waiting": "input-required",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "canceled",
}
TEXT_INPUTS = ("prompt", "message", "task", "request")


async def _workflow(ctx: Ctx, namespace: str, name: str, permission: str) -> Workflow:
    await ctx.namespace(namespace, permission)
    row = await ctx.engine.store.get_resource_row(ctx.tenant_id, "Workflow", name, namespace=namespace, fallback=False)
    return Workflow.model_validate(row.document)


def _endpoint(ctx: Ctx, namespace: str, name: str) -> str:
    return f"{ctx.engine.settings.public_url.rstrip('/')}/v1/namespaces/{namespace}/a2a/{name}"


@router.get("/{workflow}/.well-known/agent-card.json", summary="A2A Agent Card for a workflow")
async def agent_card(namespace: str, workflow: str, ctx: CtxDep) -> dict[str, Any]:
    wf = await _workflow(ctx, namespace, workflow, "workflows:read")
    spec = wf.spec
    return {
        "protocolVersion": "0.3.0",
        "name": wf.metadata.name,
        "description": wf.metadata.description or spec.description or f"Shoal workflow {wf.metadata.name}",
        "url": _endpoint(ctx, namespace, workflow),
        "preferredTransport": "JSONRPC",
        "version": "1",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "description": "Shoal API key"}},
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": wf.metadata.name,
                "name": wf.metadata.name,
                "description": spec.description or "Run this workflow",
                "tags": sorted(wf.metadata.labels.values()) or ["workflow"],
                "inputSchema": {
                    "type": "object",
                    "properties": {k: v.json_schema() for k, v in spec.inputs.items()},
                    "required": [k for k, v in spec.inputs.items() if v.is_required],
                },
            }
        ],
    }


def _inputs(wf: Workflow, message: dict[str, Any]) -> dict[str, Any]:
    parts = message.get("parts") or []
    inputs: dict[str, Any] = {}
    for part in parts:
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            inputs = dict(part["data"])
            break
    text = "\n".join(p.get("text", "") for p in parts if p.get("kind") == "text" and p.get("text"))
    if text:
        strings = [k for k, v in wf.spec.inputs.items() if v.type in ("string", "any")]
        target = next((k for k in TEXT_INPUTS if k in strings), strings[0] if len(strings) == 1 else None)
        if target and target not in inputs:
            inputs[target] = text
    return inputs


def _task(run: t.Run, context_id: str | None = None) -> dict[str, Any]:
    state = STATES.get(run.status, "unknown")
    task: dict[str, Any] = {
        "kind": "task",
        "id": run.id,
        "contextId": context_id or (run.labels or {}).get("a2a_context") or run.id,
        "status": {"state": state, "timestamp": (run.finished_at or run.created_at).isoformat()},
        "metadata": {"shoal_run_id": run.id, "workflow": run.workflow_name, "namespace": run.namespace},
    }
    if state == "failed" and run.error:
        task["status"]["message"] = {
            "kind": "message", "role": "agent", "messageId": f"{run.id}-error",
            "parts": [{"kind": "text", "text": str(run.error.get("message", "run failed"))}],
        }
    if state == "input-required":
        task["status"]["message"] = {
            "kind": "message", "role": "agent", "messageId": f"{run.id}-waiting",
            "parts": [{"kind": "text", "text": "Waiting for an approval in Shoal."}],
        }
    if run.outputs:
        task["artifacts"] = [{"artifactId": f"{run.id}-outputs", "name": "outputs", "parts": [{"kind": "data", "data": run.outputs}]}]
    return task


def _error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


@router.post("/{workflow}", summary="A2A JSON-RPC endpoint (message/send, tasks/get, tasks/cancel)")
async def rpc(namespace: str, workflow: str, request: Request, ctx: CtxDep) -> Any:
    try:
        body = await request.json()
    except ValueError:
        return _error(None, -32700, "parse error")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return _error(body.get("id") if isinstance(body, dict) else None, -32600, "invalid request")
    rpc_id, method, params = body.get("id"), body["method"], body.get("params") or {}
    try:
        if method == "message/send":
            wf = await _workflow(ctx, namespace, workflow, "runs:create")
            message = params.get("message") or {}
            if message.get("taskId"):
                return _error(rpc_id, -32004, "continuing an existing task is not supported; approve it in Shoal")
            initiator = await ctx.engine.identities.resolve(
                ctx.tenant_id, actor=ctx.actor, source="a2a", email=ctx.principal.email, display_name=ctx.principal.display_name
            )
            labels = {"source": "a2a"}
            if message.get("contextId"):
                labels["a2a_context"] = str(message["contextId"])[:200]
            with use_namespace(namespace):
                try:
                    run = await ctx.engine.executor.start_run(
                        ctx.tenant_id, wf.metadata.name, _inputs(wf, message), started_by=ctx.actor, labels=labels,
                        initiator=initiator, admit=True,
                    )
                except ValueError as exc:
                    return _error(rpc_id, -32602, str(exc))
            await ctx.audit("a2a.message", run.id, workflow=workflow)
            result = _task(run, message.get("contextId"))
        elif method in ("tasks/get", "tasks/cancel"):
            await _workflow(ctx, namespace, workflow, "runs:read")
            run = await ctx.engine.store.get_run(ctx.tenant_id, str(params.get("id", "")))
            if run.workflow_name != workflow or run.namespace != namespace:
                raise NotFound("task not found")
            if method == "tasks/cancel":
                await ctx.require("runs:cancel", namespace)
                if run.status in ("succeeded", "failed", "cancelled"):
                    return _error(rpc_id, -32002, "task cannot be canceled")
                await ctx.engine.executor.request_cancel(ctx.tenant_id, run.id)
                run = await ctx.engine.store.get_run(ctx.tenant_id, run.id)
            result = _task(run)
        else:
            return _error(rpc_id, -32601, f"method '{method}' not found")
    except NotFound as exc:
        return _error(rpc_id, -32001, exc.message)
    except ShoalError as exc:
        if exc.status_code in (401, 403):
            raise
        return _error(rpc_id, -32603, exc.message)
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
