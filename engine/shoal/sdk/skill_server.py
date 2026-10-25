"""Serve any skill source plugin over ``shoal.skill-source/v1``.

Run a source as its own service and add it to Shoal as a ``registry`` skill
source pointing at the service::

    python -m shoal.sdk.skill_server shoal_acme:AcmeSkills --config config.json --port 8080

The source's own settings (``--config``, a JSON file validated against its
``config_model``) and credentials stay with the service. The bearer token comes
from ``--token`` or ``SHOAL_SKILL_SOURCE_TOKEN``; the server refuses to start
without one unless ``--insecure-no-auth`` is given.
"""

from __future__ import annotations

import argparse
import hmac
import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shoal.skills.base import SkillSource, SkillSourceContext, SkillSourceError
from shoal.skills.registry import PROTOCOL


@dataclass
class _ServiceSettings:
    skill_source_cache_dir: str | None = None
    skill_source_allow_local: bool = False


@dataclass
class _ServiceEngine:
    """The slice of the engine a ``SkillSourceContext`` uses outside Shoal."""

    http_transport: httpx.AsyncBaseTransport | None = None
    settings: _ServiceSettings | None = None


def _error(status: int, message: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "retryable": retryable}}, status_code=status)


def create_app(
    source: SkillSource | type[SkillSource],
    config: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    allow_unauthenticated: bool = False,
    allow_local: bool = False,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """An ASGI app serving ``source`` with ``config``; ``http_transport`` is for tests."""
    instance = source() if isinstance(source, type) else source
    if not token and not allow_unauthenticated:
        raise ValueError("a token is required (or pass allow_unauthenticated=True for local development)")
    engine = _ServiceEngine(http_transport, _ServiceSettings(skill_source_allow_local=allow_local))
    ctx = SkillSourceContext(
        engine=engine,  # type: ignore[arg-type]
        tenant_id="remote",
        name=instance.key,
        config=instance.config_model.model_validate(config or {}),
        source=instance,
    )
    app = FastAPI(title=f"{instance.title} (Shoal skill source)", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        if token and request.url.path != "/healthz":
            presented = request.headers.get("authorization", "")
            if not hmac.compare_digest(presented.encode(), f"Bearer {token}".encode()):
                return _error(401, "missing or invalid bearer token")
        return await call_next(request)

    @app.exception_handler(SkillSourceError)
    async def source_error(_: Request, exc: SkillSourceError) -> JSONResponse:
        status = exc.status if exc.status and 400 <= exc.status < 500 else 502
        return _error(status, str(exc), retryable=exc.retryable)

    @app.exception_handler(ValidationError)
    async def validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        return _error(422, str(exc))

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/manifest")
    async def manifest() -> dict[str, Any]:
        return {"protocol": PROTOCOL, "key": instance.key, "title": instance.title, "description": instance.description}

    @app.get("/skills")
    async def skills(
        q: str | None = None, cursor: str | None = None, limit: int = Query(50, ge=1, le=500)
    ) -> dict[str, Any]:
        page = await (instance.search(ctx, q, cursor, limit) if q else instance.list(ctx, cursor, limit))
        return page.model_dump(mode="json")

    @app.get("/skills/{skill_id:path}")
    async def skill(skill_id: str, version: str | None = None) -> dict[str, Any]:
        return (await instance.fetch(ctx, skill_id, version)).model_dump(mode="json")

    app.state.protocol = PROTOCOL
    return app


def load_source(target: str) -> type[SkillSource]:
    """``package.module:ClassName`` -> the class."""
    module, _, attr = target.partition(":")
    if not attr:
        raise ValueError("expected module:Class, e.g. shoal_acme:AcmeSkills")
    obj = getattr(importlib.import_module(module), attr)
    if not (isinstance(obj, type) and issubclass(obj, SkillSource)):
        raise TypeError(f"{target} is not a SkillSource subclass")
    return obj


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - thin CLI wrapper
    import uvicorn

    parser = argparse.ArgumentParser(description="Serve a Shoal skill source over HTTP.")
    parser.add_argument("source", help="module:Class, e.g. shoal.skills.git:GitSkillSource")
    parser.add_argument("--config", type=Path, help="JSON file with the source's settings.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default=os.environ.get("SHOAL_SKILL_SOURCE_TOKEN"))
    parser.add_argument("--insecure-no-auth", action="store_true", help="Serve without a bearer token (development only).")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text()) if args.config else {}
    app = create_app(load_source(args.source), config, token=args.token, allow_unauthenticated=args.insecure_no_auth)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
