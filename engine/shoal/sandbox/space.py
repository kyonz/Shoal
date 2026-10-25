"""Perplexity SPACE backend: Firecracker microVM sandboxes over the SPACE HTTP API.

https://github.com/perplexityai/SPACE

Lifecycle mutations are asynchronous operations that are polled; command
output streams over server-sent events from the data plane using a
per-command attachment token. Run-scoped sandboxes are *suspended* during long
waits (VM memory and disk are saved, compute is released) and woken on resume.

Profile ``config``:

* ``api_url`` / ``data_plane_url`` (or ``SPACE_API_URL`` / ``SPACE_DATA_PLANE_URL``)
* ``token_secret`` - tenant secret holding the API token (or ``SPACE_API_TOKEN``)
* ``runtime`` - default ``fc``
* ``read_chunk_bytes`` - read size for ``files:read`` (default 4 MiB)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from shoal.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec

if TYPE_CHECKING:
    from shoal.engine import Engine

FAILED_STATES = {"failed", "canceled", "expired", "deleted", "deleting"}


class SpaceError(RuntimeError):
    pass


class SpaceClient:
    def __init__(self, api_url: str, data_plane_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.api_url = api_url.rstrip("/")
        self.data_plane_url = data_plane_url.rstrip("/")
        self.token = token
        self.transport = transport

    def _client(self, base: str, token: str, timeout: float = 30) -> httpx.AsyncClient:
        # Never follow redirects with bearer tokens; never use env proxies for the data plane.
        return httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=False,
            transport=self.transport,
        )

    async def call(self, method: str, path: str, body: Any = None, *, idempotent: bool = False, raw: bool = False) -> Any:
        headers = {"Accept": "application/octet-stream" if raw else "application/json"}
        if idempotent:
            headers["Idempotency-Key"] = f"shoal-{uuid.uuid4().hex}"
        async with self._client(self.api_url, self.token) as client:
            response = await client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise SpaceError(f"SPACE {method} {path}: HTTP {response.status_code}: {response.text[:400]}")
        if raw:
            return response.content
        return response.json() if response.content else {}

    async def wait_state(self, path: str, target: str, timeout: float = 300) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            result = await self.call("GET", path)
            state = result.get("state")
            if state == target:
                return result
            if state in FAILED_STATES:
                raise SpaceError(f"{path} is {state}, expected {target}")
            if time.monotonic() > deadline:
                raise SpaceError(f"timed out waiting for {path} to reach {target}")
            await asyncio.sleep(1)

    async def stream_command(self, sandbox_id: str, command: dict[str, Any], timeout: float | None) -> ExecResult:
        started = time.monotonic()
        created = await self.call("POST", f"/v1/sandboxes/{sandbox_id}/commands", command, idempotent=True)
        attach = created["attach"]
        events_url = attach["events_url"]
        if events_url.startswith("http"):
            if not events_url.startswith(self.data_plane_url + "/"):
                raise SpaceError("unexpected events URL origin")
            events_url = events_url[len(self.data_plane_url) :]
        stdout, stderr = bytearray(), bytearray()
        exit_code: int | None = None
        async with self._client(self.data_plane_url, attach["token"], timeout=None) as client:  # type: ignore[arg-type]
            async with client.stream("GET", events_url, headers={"Accept": "text/event-stream"}) as response:
                if response.status_code >= 400:
                    raise SpaceError(f"SPACE events stream: HTTP {response.status_code}")
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if timeout is not None and time.monotonic() - started > timeout + 30:
                        break
                    if line == "":
                        if data_lines:
                            frame = json.loads("\n".join(data_lines))
                            data_lines = []
                            kind = frame.get("type")
                            if kind in ("stdout", "stderr"):
                                target = stdout if kind == "stdout" else stderr
                                target.extend(base64.b64decode(frame["data_base64"]))
                            elif kind == "process_exited":
                                exit_code = int(frame.get("exit_code", -1))
                                break
                            elif kind in ("error", "resync_required"):
                                raise SpaceError(f"command stream reported {kind}")
                        continue
                    field, _, value = line.partition(":")
                    if field == "data":
                        data_lines.append(value[1:] if value.startswith(" ") else value)
        timed_out = exit_code is None
        return ExecResult(
            exit_code=exit_code if exit_code is not None else 124,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            timed_out=timed_out,
            duration_seconds=time.monotonic() - started,
        )


class SpaceSandbox(Sandbox):
    supports_suspend = True

    def __init__(self, client: SpaceClient, sandbox_id: str, spec: SandboxSpec):
        self.client = client
        self.id = sandbox_id
        self.workdir = spec.workdir
        self.chunk = int(spec.config.get("read_chunk_bytes", 4 * 1024 * 1024))

    async def exec(self, command, *, cwd=None, env=None, timeout=None) -> ExecResult:  # type: ignore[no-untyped-def]
        argv = ["sh", "-lc", command] if isinstance(command, str) else list(command)
        body: dict[str, Any] = {"command": argv, "cwd": self.resolve(cwd) if cwd else self.workdir, "env": env or {}}
        if timeout:
            body["timeout"] = f"{int(timeout)}s"
        return await self.client.stream_command(self.id, body, timeout)

    async def write_file(self, path: str, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        await self.client.call(
            "POST",
            f"/v1/sandboxes/{self.id}/files:write",
            {"path": self.resolve(path), "data_base64": base64.b64encode(raw).decode(), "parents": True},
            idempotent=True,
        )

    async def read_file(self, path: str) -> bytes:
        out = bytearray()
        offset = 0
        while True:
            try:
                chunk = await self.client.call(
                    "POST",
                    f"/v1/sandboxes/{self.id}/files:read",
                    {"path": self.resolve(path), "offset": offset, "length": self.chunk},
                    raw=True,
                )
            except SpaceError as exc:
                if "404" in str(exc):
                    raise FileNotFoundError(path) from exc
                raise
            out.extend(chunk)
            if len(chunk) < self.chunk:
                return bytes(out)
            offset += len(chunk)

    async def suspend(self) -> None:
        op = await self.client.call("POST", f"/v1/sandboxes/{self.id}:suspend", idempotent=True)
        if op.get("operation_id"):
            await self.client.wait_state(f"/v1/operations/{op['operation_id']}", "succeeded")

    async def resume(self) -> None:
        op = await self.client.call("POST", f"/v1/sandboxes/{self.id}:wake", idempotent=True)
        if op.get("operation_id"):
            await self.client.wait_state(f"/v1/operations/{op['operation_id']}", "succeeded")
        await self.client.wait_state(f"/v1/sandboxes/{self.id}", "running")

    async def close(self) -> None:
        try:
            await self.client.call("DELETE", f"/v1/sandboxes/{self.id}", idempotent=True)
        except SpaceError as exc:
            if "404" not in str(exc):
                raise


class SpaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: str | None = Field(None, description="Control plane URL (default: SPACE_API_URL).")
    data_plane_url: str | None = Field(None, description="Data plane URL (default: SPACE_DATA_PLANE_URL).")
    token_secret: str | None = Field(None, description="Secret holding the API token (default: SPACE_API_TOKEN).")
    runtime: str = Field("fc", description="SPACE runtime, e.g. fc (Firecracker).")
    read_chunk_bytes: int = Field(4 * 1024 * 1024, gt=0, description="Read size for file downloads.")


class SpaceBackend(SandboxBackend):
    name = "space"
    description = "Perplexity SPACE: Firecracker microVM sandboxes with suspend/wake (self-hosted)."
    config_schema = SpaceConfig

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    async def _client(self, spec: SandboxSpec) -> SpaceClient:
        cfg = spec.config
        token = os.environ.get("SPACE_API_TOKEN")
        if cfg.get("token_secret"):
            token = await self.engine.secrets.get(spec.labels["shoal/tenant"], cfg["token_secret"]) or token
        if not token:
            raise SpaceError("SPACE API token not configured (config.token_secret or SPACE_API_TOKEN)")
        return SpaceClient(
            cfg.get("api_url") or os.environ.get("SPACE_API_URL", "http://127.0.0.1:8080"),
            cfg.get("data_plane_url") or os.environ.get("SPACE_DATA_PLANE_URL", "http://127.0.0.1:8090"),
            token,
            transport=self.engine.http_transport,
        )

    async def create(self, spec: SandboxSpec) -> Sandbox:
        client = await self._client(spec)
        resources = {
            k: v
            for k, v in {
                "vcpu": int(spec.cpu) if spec.cpu else None,
                "memory_mib": spec.memory_mb,
                "disk_mib": spec.disk_mb,
            }.items()
            if v
        }
        body: dict[str, Any] = {
            "template": spec.image or "seed:latest",
            "runtime": spec.config.get("runtime", "fc"),
            "name": f"shoal-{spec.labels.get('shoal/run', 'sandbox')}"[:63],
            "metadata": spec.labels,
            "env": spec.env,
        }
        if resources:
            body["resources"] = resources
        created = await client.call("POST", "/v1/sandboxes", body, idempotent=True)
        sandbox_id = created["sandbox_id"]
        if created.get("operation_id"):
            await client.wait_state(f"/v1/operations/{created['operation_id']}", "succeeded")
        await client.wait_state(f"/v1/sandboxes/{sandbox_id}", "running")
        sandbox = SpaceSandbox(client, sandbox_id, spec)
        await sandbox.exec(["mkdir", "-p", spec.workdir], cwd="/")
        return sandbox

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        client = await self._client(spec)
        try:
            info = await client.call("GET", f"/v1/sandboxes/{sandbox_id}")
        except SpaceError:
            return None
        if info.get("state") in FAILED_STATES:
            return None
        return SpaceSandbox(client, sandbox_id, spec)
