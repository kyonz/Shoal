"""Temporal orchestrator: each Shoal run is a Temporal workflow, each pass over it an activity.

What Temporal takes over from the built-in orchestrator is *scheduling*: durable
timers for timed waits, polls and retries; ``wake`` signals for everything that
wakes a run (Shoal signals, approvals, integration events, cancels, child runs);
retrying a pass whose worker died; history and visibility in the Temporal UI.

What stays in Shoal's store is everything else, so the API, UI, CLI and
campaigns work the same: resources, the run record and its pinned workflow
version, step state, events, signals and approvals. A pass is the same
``Executor.advance`` the built-in workers run, under the same run lease, so steps
of one run never execute on two workers at once and run-scoped sandboxes behave
identically.

Every way of reaching a run (start, wake, repair) is a signal-with-start of the
workflow ``shoal-run-<run id>``, so a run whose start was missed (Temporal was
down when it was created) is picked up by ``tick`` once it has been due for
``SHOAL_TEMPORAL_RECONCILE_AFTER_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from temporalio import activity
from temporalio.client import Client, TLSConfig
from temporalio.exceptions import ApplicationError

from shoal.core import clock
from shoal.core.errors import NotFound
from shoal.core.models import RunStatus
from shoal.runtime.temporal.workflow import ADVANCE, CANCEL, WAKE, RunWorkflow, workflow_id

if TYPE_CHECKING:
    from temporalio.worker import WorkerDeploymentConfig

    from shoal.config import Settings
    from shoal.engine import Engine
    from shoal.store.tables import Run

log = logging.getLogger(__name__)


def _read(path: str | None) -> bytes | None:
    return Path(path).read_bytes() if path else None


def tls_config(settings: Settings) -> bool | TLSConfig:
    if settings.temporal_tls_cert_path or settings.temporal_tls_ca_path:
        return TLSConfig(
            client_cert=_read(settings.temporal_tls_cert_path),
            client_private_key=_read(settings.temporal_tls_key_path),
            server_root_ca_cert=_read(settings.temporal_tls_ca_path),
        )
    return settings.temporal_tls or settings.temporal_api_key is not None


async def connect(settings: Settings) -> Client:
    api_key = settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None
    return await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace, api_key=api_key, tls=tls_config(settings),
    )


class Activities:
    """The only code that touches Shoal's store or calls out (models, tools, sandboxes, integrations)."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @activity.defn(name=ADVANCE)
    async def advance(self, run_id: str) -> dict[str, Any]:
        return await self._heartbeating(self._advance(run_id))

    @activity.defn(name=CANCEL)
    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self._heartbeating(self._cancel(run_id))

    async def _advance(self, run_id: str) -> dict[str, Any]:
        engine = self.engine
        try:
            run = await engine.store.get_run(None, run_id)
            if not RunStatus(run.status).terminal:
                owner = f"temporal:{engine.worker_id}"
                if not await engine.store.lease_run(run_id, owner, timedelta(seconds=engine.settings.lease_seconds)):
                    raise ApplicationError(f"run {run_id} is being advanced elsewhere", type="RunLeased")
                await engine.executor.advance(run_id, owner)
                run = await engine.store.get_run(None, run_id)
        except NotFound:
            return {"done": True, "status": "deleted", "wake_in": None}
        if RunStatus(run.status).terminal:
            return {"done": True, "status": run.status, "wake_in": None}
        wake_in = (run.next_wake_at - clock.now()).total_seconds() if run.next_wake_at else None
        return {"done": False, "status": run.status, "wake_in": wake_in}

    async def _cancel(self, run_id: str) -> dict[str, Any]:
        with contextlib.suppress(NotFound):
            run = await self.engine.store.get_run(None, run_id)
            await self.engine.executor.request_cancel(run.tenant_id, run_id)
        return await self._advance(run_id)

    @staticmethod
    async def _heartbeating(work: Any) -> Any:
        info = activity.info()
        every = (info.heartbeat_timeout.total_seconds() / 3) if info.heartbeat_timeout else 10.0

        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(every)

        task = asyncio.create_task(beat())
        try:
            return await work
        finally:
            task.cancel()


class TemporalOrchestrator:
    name = "temporal"

    def __init__(self) -> None:
        self.client: Client | None = None
        self._poked: dict[str, datetime] = {}

    def setup(self, engine: Engine) -> None:
        self.engine = engine
        self.settings = engine.settings

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Connect; while Temporal is unreachable, runs are still created and are picked up once it is back."""
        if self.client is None:
            try:
                self.client = await connect(self.settings)
            except Exception as exc:
                log.warning("cannot reach Temporal at %s: %s", self.settings.temporal_address, exc)

    async def close(self) -> None:
        self.client = None

    def worker(self) -> Any:
        from temporalio.worker import Worker

        assert self.client is not None
        activities = Activities(self.engine)
        return Worker(
            self.client,
            task_queue=self.settings.temporal_task_queue,
            workflows=[RunWorkflow],
            activities=[activities.advance, activities.cancel],
            max_concurrent_activities=self.settings.worker_concurrency,
            identity=self.engine.worker_id,
            deployment_config=self._deployment(),
        )

    async def serve(self) -> None:
        while True:
            try:
                await self.start()
                if self.client is None:
                    raise ConnectionError(f"Temporal at {self.settings.temporal_address} is unreachable")
                log.info("temporal worker polling task queue %s on %s", self.settings.temporal_task_queue,
                         self.settings.temporal_address)
                await self.worker().run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("temporal worker stopped; restarting in 5s")
                await asyncio.sleep(5)

    def _deployment(self) -> WorkerDeploymentConfig | None:
        if not self.settings.temporal_build_id:
            return None
        from temporalio.common import VersioningBehavior
        from temporalio.worker import WorkerDeploymentConfig, WorkerDeploymentVersion

        behavior = {"auto-upgrade": VersioningBehavior.AUTO_UPGRADE, "pinned": VersioningBehavior.PINNED}
        return WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=self.settings.temporal_deployment_name or self.settings.temporal_task_queue,
                build_id=self.settings.temporal_build_id,
            ),
            use_worker_versioning=True,
            default_versioning_behavior=behavior[self.settings.temporal_versioning_behavior],
        )

    # ------------------------------------------------------------------ reaching runs

    async def submit(self, run: Run) -> None:
        await self._poke(run)

    async def wake(self, run_id: str) -> None:
        try:
            run = await self.engine.store.get_run(None, run_id)
        except NotFound:
            return
        if run.orchestrator == self.name and not RunStatus(run.status).terminal:
            await self._poke(run)

    async def _poke(self, run: Run) -> bool:
        """Signal the run's workflow, starting it if it is not running."""
        try:
            await self.start()
            if self.client is None:
                return False
            await self.client.start_workflow(
                RunWorkflow.run,
                args=[run.id, {"pass_timeout": self.settings.temporal_pass_timeout_seconds,
                               "heartbeat_timeout": self.settings.lease_seconds}],
                id=workflow_id(run.id),
                task_queue=self.settings.temporal_task_queue,
                start_signal=WAKE,
                static_summary=f"{run.workflow_name} v{run.workflow_version}",
                memo={"tenant": run.tenant_id, "namespace": run.namespace, "workflow": run.workflow_name,
                      "version": run.workflow_version},
            )
        except Exception as exc:  # the store keeps the run due; tick() retries
            log.warning("could not reach the Temporal workflow of run %s: %s", run.id, exc)
            return False
        self._poked[run.id] = clock.now()
        return True

    async def tick(self) -> None:
        """Start or signal workflows of runs that have been due for a while (a missed start or wake)."""
        now = clock.now()
        grace = timedelta(seconds=self.settings.temporal_reconcile_after_seconds)
        for run in await self.engine.store.due_runs(self.name, now - grace):
            leased = run.lease_expires_at is not None and run.lease_expires_at > now
            last = self._poked.get(run.id)
            if leased or (last is not None and now - last < grace):
                continue
            log.info("run %s is overdue; signalling its Temporal workflow", run.id)
            await self._poke(run)
        if len(self._poked) > 10_000:
            self._poked = {k: v for k, v in self._poked.items() if now - v < grace}

    async def drain(self, timeout: float = 60.0) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        poked: dict[str, datetime] = {}  # run id -> its updated_at when we signalled it
        seen: set[str] = set()
        while True:
            now = clock.now()
            due = await self.engine.store.due_runs(self.name, now, include_leased=True)
            if not due:
                return len(seen)
            for run in due:
                seen.add(run.id)
                leased = run.lease_expires_at is not None and run.lease_expires_at > now
                if not leased and not RunStatus(run.status).terminal and poked.get(run.id) != run.updated_at:
                    poked[run.id] = run.updated_at
                    await self._poke(run)
            if loop.time() > deadline:
                raise RuntimeError(f"Temporal runs did not settle within {timeout}s: {[r.id for r in due]}")
            await asyncio.sleep(0.05)
