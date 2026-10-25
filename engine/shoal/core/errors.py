"""Error types shared across the engine and mapped to HTTP problems by the API."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote


class ShoalError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(ShoalError):
    status_code = 404
    code = "not_found"


class Conflict(ShoalError):
    status_code = 409
    code = "conflict"


class InvalidRequest(ShoalError):
    status_code = 422
    code = "invalid_request"


class Unauthorized(ShoalError):
    status_code = 401
    code = "unauthorized"


class Forbidden(ShoalError):
    status_code = 403
    code = "forbidden"


class QuotaExceeded(ShoalError):
    status_code = 429
    code = "quota_exceeded"


class CredentialRequired(ShoalError):
    """A connection needs the credential of the person a call acts for, and they have not connected it."""

    status_code = 428
    code = "credential_required"

    def __init__(self, message: str, *, tenant_id: str, owner: str | None, namespace: str, connection: str, fields: list[str]):
        fix = {"href": f"/settings/credentials?connection={namespace}/{connection}", "label": "Connect your account",
               "permission": None, "namespace": None}
        super().__init__(message, details={"owner": owner, "namespace": namespace, "connection": connection, "fields": fields,
                                           "fix": fix})
        self.tenant_id, self.owner, self.namespace, self.connection, self.fields = tenant_id, owner, namespace, connection, fields

    @property
    def wait_key(self) -> str:
        return credential_key(self.tenant_id, self.owner or "", self.namespace, self.connection)


def credential_key(tenant_id: str, owner: str, namespace: str, connection: str) -> str:
    return f"credential:{tenant_id}:{owner}:{namespace}/{connection}"


def kind_words(kind: str) -> str:
    """``ModelPolicy`` -> ``model policy``."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", kind).lower()


def fix_for(kind: str, name: str | None = None, namespace: str | None = None, *, exists: bool = False) -> dict[str, Any]:
    """Where in the web UI to set ``kind``/``name`` up, and the permission that takes.

    ``href`` is a UI path; ``permission`` (with ``namespace``) lets a client
    offer the fix to people who hold it and tell everyone else to ask an admin.
    """
    from shoal.core.models import RESOURCE_KINDS
    from shoal.core.scope import TENANT_SCOPED_KINDS, current_namespace

    namespace = namespace or current_namespace()
    words = kind_words(kind)
    if kind == "Secret":
        return {"href": f"/ns/{namespace}/secrets", "label": "Set the secret", "permission": "secrets:write", "namespace": namespace}
    if kind == "Setting":  # server configuration (SHOAL_*): whoever runs the server changes it, not anyone in the UI
        return {"href": "/docs/getting-started", "label": "Read about server settings", "permission": None, "namespace": None}
    exists = exists and bool(name)
    plural = getattr(RESOURCE_KINDS.get(kind), "plural", f"{kind.lower()}s")
    if kind in TENANT_SCOPED_KINDS:  # edited in the admin console
        path, permission, namespace = f"/admin/{plural}", f"{plural}:manage", None
    else:
        path, permission = f"/ns/{namespace}/{plural}", f"{plural}:write"
    if exists:
        href, label = f"{path}/{name}", f"Edit the {words}"
    elif name:  # the editor fills in the name from ?name=
        href, label = f"{path}/new?name={quote(name)}", f"Add the {name} {words}"
    else:
        href, label = f"{path}/new", f"Add {'an' if words[0] in 'aeiou' else 'a'} {words}"
    return {"href": href, "label": label, "permission": permission, "namespace": namespace}


class PluginError(ShoalError):
    code = "plugin_error"


class StepError(ShoalError):
    """Raised by step handlers. ``retryable`` controls the retry policy."""

    code = "step_failed"

    def __init__(self, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message, details=details)
        self.retryable = retryable


class MissingConfiguration(StepError):
    """Something has to be set up first: a gateway, a model policy, a connection, a secret...

    The problem's details name what is missing (``missing``) and where to fix it
    (``fix``: a web UI path and the permission that takes). It is a
    ``StepError`` too, so a step that hits it fails with the same fields in its
    error, without retrying.
    """

    status_code = 409
    code = "missing_configuration"

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        name: str | None = None,
        namespace: str | None = None,
        field: str | None = None,
        exists: bool = False,
        fix: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ):
        missing = {k: v for k, v in {"kind": kind, "name": name, "namespace": namespace, "field": field}.items() if v}
        fix = fix or fix_for(kind, name, namespace, exists=exists)
        super().__init__(message, retryable=False, details={**(details or {}), "code": self.code, "missing": missing, "fix": fix})
        self.kind, self.name, self.namespace = kind, name, namespace
