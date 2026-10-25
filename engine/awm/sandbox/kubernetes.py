"""Kubernetes backend: one Pod per sandbox, commands over the exec API.

Requires ``pip install 'awm[kubernetes]'``. Uses in-cluster config when
available, else the local kubeconfig.

Profile ``config``:

* ``namespace`` - default ``awm-sandboxes``; ``{tenant}`` is substituted, so
  ``awm-{tenant}`` gives one namespace per tenant
* ``runtime_class`` - e.g. ``gvisor`` or ``kata`` for stronger isolation
* ``service_account``, ``image_pull_secrets``, ``node_selector``, ``tolerations``
* ``run_as_user`` - default 1000
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

from awm.core.ids import ulid
from awm.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec, shell_quote
from awm.sandbox.wrap import parse_wrapped, wrapped_command, write_chunks

DEFAULT_IMAGE = "ghcr.io/kyonz/awm-sandbox:latest"


def _quantity_cpu(cpu: float) -> str:
    return f"{int(cpu * 1000)}m"


class KubernetesSandbox(Sandbox):
    def __init__(self, backend: KubernetesBackend, name: str, namespace: str, spec: SandboxSpec):
        self.backend = backend
        self.id = f"{namespace}/{name}"
        self.name = name
        self.namespace = namespace
        self.workdir = spec.workdir
        self.env = spec.env

    async def _exec_raw(self, argv: list[str]) -> str:
        from kubernetes_asyncio import client
        from kubernetes_asyncio.stream import WsApiClient

        async with WsApiClient(configuration=await self.backend.configuration()) as ws:
            api = client.CoreV1Api(api_client=ws)
            return await api.connect_get_namespaced_pod_exec(
                self.name, self.namespace, command=argv, container="sandbox",
                stderr=True, stdin=False, stdout=True, tty=False,
            )

    async def exec(self, command, *, cwd=None, env=None, timeout=None) -> ExecResult:  # type: ignore[no-untyped-def]
        started = time.monotonic()
        argv = wrapped_command(command, cwd=self.resolve(cwd) if cwd else self.workdir, env=env, timeout=timeout)
        output = await self._exec_raw(argv)
        return parse_wrapped(output, time.monotonic() - started)

    async def write_file(self, path: str, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        for argv in write_chunks(self.resolve(path), raw):
            await self._exec_raw(argv)

    async def read_file(self, path: str) -> bytes:
        output = await self._exec_raw(
            ["sh", "-c", f"test -f {shell_quote(self.resolve(path))} && base64 {shell_quote(self.resolve(path))} | tr -d '\\n' || echo __AWM_MISSING__"]
        )
        if "__AWM_MISSING__" in output:
            raise FileNotFoundError(path)
        return base64.b64decode(output.strip())

    async def close(self) -> None:
        from kubernetes_asyncio import client

        async with client.ApiClient(await self.backend.configuration()) as api:
            try:
                await client.CoreV1Api(api).delete_namespaced_pod(self.name, self.namespace, grace_period_seconds=5)
            except client.ApiException as exc:
                if exc.status != 404:
                    raise


class KubernetesBackend(SandboxBackend):
    name = "kubernetes"
    description = "One Pod per sandbox on Kubernetes (supports gVisor/Kata runtime classes)."

    def __init__(self) -> None:
        self._config: Any = None

    async def configuration(self) -> Any:
        if self._config is None:
            from kubernetes_asyncio import client, config

            cfg = client.Configuration()
            try:
                config.load_incluster_config(client_configuration=cfg)
            except config.ConfigException:
                await config.load_kube_config(client_configuration=cfg)
            self._config = cfg
        return self._config

    def _namespace(self, spec: SandboxSpec) -> str:
        return spec.config.get("namespace", "awm-sandboxes").format(tenant=spec.labels.get("awm/tenant", "default")).lower()

    def pod_manifest(self, name: str, spec: SandboxSpec) -> dict[str, Any]:
        cfg = spec.config
        resources: dict[str, Any] = {}
        if spec.cpu or spec.memory_mb:
            limits = {}
            if spec.cpu:
                limits["cpu"] = _quantity_cpu(spec.cpu)
            if spec.memory_mb:
                limits["memory"] = f"{spec.memory_mb}Mi"
            resources = {"limits": limits, "requests": limits}
        labels = {k.replace("awm/", "awm.dev/"): v[:63] for k, v in spec.labels.items()}
        pod: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "awm", **labels}},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": spec.ttl_seconds,
                "securityContext": {"runAsUser": cfg.get("run_as_user", 1000), "fsGroup": cfg.get("run_as_user", 1000)},
                "containers": [
                    {
                        "name": "sandbox",
                        "image": spec.image or DEFAULT_IMAGE,
                        "command": ["sh", "-c", f"mkdir -p {shell_quote(spec.workdir)} && exec sleep infinity"],
                        "workingDir": "/",
                        "env": [{"name": k, "value": v} for k, v in spec.env.items()],
                        "resources": resources,
                        "volumeMounts": [{"name": "workspace", "mountPath": spec.workdir}],
                        "securityContext": {"allowPrivilegeEscalation": False},
                    }
                ],
                "volumes": [
                    {"name": "workspace", "emptyDir": {"sizeLimit": f"{spec.disk_mb}Mi"} if spec.disk_mb else {}}
                ],
            },
        }
        if cfg.get("runtime_class"):
            pod["spec"]["runtimeClassName"] = cfg["runtime_class"]
        if cfg.get("service_account"):
            pod["spec"]["serviceAccountName"] = cfg["service_account"]
        if cfg.get("image_pull_secrets"):
            pod["spec"]["imagePullSecrets"] = [{"name": n} for n in cfg["image_pull_secrets"]]
        if cfg.get("node_selector"):
            pod["spec"]["nodeSelector"] = cfg["node_selector"]
        if cfg.get("tolerations"):
            pod["spec"]["tolerations"] = cfg["tolerations"]
        return pod

    async def create(self, spec: SandboxSpec) -> Sandbox:
        from kubernetes_asyncio import client

        namespace = self._namespace(spec)
        name = f"awm-{ulid().lower()[-16:]}"
        async with client.ApiClient(await self.configuration()) as api:
            core = client.CoreV1Api(api)
            await core.create_namespaced_pod(namespace, self.pod_manifest(name, spec))
            deadline = time.monotonic() + float(spec.config.get("startup_timeout", 300))
            while True:
                pod = await core.read_namespaced_pod(name, namespace)
                phase = pod.status.phase
                if phase == "Running":
                    break
                if phase in ("Failed", "Succeeded") or time.monotonic() > deadline:
                    await core.delete_namespaced_pod(name, namespace)
                    raise RuntimeError(f"sandbox pod {name} did not start (phase {phase})")
                await asyncio.sleep(1)
        return KubernetesSandbox(self, name, namespace, spec)

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        from kubernetes_asyncio import client

        namespace, _, name = sandbox_id.partition("/")
        async with client.ApiClient(await self.configuration()) as api:
            try:
                pod = await client.CoreV1Api(api).read_namespaced_pod(name, namespace)
            except client.ApiException:
                return None
        if pod.status.phase != "Running":
            return None
        return KubernetesSandbox(self, name, namespace, spec)
