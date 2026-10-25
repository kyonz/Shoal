"""Generic inbound webhook integration (any system that can POST JSON)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from shoal.integrations.base import ConnectionContext, EventDef, Integration, IntegrationEvent


class WebhookConnection(BaseModel):
    secret: SecretStr | None = Field(None, description="HMAC-SHA256 secret, or a shared token.")
    signature_header: str = Field("X-Signature-256", description="Header carrying the HMAC signature.")
    signature_prefix: str = "sha256="
    event_type_header: str = Field("X-Event-Type", description="Header naming the event type.")
    event_type_field: str | None = Field(None, description="Dot path into the JSON body for the event type.")
    id_field: str | None = Field(None, description="Dot path for a delivery id (dedupe).")


def _dig(data: Any, path: str | None) -> Any:
    if not path:
        return None
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


class WebhookIntegration(Integration):
    key = "webhook"
    title = "Generic webhook"
    description = "Receive JSON webhooks from any system, optionally HMAC-signed."
    connection_model = WebhookConnection
    events = [EventDef("received", "Any delivery (or the type named by the event type header/field).")]

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        cfg: WebhookConnection = ctx.config
        if cfg.secret is None:
            return True
        secret = cfg.secret.get_secret_value()
        token = headers.get("x-shoal-token")
        if token:
            return hmac.compare_digest(token, secret)
        signature = headers.get(cfg.signature_header.lower(), "")
        expected = cfg.signature_prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def parse_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> list[IntegrationEvent]:
        cfg: WebhookConnection = ctx.config
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {"raw": body.decode(errors="replace")}
        if not isinstance(payload, dict):
            payload = {"items": payload}
        event_type = headers.get(cfg.event_type_header.lower()) or _dig(payload, cfg.event_type_field) or "received"
        event_id = _dig(payload, cfg.id_field) or headers.get("x-request-id") or headers.get("x-delivery-id")
        wake = [k.strip() for k in headers.get("x-shoal-wake-key", "").split(",") if k.strip()]
        return [IntegrationEvent(type=str(event_type), id=event_id, data=payload, wake_keys=wake)]
