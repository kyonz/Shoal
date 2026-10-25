"""Small helpers most integrations need: webhook signatures, cursors and identity hints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from pydantic import SecretStr


def _secret(secret: str | bytes | SecretStr) -> bytes:
    if isinstance(secret, SecretStr):
        secret = secret.get_secret_value()
    return secret if isinstance(secret, bytes) else secret.encode()


def hmac_sha256(secret: str | bytes | SecretStr, body: bytes, *, prefix: str = "", encoding: str = "hex") -> str:
    """``prefix + HMAC-SHA256(secret, body)`` as hex (GitHub, generic) or base64 (Shopify, Slack-style)."""
    digest = hmac.new(_secret(secret), body, hashlib.sha256).digest()
    text = base64.b64encode(digest).decode() if encoding == "base64" else digest.hex()
    return prefix + text


def verify_hmac_sha256(
    secret: str | bytes | SecretStr | None,
    body: bytes,
    signature: str | None,
    *,
    prefix: str = "",
    encoding: str = "hex",
) -> bool:
    """Constant-time check of an HMAC-SHA256 webhook signature. A missing secret or signature fails."""
    if not secret or not signature:
        return False
    return hmac.compare_digest(signature.strip(), hmac_sha256(secret, body, prefix=prefix, encoding=encoding))


def verify_token(expected: str | SecretStr | None, presented: str | None) -> bool:
    """Constant-time comparison for shared-token webhooks."""
    if isinstance(expected, SecretStr):
        expected = expected.get_secret_value()
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode(), presented.encode())


def encode_cursor(state: dict[str, Any]) -> str:
    """Opaque pagination cursor from any JSON state (page number, offset, provider token...)."""
    return base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":"), sort_keys=True).encode()).decode()


def decode_cursor(cursor: str | None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not cursor:
        return dict(default or {})
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid cursor")
    return value


def json_body(body: bytes) -> dict[str, Any]:
    """Parse a webhook body; non-object payloads are wrapped so ``data`` is always a dict."""
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return {"raw": body.decode(errors="replace")}
    return payload if isinstance(payload, dict) else {"items": payload}


def actor(
    *, email: str | None = None, display_name: str | None = None, **provider_ids: str | None
) -> dict[str, Any]:
    """Identity hints for ``IntegrationEvent.actor`` (``actor(email=..., linear="user-id")``)."""
    out = {k: v for k, v in provider_ids.items() if v}
    if email:
        out["email"] = email
    if display_name:
        out["display_name"] = display_name
    return out
