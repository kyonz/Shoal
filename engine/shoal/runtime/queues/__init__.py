"""Dispatch queues: how workers hear that a built-in run is due.

The ``runs`` table is always the source of truth. A run is due when its
``next_wake_at`` has passed, and a worker advances it only after claiming it
with a lease. A dispatch queue only carries wake-ups ("run X is due at T"), so a
duplicate, early, late or lost message is harmless: workers re-check the run in
the database before claiming it, and still sweep the database now and then for
due runs whose message went missing (``SHOAL_DISPATCH_SWEEP_SECONDS``).

- ``database`` (default): no separate queue. Workers poll the runs table every
  ``SHOAL_POLL_INTERVAL_SECONDS`` with ``SELECT ... FOR UPDATE SKIP LOCKED``.
- ``sqs``: Amazon SQS (``shoal.runtime.queues.sqs``). Workers long-poll SQS and
  sweep the database far less often, which takes the polling load off Postgres.

Plugins add more through the ``shoal.dispatch_queues`` entry point group: a class
with the methods of ``DispatchQueue`` below (Redis, Pub/Sub and RabbitMQ fit).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from shoal.engine import Engine


@dataclass(frozen=True)
class Wakeup:
    """A built-in run is due at ``due_at`` (now or later)."""

    run_id: str
    tenant_id: str
    lane: str
    due_at: datetime


@dataclass
class Delivery:
    """A wake-up a worker received; ``handle`` is the backend's own (an SQS receipt handle)."""

    wakeup: Wakeup
    handle: Any = None
    attempts: int = 1


class QueueError(Exception):
    """The queue service refused or failed a call (workers log it and fall back on the database sweep)."""


class DispatchQueue(Protocol):
    name: str
    external: bool
    """False only for ``database``: nothing is published and workers poll the runs table instead."""
    batch_size: int
    """Most messages one ``publish`` request or ``receive`` returns (SQS: 10)."""

    def setup(self, engine: Engine) -> None: ...

    async def start(self) -> None:
        """Connect and check the configuration (every process: API servers publish wake-ups too)."""

    async def close(self) -> None: ...

    def groups(self, lanes: Sequence[str]) -> list[tuple[str, ...]]:
        """Split a worker's lanes into groups that one ``receive`` serves (one group per queue)."""

    async def publish(self, wakeups: Sequence[Wakeup]) -> None:
        """Deliver each wake-up at or after its ``due_at``, at least once.

        A backend whose longest delay is short (SQS: 15 minutes) may deliver a far-off wake-up early;
        the worker sees the run is not due yet and publishes it again for the rest of the wait.
        Raises ``QueueError`` when nothing could be sent.
        """

    async def receive(self, lanes: Sequence[str], max_messages: int, wait: float) -> list[Delivery]:
        """Wake-ups for one group of lanes, waiting up to ``wait`` seconds for the first one.

        A received message stays hidden from other workers until it is acked or retried.
        """

    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        """Delete handled messages."""

    async def retry(self, deliveries: Sequence[Delivery], delay: float) -> None:
        """Hand messages back to be received again after ``delay`` seconds (0: straight away)."""


class DatabaseQueue:
    """The default: the runs table is the queue, and workers poll it."""

    name = "database"
    external = False
    batch_size = 1

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def groups(self, lanes: Sequence[str]) -> list[tuple[str, ...]]:
        return []

    async def publish(self, wakeups: Sequence[Wakeup]) -> None:
        return None  # the run's next_wake_at is the message

    async def receive(self, lanes: Sequence[str], max_messages: int, wait: float) -> list[Delivery]:
        return []

    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        return None

    async def retry(self, deliveries: Sequence[Delivery], delay: float) -> None:
        return None
