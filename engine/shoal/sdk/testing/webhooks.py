"""Build signed webhook deliveries for tests."""

from __future__ import annotations

import json
from typing import Any

from shoal.sdk.helpers import hmac_sha256


def webhook_body(payload: dict[str, Any] | list[Any] | bytes | str) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode()
    return json.dumps(payload, separators=(",", ":")).encode()


def signed_webhook(
    secret: str,
    payload: dict[str, Any] | list[Any] | bytes | str,
    *,
    header: str = "X-Signature-256",
    prefix: str = "sha256=",
    encoding: str = "hex",
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, str], bytes]:
    """``(headers, body)`` for an HMAC-SHA256 signed delivery (GitHub style by default)."""
    body = webhook_body(payload)
    return {**(headers or {}), header: hmac_sha256(secret, body, prefix=prefix, encoding=encoding)}, body


def github_webhook(secret: str, event: str, payload: dict[str, Any], *, delivery: str = "delivery-1") -> tuple[dict[str, str], bytes]:
    return signed_webhook(
        secret, payload, header="X-Hub-Signature-256", headers={"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    )
