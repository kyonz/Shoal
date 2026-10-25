"""A route-based mock of a provider's HTTP API, served through ``httpx.MockTransport``."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

Handler = Callable[..., Any]


class UnmatchedRequest(AssertionError):
    """An integration called an endpoint the test did not mock."""


@dataclass
class RecordedCall:
    method: str
    url: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    content: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.content) if self.content else None


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    template: str
    handler: Handler | None
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    host: str | None = None
    calls: list[RecordedCall] = field(default_factory=list)

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def last(self) -> RecordedCall:
        assert self.calls, f"{self.method} {self.template} was never called"
        return self.calls[-1]


def _compile(template: str) -> re.Pattern[str]:
    regex = ""
    for part in re.split(r"(\{[a-zA-Z_][a-zA-Z0-9_]*(?::path)?\})", template):
        if part.startswith("{") and part.endswith("}"):
            name, _, kind = part[1:-1].partition(":")
            regex += f"(?P<{name}>.+)" if kind == "path" else f"(?P<{name}>[^/]+)"
        else:
            regex += re.escape(part)
    return re.compile(regex + "/?")


def _response(value: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    if isinstance(value, httpx.Response):
        return value
    if isinstance(value, tuple):
        status, value = value[0], value[1] if len(value) > 1 else None
    if value is None:
        return httpx.Response(status, headers=headers)
    if isinstance(value, bytes | str):
        return httpx.Response(status, content=value, headers=headers)
    return httpx.Response(status, json=value, headers=headers)


class MockHTTP:
    """Mock an HTTP API for an integration under test.

    ::

        api = MockHTTP()
        api.get("/repos/{owner}/{repo}", json={"name": "api"})
        api.post("/issues", handler=lambda request: (201, {"id": 7, **json.loads(request.content)}))

    Paths use ``{name}`` placeholders (``{name:path}`` spans slashes); a handler
    receives the ``httpx.Request`` and the placeholders as keyword arguments and
    returns a response, a JSON value, or ``(status, json)``. Requests that match
    no route raise :class:`UnmatchedRequest`, so a test fails loudly.
    """

    def __init__(self, *, host: str | None = None):
        self.host = host
        self.routes: list[Route] = []
        self.calls: list[RecordedCall] = []

    def add(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        status: int = 200,
        handler: Handler | None = None,
        headers: dict[str, str] | None = None,
        host: str | None = None,
    ) -> Route:
        route = Route(method.upper(), _compile(path), path, handler, status, json, dict(headers or {}), host or self.host)
        self.routes.insert(0, route)  # later routes win, so a test can override a default
        return route

    def get(self, path: str, **kwargs: Any) -> Route:
        return self.add("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Route:
        return self.add("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Route:
        return self.add("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Route:
        return self.add("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Route:
        return self.add("DELETE", path, **kwargs)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        call = RecordedCall(
            method=request.method,
            url=str(request.url),
            path=request.url.path,
            params=dict(request.url.params),
            headers={k.lower(): v for k, v in request.headers.items()},
            content=request.content,
        )
        self.calls.append(call)
        for route in self.routes:
            if route.method != request.method or (route.host and route.host != request.url.host):
                continue
            match = route.pattern.fullmatch(request.url.path)
            if not match:
                continue
            route.calls.append(call)
            if route.handler is not None:
                return _response(route.handler(request, **match.groupdict()), headers=route.headers)
            return _response(route.body, route.status, route.headers)
        known = ", ".join(f"{r.method} {r.template}" for r in reversed(self.routes)) or "none"
        raise UnmatchedRequest(f"unmocked request {request.method} {request.url} (mocked: {known})")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def requests(self, method: str | None = None, path: str | None = None) -> list[RecordedCall]:
        return [c for c in self.calls if (method is None or c.method == method.upper()) and (path is None or c.path == path)]

    def assert_all_called(self) -> None:
        missing = [f"{r.method} {r.template}" for r in self.routes if not r.called]
        assert not missing, f"mocked routes never called: {', '.join(missing)}"
