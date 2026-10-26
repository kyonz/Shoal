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
    allow_unsigned: bool = Field(
        False,
        description="Accept deliveries that carry no signature or token (anyone who has the URL can fire triggers). "
        "With a secret set, deliveries that do carry one are still checked.",
    )
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
        token = headers.get("x-shoal-token")
        signature = headers.get(cfg.signature_header.lower())
        if cfg.secret is None or (not token and not signature):
            return cfg.allow_unsigned
        secret = cfg.secret.get_secret_value()
        if token:
            return hmac.compare_digest(token, secret)
        expected = cfg.signature_prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature or "", expected)

    def webhook_setup_instructions(self, url: str) -> str:
        return (
            f"POST JSON to {url}. Sign each delivery with the connection's secret (an HMAC-SHA256 of the raw body), "
            "or send the secret itself in an X-Shoal-Token header (or a ?token= query parameter). "
            "Without a secret, deliveries are rejected unless 'Allow unsigned' is on."
        )

    def webhook_setup_guide(self, url: str, config: Any, secrets_set: set[str]) -> dict[str, Any] | None:
        cfg = config if isinstance(config, WebhookConnection) else WebhookConnection()
        header, prefix = cfg.signature_header, cfg.signature_prefix
        signed = "secret" in secrets_set
        body = "BODY='{\"message\": \"hello from curl\"}'"
        examples = [
            {
                "title": "Signed (HMAC-SHA256)",
                "description": f"Signs the exact body with the secret and sends it in {header}.",
                "command": (
                    "SHOAL_WEBHOOK_SECRET='<the connection secret>'\n"
                    f"{body}\n"
                    f"SIG=\"{prefix}$(printf '%s' \"$BODY\" | openssl dgst -sha256 -hmac \"$SHOAL_WEBHOOK_SECRET\" | awk '{{print $NF}}')\"\n"
                    f"curl -sS -X POST '{url}' \\\n"
                    "  -H 'Content-Type: application/json' \\\n"
                    f"  -H \"{header}: $SIG\" \\\n"
                    "  --data-binary \"$BODY\""
                ),
            },
            {
                "title": "Shared token",
                "description": "For senders that cannot compute a signature: send the secret itself.",
                "command": (
                    "SHOAL_WEBHOOK_SECRET='<the connection secret>'\n"
                    f"curl -sS -X POST '{url}' \\\n"
                    "  -H 'Content-Type: application/json' \\\n"
                    "  -H \"X-Shoal-Token: $SHOAL_WEBHOOK_SECRET\" \\\n"
                    "  --data-binary '{\"message\": \"hello from curl\"}'"
                ),
            },
        ]
        if cfg.allow_unsigned:
            examples.append({
                "title": "Unsigned",
                "description": "Accepted because 'Allow unsigned' is on.",
                "command": (
                    f"curl -sS -X POST '{url}' \\\n"
                    "  -H 'Content-Type: application/json' \\\n"
                    "  --data-binary '{\"message\": \"hello from curl\"}'"
                ),
            })
        if signed and cfg.allow_unsigned:
            accepts = "Signed, token and unsigned deliveries. A delivery that carries a wrong signature or token is rejected."
        elif signed:
            accepts = "Signed or token deliveries only; unsigned ones are rejected."
        elif cfg.allow_unsigned:
            accepts = "Any delivery: no secret is set, so nothing is checked."
        else:
            accepts = "Nothing yet: set a webhook secret, or turn on 'Allow unsigned'."
        event = (f"the JSON field '{cfg.event_type_field}'" if cfg.event_type_field else None)
        return {
            "method": "POST",
            "content_type": "application/json",
            "signature_header": header,
            "signature": f"{prefix}<hex HMAC-SHA256 of the raw request body, keyed with the secret>",
            "token_header": "X-Shoal-Token",
            "secret_set": signed,
            "allow_unsigned": cfg.allow_unsigned,
            "accepts": accepts,
            "event_type": (
                f"Triggers see the event type from the {cfg.event_type_header} header"
                + (f", else {event}" if event else "")
                + ", else 'received'."
            ),
            "examples": examples,
        }

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
