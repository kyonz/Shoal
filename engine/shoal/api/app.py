"""The HTTP API: ``create_app()`` builds the FastAPI application.

Errors are RFC 9457 problem documents (``application/problem+json``) with a
stable ``code``; SCIM endpoints use the SCIM error format instead.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from shoal import __version__
from shoal.api.deps import authenticate
from shoal.api.routers import (
    a2a,
    admin,
    assistant,
    auth,
    blueprints,
    evals,
    federation,
    hooks,
    resources,
    runs,
    scim,
    system,
)
from shoal.config import Settings
from shoal.core.errors import ShoalError
from shoal.core.ids import new_id
from shoal.core.requestctx import RequestInfo, current_request, reset_request, set_request
from shoal.engine import Engine
from shoal.federation.protocol import ASSERTION_HEADER
from shoal.integrations.base import IntegrationError

log = logging.getLogger(__name__)

PROBLEM = "application/problem+json"


def problem(request: Request, status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    if request.url.path.startswith("/scim/"):
        return scim.scim({"schemas": [scim.ERROR_SCHEMA], "status": str(status), "detail": detail}, status)
    body = {"type": f"https://shoal.dev/problems/{code}", "title": code.replace("_", " "), "status": status, "code": code, "detail": detail}
    body.update({k: v for k, v in extra.items() if v})
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


def _errors(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ShoalError)
    async def _shoal(request: Request, exc: ShoalError) -> JSONResponse:
        if request.url.path.startswith("/scim/"):
            return scim.scim_error(exc)
        if exc.status_code >= 500:
            log.error("request failed: %s", exc.message, extra={"details": exc.details})
        status = exc.status_code
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        response = problem(request, status, exc.code, exc.message, details=exc.details)
        if headers:
            response.headers.update(headers)
        return response

    @app.exception_handler(IntegrationError)
    async def _integration(request: Request, exc: IntegrationError) -> JSONResponse:
        return problem(request, 502, "integration_error", str(exc), upstream_status=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(request, 422, "invalid_request", "request validation failed", errors=_errors(exc))

    @app.exception_handler(ValidationError)
    async def _invalid(request: Request, exc: ValidationError) -> JSONResponse:
        return problem(request, 422, "invalid_request", f"invalid {exc.title}", errors=_errors(exc))


MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
UNAUDITED_PREFIXES = ("/v1/hooks/", "/v1/auth/")  # webhook deliveries and sign-in keep their own records


def install_request_context(app: FastAPI) -> None:
    """Request ids, and an audit entry for every mutating call that no handler audited itself
    (so failed, rejected and unexpected writes are recorded too)."""

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id") or new_id("req")
        client = request.headers.get("x-shoal-client", "")
        info = RequestInfo(
            request_id=request_id[:64],
            method=request.method,
            path=request.url.path,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            source="scim" if request.url.path.startswith("/scim/") else (client if client in ("ui", "assistant") else "api"),
        )
        token = set_request(info)
        status = 500  # also when the handler is cancelled (BaseException)
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            if (
                request.method in MUTATING
                and not info.audited
                and not request.url.path.startswith(UNAUDITED_PREFIXES)
            ):
                principal = getattr(request.state, "principal", None)
                if principal is not None or status >= 400:
                    outcome = "success" if status < 400 else ("denied" if status in (401, 403) else "error")
                    try:
                        await app.state.engine.store.audit(
                            principal.tenant_id if principal else None,
                            principal.actor if principal else None,
                            f"api.{request.method.lower()}",
                            request.url.path,
                            outcome=outcome,
                            status=status,
                        )
                    except Exception:  # auditing must never break the request
                        log.exception("could not write audit entry")
            reset_request(token)
        response.headers["X-Request-Id"] = info.request_id
        return response


# Served by the hub itself even for federated tenants: the hub's own directory, sign-in,
# federation plumbing and operator APIs. Everything else under /v1 goes to the member.
HUB_LOCAL_PREFIXES = (
    "/v1/federation", "/v1/auth", "/v1/tenants", "/v1/hooks", "/v1/users", "/v1/groups", "/v1/service-accounts",
    "/v1/api-keys", "/v1/oidc-providers", "/v1/openapi.json", "/v1/docs", "/v1/info",
)


def install_federation(app: FastAPI) -> None:
    """Hub routing: calls for a federated tenant are forwarded to its member deployment."""

    @app.middleware("http")
    async def federation_router(request: Request, call_next: Any) -> Any:
        path = request.url.path
        engine = app.state.engine
        if (
            not path.startswith("/v1/")
            or path == "/v1/me"
            or path.startswith(HUB_LOCAL_PREFIXES)
            or request.headers.get(ASSERTION_HEADER)  # already forwarded: this is the member
        ):
            return await call_next(request)
        try:
            principal = await authenticate(request, engine)
        except ShoalError:
            return await call_next(request)  # let the route produce the proper error
        link_id = await engine.federation.federated_link_id(principal.tenant_id) if principal.tenant_id else None
        if link_id is None:
            request.state.principal = principal
            return await call_next(request)
        info = current_request()
        try:
            answer = await engine.federation.forward(
                link_id, principal, method=request.method, path=path, query=request.url.query,
                headers=dict(request.headers), body=await request.body(), request_id=info.request_id if info else new_id("req"),
            )
        except ShoalError as exc:  # raised outside the routes, so render the problem here
            return problem(request, exc.status_code, exc.code, exc.message, details=exc.details)
        if info is not None:
            info.audited = True  # the member audits the action; the hub records the forward below
        if request.method in MUTATING:
            await engine.store.audit(principal.tenant_id, principal.actor, f"federation.{request.method.lower()}", path,
                                     outcome="success" if answer["status"] < 400 else "error", status=answer["status"])
        return Response(
            content=base64.b64decode(answer.get("body") or ""), status_code=answer["status"],
            headers={k: v for k, v in (answer.get("headers") or {}).items() if k.lower() != "content-length"},
        )


def _mount_frontend(app: FastAPI, static_dir: str) -> None:
    root = Path(static_dir).resolve()
    index = root / "index.html"
    if not index.exists():
        log.warning("static_dir %s has no index.html; not serving the UI", root)
        return

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)  # client-side routes (/runs/..., /docs/..., /admin/...)


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    run_worker: bool = False,
) -> FastAPI:
    """Build the API. ``run_worker`` embeds a worker + scheduler in the process."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        eng = app.state.engine
        await eng.start()
        eng.federation_member.start(app)
        if run_worker:
            eng.start_background_worker()
        try:
            yield
        finally:
            if owns_engine:
                await eng.close()

    owns_engine = engine is None
    engine = engine or Engine(settings)
    app = FastAPI(
        title="Shoal API",
        version=__version__,
        description="Multi-tenant engine for multi-agent AI workflows. Authenticate with `Authorization: Bearer <api key>`.",
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )
    app.state.engine = engine
    install_error_handlers(app)
    install_federation(app)
    install_request_context(app)  # outermost: request ids and audit context wrap federation routing too
    app.add_middleware(
        CORSMiddleware,
        allow_origins=engine.settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Location", "X-Request-Id"],
    )
    for module in (system, auth, hooks, scim, federation, admin, runs, evals, a2a, blueprints, assistant, resources):
        app.include_router(module.router)
    if engine.settings.static_dir:
        _mount_frontend(app, engine.settings.static_dir)
    return app
