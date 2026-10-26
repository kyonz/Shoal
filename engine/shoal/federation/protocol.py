"""Wire format shared by hub and member: link credentials, hub assertions and modes.

The tenant connector authenticates to the hub with a link credential
(``fedlink_<link id>.<secret>``). Every request the hub forwards carries an
assertion: a compact HS256 token signed with the link secret that names the
person, their hub groups, the hub request id, and the method and path it was
issued for, valid for one minute. The member checks it and then applies its
own RBAC.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any

from shoal.core.errors import Unauthorized

PROTOCOL_VERSION = 1
MODES = ("read", "read_write")
VISIBILITIES = ("full", "metadata")
CREDENTIAL_PREFIX = "fedlink_"
ENROLLMENT_PREFIX = "fedjoin_"
ASSERTION_HEADER = "x-shoal-hub-assertion"
ASSERTION_TTL = 60
READ_METHODS = ("GET", "HEAD")
# Read-only calls that use POST because they take a body: connection resource queries.
READ_POST = re.compile(r"^/v1/namespaces/[^/]+/connections/[^/]+/resources/[^/]+/query$")


def effective_mode(requested: str, ceiling: str | None) -> str:
    """The hub can only ask for less than the member allows."""
    if ceiling is None:
        return "read"
    return "read_write" if requested == "read_write" and ceiling == "read_write" else "read"


def is_read(method: str, path: str) -> bool:
    if method.upper() in READ_METHODS:
        return True
    return method.upper() == "POST" and bool(READ_POST.match(path))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def credential(link_id: str, secret: str) -> str:
    return f"{CREDENTIAL_PREFIX}{link_id}.{secret}"


def parse_credential(token: str) -> tuple[str, str]:
    if not token.startswith(CREDENTIAL_PREFIX) or "." not in token:
        raise Unauthorized("not a federation link credential")
    link_id, secret = token[len(CREDENTIAL_PREFIX):].split(".", 1)
    return link_id, secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_assertion(secret: str, claims: dict[str, Any], *, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    body = {**claims, "iat": issued, "exp": issued + ASSERTION_TTL, "v": PROTOCOL_VERSION}
    header = _b64(json.dumps({"alg": "HS256", "typ": "shoal-hub"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def verify_assertion(secret: str, token: str, *, method: str, path: str, now: float | None = None) -> dict[str, Any]:
    try:
        header, payload, signature = token.split(".")
    except ValueError as exc:
        raise Unauthorized("malformed hub assertion") from exc
    expected = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(signature)):
        raise Unauthorized("hub assertion signature does not match")
    claims = json.loads(_unb64(payload))
    current = now if now is not None else time.time()
    if claims.get("exp", 0) < current:
        raise Unauthorized("hub assertion expired")
    if claims.get("method") != method.upper() or claims.get("path") != path:
        raise Unauthorized("hub assertion was issued for a different request")
    return claims
