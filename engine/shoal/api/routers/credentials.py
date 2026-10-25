"""Per-user connection credentials (architecture §11.4).

People manage their own credentials under ``/v1/me/credentials``: paste a token,
or connect with OAuth when the connection has ``oauth:``. Values are write-only.
Admins can see who has connected what and revoke it, never the values.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from shoal.api.deps import SESSION_COOKIE, Ctx, CtxDep, EngineDep
from shoal.auth.oauth import BINDING_COOKIE, STATE_TTL, OAuthFlows
from shoal.auth.sessions import verify_session
from shoal.core.errors import Forbidden, InvalidRequest, NotFound, ShoalError
from shoal.core.models import Connection
from shoal.store import tables as t

router = APIRouter(tags=["credentials"])


class CredentialIn(BaseModel):
    values: dict[str, str] = Field(description="Connection field -> value, for the connection's user fields.")


class CredentialOut(BaseModel):
    owner: str
    namespace: str
    connection: str
    kind: str = Field(description="token or oauth.")
    fields: list[str] = Field(description="Fields held (values are never returned).")
    expires_at: datetime | None
    updated_at: datetime

    @classmethod
    def of(cls, row: t.UserCredential) -> CredentialOut:
        return cls(owner=row.owner, namespace=row.namespace, connection=row.connection, kind=row.kind,
                   fields=row.fields, expires_at=row.expires_at, updated_at=row.updated_at)


class OAuthStart(BaseModel):
    authorize_url: str


def _owner(ctx: Ctx) -> str:
    if not ctx.actor.startswith("user:"):
        raise Forbidden("only people have their own credentials; service accounts use the connection's")
    return ctx.actor


async def _personal_connection(ctx: Ctx, namespace: str, name: str) -> Connection:
    await ctx.namespace(namespace, "connections:read")
    row = await ctx.engine.store.get_resource_row(ctx.tenant_id, "Connection", name, namespace=namespace, fallback=False)
    connection = Connection.model_validate(row.document)
    if connection.spec.credentials == "shared":
        raise InvalidRequest(f"connection '{name}' uses one shared credential, not each person's own")
    return connection


def _user_fields(connection: Connection) -> set[str]:
    fields = set(connection.spec.user_fields or connection.spec.secrets)
    if connection.spec.oauth:
        fields.add(connection.spec.oauth.token_field)
    return fields


@router.get("/v1/me/credentials", response_model=list[CredentialOut], summary="My connection credentials")
async def my_credentials(ctx: CtxDep) -> Any:
    return [CredentialOut.of(r) for r in await ctx.engine.user_credentials.list(ctx.tenant_id, _owner(ctx))]


@router.put("/v1/me/credentials/{namespace}/{connection}", response_model=CredentialOut, summary="Set my credential for a connection")
async def put_credential(namespace: str, connection: str, body: CredentialIn, ctx: CtxDep) -> Any:
    owner = _owner(ctx)
    conn = await _personal_connection(ctx, namespace, connection)
    allowed = _user_fields(conn)
    if not body.values or (extra := set(body.values) - allowed):
        raise InvalidRequest(f"this connection takes {sorted(allowed)} from each person" + (f", not {sorted(extra)}" if body.values else ""))
    row = await ctx.engine.user_credentials.put(ctx.tenant_id, owner, namespace, connection, dict(body.values))
    await ctx.audit("credential.set", f"{namespace}/{connection}", namespace=namespace, fields=sorted(body.values))
    return CredentialOut.of(row)


@router.delete("/v1/me/credentials/{namespace}/{connection}", status_code=204, summary="Remove my credential")
async def delete_credential(namespace: str, connection: str, ctx: CtxDep) -> Response:
    if not await ctx.engine.user_credentials.delete(ctx.tenant_id, _owner(ctx), namespace, connection):
        raise NotFound("no credential for that connection")
    await ctx.audit("credential.removed", f"{namespace}/{connection}", namespace=namespace)
    return Response(status_code=204)


@router.post(
    "/v1/me/credentials/{namespace}/{connection}/oauth", response_model=OAuthStart, summary="Connect my account with OAuth",
    description="Open the URL in a browser that is signed in to Shoal as you (or the one that made this call).",
)
async def start_oauth(namespace: str, connection: str, ctx: CtxDep, response: Response) -> Any:
    owner = _owner(ctx)
    conn = await _personal_connection(ctx, namespace, connection)
    if conn.spec.oauth is None:
        raise InvalidRequest(f"connection '{connection}' has no OAuth settings; paste a token instead")
    url = ctx.engine.oauth.start(ctx.tenant_id, owner, namespace, connection, conn.spec.oauth)
    state = parse_qs(urlsplit(url).query)["state"][0]
    response.set_cookie(
        BINDING_COOKIE, OAuthFlows.binding(state), max_age=int(STATE_TTL.total_seconds()), path="/v1/oauth/callback",
        httponly=True, secure=ctx.engine.settings.public_url.startswith("https://"), samesite="lax",
    )
    return OAuthStart(authorize_url=url)


async def _signed_in_as(request: Request, engine: Any, tenant_id: str, owner: str) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        claims = verify_session(engine.settings, token)
        user = await engine.directory.get_user(claims["tid"], claims["sub"])
    except ShoalError:
        return False
    return user.tenant_id == tenant_id and user.active and f"user:{user.username}" == owner


@router.get("/v1/oauth/callback", include_in_schema=False)
async def oauth_callback(
    request: Request, engine: EngineDep, code: str | None = None, state: str | None = None, error: str | None = None
) -> Any:
    ui = f"{engine.settings.public_url.rstrip('/')}/settings/credentials"
    if error or not code or not state:
        return RedirectResponse(f"{ui}?error={quote(error or 'missing_code')}", status_code=303)
    data = engine.oauth.read_state(state)
    bound = hmac.compare_digest(request.cookies.get(BINDING_COOKIE, ""), OAuthFlows.binding(state))
    if not bound and not await _signed_in_as(request, engine, data["t"], data["o"]):
        # Someone else's authorize link: connecting would file this browser's account under them.
        await engine.store.audit(data["t"], data["o"], "credential.oauth_refused", f"{data['n']}/{data['c']}", outcome="denied",
                                 namespace=data["n"], reason="browser not signed in as the owner")
        return RedirectResponse(f"{ui}?error=sign_in_required", status_code=303)
    tenant_id, owner, namespace, connection = await engine.oauth.callback(code, state)
    await engine.store.audit(tenant_id, owner, "credential.set", f"{namespace}/{connection}", namespace=namespace, kind="oauth")
    return RedirectResponse(f"{ui}?connected={namespace}/{connection}", status_code=303)


@router.get("/v1/install/callback", include_in_schema=False)
async def install_callback(engine: EngineDep, code: str | None = None, state: str | None = None, error: str | None = None) -> Any:
    """Where an app install (e.g. Add to Slack) returns; stores the token as the connection's secret."""
    base = engine.settings.public_url.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(f"{base}/?install_error={error or 'missing_code'}", status_code=303)
    tenant_id, namespace, connection, actor, fields, then = await engine.integrations.install_callback(code, state)
    await engine.store.audit(tenant_id, actor, "connection.installed", f"{namespace}/{connection}", namespace=namespace, fields=fields)
    page = "/setup" if then == "setup" else ""
    return RedirectResponse(f"{base}/ns/{namespace}/connections/{connection}{page}?installed=1", status_code=303)


@router.get("/v1/users/{username}/credentials", response_model=list[CredentialOut], summary="Who connected what (admins)")
async def user_credentials(username: str, ctx: CtxDep) -> Any:
    await ctx.require("users:manage")
    return [CredentialOut.of(r) for r in await ctx.engine.user_credentials.list(ctx.tenant_id, f"user:{username}")]


@router.delete("/v1/users/{username}/credentials/{namespace}/{connection}", status_code=204, summary="Revoke a person's credential")
async def revoke_credential(username: str, namespace: str, connection: str, ctx: CtxDep) -> Response:
    await ctx.require("users:manage")
    if not await ctx.engine.user_credentials.delete(ctx.tenant_id, f"user:{username}", namespace, connection):
        raise NotFound("no credential for that connection")
    await ctx.audit("credential.revoked", f"user:{username}/{namespace}/{connection}", namespace=namespace)
    return Response(status_code=204)
