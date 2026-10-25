"""Browser sign-in through the tenant's OIDC providers."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from shoal.api.deps import SESSION_COOKIE, EngineDep
from shoal.api.schemas import LoginProvider
from shoal.core.errors import NotFound, ShoalError

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _callback_url(engine: Any) -> str:
    return f"{engine.settings.public_url.rstrip('/')}/v1/auth/callback"


def _safe_redirect(target: str | None) -> str:
    """Only same-site relative paths (no open redirects)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@router.get("/providers", response_model=list[LoginProvider], summary="Sign-in options for a tenant (public)")
async def providers(tenant: str, engine: EngineDep) -> Any:
    row = await engine.store.get_tenant_by_slug(tenant)
    if row is None:
        raise NotFound(f"tenant '{tenant}' not found")
    base = engine.settings.public_url.rstrip("/")
    return [
        LoginProvider(name=p.name, display_name=p.display_name, login_url=f"{base}/v1/auth/login/{tenant}/{p.name}")
        for p in await engine.directory.list_oidc_providers(row.id)
        if p.enabled
    ]


@router.get("/login/{tenant}/{provider}", summary="Start OIDC sign-in (redirects to the provider)")
async def login(tenant: str, provider: str, engine: EngineDep, redirect_to: str | None = None) -> RedirectResponse:
    row = await engine.store.get_tenant_by_slug(tenant)
    if row is None:
        raise NotFound(f"tenant '{tenant}' not found")
    url = await engine.oidc.begin(row.id, provider, _callback_url(engine), _safe_redirect(redirect_to))
    return RedirectResponse(url, status_code=302)


@router.get("/callback", summary="OIDC redirect URI")
async def callback(request: Request, engine: EngineDep, state: str | None = None, code: str | None = None, error: str | None = None) -> Response:
    if error or not state or not code:
        return RedirectResponse(f"/login?error={quote(error or 'missing_code')}", status_code=302)
    try:
        result = await engine.oidc.complete(state, code, _callback_url(engine))
    except ShoalError as exc:
        return RedirectResponse(f"/login?error={quote(exc.message[:200])}", status_code=302)
    response = RedirectResponse(_safe_redirect(result["redirect_to"]), status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        result["session"],
        httponly=True,
        secure=engine.settings.public_url.startswith("https://"),
        samesite="lax",
        max_age=12 * 3600,
        path="/",
    )
    return response


@router.post("/logout", status_code=204, summary="Clear the session cookie")
async def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
