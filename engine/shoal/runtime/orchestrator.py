"""Orchestrators decide when and where a run is advanced; the executor decides what advancing means.

Every run records the orchestrator that drives it (``runs.orchestrator``), chosen
from ``SHOAL_ORCHESTRATOR`` when the run starts:

- ``builtin`` (default): the store is the scheduler. A run's ``next_wake_at`` says
  when it is due, workers claim due runs with a lease and advance them, and a wake
  (signal, approval, event, cancel, child run finishing) is an update of
  ``next_wake_at``. Needs nothing but the database.
- ``temporal`` (``pip install 'shoal[temporal]'``): each run is a Temporal workflow
  whose passes are activities (see ``shoal.runtime.temporal``).

Whatever drives a run, the store stays the source of truth for resources, runs,
steps, events and signals, and a pass over a run is always ``Executor.advance``.
Plugins can add orchestrators through the ``shoal.orchestrators`` entry point group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from shoal.engine import Engine
    from shoal.store.tables import Run


class Orchestrator(Protocol):
    name: str

    def setup(self, engine: Engine) -> None: ...

    async def start(self) -> None:
        """Connect (every process: API servers submit and wake runs too)."""

    async def close(self) -> None: ...

    async def submit(self, run: Run) -> None:
        """A run was created; it is due now. Must not raise: ``tick`` recovers runs a submit missed."""

    async def wake(self, run_id: str) -> None:
        """Something a run may be waiting on changed (called after every ``Store.wake_run``)."""

    async def serve(self) -> None:
        """Worker side: do the orchestrator's work until cancelled (runs next to the built-in claim loop)."""

    async def tick(self) -> None:
        """Scheduler side, every few seconds: repair anything a lost submit or wake left behind."""

    async def drain(self, timeout: float = 60.0) -> int:
        """Advance every run that is due now and wait until they are parked (tests, ``--wait``).

        Returns how many runs were advanced.
        """


class BuiltinOrchestrator:
    """The store-backed default; its claim loop lives in ``Engine.run_worker``."""

    name = "builtin"

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def submit(self, run: Run) -> None:
        return None  # created with next_wake_at = now

    async def wake(self, run_id: str) -> None:
        return None  # wake_run already moved next_wake_at

    async def serve(self) -> None:
        return None

    async def tick(self) -> None:
        return None

    async def drain(self, timeout: float = 60.0) -> int:
        return 0  # run_until_idle processes built-in runs itself
