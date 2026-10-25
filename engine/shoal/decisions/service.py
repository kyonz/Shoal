"""Decision service: resolves the provider plugin and its credentials, and times calls."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from shoal.core.errors import StepError
from shoal.core.models import DecisionQuestion
from shoal.decisions.base import DecisionRequest, DecisionResult

if TYPE_CHECKING:
    from shoal.engine import Engine
    from shoal.secrets.providers import Redactor


class DecisionService:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def decide(
        self,
        tenant_id: str,
        state: Any,
        questions: dict[str, DecisionQuestion],
        *,
        provider: str | None = None,
        config: dict[str, Any] | None = None,
        redactor: Redactor | None = None,
    ) -> DecisionResult:
        name = provider or self.engine.settings.default_decision_provider
        plugin = self.engine.plugin("decision_providers", name)
        resolved = dict(config or {})
        secret_name = resolved.pop("api_key_secret", None)
        if secret_name:
            value = await self.engine.secrets.get(tenant_id, secret_name)
            if value is None:
                raise StepError(f"secret '{secret_name}' (decision provider api key) is not set")
            resolved["api_key"] = value
            if redactor is not None:
                redactor.add(value)
        started = time.monotonic()
        result: DecisionResult = await plugin.decide(DecisionRequest(tenant_id, state, questions, resolved))
        result.latency_ms = (time.monotonic() - started) * 1000
        missing = set(questions) - set(result.answers)
        if missing:
            raise StepError(f"decision provider '{name}' did not answer: {', '.join(sorted(missing))}", retryable=True)
        return result
