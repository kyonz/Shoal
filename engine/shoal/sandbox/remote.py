"""Generic remote backend speaking the small Shoal Remote Sandbox Protocol over HTTP.

Any provider can be bridged by implementing (see docs/sandbox-protocol.md):

    POST   /sandboxes                 {spec}                      -> {"id": "..."}
    GET    /sandboxes/{id}                                        -> 200 | 404
    POST   /sandboxes/{id}/exec       {command, cwd, env, timeout} -> {exit_code, stdout, stderr, timed_out}
    PUT    /sandboxes/{id}/files?path=...   (raw body)
    GET    /sandboxes/{id}/files?path=...   -> raw body | 404
    POST   /sandboxes/{id}/suspend | /resume   (optional; advertise with "suspend": true)
    DELETE /sandboxes/{id}

Profile ``config``: ``url``, ``token_secret`` (bearer token secret name).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from shoal.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec

if TYPE_CHECKING:
    from shoal.engine import Engine


class RemoteSandbox(Sandbox):
    def __init__(self, backend: RemoteBackend, sandbox_id: str, spec: SandboxSpec, info: dict[str, Any]):
        self.backend = backend
        self.spec = spec
        self.id = sandbox_id
        self.workdir = spec.workdir
        self.supports_suspend = bool(info.get("suspend"))

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with await self.backend.client(self.spec) as client:
            response = await client.request(method, f"/sandboxes/{self.id}{path}", **kwargs)
        return response

    async def exec(self, command, *, cwd=None, env=None, timeout=None) -> ExecResult:  # type: ignore[no-untyped-def]
        started = time.monotonic()
        response = await self._call(
            "POST",
            "/exec",
            json={"command": command, "cwd": self.resolve(cwd) if cwd else self.workdir, "env": env or {}, "timeout": timeout},
            timeout=(timeout or 600) + 30,
        )
        response.raise_for_status()
        data = response.json()
        return ExecResult(
            exit_code=int(data.get("exit_code", -1)),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            timed_out=bool(data.get("timed_out")),
            duration_seconds=time.monotonic() - started,
        )

    async def write_file(self, path: str, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        (await self._call("PUT", "/files", params={"path": self.resolve(path)}, content=raw)).raise_for_status()

    async def read_file(self, path: str) -> bytes:
        response = await self._call("GET", "/files", params={"path": self.resolve(path)})
        if response.status_code == 404:
            raise FileNotFoundError(path)
        response.raise_for_status()
        return response.content

    async def suspend(self) -> None:
        (await self._call("POST", "/suspend")).raise_for_status()

    async def resume(self) -> None:
        (await self._call("POST", "/resume")).raise_for_status()

    async def close(self) -> None:
        response = await self._call("DELETE", "")
        if response.status_code not in (200, 202, 204, 404):
            response.raise_for_status()


class RemoteBackend(SandboxBackend):
    name = "remote"
    description = "Any provider implementing the Shoal Remote Sandbox Protocol over HTTP."

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    async def client(self, spec: SandboxSpec) -> httpx.AsyncClient:
        headers = {}
        if spec.config.get("token_secret"):
            token = await self.engine.secrets.get(spec.labels["shoal/tenant"], spec.config["token_secret"])
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url=spec.config["url"].rstrip("/"), headers=headers, timeout=60, transport=self.engine.http_transport)

    async def create(self, spec: SandboxSpec) -> Sandbox:
        async with await self.client(spec) as client:
            response = await client.post("/sandboxes", json=spec.model_dump(mode="json"))
        response.raise_for_status()
        info = response.json()
        return RemoteSandbox(self, info["id"], spec, info)

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        async with await self.client(spec) as client:
            response = await client.get(f"/sandboxes/{sandbox_id}")
        if response.status_code != 200:
            return None
        return RemoteSandbox(self, sandbox_id, spec, response.json())
