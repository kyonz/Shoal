"""Docker backend: one container per sandbox (``pip install 'shoal[docker]'``)."""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from typing import Any

from shoal.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec, shell_quote

DEFAULT_IMAGE = "ghcr.io/kyonz/shoal-sandbox:latest"


class DockerSandbox(Sandbox):
    def __init__(self, container: Any, spec: SandboxSpec):
        self.container = container
        self.id = container.id
        self.workdir = spec.workdir

    async def exec(self, command, *, cwd=None, env=None, timeout=None) -> ExecResult:  # type: ignore[no-untyped-def]
        cmd = command if isinstance(command, str) else " ".join(shell_quote(c) for c in command)
        if timeout:
            cmd = f"timeout {int(timeout)} sh -lc {shell_quote(cmd)}"
        started = time.monotonic()
        code, (out, err) = await asyncio.to_thread(
            self.container.exec_run,
            ["sh", "-lc", cmd],
            workdir=self.resolve(cwd) if cwd else self.workdir,
            environment=env or {},
            demux=True,
        )
        return ExecResult(
            exit_code=code,
            stdout=(out or b"").decode(errors="replace"),
            stderr=(err or b"").decode(errors="replace"),
            timed_out=code == 124,
            duration_seconds=time.monotonic() - started,
        )

    async def write_file(self, path: str, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        full = self.resolve(path)
        directory, name = full.rsplit("/", 1)
        await self.exec(["mkdir", "-p", directory])
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(raw))
        await asyncio.to_thread(self.container.put_archive, directory or "/", buffer.getvalue())

    async def read_file(self, path: str) -> bytes:
        try:
            stream, _ = await asyncio.to_thread(self.container.get_archive, self.resolve(path))
        except Exception as exc:  # docker.errors.NotFound
            raise FileNotFoundError(path) from exc
        buffer = io.BytesIO(b"".join(stream))
        with tarfile.open(fileobj=buffer) as tar:
            member = tar.next()
            extracted = tar.extractfile(member) if member else None
            if extracted is None:
                raise IsADirectoryError(path)
            return extracted.read()

    async def close(self) -> None:
        await asyncio.to_thread(self.container.remove, force=True)


class DockerBackend(SandboxBackend):
    name = "docker"
    description = "One Docker container per sandbox on the worker's Docker host."

    def __init__(self) -> None:
        self._client: Any = None

    def client(self) -> Any:
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    async def create(self, spec: SandboxSpec) -> Sandbox:
        kwargs: dict[str, Any] = {
            "image": spec.image or DEFAULT_IMAGE,
            "command": ["sh", "-c", f"mkdir -p {shell_quote(spec.workdir)} && exec sleep infinity"],
            "detach": True,
            "labels": spec.labels,
            "environment": spec.env,
            "network_disabled": spec.network.egress == "deny",
        }
        if spec.memory_mb:
            kwargs["mem_limit"] = f"{spec.memory_mb}m"
        if spec.cpu:
            kwargs["nano_cpus"] = int(spec.cpu * 1e9)
        if spec.config.get("runtime"):
            kwargs["runtime"] = spec.config["runtime"]
        container = await asyncio.to_thread(self.client().containers.run, **kwargs)
        return DockerSandbox(container, spec)

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        try:
            container = await asyncio.to_thread(self.client().containers.get, sandbox_id)
        except Exception:
            return None
        return DockerSandbox(container, spec) if container.status == "running" else None
