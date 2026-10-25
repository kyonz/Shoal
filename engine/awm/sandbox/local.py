"""Local subprocess sandbox. For development and tests only: no isolation."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

from awm.core.ids import new_id
from awm.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec

_ROOT = Path(os.environ.get("AWM_LOCAL_SANDBOX_ROOT", Path(tempfile.gettempdir()) / "awm-sandboxes"))


class LocalSandbox(Sandbox):
    supports_suspend = True  # nothing to free; suspend/resume are no-ops

    def __init__(self, sandbox_id: str, root: Path, spec: SandboxSpec):
        self.id = sandbox_id
        self.root = root
        self.workdir = str(root / "workspace")
        self.env = dict(spec.env)
        Path(self.workdir).mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str) -> str:
        if path.startswith("/workspace"):
            path = self.workdir + path[len("/workspace") :]
        return super().resolve(path)

    async def exec(self, command, *, cwd=None, env=None, timeout=None) -> ExecResult:  # type: ignore[no-untyped-def]
        argv = ["/bin/sh", "-lc", command] if isinstance(command, str) else list(command)
        full_env = {**os.environ, **self.env, **(env or {}), "HOME": str(self.root), "AWM_WORKSPACE": self.workdir}
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.resolve(cwd) if cwd else self.workdir,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
            timed_out=timed_out,
            duration_seconds=time.monotonic() - started,
        )

    async def write_file(self, path: str, data: bytes | str) -> None:
        target = Path(self.resolve(path))
        raw = data.encode() if isinstance(data, str) else data

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

        await asyncio.to_thread(write)

    async def read_file(self, path: str) -> bytes:
        return await asyncio.to_thread(Path(self.resolve(path)).read_bytes)

    async def suspend(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class LocalBackend(SandboxBackend):
    name = "local"
    description = "Runs commands as local subprocesses in a temporary directory. Development only."

    async def create(self, spec: SandboxSpec) -> Sandbox:
        sandbox_id = new_id("sbx")
        root = _ROOT / sandbox_id
        root.mkdir(parents=True, exist_ok=True)
        return LocalSandbox(sandbox_id, root, spec)

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        root = _ROOT / sandbox_id
        if not root.exists():
            return None
        return LocalSandbox(sandbox_id, root, spec)
