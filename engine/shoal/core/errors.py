"""Error types shared across the engine and mapped to HTTP problems by the API."""

from __future__ import annotations

from typing import Any


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
        super().__init__(message, details={"owner": owner, "namespace": namespace, "connection": connection, "fields": fields})
        self.tenant_id, self.owner, self.namespace, self.connection, self.fields = tenant_id, owner, namespace, connection, fields

    @property
    def wait_key(self) -> str:
        return credential_key(self.tenant_id, self.owner or "", self.namespace, self.connection)


def credential_key(tenant_id: str, owner: str, namespace: str, connection: str) -> str:
    return f"credential:{tenant_id}:{owner}:{namespace}/{connection}"


class PluginError(ShoalError):
    code = "plugin_error"


class StepError(ShoalError):
    """Raised by step handlers. ``retryable`` controls the retry policy."""

    code = "step_failed"

    def __init__(self, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message, details=details)
        self.retryable = retryable
