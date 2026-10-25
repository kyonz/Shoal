"""Which orchestrator drives new runs, and every orchestrator that still drives runs.

The orchestrator comes from ``SHOAL_ORCHESTRATOR`` (and ``SHOAL_TEMPORAL_*``) unless
an instance admin set it in the instance console, which stores it as the
``orchestrator`` instance setting. A saved setting wins over the environment, and
every process (API servers and workers) notices a change within a few seconds,
so switching needs no restart:

- New runs go to the chosen orchestrator; each run keeps the one it started on.
- While a Temporal connection is saved, Temporal keeps driving its runs after a
  switch back to the built-in orchestrator, so nothing has to drain first.
- Credentials (a Temporal Cloud API key, mTLS certificate and key, a CA) are
  secrets in the ``shared`` namespace of a tenant the admin picks, resolved when
  the setting is loaded. Saving again picks up a rotated secret.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, model_validator

from shoal.core.errors import InvalidRequest
from shoal.core.scope import SHARED_NAMESPACE, use_namespace

if TYPE_CHECKING:
    from shoal.config import Settings
    from shoal.engine import Engine
    from shoal.runtime.orchestrator import Orchestrator
    from shoal.store.tables import InstanceSetting

log = logging.getLogger(__name__)

SETTING_KEY = "orchestrator"
REFRESH_SECONDS = 5.0


@dataclass(frozen=True)
class TemporalConnection:
    """Everything needed to reach a Temporal namespace (no temporalio import, so it is safe without the extra)."""

    address: str
    namespace: str = "default"
    task_queue: str = "shoal"
    api_key: str | None = None
    tls: bool = False
    client_cert: bytes | None = None
    client_key: bytes | None = None
    server_ca: bytes | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> TemporalConnection:
        def read(path: str | None) -> bytes | None:
            if not path:
                return None
            with open(path, "rb") as f:
                return f.read()

        return cls(
            address=settings.temporal_address,
            namespace=settings.temporal_namespace,
            task_queue=settings.temporal_task_queue,
            api_key=settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None,
            tls=settings.temporal_tls,
            client_cert=read(settings.temporal_tls_cert_path),
            client_key=read(settings.temporal_tls_key_path),
            server_ca=read(settings.temporal_tls_ca_path),
        )


class TemporalSettings(BaseModel):
    """A Temporal connection as the instance console saves it: secrets by name, never their values."""

    address: str = Field(min_length=1, max_length=500, description="Frontend host:port.")
    namespace: str = Field("default", min_length=1, max_length=200)
    task_queue: str = Field("shoal", min_length=1, max_length=200,
                            description="One per Shoal installation sharing a namespace.")
    tls: bool = Field(False, description="TLS without client certificates (implied by an API key or certificates).")
    secrets_tenant: str | None = Field(None, description="Tenant whose shared namespace holds the secrets below.")
    api_key_secret: str | None = Field(None, description="Secret holding a Temporal Cloud API key.")
    tls_cert_secret: str | None = Field(None, description="Secret holding the mTLS client certificate (PEM).")
    tls_key_secret: str | None = Field(None, description="Secret holding the mTLS client key (PEM).")
    tls_ca_secret: str | None = Field(None, description="Secret holding the CA for the server certificate (PEM).")

    @model_validator(mode="after")
    def _consistent(self) -> TemporalSettings:
        if bool(self.tls_cert_secret) != bool(self.tls_key_secret):
            raise ValueError("set both the client certificate and the client key secrets, or neither")
        if self.secret_names() and not self.secrets_tenant:
            raise ValueError("choose the tenant whose shared namespace holds the secrets")
        return self

    def secret_names(self) -> list[str]:
        return [n for n in (self.api_key_secret, self.tls_cert_secret, self.tls_key_secret, self.tls_ca_secret) if n]


class OrchestratorSetting(BaseModel):
    orchestrator: Literal["builtin", "temporal"] = "builtin"
    temporal: TemporalSettings | None = Field(
        None, description="Saved Temporal connection. Kept when switching back, so Temporal finishes its runs."
    )

    @model_validator(mode="after")
    def _temporal_needs_connection(self) -> OrchestratorSetting:
        if self.orchestrator == "temporal" and self.temporal is None:
            raise ValueError("connect a Temporal server before switching runs to Temporal")
        return self


async def resolve_connection(engine: Engine, temporal: TemporalSettings) -> TemporalConnection:
    """Look up the connection's secrets; a missing tenant or secret is an InvalidRequest naming it."""
    values: dict[str, str] = {}
    if temporal.secret_names():
        tenant = await engine.store.get_tenant_by_slug(temporal.secrets_tenant or "")
        if tenant is None:
            raise InvalidRequest(f"tenant '{temporal.secrets_tenant}' not found")
        with use_namespace(SHARED_NAMESPACE):
            for name in temporal.secret_names():
                value = await engine.secrets.get(tenant.id, name)
                if value is None:
                    raise InvalidRequest(f"secret '{name}' is not set in the shared namespace of tenant '{tenant.slug}'")
                values[name] = value

    def pem(name: str | None) -> bytes | None:
        return values[name].encode() if name else None

    return TemporalConnection(
        address=temporal.address,
        namespace=temporal.namespace,
        task_queue=temporal.task_queue,
        api_key=values[temporal.api_key_secret] if temporal.api_key_secret else None,
        tls=temporal.tls,
        client_cert=pem(temporal.tls_cert_secret),
        client_key=pem(temporal.tls_key_secret),
        server_ca=pem(temporal.tls_ca_secret),
    )


def temporal_orchestrator(engine: Engine, connection: TemporalConnection) -> Orchestrator:
    try:
        from shoal.runtime.temporal.orchestrator import TemporalOrchestrator
    except ImportError as exc:
        raise InvalidRequest("this Shoal installation lacks the temporal extra: pip install 'shoal[temporal]'") from exc
    orchestrator = TemporalOrchestrator(connection)
    orchestrator.setup(engine)
    return orchestrator


async def check_temporal(connection: TemporalConnection) -> dict[str, Any]:
    try:
        from shoal.runtime.temporal.orchestrator import check
    except ImportError:
        return {"ok": False, "detail": "this Shoal installation lacks the temporal extra: pip install 'shoal[temporal]'"}
    return await check(connection)


class Orchestrators:
    """The engine's orchestrators: ``current`` takes new runs, ``active`` all serve, tick, drain and get wakes."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.default: Orchestrator = engine.plugin("orchestrators", engine.settings.orchestrator)
        self.current: Orchestrator = self.default
        self.active: dict[str, Orchestrator] = {self.default.name: self.default}
        self.source: Literal["environment", "console"] = "environment"
        self.revision: int | None = None
        self.error: str | None = None
        self._checked: float | None = None
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.refresh(force=True)
        for orchestrator in self.active.values():
            await orchestrator.start()

    async def close(self) -> None:
        for orchestrator in self.active.values():
            await orchestrator.close()

    async def refresh(self, *, force: bool = False) -> None:
        """Pick up a setting saved in the instance console (by any process), at most every few seconds."""
        now = asyncio.get_running_loop().time()
        if not force and self._checked is not None and now - self._checked < REFRESH_SECONDS:
            return
        async with self._lock:
            self._checked = now
            row = await self.engine.store.get_instance_setting(SETTING_KEY)
            if (row.revision if row else None) == self.revision:
                return
            await self._apply(row)

    async def _apply(self, row: InstanceSetting | None) -> None:
        revision = row.revision if row else None
        try:
            current, active = await self._build(row)
        except Exception as exc:  # keep driving runs with what we have; the console shows the error
            if self.error is None or self.revision != revision:
                log.error("cannot apply the orchestrator setting (revision %s): %s", revision, exc)
            self.error, self.revision = str(exc), revision
            return
        previous = list(self.active.values())
        self.current, self.active = current, active
        self.source = "console" if row else "environment"
        self.revision, self.error = revision, None
        for orchestrator in previous:
            if all(orchestrator is not o for o in active.values()):
                await orchestrator.close()
        for orchestrator in active.values():
            if all(orchestrator is not o for o in previous):
                await orchestrator.start()
        self._changed.set()
        log.info("orchestrator for new runs: %s (%s); driving runs: %s", current.name, self.source, ", ".join(active))

    async def _build(self, row: InstanceSetting | None) -> tuple[Orchestrator, dict[str, Orchestrator]]:
        if row is None:
            return self.default, {self.default.name: self.default}
        setting = OrchestratorSetting.model_validate(row.value)
        builtin = self.engine.plugin("orchestrators", "builtin")
        temporal = None
        if setting.temporal is not None:
            temporal = temporal_orchestrator(self.engine, await resolve_connection(self.engine, setting.temporal))
        current = temporal if setting.orchestrator == "temporal" and temporal else builtin
        active = {current.name: current}
        if temporal is not None:
            active.setdefault(temporal.name, temporal)
        active.setdefault(self.default.name, self.default)  # runs started from the environment's orchestrator
        return current, active

    # ------------------------------------------------------------------ fan-out

    async def wake(self, run_id: str) -> None:
        await self.refresh()
        for orchestrator in list(self.active.values()):
            await orchestrator.wake(run_id)

    async def tick(self) -> None:
        await self.refresh()
        for orchestrator in list(self.active.values()):
            await orchestrator.tick()

    async def drain(self, timeout: float = 60.0) -> int:
        return sum([await o.drain(timeout) for o in list(self.active.values())])

    async def serve(self) -> None:
        """Serve every active orchestrator, restarting the set when the setting changes."""
        tasks: dict[int, asyncio.Task[Any]] = {}
        try:
            while True:
                wanted = {id(o): o for o in self.active.values()}
                for key in [k for k in tasks if k not in wanted]:
                    tasks.pop(key).cancel()
                for key, orchestrator in wanted.items():
                    if key not in tasks:
                        tasks[key] = asyncio.create_task(orchestrator.serve())
                self._changed.clear()
                try:
                    await asyncio.wait_for(self._changed.wait(), REFRESH_SECONDS)
                except TimeoutError:
                    await self.refresh()
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
