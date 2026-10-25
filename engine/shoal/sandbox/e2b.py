"""E2B backend (cloud or self-hosted E2B, and Perplexity SPACE via its E2B compatibility wrapper).

Requires ``pip install 'shoal[e2b]'``. Profile ``config``:

* ``api_key_secret`` - tenant secret with the E2B API key (or ``E2B_API_KEY``)
* ``domain`` - E2B domain; point it at a SPACE ECW host to run on SPACE
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from shoal.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec, shell_quote

if TYPE_CHECKING:
    from shoal.engine import Engine


class E2BSandbox(Sandbox):
    def __init__(self, sandbox: Any, spec: SandboxSpec):
        self.sbx = sandbox
        self.id = sandbox.sandbox_id
        self.workdir = spec.workdir

    async def exec(self, command, *, cwd=None, env=None, timeout=None) -> ExecResult:  # type: ignore[no-untyped-def]
        from e2b.sandbox.commands.command_handle import CommandExitException

        cmd = command if isinstance(command, str) else " ".join(shell_quote(c) for c in command)
        started = time.monotonic()
        try:
            result = await self.sbx.commands.run(
                cmd, cwd=self.resolve(cwd) if cwd else self.workdir, envs=env or {}, timeout=timeout or 0
            )
            return ExecResult(result.exit_code, result.stdout, result.stderr, duration_seconds=time.monotonic() - started)
        except CommandExitException as exc:
            return ExecResult(exc.exit_code, exc.stdout, exc.stderr, duration_seconds=time.monotonic() - started)
        except TimeoutError:
            return ExecResult(124, "", "timed out", timed_out=True, duration_seconds=time.monotonic() - started)

    async def write_file(self, path: str, data: bytes | str) -> None:
        await self.sbx.files.write(self.resolve(path), data)

    async def read_file(self, path: str) -> bytes:
        try:
            return bytes(await self.sbx.files.read(self.resolve(path), format="bytes"))
        except Exception as exc:
            if "not found" in str(exc).lower():
                raise FileNotFoundError(path) from exc
            raise

    async def close(self) -> None:
        await self.sbx.kill()


class E2BBackend(SandboxBackend):
    name = "e2b"
    description = "E2B cloud or self-hosted sandboxes (also Perplexity SPACE through its E2B wrapper)."

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    async def _options(self, spec: SandboxSpec) -> dict[str, Any]:
        key = os.environ.get("E2B_API_KEY")
        if spec.config.get("api_key_secret"):
            key = await self.engine.secrets.get(spec.labels["shoal/tenant"], spec.config["api_key_secret"]) or key
        options: dict[str, Any] = {"api_key": key}
        if spec.config.get("domain"):
            options["domain"] = spec.config["domain"]
        return options

    async def create(self, spec: SandboxSpec) -> Sandbox:
        from e2b import AsyncSandbox

        sbx = await AsyncSandbox.create(
            template=spec.image or "base",
            timeout=spec.ttl_seconds,
            metadata=spec.labels,
            envs=spec.env,
            allow_internet_access=spec.network.egress != "deny",
            **await self._options(spec),
        )
        sandbox = E2BSandbox(sbx, spec)
        await sandbox.exec(f"mkdir -p {shell_quote(spec.workdir)}", cwd="/")
        return sandbox

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        from e2b import AsyncSandbox

        try:
            sbx = await AsyncSandbox.connect(sandbox_id, **await self._options(spec))
        except Exception:
            return None
        return E2BSandbox(sbx, spec)
