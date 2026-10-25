"""Publishing wake-ups to the dispatch queue, and turning received ones into claimed runs.

See ``shoal.runtime.queues`` for the model. With an external queue:

- Whenever the store moves a built-in run's ``next_wake_at`` (a run starts, is
  woken, parks until a timer, backs off after an error), it hands the wake-up to
  ``Dispatch.publish`` after the transaction commits. Wake-ups are sent in the
  background, batched while a send is in flight, so a checkpoint never waits on
  the queue; a failed send is logged and the database sweep finds the run.
- Workers receive wake-ups (``Dispatch.consume``) and claim those runs with a
  lease, taking turns across tenants and stopping at each tenant's cap, exactly
  as the database sweep does. A message is acked once its run is claimed, or
  when there is nothing to do: the run finished, another worker holds it (that
  worker publishes its next wake-up when it parks), or it was woken again since
  (that wake published its own message). A run that is due but over its tenant's
  cap goes back on the queue for a few seconds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from shoal.core import clock
from shoal.core.models import RunStatus
from shoal.runtime.queues import Delivery, DispatchQueue, QueueError, Wakeup

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

MAX_RETRY_DELAY = 30.0  # seconds a run over its tenant's cap waits before its message comes back
SAME_TIME = timedelta(milliseconds=1)


class Dispatch:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.queue: DispatchQueue = engine.plugin("dispatch_queues", engine.settings.dispatch_queue)
        self._pending: dict[tuple[str, Any], Wakeup] = {}
        self._flusher: asyncio.Task[None] | None = None
        self._publish_failing = False
        self._receive_failing = False

    @property
    def external(self) -> bool:
        return bool(self.queue.external)

    @property
    def failing(self) -> bool:
        """The last send or receive failed: workers poll the database meanwhile."""
        return self._publish_failing or self._receive_failing

    def receiving(self, error: Exception | None) -> None:
        """A worker's receive succeeded (``None``) or failed."""
        if error is not None and not self._receive_failing:
            log.warning("cannot receive from the %s queue (%s); polling the database until it recovers",
                        self.queue.name, error)
        elif error is None and self._receive_failing:
            log.info("receiving from the %s queue works again", self.queue.name)
        self._receive_failing = error is not None

    async def start(self) -> None:
        await self.queue.start()
        if self.external and self.publish not in self.engine.store.due_hooks:
            self.engine.store.due_hooks.append(self.publish)

    async def close(self) -> None:
        await self.flush()
        await self.queue.close()

    # ------------------------------------------------------------------ producer

    async def publish(self, wakeups: Sequence[Wakeup]) -> None:
        """Queue wake-ups for sending; returns at once (sending happens in the background)."""
        if not self.external:
            return
        for wakeup in wakeups:
            self._pending[(wakeup.run_id, wakeup.due_at)] = wakeup
        if self._pending and (self._flusher is None or self._flusher.done()):
            self._flusher = asyncio.create_task(self._flush_loop())

    async def flush(self) -> None:
        """Wait until every queued wake-up was sent (or failed)."""
        if self._flusher is not None:
            await asyncio.gather(self._flusher, return_exceptions=True)

    async def _flush_loop(self) -> None:
        while self._pending:
            batch = list(self._pending.values())[: self.queue.batch_size * 10]
            for wakeup in batch:
                self._pending.pop((wakeup.run_id, wakeup.due_at), None)
            try:
                await self.queue.publish(batch)
            except Exception as exc:  # the database sweep finds these runs anyway
                if not self._publish_failing:
                    log.warning("could not publish %d wake-up(s) to the %s queue: %s", len(batch), self.queue.name, exc)
                self._publish_failing = True
            else:
                if self._publish_failing:
                    log.info("publishing wake-ups to the %s queue works again", self.queue.name)
                self._publish_failing = False

    # ------------------------------------------------------------------ consumer

    async def consume(self, worker_id: str, lanes: Sequence[str], *, limit: int, wait: float) -> list[str]:
        """Receive up to ``limit`` wake-ups for ``lanes`` and claim their runs; returns the claimed run ids."""
        deliveries = await self.queue.receive(lanes, min(limit, self.queue.batch_size), wait)
        if not deliveries:
            return []
        return await self.settle(worker_id, lanes, deliveries, limit=limit)

    async def settle(self, worker_id: str, lanes: Sequence[str], deliveries: Sequence[Delivery], *, limit: int) -> list[str]:
        """Claim what is due and ack, retry or re-publish every delivery."""
        store = self.engine.store
        mine = [d for d in deliveries if d.wakeup.lane in lanes]
        run_ids = list(dict.fromkeys(d.wakeup.run_id for d in mine))
        claimed: list[str] = []
        if run_ids:
            claimed = await store.claim_runs(
                worker_id, limit=limit, lease=timedelta(seconds=self.engine.settings.lease_seconds),
                tenant_limits=self.engine.quotas.concurrency_limits, lanes=lanes, run_ids=run_ids,
            )
        taken = set(claimed)
        views = await store.dispatch_view([r for r in run_ids if r not in taken])
        now = clock.now()
        ack: list[Delivery] = []
        release: list[Delivery] = []
        again: dict[float, list[Delivery]] = {}
        resend: dict[str, Wakeup] = {}  # copies of one wake-up that arrive together go out again as one
        for delivery in deliveries:
            wakeup = delivery.wakeup
            view = views.get(wakeup.run_id)
            if wakeup.lane not in lanes:
                release.append(delivery)  # a queue shared with another pool: let one of its workers have it
            elif wakeup.run_id in taken:
                ack.append(delivery)
            elif (view is None or view["orchestrator"] != "builtin" or RunStatus(view["status"]).terminal
                  or view["next_wake_at"] is None):
                ack.append(delivery)
            elif view["lease_expires_at"] is not None and view["lease_expires_at"] > now:
                ack.append(delivery)  # being advanced; its worker publishes the next wake-up when it parks
            elif view["next_wake_at"] > now:
                ack.append(delivery)
                if abs(view["next_wake_at"] - wakeup.due_at) < SAME_TIME:
                    resend[wakeup.run_id] = wakeup  # delivered early (longer than the queue's longest delay)
            else:  # due and free, but its tenant is at its cap (or the batch was full)
                again.setdefault(min(MAX_RETRY_DELAY, float(delivery.attempts)), []).append(delivery)
        try:
            if resend:
                await self.queue.publish(list(resend.values()))
            if ack:
                await self.queue.ack(ack)
            if release:
                await self.queue.retry(release, 0)
            for delay, items in again.items():
                await self.queue.retry(items, delay)
        except QueueError as exc:  # unacked messages come back after their visibility timeout
            log.warning("dispatch queue: %s", exc)
        return claimed
