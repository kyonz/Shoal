from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

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


# --------------------------------------------------------------------------- docker, kubernetes, e2b against fakes
# Each fake runs commands in a local directory standing in for the container, so the backend's own command
# wrapping, file transfer and lifecycle code is what gets exercised.



class FakeContainer:
    def __init__(self, root: Path, **kwargs):
        self.root, self.kwargs, self.id, self.status, self.removed = root, kwargs, "c0ffee", "running", False

    def _local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def exec_run(self, argv, workdir, environment, demux):
        assert argv[:2] == ["sh", "-lc"] and demux
        script = argv[2].replace("/workspace", str(self._local("/workspace")))
        self._local(workdir).mkdir(parents=True, exist_ok=True)
        done = subprocess.run(["sh", "-c", script], cwd=self._local(workdir), env={"PATH": "/usr/bin:/bin", **environment},
                              capture_output=True)
        return done.returncode, (done.stdout or None, done.stderr or None)

    def put_archive(self, directory, data):
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(self._local(directory), filter="data")

    def get_archive(self, path):
        local = self._local(path)
        if not local.exists():
            raise RuntimeError("404 Client Error: Not Found")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            tar.add(local, arcname=local.name)
        return [buffer.getvalue()], {}

    def remove(self, force):
        self.removed = True


async def test_docker_backend_against_fake_daemon(tmp_path):
    from shoal.sandbox.docker import DockerBackend

    created = []

    class Containers:
        def run(self, **kwargs):
            created.append(FakeContainer(tmp_path, **kwargs))
            return created[-1]

        def get(self, cid):
            return created[0]

    backend = DockerBackend()
    backend._client = type("Client", (), {"containers": Containers()})()
    spec = SandboxSpec(image="python:3.13-slim", cpu=1.5, memory_mb=512, env={"FOO": "bar"}, network={"egress": "deny"},
                       config={"runtime": "runsc"})
    sandbox = await backend.create(spec)
    run = created[0].kwargs
    assert run["image"] == "python:3.13-slim" and run["mem_limit"] == "512m" and run["nano_cpus"] == 1_500_000_000
    assert run["network_disabled"] is True and run["runtime"] == "runsc" and run["environment"] == {"FOO": "bar"}

    await sandbox.write_file("dir/x.txt", "hello")
    assert await sandbox.read_file("dir/x.txt") == b"hello"
    result = await sandbox.exec("cat dir/x.txt; echo err >&2; exit 3")
    assert (result.exit_code, result.stdout, result.stderr) == (3, "hello", "err\n")
    try:
        await sandbox.read_file("missing.txt")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    assert (await backend.attach("c0ffee", spec)) is not None
    await sandbox.close()
    assert created[0].removed


async def test_kubernetes_backend_lifecycle_against_fake_api(tmp_path, monkeypatch):
    from kubernetes_asyncio import client

    from shoal.sandbox import kubernetes as k8s

    calls: list[tuple] = []
    phases = iter(["Pending", "Pending", "Running"])

    class FakeCore:
        def __init__(self, api=None, api_client=None):
            pass

        async def create_namespaced_pod(self, namespace, manifest):
            calls.append(("create", namespace, manifest))

        async def read_namespaced_pod(self, name, namespace):
            phase = next(phases, "Running")
            return type("Pod", (), {"status": type("S", (), {"phase": phase})()})()

        async def delete_namespaced_pod(self, name, namespace, grace_period_seconds=None):
            calls.append(("delete", namespace, name))

    class FakeApiClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(client, "CoreV1Api", FakeCore)
    monkeypatch.setattr(client, "ApiClient", FakeApiClient)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(k8s.asyncio, "sleep", lambda s: real_sleep(0))
    workspace = tmp_path / "workspace"

    async def exec_raw(self, argv):  # the pod's exec API, played by a local shell
        argv = [a.replace("/workspace", str(workspace)) for a in argv]
        workspace.mkdir(exist_ok=True)
        return await _run(argv)

    monkeypatch.setattr(k8s.KubernetesSandbox, "_exec_raw", exec_raw)
    backend = k8s.KubernetesBackend()
    backend._config = object()
    spec = SandboxSpec(cpu=2, memory_mb=4096, disk_mb=1024, labels={"shoal/tenant": "Acme", "shoal/run": "run_1"},
                       config={"namespace": "shoal-{tenant}", "runtime_class": "gvisor", "node_selector": {"pool": "sbx"}})
    sandbox = await backend.create(spec)

    _, namespace, pod = calls[0]
    assert namespace == "shoal-acme" and sandbox.id.startswith("shoal-acme/shoal-")
    container = pod["spec"]["containers"][0]
    assert container["resources"]["limits"] == {"cpu": "2000m", "memory": "4096Mi"}
    assert pod["spec"]["runtimeClassName"] == "gvisor" and pod["spec"]["nodeSelector"] == {"pool": "sbx"}
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["metadata"]["labels"]["shoal.dev/tenant"] == "Acme"
    assert pod["spec"]["volumes"][0]["emptyDir"] == {"sizeLimit": "1024Mi"}

    data = bytes(range(256)) * 300
    await sandbox.write_file("blob.bin", data)
    assert await sandbox.read_file("blob.bin") == data
    result = await sandbox.exec("echo out; echo err >&2; exit 2")
    assert (result.exit_code, result.stdout, result.stderr) == (2, "out\n", "err\n")
    try:
        await sandbox.read_file("nope")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    await sandbox.close()
    assert calls[-1][0] == "delete"


async def test_e2b_backend_against_fake_sdk(tmp_path, monkeypatch):
    import e2b
    from e2b.sandbox.commands.command_handle import CommandExitException

    from shoal.sandbox.e2b import E2BBackend

    root = tmp_path / "e2b"

    class FakeAsyncSandbox:
        created: list[dict] = []

        def __init__(self):
            self.sandbox_id, self.killed = "sbx-1", False
            self.commands = type("C", (), {"run": self._run})()
            self.files = type("F", (), {"write": self._write, "read": self._read})()

        @classmethod
        async def create(cls, **kwargs):
            cls.created.append(kwargs)
            return cls()

        @classmethod
        async def connect(cls, sandbox_id, **kwargs):
            return cls()

        async def _run(self, cmd, cwd, envs, timeout):
            local = root / cwd.lstrip("/")
            local.mkdir(parents=True, exist_ok=True)
            done = subprocess.run(  # noqa: ASYNC221 - a test fake, blocking is fine
                ["sh", "-c", cmd.replace("/workspace", str(root / "workspace"))], cwd=local,
                env={"PATH": "/usr/bin:/bin", **envs}, capture_output=True, text=True)
            if done.returncode:
                raise CommandExitException(stderr=done.stderr, stdout=done.stdout, exit_code=done.returncode, error=None)
            return type("R", (), {"exit_code": 0, "stdout": done.stdout, "stderr": done.stderr})()

        async def _write(self, path, data):
            local = root / path.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data.encode() if isinstance(data, str) else data)

        async def _read(self, path, format):
            local = root / path.lstrip("/")
            if not local.exists():
                raise RuntimeError(f"file {path} not found")
            return bytearray(local.read_bytes())

        async def kill(self):
            self.killed = True

    monkeypatch.setattr(e2b, "AsyncSandbox", FakeAsyncSandbox)
    monkeypatch.setenv("E2B_API_KEY", "e2b_test")
    backend = E2BBackend()
    spec = SandboxSpec(image="python", env={"A": "1"}, network={"egress": "deny"}, config={"domain": "space.example"})
    sandbox = await backend.create(spec)
    options = FakeAsyncSandbox.created[0]
    assert options["template"] == "python" and options["api_key"] == "e2b_test" and options["domain"] == "space.example"
    assert options["allow_internet_access"] is False and options["envs"] == {"A": "1"}

    await sandbox.write_file("x.txt", "hi")
    assert await sandbox.read_file("x.txt") == b"hi"
    assert (await sandbox.exec("cat x.txt; echo $A", env={"A": "1"})).stdout == "hi1\n"
    failed = await sandbox.exec("echo nope >&2; exit 4")
    assert (failed.exit_code, failed.stderr) == (4, "nope\n")
    try:
        await sandbox.read_file("missing")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    assert await backend.attach("sbx-1", spec) is not None
    await sandbox.close()
    assert sandbox.sbx.killed
    shutil.rmtree(root)
