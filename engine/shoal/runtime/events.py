"""Run events: persisted (audit + UI history) and fanned out to SSE streams.

Events from this process wake its streams directly. On Postgres, every event
insert also sends ``NOTIFY shoal_run_events, <run id>``; API processes keep one
``LISTEN`` connection (``start_listening``) and wake the streams of that run, so
a stream only reads the database when there is something new. Without the
listener (SQLite, or while it reconnects) streams fall back to polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from shoal.store.store import Store
    from shoal.store.tables import Run


class EventBus:
    def __init__(self, store: Store):
        self.store = store
        self._waiters: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._listeners: list[Any] = []
        self._listen_task: asyncio.Task[None] | None = None
        self.listening = False  # True while the Postgres LISTEN connection is up

    @property
    def poll_seconds(self) -> float:
        """How often a stream re-reads the database when nothing woke it."""
        return 15.0 if self.listening else 2.0

    def start_listening(self) -> None:
        if self.store.is_sqlite or self._listen_task is not None:
            return
        self._listen_task = asyncio.create_task(self._listen())

    async def stop_listening(self) -> None:
        if self._listen_task is not None:
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listen_task
            self._listen_task = None
        self.listening = False

    def _wake(self, run_id: str) -> None:
        for waiter in list(self._waiters.get(run_id, ())):
            waiter.set()

    async def _listen(self) -> None:
        import asyncpg

        from shoal.store.store import EVENTS_CHANNEL

        dsn = self.store.direct_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        delay = 1.0
        while True:
            conn = None
            try:
                conn = await asyncpg.connect(dsn)
                await conn.add_listener(EVENTS_CHANNEL, lambda _c, _pid, _ch, run_id: self._wake(run_id))
                self.listening, delay = True, 1.0
                for waiters in list(self._waiters.values()):  # catch up on anything sent while disconnected
                    for waiter in list(waiters):
                        waiter.set()
                while not conn.is_closed():
                    await asyncio.sleep(5)
                    await conn.execute("SELECT 1")  # notices a dropped connection
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("run event listener disconnected (%s); streams poll until it is back", exc)
            finally:
                self.listening = False
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(Exception):
                        await conn.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    def add_listener(self, fn: Any) -> None:
        """``async fn(run, type, data, step_id)`` called for every event (metrics, webhooks)."""
        self._listeners.append(fn)

    async def emit(self, run: Run, type: str, data: dict[str, Any] | None = None, *, step_id: str | None = None) -> None:
        await self.store.add_event(run.tenant_id, run.id, type, data or {}, step_id=step_id)
        await self.notify(run, type, data, step_id=step_id)

    async def notify(self, run: Run, type: str, data: dict[str, Any] | None = None, *, step_id: str | None = None) -> None:
        """Tell waiters and listeners about an event already stored (e.g. in the caller's transaction)."""
        self._wake(run.id)
        for listener in self._listeners:
            try:
                await listener(run, type, data or {}, step_id)
            except Exception:  # pragma: no cover - listeners must not break runs
                pass

    async def wait(self, run_id: str, timeout: float) -> bool:
        """Wait for the next event on a run. Returns False on timeout."""
        event = asyncio.Event()
        self._waiters[run_id].add(event)
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self._waiters[run_id].discard(event)
            if not self._waiters[run_id]:
                self._waiters.pop(run_id, None)
