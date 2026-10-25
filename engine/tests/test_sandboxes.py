from __future__ import annotations

import asyncio
import base64
import json

import httpx

from shoal.sandbox.base import SandboxSpec
from shoal.sandbox.local import LocalBackend
from shoal.sandbox.space import SpaceBackend
from shoal.sandbox.wrap import parse_wrapped, wrapped_command, write_chunks


async def _run(argv: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return out.decode()


async def test_wrapped_exec_separates_streams(tmp_path):
    argv = wrapped_command("echo out; echo err >&2; exit 3", cwd=str(tmp_path), env={"X": "y z"}, timeout=None)
    result = parse_wrapped(await _run(argv), 0.1)
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n")
    argv = wrapped_command(["sh", "-c", "printf %s \"$X\""], cwd=str(tmp_path), env={"X": "it's"}, timeout=5)
    assert parse_wrapped(await _run(argv), 0.1).stdout == "it's"


async def test_write_chunks_roundtrip(tmp_path):
    target = tmp_path / "a" / "b.bin"
    data = bytes(range(256)) * 600
    for argv in write_chunks(str(target), data, chunk=4096):
        await _run(argv)
    assert target.read_bytes() == data


async def test_local_backend(tmp_path, monkeypatch):
    sandbox = await LocalBackend().create(SandboxSpec(env={"FOO": "bar"}))
    await sandbox.write_file("dir/x.txt", "hello")
    assert (await sandbox.read_file("/workspace/dir/x.txt")) == b"hello"
    result = await sandbox.exec("cat dir/x.txt; echo $FOO")
    assert result.stdout == "hellobar\n"
    assert (await sandbox.exec("sleep 5", timeout=0.2)).timed_out
    await sandbox.close()


class FakeSpace:
    """Just enough of the SPACE API: operations, sandboxes, SSE command streams, files, suspend/wake."""

    def __init__(self):
        self.sandboxes: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.commands: list[dict] = []
        self.idempotency_keys: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        assert request.headers["authorization"].startswith("Bearer ")
        if "idempotency-key" in request.headers:
            self.idempotency_keys.append(request.headers["idempotency-key"])
        if request.url.host == "space-data.test":
            assert request.headers["authorization"] == "Bearer attach-token"
            cmd = self.commands[-1]["command"]
            out = f"ran {' '.join(cmd)}".encode()
            frames = [
                {"type": "stdout", "data_base64": base64.b64encode(out).decode()},
                {"type": "stderr", "data_base64": base64.b64encode(b"warn").decode()},
                {"type": "process_exited", "exit_code": 0},
            ]
            text = "".join(f"id: {i}\ndata: {json.dumps(f)}\n\n" for i, f in enumerate(frames))
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        if path == "/v1/sandboxes" and method == "POST":
            self.sandboxes["sbx_1"] = {"state": "running", "template": body["template"]}
            return httpx.Response(202, json={"sandbox_id": "sbx_1", "operation_id": "op_1"})
        if path.startswith("/v1/operations/"):
            return httpx.Response(200, json={"state": "succeeded"})
        if path == "/v1/sandboxes/sbx_1" and method == "GET":
            return httpx.Response(200, json=self.sandboxes["sbx_1"])
        if path == "/v1/sandboxes/sbx_1" and method == "DELETE":
            self.sandboxes["sbx_1"]["state"] = "deleted"
            return httpx.Response(202, json={"operation_id": "op_del"})
        if path == "/v1/sandboxes/sbx_1/commands":
            self.commands.append(body)
            return httpx.Response(201, json={"session_id": "proc_1", "process_id": "proc_1", "attach": {"events_url": "/v1/sessions/proc_1/events", "token": "attach-token"}})
        if path == "/v1/sandboxes/sbx_1/files:write":
            self.files[body["path"]] = base64.b64decode(body["data_base64"])
            return httpx.Response(200, json={"ok": True})
        if path == "/v1/sandboxes/sbx_1/files:read":
            data = self.files.get(body["path"])
            if data is None:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, content=data[body["offset"] : body["offset"] + body["length"]])
        if path in ("/v1/sandboxes/sbx_1:suspend", "/v1/sandboxes/sbx_1:wake"):
            self.sandboxes["sbx_1"]["state"] = "suspended" if path.endswith("suspend") else "running"
            return httpx.Response(202, json={"operation_id": "op_2"})
        return httpx.Response(404, json={"error": f"no route {method} {path}"})


async def test_space_backend_against_fake_api(monkeypatch):
    fake = FakeSpace()
    monkeypatch.setenv("SPACE_API_TOKEN", "mgmt-token")
    backend = SpaceBackend()

    class E:  # minimal engine stand-in
        http_transport = httpx.MockTransport(fake)

    backend.setup(E())  # type: ignore[arg-type]
    spec = SandboxSpec(image="seed:latest", cpu=2, memory_mb=2048, config={"api_url": "https://space.test", "data_plane_url": "https://space-data.test", "read_chunk_bytes": 4}, labels={"shoal/tenant": "t", "shoal/run": "run_1"})
    sandbox = await backend.create(spec)
    assert fake.sandboxes["sbx_1"]["template"] == "seed:latest"
    result = await sandbox.exec("pytest -q", timeout=60)
    assert result.ok and result.stdout == "ran sh -lc pytest -q" and result.stderr == "warn"
    assert fake.commands[-1]["timeout"] == "60s" and fake.commands[-1]["cwd"] == "/workspace"
    await sandbox.write_file("notes/a.txt", "hello world")
    assert await sandbox.read_file("notes/a.txt") == b"hello world"  # read in 4-byte chunks
    await sandbox.suspend()
    assert fake.sandboxes["sbx_1"]["state"] == "suspended"
    await sandbox.resume()
    again = await backend.attach("sbx_1", spec)
    assert again is not None
    await sandbox.close()
    assert await backend.attach("sbx_1", spec) is None
    assert len(set(fake.idempotency_keys)) == len(fake.idempotency_keys) > 0
