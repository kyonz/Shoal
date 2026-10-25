"""The Engine wires the store, plugins and services together.

It is the single object the API server, workers, CLI and tests share::

    engine = Engine(Settings(database_url="sqlite+aiosqlite:///shoal.db"))
    await engine.start()
    run = await engine.executor.start_run(tenant_id, "jira-bugfix", {...})
    await engine.run_until_idle()
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta
from typing import Any

import httpx

from shoal.auth.directory import Directory
from shoal.auth.oauth import OAuthFlows
from shoal.auth.oidc import OIDCService
from shoal.auth.rbac import Authorizer
from shoal.auth.user_credentials import UserCredentials
from shoal.auth.workload import WorkloadIssuer
from shoal.blueprints import BlueprintService
from shoal.campaigns.service import CampaignService
from shoal.cloud import CloudIdentities
from shoal.config import Settings, get_settings
from shoal.core import clock
from shoal.decisions.service import DecisionService
from shoal.evals.service import EvalService
from shoal.federation import FederationHub, FederationMember
from shoal.identity import IdentityService
from shoal.integrations.manager import IntegrationManager
from shoal.integrations.remote import register_remote_integration
from shoal.llm.router import ModelRouter
from shoal.plugins.registry import Registry
from shoal.runtime.events import EventBus
from shoal.runtime.executor import Executor
from shoal.runtime.signals import Signals
from shoal.sandbox.manager import SandboxManager
from shoal.secrets.providers import SecretManager
from shoal.store.store import Store
from shoal.store.tables import Run
from shoal.triggers import TriggerService

log = logging.getLogger(__name__)


def _builtin_steps(registry: Registry) -> None:
    from shoal.runtime.steps.agent import AgentHandler
    from shoal.runtime.steps.builtin import (
        ActionHandler,
        ApprovalHandler,
        ScriptHandler,
        SetHandler,
        WaitHandler,
        WorkflowHandler,
    )
    from shoal.runtime.steps.decide import DecideHandler, GradeHandler

    for name, cls in {
        "agent": AgentHandler,
        "action": ActionHandler,
        "script": ScriptHandler,
        "wait": WaitHandler,
        "approval": ApprovalHandler,
        "workflow": WorkflowHandler,
        "set": SetHandler,
        "decide": DecideHandler,
        "grade": GradeHandler,
    }.items():
        registry.register("steps", name, cls, replace=False)
    from shoal.agents.providers.openai_compat import OpenAICompatibleProvider
    from shoal.decisions.jev import JevProvider
    from shoal.decisions.llm import LLMDecisionProvider

    registry.register("providers", "openai-compatible", OpenAICompatibleProvider, replace=False)
    registry.register("decision_providers", "llm", LLMDecisionProvider, replace=False)
    registry.register("decision_providers", "jev", JevProvider, replace=False)
    from shoal.evals.sinks.braintrust import BraintrustSink
    from shoal.evals.sinks.langfuse import LangfuseSink

    registry.register("eval_sinks", "braintrust", BraintrustSink, replace=False)
    registry.register("eval_sinks", "langfuse", LangfuseSink, replace=False)
    from shoal.cloud.aws import AwsProvider

    registry.register("credentials", "aws", AwsProvider, replace=False)


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
        self._last_audit_prune: datetime | None = None

        self.directory = Directory(self.store)
        self.authz = Authorizer(self)
        self.oidc = OIDCService(self)
        self.events = EventBus(self.store)
        self.secrets = SecretManager(self, self.settings.secret_providers)
        self.sandboxes = SandboxManager(self)
        self.integrations = IntegrationManager(self)
        self.identities = IdentityService(self)
        self.workload = WorkloadIssuer(self.settings)
        self.cloud = CloudIdentities(self)
        self.models = ModelRouter(self)
        self.decisions = DecisionService(self)
        self.executor = Executor(self)
        self.triggers = TriggerService(self)
        self.campaigns = CampaignService(self)
        self.blueprints = BlueprintService(self)
        self.signals = Signals(self)
        self.user_credentials = UserCredentials(self)
        self.oauth = OAuthFlows(self)
        self.federation = FederationHub(self)
        self.federation_member = FederationMember(self)
        self.evals = EvalService(self)
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
        for remote in self.settings.remote_integrations:
            try:
                await register_remote_integration(
                    self.registry, remote.url, remote.token.get_secret_value() if remote.token else None,
                    transport=self.http_transport,
                )
            except Exception:  # one unreachable service must not stop the engine
                log.exception("could not register remote integration %s", remote.url)

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
        await self.evals.on_run_finished(run)

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
        await self.evals.tick_due()
        await self._prune_audit()

    async def _prune_audit(self) -> None:
        """Apply audit retention at most once an hour."""
        days = self.settings.audit_retention_days
        now = clock.now()
        if not days or (self._last_audit_prune and now - self._last_audit_prune < timedelta(hours=1)):
            return
        self._last_audit_prune = now
        await self.store.prune_audit(now - timedelta(days=days))
        await self.federation.prune(now - timedelta(hours=1))  # forwarded calls are only kept briefly

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
