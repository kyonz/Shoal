"""Resolves sandbox profiles and manages sandbox lifecycles for runs."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from awm.core.models import SandboxProfileSpec
from awm.sandbox.base import Sandbox, SandboxBackend, SandboxSpec

if TYPE_CHECKING:
    from awm.engine import Engine
    from awm.store.tables import Run

log = logging.getLogger(__name__)


class SandboxManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._live: dict[tuple[str, str], Sandbox] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def profile(self, tenant_id: str, name: str | None) -> tuple[str, SandboxProfileSpec]:
        name = name or "default"
        resource = await self.engine.store.find_resource(tenant_id, "SandboxProfile", name)
        if resource is not None:
            return name, resource.spec  # type: ignore[attr-defined]
        if name == "default":
            return name, SandboxProfileSpec(backend=self.engine.settings.default_sandbox_backend)
        raise ValueError(f"sandbox profile '{name}' not found")

    def backend(self, name: str) -> SandboxBackend:
        return self.engine.plugin("sandboxes", name)

    @staticmethod
    def spec_for(profile: SandboxProfileSpec, run: Run, extra_env: dict[str, str] | None = None) -> SandboxSpec:
        return SandboxSpec(
            image=profile.image,
            cpu=profile.cpu,
            memory_mb=profile.memory_mb,
            disk_mb=profile.disk_mb,
            env={**profile.env, **(extra_env or {})},
            workdir=profile.workdir,
            ttl_seconds=int(profile.ttl.total_seconds()),
            network=profile.network,
            labels={"awm/tenant": run.tenant_id, "awm/run": run.id, "awm/workflow": run.workflow_name},
            config=profile.config,
        )

    @asynccontextmanager
    async def acquire(self, run: Run, profile_name: str | None, *, env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        """Yield a sandbox for a step. Run-scoped sandboxes outlive the step."""
        name, profile = await self.profile(run.tenant_id, profile_name)
        backend = self.backend(profile.backend)
        spec = self.spec_for(profile, run, env)
        if profile.scope == "step":
            sandbox = await backend.create(spec)
            try:
                yield sandbox
            finally:
                await _quiet_close(sandbox)
            return

        key = (run.id, name)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            sandbox = self._live.get(key)
            record = (run.state.get("sandboxes") or {}).get(name)
            if sandbox is None and record:
                sandbox = await backend.attach(record["id"], spec)
                if sandbox is not None and record.get("suspended") and sandbox.supports_suspend:
                    await sandbox.resume()
            if sandbox is None:
                sandbox = await backend.create(spec)
                await self.engine.events.emit(
                    run, "sandbox.created", {"profile": name, "backend": profile.backend, "sandbox_id": sandbox.id}
                )
            self._live[key] = sandbox
            await self._record(run, name, {"id": sandbox.id, "backend": profile.backend, "suspended": False})
        yield sandbox

    async def _record(self, run: Run, name: str, record: dict[str, Any] | None) -> None:
        sandboxes = dict(run.state.get("sandboxes") or {})
        if record is None:
            sandboxes.pop(name, None)
        else:
            sandboxes[name] = record
        run.state = {**run.state, "sandboxes": sandboxes}
        await self.engine.store.update_run(run.id, state=run.state)

    async def suspend_run(self, run: Run) -> None:
        """Called when a run starts a long wait: park sandboxes that support it."""
        for name, record in (run.state.get("sandboxes") or {}).items():
            if record.get("suspended"):
                continue
            try:
                _, profile = await self.profile(run.tenant_id, name)
                if not profile.suspend_on_wait:
                    continue
                sandbox = self._live.pop((run.id, name), None) or await self.backend(record["backend"]).attach(
                    record["id"], self.spec_for(profile, run)
                )
                if sandbox is None or not sandbox.supports_suspend:
                    continue
                await sandbox.suspend()
                await self._record(run, name, {**record, "suspended": True})
                await self.engine.events.emit(run, "sandbox.suspended", {"profile": name, "sandbox_id": record["id"]})
            except Exception:  # pragma: no cover - best effort
                log.exception("failed to suspend sandbox %s for run %s", name, run.id)

    async def release_run(self, run: Run) -> None:
        """Destroy every run-scoped sandbox (run finished)."""
        for name, record in list((run.state.get("sandboxes") or {}).items()):
            sandbox = self._live.pop((run.id, name), None)
            try:
                if sandbox is not None:
                    await sandbox.close()
                else:
                    _, profile = await self.profile(run.tenant_id, name)
                    await self.backend(record["backend"]).destroy(record["id"], self.spec_for(profile, run))
            except Exception:  # pragma: no cover - best effort
                log.exception("failed to release sandbox %s for run %s", name, run.id)
            self._locks.pop((run.id, name), None)
        if run.state.get("sandboxes"):
            run.state = {**run.state, "sandboxes": {}}
            await self.engine.store.update_run(run.id, state=run.state)


async def _quiet_close(sandbox: Sandbox) -> None:
    try:
        await sandbox.close()
    except Exception:  # pragma: no cover
        log.exception("failed to close sandbox %s", sandbox.id)
