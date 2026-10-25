"""Sandbox contract implemented by every execution backend."""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from shoal.core.models import NetworkPolicy


class SandboxSpec(BaseModel):
    """What a step asks the backend for (resolved from a SandboxProfile)."""

    image: str | None = None
    cpu: float | None = None
    memory_mb: int | None = None
    disk_mb: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/workspace"
    ttl_seconds: int = 4 * 3600
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    labels: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self, limit: int = 20000) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": _tail(self.stdout, limit),
            "stderr": _tail(self.stderr, limit),
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
        }


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} characters truncated ...]\n" + text[-limit:]


class Sandbox(abc.ABC):
    id: str
    workdir: str
    supports_suspend: bool = False

    @abc.abstractmethod
    async def exec(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Run a command. A string runs through ``sh -lc``."""

    @abc.abstractmethod
    async def write_file(self, path: str, data: bytes | str) -> None: ...

    @abc.abstractmethod
    async def read_file(self, path: str) -> bytes: ...

    async def list_files(self, path: str = ".", max_depth: int = 2) -> list[str]:
        result = await self.exec(
            f"find {shell_quote(path)} -maxdepth {int(max_depth)} -not -path '*/.git/*' | head -n 500"
        )
        return [line for line in result.stdout.splitlines() if line]

    async def suspend(self) -> None:
        raise NotImplementedError

    async def resume(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def close(self) -> None: ...

    def resolve(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return f"{self.workdir.rstrip('/')}/{path}"


class SandboxBackend(abc.ABC):
    name: str
    description: str = ""
    config_schema: type[BaseModel] | None = None
    # Which parts of a profile's network policy the backend enforces: "deny" (no egress) and
    # "allow_hosts" (egress only to listed hosts). A profile asking for more is refused, never
    # run with a weaker policy than it states.
    network_controls: frozenset[str] = frozenset()

    def network_gap(self, network: NetworkPolicy) -> str | None:
        """Why this backend cannot enforce ``network``, or None when it can."""
        if network.allow_hosts and "allow_hosts" not in self.network_controls:
            return f"sandbox backend '{self.name}' cannot restrict egress to network.allow_hosts"
        if network.egress == "deny" and not network.allow_hosts and "deny" not in self.network_controls:
            return f"sandbox backend '{self.name}' cannot enforce network.egress: deny"
        return None

    @abc.abstractmethod
    async def create(self, spec: SandboxSpec) -> Sandbox: ...

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        """Reattach to an existing sandbox (after a worker restart). None if gone."""
        return None

    async def destroy(self, sandbox_id: str, spec: SandboxSpec) -> None:
        sandbox = await self.attach(sandbox_id, spec)
        if sandbox is not None:
            await sandbox.close()

    def reap_scope(self, config: dict[str, Any], tenant_id: str) -> str | None:
        """Where sandboxes for a profile with this ``config`` live, for :meth:`reap` (None: nothing to sweep)."""
        return None

    async def reap(
        self, scope: str, run_state: Callable[[str | None, str | None], Awaitable[str]], *, min_age_seconds: float
    ) -> int:
        """Delete sandboxes in ``scope`` that no live run owns (left behind by a crash or a ttl). Returns how many.

        ``run_state(tenant_id, run_id)`` from a sandbox's labels is ``active``, ``over`` (finished or unknown run),
        or ``foreign`` (not one of this deployment's tenants: never touch it).
        """
        return 0


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
