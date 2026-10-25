"""Run events: persisted (audit + UI history) and fanned out in-process for SSE."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shoal.store.store import Store
    from shoal.store.tables import Run


class EventBus:
    def __init__(self, store: Store):
        self.store = store
        self._waiters: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._listeners: list[Any] = []

    def add_listener(self, fn: Any) -> None:
        """``async fn(run, type, data, step_id)`` called for every event (metrics, webhooks)."""
        self._listeners.append(fn)

    async def emit(self, run: Run, type: str, data: dict[str, Any] | None = None, *, step_id: str | None = None) -> None:
        await self.store.add_event(run.tenant_id, run.id, type, data or {}, step_id=step_id)
        for waiter in list(self._waiters.get(run.id, ())):
            waiter.set()
        for listener in self._listeners:
            try:
                await listener(run, type, data or {}, step_id)
            except Exception:  # pragma: no cover - listeners must not break runs
                pass

    async def wait(self, run_id: str, timeout: float) -> None:
        """Wait for the next event on a run (or timeout; other processes are polled)."""
        event = asyncio.Event()
        self._waiters[run_id].add(event)
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self._waiters[run_id].discard(event)
            if not self._waiters[run_id]:
                self._waiters.pop(run_id, None)
