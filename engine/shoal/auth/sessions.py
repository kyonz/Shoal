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
    from shoal.auth.accounts import Accounts
    from shoal.auth.directory import Directory
    from shoal.store import tables as t

ISSUER = "shoal"
AUDIENCE = "shoal-session"
SESSION_PREFIX = "shoals_"
REVOKED_ATTRIBUTE = "revoked_sessions"  # on the user: session id -> expiry, until the token would have expired anyway


def _key(settings: Settings) -> bytes:
    return hashlib.sha256(b"shoal-session:" + settings.secret_key.get_secret_value().encode()).digest()


def issue_session(
    settings: Settings, *, tenant_id: str | None, user_id: str | None, account_id: str | None = None,
    ttl: timedelta = timedelta(hours=12),
) -> str:
    """A session for one tenant (``tid``, as user ``sub``), held by an instance account (``acc``) when there is one.

    Instance admins can hold a session with no tenant, or for a tenant they are not a member of.
    """
    now = clock.now()
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        **{k: v for k, v in (("sub", user_id), ("tid", tenant_id), ("acc", account_id)) if v},
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


def is_revoked(holder: t.User | t.Account, claims: dict[str, Any]) -> bool:
    """Whether this session was signed out: recorded on the account when it has one, else on the user."""
    revoked = holder.revoked_sessions if hasattr(holder, "revoked_sessions") else (holder.attributes or {}).get(REVOKED_ATTRIBUTE)
    return claims.get("sid") in (revoked or {})


def _pruned(revoked: dict[str, Any] | None, claims: dict[str, Any]) -> dict[str, Any]:
    now = clock.now().timestamp()
    return {**{sid: exp for sid, exp in (revoked or {}).items() if exp > now}, claims["sid"]: claims["exp"]}


async def revoke_session(directory: Directory, user: t.User, claims: dict[str, Any]) -> None:
    """Signing out ends this session token for good, not just the cookie that carried it."""
    revoked = _pruned((user.attributes or {}).get(REVOKED_ATTRIBUTE), claims)
    await directory.update_user(user.tenant_id, user.id, attributes={**(user.attributes or {}), REVOKED_ATTRIBUTE: revoked})


async def revoke_account_session(accounts: Accounts, account: t.Account, claims: dict[str, Any]) -> None:
    """Sign out a session held by an instance account (including one with no tenant user)."""
    await accounts.update(account.id, revoked_sessions=_pruned(account.revoked_sessions, claims))
