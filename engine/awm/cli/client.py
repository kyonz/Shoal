"""A small synchronous client for the AWM HTTP API, used by the CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from awm import __version__
from awm.cli.config import Target


class APIError(Exception):
    """A failed call, carrying the server's problem document when there is one."""

    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


def make_http(base_url: str, headers: dict[str, str], timeout: float) -> httpx.Client:
    """Build the transport. Tests replace this to talk to an in-process app."""
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout)


class Client:
    def __init__(self, target: Target, *, timeout: float = 30.0):
        self.target = target
        headers = {"User-Agent": f"awm-cli/{__version__}", "Accept": "application/json"}
        if target.token:
            headers["Authorization"] = f"Bearer {target.token}"
        if target.tenant:
            headers["X-AWM-Tenant"] = target.tenant
        self.http = make_http(target.server, headers, timeout)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ plumbing

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise APIError(0, f"cannot reach {self.target.server}: {exc}") from exc
        if response.status_code >= 400:
            raise _error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def events(self, path: str, *, after: int = 0) -> Iterator[tuple[str, dict[str, Any]]]:
        """Follow a server-sent event stream, yielding ``(event, data)`` pairs."""
        headers = {"Accept": "text/event-stream", "Last-Event-ID": str(after)}
        try:
            with self.http.stream("GET", path, headers=headers, timeout=httpx.Timeout(30.0, read=None)) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _error(response)
                yield from _parse_sse(response.iter_lines())
        except httpx.TransportError as exc:
            raise APIError(0, f"lost the event stream from {self.target.server}: {exc}") from exc


def _error(response: httpx.Response) -> APIError:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and (body.get("detail") or body.get("title")):
        message = str(body.get("detail") or body.get("title"))
    else:
        message = response.text.strip()[:500] or response.reason_phrase
    return APIError(response.status_code, message, body)


def _parse_sse(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    event, data = "message", []
    for line in lines:
        if not line:
            if data:
                payload = "\n".join(data)
                try:
                    yield event, json.loads(payload)
                except ValueError:
                    yield event, {"raw": payload}
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue  # keep-alive comment
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
