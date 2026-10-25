"""Step execution context and handler results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from awm.core import clock
from awm.core.expressions import render, truthy
from awm.core.models import StepBase, WorkflowSpec
from awm.secrets.providers import Redactor

if TYPE_CHECKING:
    from awm.engine import Engine
    from awm.store.tables import Run, StepRun


@dataclass
class Completed:
    output: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, float] = field(default_factory=dict)


@dataclass
class Suspended:
    """The step is waiting. The run is woken at ``wake_at`` or by ``wait_keys``."""

    wake_at: datetime | None = None
    wait_keys: list[str] = field(default_factory=list)
    reason: str = "waiting"
    usage: dict[str, float] = field(default_factory=dict)


StepResult = Completed | Suspended


@dataclass
class StepContext:
    engine: Engine
    run: Run
    row: StepRun
    step: StepBase
    workflow: WorkflowSpec
    expr: dict[str, Any]
    redactor: Redactor

    @property
    def tenant_id(self) -> str:
        return self.run.tenant_id

    @property
    def state(self) -> dict[str, Any]:
        return self.row.state

    @property
    def attempt(self) -> int:
        return self.row.attempt

    @property
    def idempotency_key(self) -> str:
        return f"{self.run.id}:{self.step.id}:{self.row.attempt}"

    def now(self) -> datetime:
        return clock.now()

    def render(self, value: Any, **extra: Any) -> Any:
        return render(value, {**self.expr, **extra} if extra else self.expr)

    def truthy(self, expr: str, **extra: Any) -> bool:
        return truthy(expr, {**self.expr, **extra} if extra else self.expr)

    async def emit(self, type: str, data: dict[str, Any] | None = None) -> None:
        await self.engine.events.emit(self.run, type, self.redactor(data or {}), step_id=self.step.id)

    async def checkpoint(self) -> None:
        """Persist ``state`` mid-step (e.g. after creating an external resource)."""
        await self.engine.store.save_step(self.row)

    def connection_for(self, integration: str, explicit: str | None = None) -> str | None:
        if explicit:
            return explicit
        step_map = getattr(self.step, "connections", None) or {}
        return step_map.get(integration) or self.workflow.connections.get(integration)

    def deadline(self, key: str, after: timedelta) -> datetime:
        """A deadline stored in step state on first use (stable across resumes)."""
        stored = self.state.get(key)
        if stored:
            return datetime.fromisoformat(stored)
        value = self.now() + after
        self.state[key] = value.isoformat()
        return value


class StepHandler(Protocol):
    async def run(self, ctx: StepContext) -> StepResult: ...
