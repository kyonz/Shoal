"""Claude Code gateways: each person links their own Claude subscription.

Admins create a Gateway of ``type: claude-code`` (Gateways screen). In
subscription mode every person who starts runs on it links their own plan
here, either by signing in from the browser (Shoal runs the unmodified
``claude setup-token`` and relays Anthropic's sign-in URL and the code) or by
pasting a token they made with ``claude setup-token``. Tokens are stored as the
person's own credential: encrypted, write-only, used only for runs they start.
Admins see who has linked which gateway under ``/v1/users/{username}/credentials``
and can revoke it there.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from shoal.agents.claude_code import (
    GATEWAY_TYPE,
    SUBSCRIPTION_TERMS,
    TOKEN_FIELD,
    gateway_mode,
    gateway_problem,
)
from shoal.api.deps import Ctx, CtxDep
from shoal.api.routers.credentials import _owner
from shoal.core import clock
from shoal.core.errors import InvalidRequest, NotFound, ShoalError
from shoal.core.models import GatewaySpec
from shoal.core.scope import SHARED_NAMESPACE

router = APIRouter(prefix="/v1/me/claude-code", tags=["credentials"])

TOKEN_LIFETIME = timedelta(days=365)  # claude setup-token issues one-year tokens


class ClaudeCodeGatewayOut(BaseModel):
    gateway: str
    description: str | None
    mode: str = Field(description="api_key (the gateway's key) or subscription (each person's own Claude plan).")
    problem: str | None = Field(None, description="Why the gateway cannot run yet, when it cannot.")
    linked: bool = Field(description="Subscription mode: whether you have linked your Claude subscription.")
    linked_at: Any = None
    expires_at: Any = None
    terms: str | None = None


class TokenIn(BaseModel):
    token: str = Field(min_length=20, description="The token `claude setup-token` printed.")


class LoginOut(BaseModel):
    login_id: str
    url: str = Field(description="Open this, sign in to Claude, and paste the code it shows into finish.")
    expires_in: int


class CodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=4000)


async def _gateways(ctx: Ctx) -> dict[str, GatewaySpec]:
    rows = await ctx.engine.store.list_resources(ctx.tenant_id, "Gateway")
    return {
        row.name: GatewaySpec.model_validate(row.document["spec"])
        for row in rows
        if row.document["spec"].get("type") == GATEWAY_TYPE
    }


async def _subscription_gateway(ctx: Ctx, gateway: str) -> None:
    spec = (await _gateways(ctx)).get(gateway)
    if spec is None:
        raise NotFound(f"no claude-code gateway '{gateway}'")
    if gateway_mode(spec.config, has_api_key=bool(spec.api_key_secret)) != "subscription":
        raise InvalidRequest(f"gateway '{gateway}' uses an API key; there is nothing to link")


async def _store(ctx: Ctx, owner: str, gateway: str, token: str, how: str) -> None:
    await ctx.engine.user_credentials.put(
        ctx.tenant_id, owner, SHARED_NAMESPACE, gateway, {TOKEN_FIELD: token},
        kind="claude-subscription", expires_at=clock.now() + TOKEN_LIFETIME,
    )
    await ctx.audit("credential.set", f"{SHARED_NAMESPACE}/{gateway}", namespace=SHARED_NAMESPACE, kind="claude-subscription", via=how)


@router.get("", response_model=list[ClaudeCodeGatewayOut], summary="Claude Code gateways and whether I have linked my subscription")
async def my_gateways(ctx: CtxDep) -> Any:
    owner = ctx.actor if ctx.actor.startswith("user:") else None
    linked = {
        row.connection: row
        for row in (await ctx.engine.user_credentials.list(ctx.tenant_id, owner) if owner else [])
        if row.namespace == SHARED_NAMESPACE and row.kind == "claude-subscription"
    }
    out = []
    for name, spec in sorted((await _gateways(ctx)).items()):
        try:
            mode = gateway_mode(spec.config, has_api_key=bool(spec.api_key_secret))
        except ShoalError:
            mode = str(spec.config.get("mode"))
        row = linked.get(name)
        out.append(ClaudeCodeGatewayOut(
            gateway=name, description=spec.description, mode=mode, problem=gateway_problem(spec),
            linked=row is not None, linked_at=row.updated_at if row else None, expires_at=row.expires_at if row else None,
            terms=SUBSCRIPTION_TERMS if mode == "subscription" else None,
        ))
    return out


@router.put("/{gateway}/token", status_code=204, summary="Link my Claude subscription with a token from `claude setup-token`")
async def put_token(gateway: str, body: TokenIn, ctx: CtxDep) -> Response:
    owner = _owner(ctx)
    await _subscription_gateway(ctx, gateway)
    token = body.token.strip()
    if not token.startswith("sk-ant-oat"):
        raise InvalidRequest("that is not a Claude Code subscription token (they start with sk-ant-oat); run `claude setup-token`")
    await _store(ctx, owner, gateway, token, "token")
    return Response(status_code=204)


@router.post("/{gateway}/login", response_model=LoginOut, summary="Start signing in to my Claude subscription from the browser")
async def start_login(gateway: str, ctx: CtxDep) -> Any:
    owner = _owner(ctx)
    await _subscription_gateway(ctx, gateway)
    login = await ctx.engine.claude_logins.start(ctx.tenant_id, owner, gateway)
    return LoginOut(login_id=login.id, url=login.url, expires_in=600)


@router.post("/{gateway}/login/{login_id}", status_code=204, summary="Finish signing in with the code claude.ai showed")
async def finish_login(gateway: str, login_id: str, body: CodeIn, ctx: CtxDep) -> Response:
    owner = _owner(ctx)
    linked_gateway, token = await ctx.engine.claude_logins.finish(ctx.tenant_id, owner, login_id, body.code)
    if linked_gateway != gateway:
        raise InvalidRequest("that sign-in was started for another gateway")
    await _store(ctx, owner, gateway, token, "browser")
    return Response(status_code=204)


@router.delete("/{gateway}/login/{login_id}", status_code=204, summary="Cancel a sign-in")
async def cancel_login(gateway: str, login_id: str, ctx: CtxDep) -> Response:
    ctx.engine.claude_logins.cancel(ctx.tenant_id, _owner(ctx), login_id)
    return Response(status_code=204)


@router.delete("/{gateway}", status_code=204, summary="Unlink my Claude subscription")
async def unlink(gateway: str, ctx: CtxDep) -> Response:
    if not await ctx.engine.user_credentials.delete(ctx.tenant_id, _owner(ctx), SHARED_NAMESPACE, gateway):
        raise NotFound("you have not linked a subscription for that gateway")
    await ctx.audit("credential.removed", f"{SHARED_NAMESPACE}/{gateway}", namespace=SHARED_NAMESPACE)
    return Response(status_code=204)
