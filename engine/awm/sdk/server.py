"""Serve any Python integration over the remote integration protocol.

This is the reference implementation of ``awm.integration/v1``: deploy an
integration as its own service (its own release cadence, network zone and
credentials) and register it with AWM as a ``remote`` connection::

    python -m awm.sdk.server awm_linear:LinearIntegration --port 8080

The bearer token comes from ``--token`` or ``AWM_INTEGRATION_TOKEN``; the
server refuses to start without one unless ``--insecure-no-auth`` is given.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from awm.integrations.base import ConnectionContext, Integration, IntegrationError
from awm.integrations.remote import PROTOCOL
from awm.sdk.manifest import manifest_for

log = logging.getLogger(__name__)


@dataclass
class ServiceEngine:
    """The slice of the engine a ``ConnectionContext`` uses outside AWM."""

    http_transport: httpx.AsyncBaseTransport | None = None


class CallContext(BaseModel):
    tenant_id: str = "remote"
    connection: str = "remote"
    run_id: str | None = None
    initiator: dict[str, Any] | None = None


class _Call(BaseModel):
    connection: dict[str, Any] = Field(default_factory=dict)
    context: CallContext = Field(default_factory=CallContext)


class ActionCall(_Call):
    params: dict[str, Any] = Field(default_factory=dict)


class QueryCall(_Call):
    filter: dict[str, Any] = Field(default_factory=dict)
    cursor: str | None = None
    limit: int = Field(100, ge=1, le=1000)


class WebhookCall(_Call):
    headers: dict[str, str] = Field(default_factory=dict)
    body_b64: str = ""

    def body(self) -> bytes:
        try:
            return base64.b64decode(self.body_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise IntegrationError("body_b64 is not valid base64", status=400) from exc


def _error(status: int, message: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "retryable": retryable}}, status_code=status)


def create_app(
    integration: Integration | type[Integration],
    *,
    token: str | None = None,
    allow_unauthenticated: bool = False,
    http_transport: httpx.AsyncBaseTransport | None = None,
    version: str | None = None,
) -> FastAPI:
    """An ASGI app serving ``integration``; ``http_transport`` is for tests (e.g. a MockHTTP)."""
    instance = integration() if isinstance(integration, type) else integration
    if not token and not allow_unauthenticated:
        raise ValueError("a token is required (or pass allow_unauthenticated=True for local development)")
    manifest = manifest_for(instance, version=version).model_dump(mode="json")
    engine = ServiceEngine(http_transport)
    app = FastAPI(title=f"{instance.title} (AWM integration)", version=version or "0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        if token and request.url.path != "/healthz":
            presented = request.headers.get("authorization", "")
            if not hmac.compare_digest(presented.encode(), f"Bearer {token}".encode()):
                return _error(401, "missing or invalid bearer token")
        return await call_next(request)

    @app.exception_handler(IntegrationError)
    async def integration_error(_: Request, exc: IntegrationError) -> JSONResponse:
        status = exc.status if exc.status and 400 <= exc.status < 500 else 502
        return _error(status, str(exc), retryable=exc.retryable)

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: Exception) -> JSONResponse:
        return _error(422, str(exc))

    def ctx(call: _Call) -> ConnectionContext:
        config = instance.connection_model.model_validate(call.connection)
        return ConnectionContext(
            engine=engine,  # type: ignore[arg-type]
            tenant_id=call.context.tenant_id,
            name=call.context.connection,
            config=config,
            integration=instance,
            initiator=call.context.initiator,
            run_id=call.context.run_id,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/manifest")
    async def get_manifest() -> dict[str, Any]:
        return manifest

    @app.post("/test")
    async def test(call: _Call) -> dict[str, Any]:
        return await instance.test_connection(ctx(call))

    @app.post("/actions/{name}")
    async def action(name: str, call: ActionCall) -> Any:
        if name not in instance.actions:
            return _error(404, f"{instance.key} has no action '{name}'")
        return {"output": await instance.call_action(ctx(call), name, call.params)}

    @app.post("/resources/{name}/query")
    async def query(name: str, call: QueryCall) -> Any:
        if name not in instance.resources:
            return _error(404, f"{instance.key} has no resource '{name}'")
        page = await instance.query(ctx(call), name, call.filter, cursor=call.cursor, limit=call.limit)
        return page.model_dump(mode="json")

    @app.post("/webhooks/verify")
    async def verify(call: WebhookCall) -> dict[str, Any]:
        headers = {k.lower(): v for k, v in call.headers.items()}
        return {"valid": bool(await instance.verify_webhook(ctx(call), headers, call.body()))}

    @app.post("/webhooks/parse")
    async def parse(call: WebhookCall) -> dict[str, Any]:
        headers = {k.lower(): v for k, v in call.headers.items()}
        events = await instance.parse_webhook(ctx(call), headers, call.body())
        return {"events": [e.model_dump(mode="json") for e in events]}

    app.state.protocol = PROTOCOL
    return app


def load_integration(target: str) -> type[Integration]:
    """``package.module:ClassName`` -> the class."""
    module, _, attr = target.partition(":")
    if not attr:
        raise ValueError("expected module:Class, e.g. awm_linear:LinearIntegration")
    obj = getattr(importlib.import_module(module), attr)
    if not (isinstance(obj, type) and issubclass(obj, Integration)):
        raise TypeError(f"{target} is not an Integration subclass")
    return obj


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - thin CLI wrapper
    import uvicorn

    parser = argparse.ArgumentParser(description="Serve an AWM integration over HTTP.")
    parser.add_argument("integration", help="module:Class")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default=os.environ.get("AWM_INTEGRATION_TOKEN"))
    parser.add_argument("--insecure-no-auth", action="store_true", help="Serve without a bearer token (development only).")
    args = parser.parse_args(argv)
    app = create_app(load_integration(args.integration), token=args.token, allow_unauthenticated=args.insecure_no_auth)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
