"""Inbound webhooks: one unguessable URL per connection, verified by the integration."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from shoal.api.deps import EngineDep

router = APIRouter(prefix="/v1/hooks", tags=["webhooks"])

MAX_BODY = 5 * 1024 * 1024


@router.post("/{webhook_id}", status_code=202, summary="Receive a webhook for a connection")
async def receive(webhook_id: str, request: Request, engine: EngineDep, token: str | None = None) -> Any:
    body = await request.body()
    if len(body) > MAX_BODY:
        from shoal.core.errors import InvalidRequest

        raise InvalidRequest("payload too large")
    headers = dict(request.headers)
    if token and "x-shoal-token" not in headers:
        headers["x-shoal-token"] = token  # for senders that cannot set headers
    if engine.settings.webhook_processing == "queue":
        result = await engine.integrations.accept_webhook(webhook_id, headers, body)
    else:
        await engine.quotas.check_webhook(webhook_id)
        result = await engine.integrations.handle_webhook(webhook_id, headers, body)
    reply = result.pop("reply", None)
    if reply is None:
        return result
    if reply.body is None:
        return Response(status_code=reply.status)
    if isinstance(reply.body, str):
        return Response(reply.body, status_code=reply.status, media_type="text/plain")
    return JSONResponse(reply.body, status_code=reply.status)


@router.post("/runs/{run_id}/{token}/signals/{name}", status_code=202, summary="Signal a run through its callback URL")
async def run_callback(run_id: str, token: str, name: str, request: Request, engine: EngineDep) -> dict[str, Any]:
    """For systems that can only call a URL: ``{{ run.signal_url }}/<name>``. A JSON body is the payload; any other body is sent as text."""
    from shoal.core.errors import InvalidRequest, NotFound
    from shoal.runtime.signals import MAX_PAYLOAD

    try:
        run = await engine.store.get_run(None, run_id)
    except NotFound:
        run = None
    if run is None or not engine.signals.verify(run, token):
        raise NotFound("not found")  # same answer for an unknown run and a wrong token
    body = await request.body()
    if len(body) > MAX_PAYLOAD:
        raise InvalidRequest("payload too large")
    try:
        payload: Any = json.loads(body) if body else None
    except ValueError:
        payload = body.decode("utf-8", "replace")
    row = await engine.signals.send(run, name, payload, source="callback", sender=request.headers.get("user-agent", ""))
    await engine.store.audit(run.tenant_id, "callback", "run.signal", f"{run.id}/{name}", namespace=run.namespace, signal=row.id)
    return {"signal": row.id}
