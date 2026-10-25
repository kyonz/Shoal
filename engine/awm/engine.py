"""The Engine wires the store, plugins and services together.

It is the single object the API server, workers, CLI and tests share::

    engine = Engine(Settings(database_url="sqlite+aiosqlite:///awm.db"))
    await engine.start()
    run = await engine.executor.start_run(tenant_id, "jira-bugfix", {...})
    await engine.run_until_idle()
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import timedelta
from typing import Any

import httpx

from awm.campaigns.service import CampaignService
from awm.config import Settings, get_settings
from awm.integrations.manager import IntegrationManager
from awm.plugins.registry import Registry
from awm.runtime.events import EventBus
from awm.runtime.executor import Executor
from awm.sandbox.manager import SandboxManager
from awm.secrets.providers import SecretManager
from awm.store.store import Store
from awm.store.tables import Run
from awm.triggers import TriggerService

log = logging.getLogger(__name__)


def _builtin_steps(registry: Registry) -> None:
    from awm.runtime.steps.agent import AgentHandler
    from awm.runtime.steps.builtin import (
        ActionHandler,
        ApprovalHandler,
        ScriptHandler,
        SetHandler,
        WaitHandler,
        WorkflowHandler,
    )

    for name, cls in {
        "agent": AgentHandler,
        "action": ActionHandler,
        "script": ScriptHandler,
        "wait": WaitHandler,
        "approval": ApprovalHandler,
        "workflow": WorkflowHandler,
        "set": SetHandler,
    }.items():
        registry.register("steps", name, cls, replace=False)


class Engine:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: Registry | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings or get_settings()
        self.store = Store(self.settings.database_url)
        self.registry = registry or Registry()
        _builtin_steps(self.registry)
        self.http_transport = http_transport
        self.worker_id = self.settings.worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._setup_done: set[int] = set()

        self.events = EventBus(self.store)
        self.secrets = SecretManager(self)
        self.sandboxes = SandboxManager(self)
        self.integrations = IntegrationManager(self)
        self.executor = Executor(self)
        self.triggers = TriggerService(self)
        self.campaigns = CampaignService(self)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = asyncio.Event()

    def plugin(self, group: str, name: str) -> Any:
        instance = self.registry.get(group, name)
        if id(instance) not in self._setup_done:
            setup = getattr(instance, "setup", None)
            if callable(setup):
                setup(self)
            self._setup_done.add(id(instance))
        return instance

    async def start(self) -> None:
        await self.store.create_all()

    async def close(self) -> None:
        self._stopping.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.store.close()

    # ------------------------------------------------------------------ hooks

    async def on_run_finished(self, run: Run) -> None:
        if run.parent_run_id:
            await self.store.wake_by_keys(run.tenant_id, [f"run:{run.id}"])
            await self.store.wake_run(run.parent_run_id)
        await self.campaigns.on_run_finished(run)

    # ------------------------------------------------------------------ work loops

    async def process_due(self, limit: int | None = None) -> int:
        """Claim and advance due runs once. Returns the number processed."""
        lease = timedelta(seconds=self.settings.lease_seconds)
        run_ids = await self.store.claim_runs(
            self.worker_id, limit=limit or self.settings.worker_concurrency, lease=lease
        )
        await asyncio.gather(*(self.executor.advance(run_id, self.worker_id) for run_id in run_ids))
        return len(run_ids)

    async def scheduler_tick(self) -> None:
        await self.triggers.tick_schedules()
        await self.campaigns.tick_due()

    async def run_until_idle(self, max_iterations: int = 1000) -> None:
        """Process everything that is due right now (tests, CLI ``--wait``)."""
        for _ in range(max_iterations):
            await self.scheduler_tick()
            if not await self.process_due():
                await self.scheduler_tick()
                if not await self.process_due():
                    return
        raise RuntimeError("run_until_idle did not settle")

    async def run_worker(self, *, scheduler: bool = True) -> None:
        """Long-running worker loop: claims runs and runs the scheduler."""
        log.info("worker %s started", self.worker_id)
        semaphore = asyncio.Semaphore(self.settings.worker_concurrency)
        lease = timedelta(seconds=self.settings.lease_seconds)
        last_tick = 0.0
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            try:
                if scheduler and loop.time() - last_tick > 5:
                    last_tick = loop.time()
                    await self.scheduler_tick()
                free = semaphore._value  # noqa: SLF001 - cheap capacity check
                claimed = []
                if free:
                    claimed = await self.store.claim_runs(self.worker_id, limit=free, lease=lease)
                for run_id in claimed:
                    await semaphore.acquire()
                    task = asyncio.create_task(self._advance(run_id, semaphore))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                if not claimed:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.settings.poll_interval_seconds)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("worker loop error")
                await asyncio.sleep(2)

    async def _advance(self, run_id: str, semaphore: asyncio.Semaphore) -> None:
        try:
            await self.executor.advance(run_id, self.worker_id)
        finally:
            semaphore.release()

    def start_background_worker(self) -> asyncio.Task[Any]:
        task = asyncio.create_task(self.run_worker())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
