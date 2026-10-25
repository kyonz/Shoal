"""Sandbox contract implemented by every execution backend."""

from __future__ import annotations

import abc
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

    @abc.abstractmethod
    async def create(self, spec: SandboxSpec) -> Sandbox: ...

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        """Reattach to an existing sandbox (after a worker restart). None if gone."""
        return None

    async def destroy(self, sandbox_id: str, spec: SandboxSpec) -> None:
        sandbox = await self.attach(sandbox_id, spec)
        if sandbox is not None:
            await sandbox.close()


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
