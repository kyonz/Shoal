"""Inbound webhooks: one unguessable URL per connection, verified by the integration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from awm.api.deps import EngineDep

router = APIRouter(prefix="/v1/hooks", tags=["webhooks"])

MAX_BODY = 5 * 1024 * 1024


@router.post("/{webhook_id}", status_code=202, summary="Receive a webhook for a connection")
async def receive(webhook_id: str, request: Request, engine: EngineDep, token: str | None = None) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_BODY:
        from awm.core.errors import InvalidRequest

        raise InvalidRequest("payload too large")
    headers = dict(request.headers)
    if token and "x-awm-token" not in headers:
        headers["x-awm-token"] = token  # for senders that cannot set headers
    return await engine.integrations.handle_webhook(webhook_id, headers, body)
