"""Per-request context for auditing (request id, client, source)."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field


@dataclass
class RequestInfo:
    request_id: str
    method: str = ""
    path: str = ""
    ip: str | None = None
    user_agent: str | None = None
    source: str = "api"  # api | ui | scim | hub | system
    via: str | None = None  # e.g. "hub" for calls forwarded by a federation hub
    audited: bool = field(default=False)  # a domain audit entry was written for this request


_current: contextvars.ContextVar[RequestInfo | None] = contextvars.ContextVar("awm_request", default=None)


def current_request() -> RequestInfo | None:
    return _current.get()


def set_request(info: RequestInfo | None) -> contextvars.Token[RequestInfo | None]:
    return _current.set(info)


def reset_request(token: contextvars.Token[RequestInfo | None]) -> None:
    _current.reset(token)
