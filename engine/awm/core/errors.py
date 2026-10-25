"""Error types shared across the engine and mapped to HTTP problems by the API."""

from __future__ import annotations

from typing import Any


class AWMError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(AWMError):
    status_code = 404
    code = "not_found"


class Conflict(AWMError):
    status_code = 409
    code = "conflict"


class InvalidRequest(AWMError):
    status_code = 422
    code = "invalid_request"


class Unauthorized(AWMError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AWMError):
    status_code = 403
    code = "forbidden"


class QuotaExceeded(AWMError):
    status_code = 429
    code = "quota_exceeded"


class PluginError(AWMError):
    code = "plugin_error"


class StepError(AWMError):
    """Raised by step handlers. ``retryable`` controls the retry policy."""

    code = "step_failed"

    def __init__(self, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message, details=details)
        self.retryable = retryable
