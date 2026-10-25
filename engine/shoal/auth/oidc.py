"""OpenID Connect sign-in (authorization code flow with PKCE).

Works with any standards-compliant provider (Okta, Microsoft Entra ID, Google,
Keycloak, Auth0, Dex). ID tokens are verified against the provider's JWKS
(signature, issuer, audience, expiry, nonce). Users are matched by
``(issuer, sub)``, then by verified email, and otherwise provisioned when the
provider allows it. A ``groups`` claim keeps OIDC-sourced group memberships in
sync, so role bindings on those groups apply at sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import delete, select

from shoal.auth.sessions import issue_session
from shoal.core import clock
from shoal.core.errors import Forbidden, InvalidRequest, Unauthorized
from shoal.secrets.providers import Cipher
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

LOGIN_STATE_TTL = timedelta(minutes=10)
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OIDCService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._discovery: dict[str, tuple[dict[str, Any], float]] = {}
        self._jwks: dict[str, tuple[dict[str, Any], float]] = {}

    @property
    def cipher(self) -> Cipher:
        return Cipher(self.engine.settings.secret_key.get_secret_value())

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self.engine.http_transport)

    async def discovery(self, issuer: str) -> dict[str, Any]:
        cached = self._discovery.get(issuer)
        if cached and cached[1] > time.time():
            return cached[0]
        async with self._client() as client:
            response = await client.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
        response.raise_for_status()
        config = response.json()
        self._discovery[issuer] = (config, time.time() + 3600)
        return config

    async def _jwk_set(self, jwks_uri: str, *, refresh: bool = False) -> dict[str, Any]:
        cached = self._jwks.get(jwks_uri)
        if cached and cached[1] > time.time() and not refresh:
            return cached[0]
        async with self._client() as client:
            response = await client.get(jwks_uri)
        response.raise_for_status()
        self._jwks[jwks_uri] = (response.json(), time.time() + 3600)
        return self._jwks[jwks_uri][0]

    async def begin(self, tenant_id: str, provider_name: str, redirect_uri: str, redirect_to: str | None = None) -> str:
        """Start sign-in; returns the provider's authorization URL."""
        provider = await self.engine.directory.get_oidc_provider(tenant_id, provider_name)
        if not provider.enabled:
            raise Forbidden(f"sign-in with '{provider_name}' is disabled")
        config = await self.discovery(provider.issuer)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        async with self.engine.store.session() as s:
            s.add(t.LoginState(id=state, tenant_id=tenant_id, provider=provider_name, nonce=nonce, code_verifier=verifier, redirect_to=redirect_to))
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(provider.scopes or ["openid", "email", "profile"]),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{config['authorization_endpoint']}?{urlencode(params)}"

    async def complete(self, state: str, code: str, redirect_uri: str) -> dict[str, Any]:
        """Finish sign-in: exchange the code, verify the ID token, find or provision the user."""
        async with self.engine.store.session() as s:
            login = await s.get(t.LoginState, state)
            if login is not None:
                await s.execute(delete(t.LoginState).where(t.LoginState.id == state))
            await s.execute(delete(t.LoginState).where(t.LoginState.created_at < clock.now() - LOGIN_STATE_TTL))
        if login is None or clock.now() - login.created_at > LOGIN_STATE_TTL:
            raise Unauthorized("sign-in expired or was already used; start again")
        provider = await self.engine.directory.get_oidc_provider(login.tenant_id, login.provider)
        config = await self.discovery(provider.issuer)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": provider.client_id,
            "code_verifier": login.code_verifier,
        }
        if provider.client_secret:
            form["client_secret"] = self.cipher.decrypt(provider.client_secret)
        async with self._client() as client:
            response = await client.post(config["token_endpoint"], data=form, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise Unauthorized(f"token exchange failed: {response.text[:300]}")
        id_token = response.json().get("id_token")
        if not id_token:
            raise Unauthorized("provider returned no id_token")
        claims = await self.verify_id_token(provider, config, id_token, login.nonce)
        user = await self._user_for(provider, claims)
        token = issue_session(self.engine.settings, tenant_id=login.tenant_id, user_id=user.id)
        await self.engine.store.audit(login.tenant_id, f"user:{user.username}", "auth.login", provider.name, user_id=user.id)
        return {"session": token, "user_id": user.id, "redirect_to": login.redirect_to}

    async def verify_id_token(self, provider: t.OIDCProvider, config: dict[str, Any], id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as exc:
            raise Unauthorized("malformed id_token") from exc
        if header.get("alg") not in ALGORITHMS:
            raise Unauthorized(f"id_token algorithm '{header.get('alg')}' is not allowed")
        key = None
        for refresh in (False, True):  # refresh once on unknown kid (key rotation)
            jwks = await self._jwk_set(config["jwks_uri"], refresh=refresh)
            for jwk in jwks.get("keys", []):
                if header.get("kid") in (None, jwk.get("kid")):
                    key = jwt.PyJWK.from_dict(jwk)
                    break
            if key is not None:
                break
        if key is None:
            raise Unauthorized("id_token signing key not found in the provider's JWKS")
        try:
            claims = jwt.decode(
                id_token, key, algorithms=[header["alg"]], audience=provider.client_id, issuer=config.get("issuer", provider.issuer), leeway=60
            )
        except jwt.InvalidTokenError as exc:
            raise Unauthorized(f"invalid id_token: {exc}") from exc
        if claims.get("nonce") != nonce:
            raise Unauthorized("id_token nonce mismatch")
        return claims

    async def _user_for(self, provider: t.OIDCProvider, claims: dict[str, Any]) -> t.User:
        directory = self.engine.directory
        tenant_id = provider.tenant_id
        email = claims.get("email")
        verified_email = email if claims.get("email_verified", True) else None
        if provider.allowed_domains and (not email or email.rsplit("@", 1)[-1].lower() not in {d.lower() for d in provider.allowed_domains}):
            raise Forbidden("your email domain is not allowed to sign in to this tenant")
        user = await directory.find_user(tenant_id, oidc_issuer=provider.issuer, oidc_subject=claims["sub"])
        if user is None and verified_email:
            user = await directory.find_user(tenant_id, email=verified_email)
            if user is not None:
                await directory.update_user(tenant_id, user.id, oidc_issuer=provider.issuer, oidc_subject=claims["sub"])
        created = False
        if user is None:
            if not provider.auto_provision:
                raise Forbidden("no account exists for you in this tenant; ask an admin to invite you")
            user = await directory.create_user(
                tenant_id,
                claims.get("preferred_username") or email or claims["sub"],
                email=email,
                display_name=claims.get("name") or claims.get("preferred_username") or email,
                oidc_issuer=provider.issuer,
                oidc_subject=claims["sub"],
            )
            created = True
        if not user.active:
            raise Forbidden("your account is deactivated")
        user = await directory.update_user(tenant_id, user.id, last_login_at=clock.now())
        if provider.groups_claim:
            await self._sync_groups(tenant_id, user.id, claims.get(provider.groups_claim) or [])
        if created and provider.default_role:
            await directory.bind(tenant_id, provider.default_role, "user", user.id, namespace=provider.default_namespace, actor="oidc")
        return user

    async def _sync_groups(self, tenant_id: str, user_id: str, names: list[str]) -> None:
        directory = self.engine.directory
        wanted: set[str] = set()
        for name in names if isinstance(names, list) else [names]:
            group = await directory.find_group(tenant_id, str(name)) or await directory.create_group(tenant_id, str(name), source="oidc")
            wanted.add(group.id)
        async with self.engine.store.session() as s:
            q = select(t.GroupMember.group_id, t.Group.source).join(t.Group, t.Group.id == t.GroupMember.group_id).where(t.GroupMember.user_id == user_id)
            current = {gid: source for gid, source in (await s.execute(q)).all()}
        for gid in wanted - set(current):
            await directory.add_group_members(tenant_id, gid, [user_id])
        for gid, source in current.items():
            if source == "oidc" and gid not in wanted:
                await directory.remove_group_members(tenant_id, gid, [user_id])

    async def configure(self, tenant_id: str, name: str, *, client_secret: str | None = None, **values: Any) -> t.OIDCProvider:
        if "issuer" in values:
            values["issuer"] = values["issuer"].rstrip("/")
        if client_secret is not None:
            values["client_secret"] = self.cipher.encrypt(client_secret) if client_secret else None
        if values.get("default_role"):
            await self.engine.directory.get_role(tenant_id, values["default_role"])
        if not values.get("issuer") and not await self._exists(tenant_id, name):
            raise InvalidRequest("issuer is required")
        return await self.engine.directory.put_oidc_provider(tenant_id, name, **values)

    async def _exists(self, tenant_id: str, name: str) -> bool:
        return any(p.name == name for p in await self.engine.directory.list_oidc_providers(tenant_id))
