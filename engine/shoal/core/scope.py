"""The namespace a piece of work runs in.

Namespaces partition a tenant (see architecture §11). Rather than threading a
namespace argument through every call, the active namespace is carried in a
context variable: the executor sets it from the run, the trigger service from
the trigger, the API from the request. Store methods that read or write
namespaced data use it unless given an explicit ``namespace``.

Reads of resources fall back to the tenant-wide ``shared`` namespace, so a
platform team can publish connections, gateways and skills for everyone.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_NAMESPACE = "default"
SHARED_NAMESPACE = "shared"
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Kinds that describe the tenant rather than a team; always stored in `shared`.
# Gateways and model policies are managed centrally by tenant admins.
TENANT_SCOPED_KINDS = frozenset({"Identity", "Blueprint", "Gateway", "ModelPolicy"})

_current: ContextVar[str] = ContextVar("shoal_namespace", default=DEFAULT_NAMESPACE)


def current_namespace() -> str:
    return _current.get()


def resolve_namespace(namespace: str | None, kind: str | None = None) -> str:
    if kind in TENANT_SCOPED_KINDS:
        return SHARED_NAMESPACE
    return namespace or _current.get()


@contextmanager
def use_namespace(namespace: str | None) -> Iterator[str]:
    token = _current.set(namespace or DEFAULT_NAMESPACE)
    try:
        yield _current.get()
    finally:
        _current.reset(token)
