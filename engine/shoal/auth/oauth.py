"""OAuth 2.0 authorization code flow (with PKCE) for per-user connection credentials.

A connection with ``oauth:`` lets each person connect their own account: the API
hands out an authorize URL, the provider redirects to ``/v1/oauth/callback``, and
the tokens are stored as that person's credential. Access tokens are refreshed
when they are about to expire. The flow state is encrypted and expires, so the
callback needs no session.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

from shoal.core import clock
from shoal.core.errors import InvalidRequest, MissingConfiguration, Unauthorized
from shoal.core.models import OAuthSpec
from shoal.core.scope import use_namespace

if TYPE_CHECKING:
    from shoal.engine import Engine

STATE_TTL = timedelta(minutes=15)
REFRESH_MARGIN = timedelta(seconds=60)


class OAuthFlows:
    def __init__(self, engine: Engine):
        self.engine = engine

    @property
    def redirect_uri(self) -> str:
        return f"{self.engine.settings.public_url.rstrip('/')}/v1/oauth/callback"

    def start(self, tenant_id: str, owner: str, namespace: str, connection: str, oauth: OAuthSpec) -> str:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = self.engine.secrets.cipher.encrypt(json.dumps({
            "t": tenant_id, "o": owner, "n": namespace, "c": connection, "v": verifier,
            "exp": (clock.now() + STATE_TTL).timestamp(),
        }))
        params = {
            "response_type": "code", "client_id": oauth.client_id, "redirect_uri": self.redirect_uri,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", **oauth.extra_params,
        }
        if oauth.scopes:
            params["scope"] = " ".join(oauth.scopes)
        separator = "&" if "?" in oauth.authorize_url else "?"
        return f"{oauth.authorize_url}{separator}{urlencode(params)}"

    async def callback(self, code: str, state: str) -> tuple[str, str, str, str]:
        """Exchange the code and store the tokens; returns (tenant, owner, namespace, connection)."""
        try:
            data = json.loads(self.engine.secrets.cipher.decrypt(state))
        except Exception as exc:
            raise Unauthorized("invalid OAuth state") from exc
        if clock.now().timestamp() > data["exp"]:
            raise Unauthorized("the OAuth sign-in took too long; start again")
        tenant_id, owner, namespace, connection = data["t"], data["o"], data["n"], data["c"]
        oauth = await self._spec(tenant_id, namespace, connection)
        tokens = await self._token(tenant_id, namespace, oauth, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri, "code_verifier": data["v"],
        })
        await self._save(tenant_id, owner, namespace, connection, oauth, tokens, previous=None)
        return tenant_id, owner, namespace, connection

    async def fresh(
        self, tenant_id: str, owner: str, namespace: str, connection: str, spec: Any, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Values with a valid access token, refreshing it first when it is about to expire."""
        meta = values.get("_oauth") or {}
        expires = meta.get("expires_at")
        if not expires or clock.now().timestamp() < expires - REFRESH_MARGIN.total_seconds() or not meta.get("refresh_token"):
            return values
        tokens = await self._token(tenant_id, namespace, spec.oauth, {"grant_type": "refresh_token", "refresh_token": meta["refresh_token"]})
        return await self._save(tenant_id, owner, namespace, connection, spec.oauth, tokens, previous=meta)

    async def _spec(self, tenant_id: str, namespace: str, connection: str) -> OAuthSpec:
        resource = await self.engine.store.find_resource(tenant_id, "Connection", connection, namespace=namespace)
        oauth = getattr(getattr(resource, "spec", None), "oauth", None)
        if oauth is None:
            raise MissingConfiguration(
                f"connection '{connection}' has no OAuth settings", kind="Connection", name=connection, namespace=namespace,
                exists=resource is not None, field="oauth",
            )
        return oauth

    async def _token(self, tenant_id: str, namespace: str, oauth: OAuthSpec, form: dict[str, str]) -> dict[str, Any]:
        form = {**form, "client_id": oauth.client_id}
        if oauth.client_secret_secret:
            with use_namespace(namespace):
                secret = await self.engine.secrets.get(tenant_id, oauth.client_secret_secret)
            if secret is None:
                raise MissingConfiguration(
                    f"secret '{oauth.client_secret_secret}' (OAuth client secret) is not set",
                    kind="Secret", name=oauth.client_secret_secret, namespace=namespace,
                )
            form["client_secret"] = secret
        kwargs: dict[str, Any] = {"timeout": 30.0}
        if self.engine.http_transport is not None:
            kwargs["transport"] = self.engine.http_transport
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.post(oauth.token_url, data=form, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise InvalidRequest(f"the OAuth provider refused the token request (HTTP {response.status_code})")
        tokens = response.json()
        if "access_token" not in tokens:
            raise InvalidRequest("the OAuth provider returned no access token")
        return tokens

    async def _save(
        self, tenant_id: str, owner: str, namespace: str, connection: str, oauth: OAuthSpec, tokens: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        expires_at = None
        if tokens.get("expires_in"):
            expires_at = clock.now() + timedelta(seconds=float(tokens["expires_in"]))
        refresh = tokens.get("refresh_token") or (previous or {}).get("refresh_token")
        values = {
            oauth.token_field: tokens["access_token"],
            "_oauth": {"refresh_token": refresh, "expires_at": expires_at.timestamp() if expires_at else None,
                       "scope": tokens.get("scope")},
        }
        await self.engine.user_credentials.put(tenant_id, owner, namespace, connection, values, kind="oauth", expires_at=expires_at)
        return values
