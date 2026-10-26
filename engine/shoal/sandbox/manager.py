"""Resolves sandbox profiles and manages sandbox lifecycles for runs."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from shoal.core.errors import NotFound, StepError
from shoal.core.models import RunStatus, SandboxProfileSpec
from shoal.sandbox.base import Sandbox, SandboxBackend, SandboxSpec

if TYPE_CHECKING:
    from shoal.engine import Engine
    from shoal.store.tables import Run

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
            labels={"shoal/tenant": run.tenant_id, "shoal/run": run.id, "shoal/workflow": run.workflow_name},
            config=profile.config,
        )

    @asynccontextmanager
    async def acquire(self, run: Run, profile_name: str | None, *, env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        """Yield a sandbox for a step. Run-scoped sandboxes outlive the step."""
        name, profile = await self.profile(run.tenant_id, profile_name)
        backend = self.backend(profile.backend)
        if gap := backend.network_gap(profile.network):
            raise StepError(f"sandbox profile '{name}': {gap}; use a backend that can, or relax the profile's network policy")
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

    async def reap_orphans(self, *, min_age_seconds: float = 300) -> int:
        """Delete sandboxes that no live run owns, in every place a sandbox profile puts them (best effort)."""
        scopes: set[tuple[str, str]] = set()
        for row in await self.engine.store.list_resources_all_tenants("SandboxProfile"):
            spec = (row.document or {}).get("spec") or {}
            try:
                backend = self.backend(spec.get("backend", "local"))
            except Exception:  # an uninstalled backend plugin: nothing of ours can be running there
                continue
            scope = backend.reap_scope(spec.get("config") or {}, row.tenant_id)
            if scope:
                scopes.add((backend.name, scope))

        # Another deployment may share a namespace: only sandboxes labelled with one of our tenants are ours.
        tenants = {tenant.id for tenant in await self.engine.store.list_tenants()}
        states: dict[str, str] = {}

        async def run_state(tenant_id: str | None, run_id: str | None) -> str:
            if tenant_id not in tenants:
                return "foreign"
            if not run_id:
                return "over"
            if run_id not in states:
                try:
                    run = await self.engine.store.get_run(None, run_id)
                    states[run_id] = "over" if RunStatus(run.status).terminal else "active"
                except NotFound:
                    states[run_id] = "over"
            return states[run_id]

        removed = 0
        for backend_name, scope in sorted(scopes):
            try:
                count = await self.backend(backend_name).reap(scope, run_state, min_age_seconds=min_age_seconds)
            except Exception:  # pragma: no cover - best effort; the next sweep tries again
                log.exception("failed to reap %s sandboxes in %s", backend_name, scope)
                continue
            if count:
                log.info("reaped %d orphaned %s sandboxes in %s", count, backend_name, scope)
            removed += count
        return removed

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
