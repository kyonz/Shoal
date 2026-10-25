"""Short-lived UI session tokens (HS256 JWTs) issued after OIDC sign-in."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import jwt

from shoal.config import Settings
from shoal.core import clock
from shoal.core.errors import Unauthorized

if TYPE_CHECKING:
    from shoal.auth.directory import Directory
    from shoal.store import tables as t

ISSUER = "shoal"
AUDIENCE = "shoal-session"
SESSION_PREFIX = "shoals_"
REVOKED_ATTRIBUTE = "revoked_sessions"  # on the user: session id -> expiry, until the token would have expired anyway


def _key(settings: Settings) -> bytes:
    return hashlib.sha256(b"shoal-session:" + settings.secret_key.get_secret_value().encode()).digest()


def issue_session(settings: Settings, *, tenant_id: str, user_id: str, ttl: timedelta = timedelta(hours=12)) -> str:
    now = clock.now()
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": user_id,
        "tid": tenant_id,
        "sid": secrets.token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return SESSION_PREFIX + jwt.encode(claims, _key(settings), algorithm="HS256")


def verify_session(settings: Settings, token: str) -> dict[str, Any]:
    if not token.startswith(SESSION_PREFIX):
        raise Unauthorized("not a session token")
    try:
        return jwt.decode(
            token[len(SESSION_PREFIX) :],
            _key(settings),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"verify_iat": False},
            leeway=5,
        ) | {}
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("invalid session token") from exc


def is_revoked(user: t.User, claims: dict[str, Any]) -> bool:
    return claims.get("sid") in (user.attributes or {}).get(REVOKED_ATTRIBUTE, {})


async def revoke_session(directory: Directory, user: t.User, claims: dict[str, Any]) -> None:
    """Signing out ends this session token for good, not just the cookie that carried it."""
    now = clock.now().timestamp()
    revoked = {sid: exp for sid, exp in (user.attributes or {}).get(REVOKED_ATTRIBUTE, {}).items() if exp > now}
    revoked[claims["sid"]] = claims["exp"]
    await directory.update_user(user.tenant_id, user.id, attributes={**(user.attributes or {}), REVOKED_ATTRIBUTE: revoked})
